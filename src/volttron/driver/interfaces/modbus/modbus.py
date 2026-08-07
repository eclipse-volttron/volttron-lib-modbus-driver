# -*- coding: utf-8 -*- {{{
# ===----------------------------------------------------------------------===
#
#                 Installable Component of Eclipse VOLTTRON
#
# ===----------------------------------------------------------------------===
#
# Copyright 2022 Battelle Memorial Institute
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy
# of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
#
# ===----------------------------------------------------------------------===
# }}}

import logging
import struct

from collections.abc import KeysView
from gevent import monkey

monkey.patch_socket()

from contextlib import closing, contextmanager
from pydantic import constr, Field, TypeAdapter, ValidationError
from pydantic.networks import IPvAnyAddress

from pymodbus.client.tcp import ModbusTcpClient as SyncModbusClient
from pymodbus.exceptions import ConnectionException, ModbusException, ModbusIOException
from pymodbus.pdu import ExceptionResponse
from volttron.driver.base.driver_locks import socket_lock
from volttron.driver.base.interfaces import (
    BaseInterface,
    BaseRegister,
    BasicRevert,
    DriverInterfaceError,
)
from volttron.driver.base.config import PointConfig, RemoteConfig


_log = logging.getLogger(__name__)


class ModbusRemoteConfig(RemoteConfig):
    ip_address: IPvAnyAddress = Field(alias='device_address')
    port: int = Field(ge=0, default=502)
    unit_id: int = Field(ge=0, default=0, alias='slave_id')


class ModbusPointConfig(PointConfig):
    default_value_configured: constr(strip_whitespace=True) | None = Field(default=None, alias='Default Value')
    io_type: constr(to_lower=True, strip_whitespace=True) = Field(alias='Modbus Register')
    mixed_endian: bool = Field(default=False, alias='Mixed Endian')
    point_address: int = Field(alias='Point Address')

    @property
    def default_value(self) -> any:
        if self.writable and self.default_value_configured:
            try:
                ta = TypeAdapter(bool) if self.io_type == 'bool' else TypeAdapter(self.python_type)
                return ta.validate_python(self.default_value_configured)
            except ValidationError:
                _log.warning(f'Using default revert method for {self.volttron_point_name} --- bad default value: {e}.')
                return None
        else:
            return None


class ModbusInterfaceException(ModbusException):
    pass


class ModbusRegisterBase(BaseRegister):

    def __init__(self,
                 address,
                 register_type,
                 read_only,
                 point_name,
                 units,
                 description='',
                 unit_id=0,
                 default_value=None):
        super(ModbusRegisterBase, self).__init__(register_type,
                                                 read_only,
                                                 point_name,
                                                 units,
                                                 description=description)
        self.address = address
        self.default_value = default_value
        self.unit_id = unit_id


class ModbusBitRegister(ModbusRegisterBase):

    def __init__(self,
                 address,
                 _,
                 point_name,
                 units,
                 read_only,
                 __,
                 description='',
                 unit_id=0,
                 default_value=None):
        super(ModbusBitRegister, self).__init__(address,
                                                "bit",
                                                read_only,
                                                point_name,
                                                units,
                                                description=description,
                                                unit_id=unit_id,
                                                default_value=default_value)

        self.python_type = bool

    def parse_value(self, starting_address, bit_stream):
        # find the bytes we care about
        index = (self.address - starting_address)
        return bit_stream[index]

    @staticmethod
    def get_register_count():
        return 1

    def get_state(self, client):
        response_bits = client.read_discrete_inputs(self.address, slave=self.unit_id) if self.read_only else \
            client.read_coils(self.address, slave=self.unit_id)
        if response_bits is None:
            raise ModbusInterfaceException("pymodbus returned None")
        return response_bits.bits[0]

    def set_state(self, client, value):
        if not self.read_only:
            response = client.write_coil(self.address, value, slave=self.unit_id)
            if response is None:
                raise ModbusInterfaceException("pymodbus returned None")
            if isinstance(response, ExceptionResponse):
                raise ModbusInterfaceException(str(response))
            return response.value
        return None


class ModbusByteRegister(ModbusRegisterBase):

    def __init__(self,
                 address,
                 type_string,
                 point_name,
                 units,
                 read_only,
                 mixed_endian=False,
                 description='',
                 unit_id=0,
                 default_value=None):
        super(ModbusByteRegister, self).__init__(address,
                                                 "byte",
                                                 read_only,
                                                 point_name,
                                                 units,
                                                 description=description,
                                                 unit_id=unit_id,
                                                 default_value=default_value)

        try:
            self.parse_struct = struct.Struct(type_string)
        except struct.error:
            raise ValueError("Invalid Modbus Register '" + type_string + "' for point " +
                             point_name)

        struct_types = [
            type(x) for x in self.parse_struct.unpack(b'\x00' * self.parse_struct.size)
        ]

        if len(struct_types) != 1:
            raise ValueError("Invalid length Modbus Register '" + type_string + "' for point " +
                             point_name)

        self.python_type = struct_types[0]

        self.mixed_endian = mixed_endian
        self.modbus_register_size = 2
        self.pymodbus_register_struct = struct.Struct('>H')

    @contextmanager
    def modbus_client(self, address, port):
        with socket_lock():
            with closing(SyncModbusClient(address, port)) as client:
                yield client

    def get_register_count(self):
        return self.parse_struct.size // self.modbus_register_size

    def parse_value(self, starting_address, byte_stream):
        # find the bytes we care about
        index = (self.address - starting_address) * 2
        width = self.parse_struct.size

        target_bytes = byte_stream[index:index + width]
        if len(target_bytes) < width:
            raise ValueError('Not enough data to parse')

        if self.mixed_endian:
            register_values = []
            for i in range(0, len(target_bytes), self.pymodbus_register_struct.size):
                register_values.extend(self.pymodbus_register_struct.unpack_from(target_bytes, i))
            register_values.reverse()

            target_bytes = bytes.join(
                b'', [self.pymodbus_register_struct.pack(value) for value in register_values])
        return self.parse_struct.unpack(target_bytes)[0]

    def get_state(self, client):
        if self.read_only:
            response = client.read_input_registers(self.address,
                                                   self.get_register_count(),
                                                   slave=self.unit_id)
        else:
            response = client.read_holding_registers(self.address,
                                                     self.get_register_count(),
                                                     slave=self.unit_id)

        if response is None:
            raise ModbusInterfaceException("pymodbus returned None")

        if self.mixed_endian:
            response.registers.reverse()

        response_bytes = response.encode()
        # skip the result count
        return self.parse_struct.unpack(response_bytes[1:])[0]

    def set_state(self, client, value):
        if not self.read_only:
            value_bytes = self.parse_struct.pack(value)
            register_values = []
            for i in range(0, len(value_bytes), self.pymodbus_register_struct.size):
                register_values.extend(self.pymodbus_register_struct.unpack_from(value_bytes, i))
            if self.mixed_endian:
                register_values.reverse()
            client.write_registers(self.address, register_values, slave=self.unit_id)
            return self.get_state(client)
        return None


class Modbus(BasicRevert, BaseInterface):

    INTERFACE_CONFIG_CLASS = ModbusRemoteConfig
    REGISTER_CONFIG_CLASS = ModbusPointConfig

    def __init__(self, config: ModbusRemoteConfig, *args, **kwargs):
        BasicRevert.__init__(self, **kwargs)
        BaseInterface.__init__(self, config, *args, **kwargs)
        self.register_ranges = {
            ('byte', True): [],
            ('byte', False): [],
            ('bit', True): [],
            ('bit', False): []
        }
        self.modbus_read_max = 100

    ##### Implemented abstract methods from BaseInterface
    
    def get_point(self, topic, **kwargs):
        register: ModbusBitRegister | ModbusByteRegister = self.get_register_by_name(topic)
        with self.modbus_client(str(self.config.ip_address), self.config.port) as client:
            try:
                result = register.get_state(client)
            except (ConnectionException, ModbusIOException, ModbusInterfaceException):
                result = None
        return result

    ##### Overridden methods from BaseInterface

    def create_register(self, register_definition: ModbusPointConfig) -> ModbusBitRegister | ModbusByteRegister:
        klass = ModbusBitRegister if register_definition.io_type.lower() == 'bool' else ModbusByteRegister
        return klass(register_definition.point_address,
                         register_definition.io_type,
                         register_definition.volttron_point_name,
                         register_definition.units,
                         not register_definition.writable,
                         mixed_endian=register_definition.mixed_endian,
                         description=register_definition.notes,
                         unit_id=self.config.unit_id,
                         default_value=register_definition.default_value)

    def insert_register(self, register: ModbusBitRegister | ModbusByteRegister, base_topic: str):
        super(Modbus, self).insert_register(register, base_topic)

        # MODBUS requires extra bookkeeping.
        register_type = register.get_register_type()
        register_range = self.register_ranges[register_type]
        register_count = register.get_register_count()

        # Store the range of registers for each point.
        start, end = register.address, register.address + register_count - 1
        register_range.append([start, end, [register]])

        if register.default_value is not None:
            self.set_default('/'.join([base_topic, register.point_name]), register.default_value)

    def finalize_setup(self, initial_setup: bool = False):
        # Merge adjacent ranges for efficiency.
        self.merge_register_ranges()


    ##### Implemented abstract methods from BasicRevert (set_point, scrape_all, revert_all, revert_point)

    def _set_point(self, topic, value):
        register: ModbusBitRegister | ModbusByteRegister = self.get_register_by_name(topic)
        if register.read_only:
            raise IOError("Trying to write to a point configured read only: " + topic)

        with self.modbus_client(str(self.config.ip_address), self.config.port) as client:
            try:
                result = register.set_state(client, value)
            except (ConnectionException, ModbusIOException, ModbusInterfaceException) as ex:
                raise IOError("Error encountered trying to write to point {}: {}".format(
                    topic, ex))
        return result

    def _get_multiple_points(self, topics: KeysView[str], **kwargs) -> (dict, dict):
        result_dict = {}
        with self.modbus_client(str(self.config.ip_address), self.config.port) as client:
            try:
                result_dict.update(self.scrape_byte_registers(client, True))
                result_dict.update(self.scrape_byte_registers(client, False))

                result_dict.update(self.scrape_bit_registers(client, True))
                result_dict.update(self.scrape_bit_registers(client, False))
            except (ConnectionException, ModbusIOException, ModbusInterfaceException) as e:
                raise DriverInterfaceError("Failed to scrape device at " + str(self.config.ip_address) + ":" +
                                           str(self.config.port) + " ID: " + str(self.config.unit_id) + str(e))
        # TODO: Build a query with just the points we want and pads. Then get rid of this filter.
        filtered_dict = {topic: result_dict[topic] for topic in topics if topic in result_dict}
        # TODO: Need error dict, if possible.
        return filtered_dict, {}

    ##### Helper methods

    def scrape_byte_registers(self, client, read_only):
        result_dict = {}
        register_ranges = self.register_ranges[('byte', read_only)]

        read_func = client.read_input_registers if read_only else client.read_holding_registers

        for register_range in register_ranges:
            start, end, registers = register_range
            result = b''

            for group in range(start, end + 1, self.modbus_read_max):
                count = min(end - group + 1, self.modbus_read_max)
                response = read_func(group, count, slave=self.config.unit_id)
                if response is None:
                    raise ModbusInterfaceException("pymodbus returned None")
                if isinstance(response, ModbusException):
                    raise response
                response_bytes = response.encode()
                # Trim off length byte.
                result += response_bytes[1:]

            for register in registers:
                point = register.point_name
                value = register.parse_value(start, result)
                result_dict[point] = value

        return result_dict

    def scrape_bit_registers(self, client, read_only):
        result_dict = {}
        register_ranges = self.register_ranges[('bit', read_only)]

        for register_range in register_ranges:
            start, end, registers = register_range
            if not registers:
                return result_dict

            result = []

            for group in range(start, end + 1, self.modbus_read_max):
                count = min(end - group + 1, self.modbus_read_max)
                response = client.read_discrete_inputs(group, count, slave=self.config.unit_id) if read_only else \
                    client.read_coils(group, count, slave=self.config.unit_id)
                if response is None:
                    raise ModbusInterfaceException("pymodbus returned None")
                if isinstance(response, ModbusException):
                    raise response
                result += response.bits

            for register in registers:
                point = register.point_name
                value = register.parse_value(start, result)
                result_dict[point] = value

        return result_dict

    def merge_register_ranges(self):
        """
        Merges any adjacent registers for more efficient scraping. May only be called after all registers have been
        inserted."""
        for key, register_ranges in self.register_ranges.items():
            if not register_ranges:
                continue
            register_ranges.sort()
            result = []
            current = register_ranges[0]
            for register_range in register_ranges[1:]:
                if register_range[0] > current[1] + 1:
                    result.append(current)
                    current = register_range
                    continue

                current[1] = register_range[1]
                current[2].extend(register_range[2])

            result.append(current)

            self.register_ranges[key] = result

    @contextmanager
    def modbus_client(self, address, port):
        with socket_lock():
            with closing(SyncModbusClient(address, port=port)) as client:
                yield client

    @classmethod
    def unique_remote_id(cls, config_name: str, config: ModbusRemoteConfig) -> tuple:
        # TODO: This should probably incorporate information which currently belongs to the BACnet Proxy Agent.
        return str(config.ip_address), config.port
