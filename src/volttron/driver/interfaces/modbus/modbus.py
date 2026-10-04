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
by address. Grouping registers into requests, padding, and value encoding are the proxy's job; the exchange itself
(registration, reply parsing, batching, failure handling) is :class:`ProxyBackedInterface`.
"""
from gevent import monkey
monkey.patch_socket()

import logging

from collections import defaultdict
from typing import Any, cast

from protocol_proxy.protocol.modbus.registers import DATATYPE, PAD, RegisterSpec, parse_type_spec

from volttron.driver.base.interfaces import BaseInterface, BaseRegister, BasicRevert
from volttron.driver.base.proxy_interface import PointError, ProxyBackedInterface

from .config import (ModbusPointConfig, ModbusRemoteConfig, Table, TransportProtocol, merge_register_map,
                     normalize_register_map)

_log = logging.getLogger(__name__)

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


class Modbus(ProxyBackedInterface, BasicRevert, BaseInterface):

    REGISTER_CONFIG_CLASS = ModbusPointConfig
    INTERFACE_CONFIG_CLASS = ModbusRemoteConfig
    PROXY_NAME, PROXY_LABEL = 'modbus', 'Modbus Proxy'
    REGISTER_METHOD, READ_METHOD, WRITE_METHOD = 'REGISTER_DEVICE', 'READ_REGISTERS', 'WRITE_REGISTERS'
    REQUEST_ERROR_KEY = 'device'

    def __init__(self, config, *args, **kwargs):
        BaseInterface.__init__(self, config, *args, **kwargs)
        BasicRevert.__init__(self, **kwargs)
        self.config: ModbusRemoteConfig
        # Pads: registry rows with data type 'pad', read to keep polls contiguous but never published.
        self.pads: dict[Table, list[RegisterSpec]] = defaultdict(list)
        self.topics_by_address: dict[tuple[Table, int], str] = {}
        self.init_proxy()

    # ------------------------------------------------------------------ registers

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

    # ------------------------------------------------------------------ the proxy exchange, in Modbus terms

    def identity_fields(self) -> dict:
        return self.config.device_fields()

    def registration_payload(self) -> dict:
        """This device's client settings and its register data types, by table."""
        tables: dict[str, list[dict]] = defaultdict(list)
        for register in self.point_map.values():
            tables[register.table.value].append(register.spec_fields())
        for table, pads in self.pads.items():
            tables[table.value].extend({'address': pad.address, 'data_type': 'PAD', 'count': pad.count} for pad in pads)
        return {**self.config.device_fields(), **self.config.client_options(),
                'unit_id': self.config.unit_id, 'max_gap': self.config.max_gap, 'tables': dict(tables), 'clear_others': True}

    def after_registration(self, result: dict, initial_setup: bool):
        for table, summary in (result.get('configured') or {}).items():
            _log.info(f"Modbus {self.config.device_address} unit {self.config.unit_id} {table} table:"
                      f" {summary.get('specs')} points, {summary.get('pads')} pads,"
                      f" {len(summary.get('blocks', []))} request(s) per poll.")

    def split_reads(self, topics: list[str], **kwargs) -> list[tuple[dict, list[str]]]:
        """One READ_REGISTERS per table; a partial table names its queries, a whole one lets the proxy use its blocks."""
        by_table: dict[Table, list[str]] = defaultdict(list)
        for topic in topics:
            by_table[cast(ModbusRegister, self.point_map[topic]).table].append(topic)
        batches = []
        for table, batch in by_table.items():
            payload = {**self.config.device_fields(), 'unit_id': self.config.unit_id, 'register_map': table.value, 'decode': True}
            if len(batch) < self._point_count(table):
                payload['queries'] = self._queries_for(table, [cast(ModbusRegister, self.point_map[t]) for t in batch])
            batches.append((payload, batch))
        return batches

    def read_key(self, topic: str, register: BaseRegister) -> str:
        return str(cast(ModbusRegister, register).address)      # replies are keyed by register address

    def read_error(self, topic: str, register: BaseRegister, request_errors: Any) -> str:
        if isinstance(request_errors, dict) and topic in request_errors:
            return str(request_errors[topic])
        return self._describe_read_error(cast(ModbusRegister, register), request_errors)

    def coerce(self, register: BaseRegister, value: Any) -> Any:
        register = cast(ModbusRegister, register)
        try:
            # Values may arrive as strings (e.g., from vctl); the proxy needs the register's Python type.
            return self._coerce(register, value)
        except (TypeError, ValueError) as e:
            raise PointError(f'Value {value!r} is not valid for this point ({register.python_type.__name__}): {e}') from e

    def split_writes(self, items: list[tuple[str, Any]], **kwargs) -> list[tuple[dict, list[tuple[str, Any]]]]:
        """One WRITE_REGISTERS per table, with the encoded values by address."""
        by_table: dict[Table, list[tuple[str, Any]]] = defaultdict(list)
        for topic, value in items:
            by_table[cast(ModbusRegister, self.point_map[topic]).table].append((topic, value))
        return [({**self.config.device_fields(), 'unit_id': self.config.unit_id, 'register_map': table.value, 'encode': True,
                  'queries': [[cast(ModbusRegister, self.point_map[t]).address, None, v] for t, v in batch]}, batch)
                for table, batch in by_table.items()]

    def interpret_write(self, items: list[tuple[str, Any]], result: Any, request_errors: Any) -> tuple[dict, dict]:
        """The proxy answers a write with positional acknowledgements and errors, one per query."""
        results, errors = {}, {}
        if isinstance(request_errors, dict) and not isinstance(result, list):
            return results, dict(request_errors)                 # Whole-request failure, already keyed by topic.
        for index, (topic, value) in enumerate(items):
            error = request_errors[index] if isinstance(request_errors, list) and index < len(request_errors) else None
            if error is None and isinstance(result, list) and index < len(result) and result[index] is not None:
                results[topic] = value                           # Modbus does not echo values; report what was requested.
            else:
                errors[topic] = error if error is not None else 'Write not acknowledged by the Modbus Proxy.'
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
