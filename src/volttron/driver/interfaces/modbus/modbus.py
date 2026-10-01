# -*- coding: utf-8 -*- {{{
# vim: set fenc=utf-8 ft=python sw=4 ts=4 sts=4 et:
#
# Copyright 2020, Battelle Memorial Institute.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# This material was prepared as an account of work sponsored by an agency of
# the United States Government. Neither the United States Government nor the
# United States Department of Energy, nor Battelle, nor any of their
# employees, nor any jurisdiction or organization that has cooperated in the
# development of these materials, makes any warranty, express or
# implied, or assumes any legal liability or responsibility for the accuracy,
# completeness, or usefulness or any information, apparatus, product,
# software, or process disclosed, or represents that its use would not infringe
# privately owned rights. Reference herein to any specific commercial product,
# process, or service by trade name, trademark, manufacturer, or otherwise
# does not necessarily constitute or imply its endorsement, recommendation, or
# favoring by the United States Government or any agency thereof, or
# Battelle Memorial Institute. The views and opinions of authors expressed
# herein do not necessarily state or reflect those of the
# United States Government or any agency thereof.
#
# PACIFIC NORTHWEST NATIONAL LABORATORY operated by
# BATTELLE for the UNITED STATES DEPARTMENT OF ENERGY
# under Contract DE-AC05-76RL01830
# }}}
"""Modbus driver interface.

All Modbus communication happens in the Modbus Protocol Proxy, a separate process shared by every Modbus device.
This interface declares each device's register data types to the proxy once, then reads and writes decoded values
by address. Grouping registers into requests, padding, and value encoding are the proxy's job.
"""
from gevent import monkey
monkey.patch_socket()

import json
import logging

from collections import defaultdict
from typing import Any, Iterable, cast

from gevent import Timeout
from gevent.event import AsyncResult

from protocol_proxy.ipc import ProtocolProxyMessage, ProtocolProxyPeer
from protocol_proxy.manager.gevent import GeventProtocolProxyManager
from protocol_proxy.protocol.modbus.registers import DATATYPE, PAD, RegisterSpec, parse_type_spec

from volttron.driver.base.interfaces import BaseInterface, BaseRegister, BasicRevert, DriverInterfaceError

from .config import (ModbusPointConfig, ModbusRemoteConfig, Table, TransportProtocol, merge_register_map,
                     normalize_register_map)

_log = logging.getLogger(__name__)

PROXY_NAME = 'modbus'

PYTHON_TYPES = {DATATYPE.BITS: bool, DATATYPE.STRING: str, DATATYPE.FLOAT32: float, DATATYPE.FLOAT64: float}


class ModbusRegister(BaseRegister):
    """One point (or pad) on a Modbus device: where it lives and how the proxy should decode it."""

    def __init__(self, point_name: str, units: str, read_only: bool, table: Table, spec: RegisterSpec,
                 description: str = '', default_value: Any = None):
        # The base driver only distinguishes 'bit' from 'byte' to label booleans in publish metadata.
        super().__init__('bit' if table.is_bits else 'byte', read_only, point_name, units, description=description)
        self.table = table
        self.spec = spec
        self.python_type = bool if table.is_bits else PYTHON_TYPES.get(spec.data_type, int)
        self.default_value = default_value       # Value to revert to, already coerced to python_type; None if unset.

    @property
    def address(self) -> int:
        return self.spec.address

    @property
    def is_pad(self) -> bool:
        return self.spec.is_pad

    def spec_fields(self) -> dict:
        """This register's entry in a CONFIGURE_REGISTERS 'tables' list."""
        fields = {'address': self.spec.address, 'data_type': self.spec.data_type.name, 'count': self.spec.count,
                  'word_order': self.spec.word_order}
        if self.spec.byte_swap:
            fields['byte_swap'] = True
        if self.spec.data_type is DATATYPE.STRING:
            fields['string_encoding'] = self.spec.string_encoding
        return fields

    def __repr__(self) -> str:
        return f'ModbusRegister({self.point_name!r}, {self.table.value}, {self.spec!r})'


class Modbus(BasicRevert, BaseInterface):

    REGISTER_CONFIG_CLASS = ModbusPointConfig
    INTERFACE_CONFIG_CLASS = ModbusRemoteConfig

    def __init__(self, config, *args, **kwargs):
        BaseInterface.__init__(self, config, *args, **kwargs)
        BasicRevert.__init__(self, **kwargs)
        self.config: ModbusRemoteConfig
        # Pads: registry rows with data type 'pad', read to keep polls contiguous but never published.
        self.pads: dict[Table, list[RegisterSpec]] = defaultdict(list)
        self.topics_by_address: dict[tuple[Table, int], str] = {}

        self.ppm: GeventProtocolProxyManager = GeventProtocolProxyManager.get_manager(PROXY_NAME)
        self.proxy_peer: ProtocolProxyPeer | None = None
        self.ppm.start()
        self.driver_agent.core.spawn(self.ppm.select_loop)

    # ------------------------------------------------------------------ setup

    def create_register(self, register_definition: ModbusPointConfig) -> ModbusRegister:
        point = register_definition
        try:
            data_type, count, little_endian = parse_type_spec(point.data_type)
        except ValueError as e:
            raise ValueError(f"Point {point.volttron_point_name}: {e}") from e
        is_pad = data_type is PAD
        if is_pad and point.table is None:
            raise ValueError(f"Pad {point.volttron_point_name} must name its table.")
        table = point.table or self._default_table(data_type, point.writable)
        if point.writable and table.read_only:
            raise ValueError(f"Point {point.volttron_point_name} is writable but the {table.value} table is read-only.")
        if table.is_bits and data_type not in (DATATYPE.BITS, PAD):
            raise ValueError(f"Point {point.volttron_point_name}: {table.value} table holds booleans, not {data_type.name}.")
        if point.word_order:
            word_order = point.word_order.value
        elif point.mixed_endian:
            word_order = 'little'
        elif little_endian:
            word_order = 'little'          # legacy '<' struct types: little-endian byte stream, see RegisterSpec
        else:
            word_order = self.config.word_order.value
        try:
            spec = RegisterSpec(self.config.addressing.resolve(point.address, table), data_type,
                                count=point.count if point.count is not None else count,
                                word_order=word_order, string_encoding=point.string_encoding, byte_swap=little_endian)
        except ValueError as e:
            raise ValueError(f"Point {point.volttron_point_name}: {e}") from e

        register = ModbusRegister(point.volttron_point_name, point.units, not point.writable, table, spec,
                                  description=point.description or point.notes)
        if point.writable and not is_pad and point.default_value not in (None, ''):
            try:
                register.default_value = self._coerce(register, point.default_value)
            except (TypeError, ValueError):
                _log.warning(f"Unable to set default value for {point.volttron_point_name}: bad default value"
                             f" {point.default_value!r} in configuration. Using default revert method.")
        return register

    def insert_register(self, register: BaseRegister, base_topic: str):
        register = cast(ModbusRegister, register)
        if register.is_pad:
            # Not a point: kept out of point_map so it is never read as a value. Its config row marked itself
            # inactive, so the platform never asks for it either.
            self.pads[register.table].append(register.spec)
            return
        super().insert_register(register, base_topic)
        topic = '/'.join([base_topic, register.point_name])
        self.topics_by_address[(register.table, register.address)] = topic
        if register.default_value is not None:
            self.set_default(topic, register.default_value)     # Revert values are tracked by full topic.

    def prepare_registry_config(self, registry_config: list[dict], remote_config=None) -> list[dict]:
        """Complete incomplete registry rows from the optional modbus_tk-style register_map (see merge_register_map).

        The map is taken from the remote configuration accompanying this registry when the platform supplies one (it
        may be newer than self.config during an update), otherwise from this interface's configuration.
        """
        if remote_config is not None and hasattr(remote_config, 'register_map'):
            register_map = normalize_register_map(remote_config.register_map, self.config.device_address)
        else:
            register_map = self.config.register_map
        return merge_register_map(registry_config, register_map, self.config.device_address)

    def finalize_setup(self, initial_setup: bool = False):
        self.proxy_peer = self.ppm.get_proxy(self.config.proxy_key())
        _log.debug(f'Modbus finalize_setup: proxy_peer is: {self.proxy_peer}')
        self.ppm.wait_peer_registered(self.proxy_peer, self.config.registration_timeout, self.register_device)

    def register_device(self):
        """Create (or reuse) this device's client in the proxy and declare its register data types."""
        tables: dict[str, list[dict]] = defaultdict(list)
        for register in self.point_map.values():
            tables[register.table.value].append(register.spec_fields())
        for table, pads in self.pads.items():
            tables[table.value].extend({'address': pad.address, 'data_type': 'PAD', 'count': pad.count} for pad in pads)
        payload = {**self.config.device_fields(), **self.config.client_options(),
                   'unit_id': self.config.unit_id, 'max_gap': self.config.max_gap,
                   'tables': dict(tables), 'clear_others': True}
        response = self._send('REGISTER_DEVICE', payload)
        result, errors = self.parse_proxy_response(response, ['device'])
        if errors:
            _log.warning(f"Failed to register Modbus device {self.config.device_address} unit {self.config.unit_id}"
                         f" with the proxy: {errors}")
            return
        for table, summary in (result.get('configured') or {}).items():
            _log.info(f"Modbus {self.config.device_address} unit {self.config.unit_id} {table} table:"
                      f" {summary.get('specs')} points, {summary.get('pads')} pads,"
                      f" {len(summary.get('blocks', []))} request(s) per poll.")

    # ------------------------------------------------------------------ reads

    def get_point(self, topic: str, **kwargs):
        results, errors = self._get_multiple_points([topic])
        if topic in results:
            return results[topic]
        message = f"Error reading point: {topic} --- {errors.get(topic, errors)}"
        _log.warning(message)
        raise RuntimeError(message)

    def _get_multiple_points(self, topics: Iterable[str], **kwargs) -> tuple[dict, dict]:
        if self.proxy_peer is None:
            raise DriverInterfaceError("Modbus interface not initialized. No proxy peer available.")
        results, errors = {}, {}
        by_table: dict[Table, list[tuple[str, ModbusRegister]]] = defaultdict(list)
        for topic in topics:
            register = self.point_map.get(topic)
            if register is None:
                errors[topic] = 'Point not configured on device.'
            else:
                by_table[register.table].append((topic, register))
        try:
            for table, points in by_table.items():
                payload = {**self.config.device_fields(), 'unit_id': self.config.unit_id,
                           'register_map': table.value, 'decode': True}
                if len(points) < self._point_count(table):
                    payload['queries'] = self._queries_for(table, [r for _, r in points])
                # Otherwise omit queries: the proxy reads its planned blocks for the whole table.
                response = self._send('READ_REGISTERS', payload)
                values, request_errors = self.parse_proxy_response(response, [t for t, _ in points])
                for topic, register in points:
                    key = str(register.address)
                    if isinstance(values, dict) and key in values:
                        results[topic] = values[key]
                    elif topic in request_errors:
                        errors[topic] = request_errors[topic]
                    else:
                        errors[topic] = self._describe_read_error(register, request_errors)
        except Timeout as e:
            _log.warning(f'Request timed out polling {self.config.device_address}: {e}')
            for topic, _ in [p for points in by_table.values() for p in points]:
                errors.setdefault(topic, f'Timeout waiting for Modbus Proxy: {e}')
        except Exception as e:
            _log.warning(f'Unexpected error polling {self.config.device_address}: {e}')
            for topic, _ in [p for points in by_table.values() for p in points]:
                errors.setdefault(topic, f'Unexpected error: {e}')
        return results, errors

    def _queries_for(self, table: Table, registers: list[ModbusRegister]) -> list[list[int]]:
        """One (start, count) per requested point, plus any configured pads lying between them.

        The proxy merges these into the fewest requests. Including the pads lets an explicitly padded gap merge
        even when only some of a table's points are polled.
        """
        queries = [[r.address, r.spec.count] for r in registers]
        first, last = min(r.address for r in registers), max(r.spec.end for r in registers)
        queries.extend([p.address, p.count] for p in self.pads.get(table, ()) if first < p.address < last)
        return queries

    def _point_count(self, table: Table) -> int:
        return sum(1 for r in self.point_map.values() if r.table is table)

    @staticmethod
    def _describe_read_error(register: ModbusRegister, request_errors: dict) -> str:
        # Request errors are keyed by the start address of the failed request, which may precede the point.
        if isinstance(request_errors, dict):
            for start, message in request_errors.items():
                try:
                    if int(start) <= register.address:
                        return f'Request starting at {start} failed: {message}'
                except (TypeError, ValueError):
                    continue
            if request_errors:
                return f'Read failed: {request_errors}'
        return 'No value returned by the Modbus Proxy.'

    # ------------------------------------------------------------------ writes

    def _set_point(self, topic: str, value: Any, **kwargs):
        results, errors = self._write_points([(topic, value)])
        if topic in errors:
            message = f"Error writing point: {topic} --- {errors[topic]}"
            _log.warning(message)
            raise RuntimeError(message)
        return results[topic]

    def set_multiple_points(self, topics_values, **kwargs):
        results, errors = self._write_points(list(topics_values))
        for topic in results:
            self._tracker.mark_dirty_point(topic)
        if errors:
            _log.warning(f'Errors encountered setting points: {errors}')
        return results, errors

    def _write_points(self, topics_values: list[tuple[str, Any]]) -> tuple[dict, dict]:
        if self.proxy_peer is None:
            raise DriverInterfaceError("Modbus interface not initialized. No proxy peer available.")
        results, errors = {}, {}
        by_table: dict[Table, list[tuple[str, ModbusRegister, Any]]] = defaultdict(list)
        for topic, value in topics_values:
            register = self.point_map.get(topic)
            if register is None:
                errors[topic] = 'Point not configured on device.'
            elif register.read_only:
                errors[topic] = 'Trying to write to a point configured read only.'
            else:
                try:
                    # Values may arrive as strings (e.g., from vctl); the proxy needs the register's Python type.
                    coerced = self._coerce(register, value)
                except (TypeError, ValueError) as e:
                    errors[topic] = f'Value {value!r} is not valid for this point ({register.python_type.__name__}): {e}'
                    continue
                by_table[register.table].append((topic, register, coerced))
        for table, points in by_table.items():
            payload = {**self.config.device_fields(), 'unit_id': self.config.unit_id, 'register_map': table.value,
                       'encode': True, 'queries': [[r.address, None, v] for _, r, v in points]}
            try:
                response = self._send('WRITE_REGISTERS', payload)
                acks, request_errors = self.parse_proxy_response(response, [t for t, _, _ in points])
            except Timeout as e:
                acks, request_errors = [], {t: f'Timeout waiting for Modbus Proxy: {e}' for t, _, _ in points}
            if isinstance(request_errors, dict) and not isinstance(acks, list):
                errors.update(request_errors)          # Whole-request failure, already keyed by topic.
                continue
            for index, (topic, register, value) in enumerate(points):
                error = request_errors[index] if isinstance(request_errors, list) and index < len(request_errors) \
                    else None
                if error is None and isinstance(acks, list) and index < len(acks) and acks[index] is not None:
                    results[topic] = value              # Modbus does not echo values; report what was requested.
                else:
                    errors[topic] = error if error is not None else 'Write not acknowledged by the Modbus Proxy.'
        return results, errors

    # ------------------------------------------------------------------ proxy plumbing

    def _send(self, method_name: str, payload: dict):
        return self.ppm.send(self.proxy_peer, ProtocolProxyMessage(method_name=method_name,
                                                                   payload=json.dumps(payload).encode('utf8'),
                                                                   response_expected=True))

    def parse_proxy_response(self, response: Any, error_keys: Iterable[str]) -> tuple[Any, Any]:
        """Normalize a reply from the Modbus Proxy into ``(result, errors)``.

        ``send`` returns an AsyncResult when a response is expected, or False when the request could not be sent.
        The proxy replies ``{'result': ..., 'error': ...}`` from its serializer, or
        ``{'status': 'error', 'error': ..., 'method': ...}`` when the endpoint raised or timed out. Whole-request
        failures are reported against every key in ``error_keys``. A gevent Timeout waiting on the AsyncResult is
        left to propagate to the caller.
        """
        error_keys = list(error_keys)

        def failed(message: str) -> tuple[dict, dict]:
            return {}, {key: message for key in error_keys}

        if not isinstance(response, AsyncResult):
            return failed(f'Unable to send request to Modbus Proxy (send returned {response!r}).')
        raw = response.get(timeout=self.config.resolved_reply_timeout)
        if not raw:
            return failed('Empty response from Modbus Proxy.')
        try:
            payload = json.loads(raw.decode('utf8') if isinstance(raw, (bytes, bytearray)) else raw)
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as e:
            return failed(f'Undecodable response from Modbus Proxy: {e}')
        if not isinstance(payload, dict):
            return failed(f'Unexpected response from Modbus Proxy: {payload!r}')
        if payload.get('status') == 'error':
            return failed(f"Modbus Proxy {payload.get('method', 'request')} failed: {payload.get('error')}")
        errors = payload.get('error')
        return payload.get('result', {}), errors if errors is not None else {}

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _default_table(data_type, writable: bool) -> Table:
        if data_type is DATATYPE.BITS:
            return Table.coil if writable else Table.discrete_input
        return Table.holding if writable else Table.input

    @staticmethod
    def _coerce(register: ModbusRegister, value: Any):
        if isinstance(value, (list, tuple)):
            return [Modbus._coerce_scalar(register.python_type, v) for v in value]
        return Modbus._coerce_scalar(register.python_type, value)

    @staticmethod
    def _coerce_scalar(python_type: type, value: Any):
        if python_type is bool:
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in ('true', 't', 'on', 'yes', '1'):
                    return True
                if lowered in ('false', 'f', 'off', 'no', '0', ''):
                    return False
                raise ValueError(value)
            return bool(value)
        if python_type is int and isinstance(value, str):
            return int(value, 0)            # accepts '7', '0x10'
        if python_type is float and isinstance(value, bool):
            raise ValueError('boolean given for a numeric point')
        return python_type(value)

    @classmethod
    def unique_remote_id(cls, config_name: str, config) -> tuple:
        """Identifies the device: one DriverAgent per Modbus unit."""
        cfg = cls.INTERFACE_CONFIG_CLASS(**config.model_dump())
        if cfg.transport_protocol is TransportProtocol.serial:
            return cfg.transport_protocol.value, cfg.device_address, cfg.unit_id
        return cfg.transport_protocol.value, cfg.device_address, cfg.resolved_port, cfg.unit_id
