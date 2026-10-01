"""Configuration models for the Modbus driver interface."""
import logging

from enum import Enum
from typing import Any

from pydantic import AliasChoices, Field, field_validator, model_validator

from volttron.driver.base.config import PointConfig, RemoteConfig

_log = logging.getLogger(__name__)


class Table(str, Enum):
    """The four Modbus data tables, named as the Modbus Protocol Proxy knows them."""
    coil = 'coil'
    discrete_input = 'discrete_input'
    holding = 'holding'
    input = 'input'

    @property
    def read_only(self) -> bool:
        return self in (Table.discrete_input, Table.input)

    @property
    def is_bits(self) -> bool:
        return self in (Table.coil, Table.discrete_input)


# Accepted spellings of table names, including those used by the modbus_tk driver.
TABLE_ALIASES: dict[str, Table] = {
    'coil': Table.coil, 'coils': Table.coil, 'discrete_output_coils': Table.coil, 'discrete_output': Table.coil,
    'discrete_input': Table.discrete_input, 'discrete_inputs': Table.discrete_input, 'contact': Table.discrete_input,
    'contacts': Table.discrete_input, 'discrete_input_contacts': Table.discrete_input,
    'holding': Table.holding, 'holding_register': Table.holding, 'holding_registers': Table.holding,
    'analog_output_holding_registers': Table.holding, 'analog_output': Table.holding,
    'input': Table.input, 'input_register': Table.input, 'input_registers': Table.input,
    'analog_input_registers': Table.input, 'analog_input': Table.input,
}

# First address of each table in "Modbus addressing" (e.g., 40001 is the first holding register).
TABLE_BASE_ADDRESS: dict[Table, int] = {Table.coil: 1, Table.discrete_input: 10001, Table.input: 30001,
                                        Table.holding: 40001}


class Addressing(str, Enum):
    """How addresses in the registry are expressed.

    offset: the zero-based protocol address (default; 'exact' is accepted as a synonym).
    offset_plus: one-based, as printed in many vendor manuals; 1 is subtracted.
    address: table-prefixed one-based (1, 10001, 30001, 40001 ranges); the table base is subtracted.
    """
    offset = 'offset'
    offset_plus = 'offset_plus'
    address = 'address'

    def resolve(self, address: int, table: Table) -> int:
        match self:
            case Addressing.offset:
                resolved = address
            case Addressing.offset_plus:
                resolved = address - 1
            case Addressing.address:
                resolved = address - TABLE_BASE_ADDRESS[table]
        if resolved < 0:
            raise ValueError(f"Address {address} is out of range for the {table.value} table with {self.value} addressing.")
        return resolved


class WordOrder(str, Enum):
    big = 'big'
    little = 'little'


class Parity(str, Enum):
    none = 'none'
    even = 'even'
    odd = 'odd'
    mark = 'mark'
    space = 'space'

    @property
    def pymodbus(self) -> str:
        return self.value[0].upper()


class StopBits(float, Enum):
    one = 1
    one_point_five = 1.5
    two = 2


class TransportProtocol(str, Enum):
    tcp = 'tcp'
    udp = 'udp'
    serial = 'serial'
    tls = 'tls'


def _lower_or_none(v):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    return v.strip().lower() if isinstance(v, str) else v


# Accepted spellings of the registry columns the driver itself needs to look up before validation. These must
# agree with the validation_alias choices on ModbusPointConfig and PointConfig below.
_DATA_TYPE_KEYS = ('data_type', 'Data Type', 'data_format', 'Data Format', 'modbus_register', 'Modbus Register',
                   'type', 'Type')
_ADDRESS_KEYS = ('address', 'point_address', 'Address', 'Point Address')
_REGISTER_NAME_KEYS = ('register_name', 'Register Name')
_POINT_NAME_KEYS = ('volttron_point_name', 'Volttron Point Name')
_PAD_TYPE_NAMES = ('pad', 'padding', 'reserved', 'skip')


def is_pad_type(data_type: str) -> bool:
    return data_type.strip().lower().split('[')[0].strip() in _PAD_TYPE_NAMES


def _is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _first_value(row: dict, keys: tuple[str, ...]):
    """The first non-blank value in row under any of keys, or None."""
    return next((row[k] for k in keys if k in row and not _is_blank(row[k])), None)


def register_name_of(row: dict) -> str | None:
    name = _first_value(row, _REGISTER_NAME_KEYS)
    return str(name).strip() if name is not None else None


def row_is_complete(row: dict) -> bool:
    """Whether a registry row can be validated on its own: it names both an address and a data type."""
    return _first_value(row, _ADDRESS_KEYS) is not None and _first_value(row, _DATA_TYPE_KEYS) is not None


def _describe_row(row: dict) -> str:
    name = _first_value(row, _POINT_NAME_KEYS) or register_name_of(row)
    return repr(name) if name is not None else repr(row)


def normalize_register_map(value, device: str = '') -> list[dict] | None:
    """The register_map setting as a list of rows, or None.

    The configuration store replaces a ``config://`` reference with the referenced file's rows before the driver sees
    it, so a string here means the reference was not resolved (usually because no such config exists in the store).
    """
    if value is None:
        return None
    if isinstance(value, str):
        _log.error(f"{device}: register_map {value!r} was not resolved to a register map. Check that the referenced"
                   " configuration exists in the store. Continuing without a register map.")
        return None
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError('register_map must be a config:// reference to a CSV file or a list of register rows.')
    return value


def merge_register_map(rows: list[dict], register_map: list[dict] | None, device: str = '') -> list[dict]:
    """Complete registry rows from a modbus_tk-style register map.

    A row which already names an address and a data type is used as-is, so the map is never required. Any other
    row is completed from the map row with the same Register Name; where both name a field, the registry row wins,
    as in modbus_tk (blank registry cells do not override the map). Rows that cannot be completed are dropped with a
    warning, and the remaining rows are returned.
    """
    if all(row_is_complete(row) for row in rows):
        return list(rows)
    map_rows: dict[str, dict] = {}
    for map_row in register_map or []:
        name = register_name_of(map_row)
        if name is None:
            _log.warning(f"{device}: ignoring register_map row with no Register Name: {map_row!r}")
        else:
            map_rows[name] = map_row
    merged = []
    for row in rows:
        if row_is_complete(row):
            merged.append(row)
            continue
        name = register_name_of(row)
        map_row = map_rows.get(name) if name is not None else None
        if map_row is None:
            if not register_map:
                reason = 'no register_map is configured'
            elif name is None:
                reason = 'it has no Register Name to look up in the register_map'
            else:
                reason = f'no register_map row is named {name!r}'
            _log.warning(f"{device}: dropping registry row {_describe_row(row)}: it lacks an address or data type"
                         f" and {reason}.")
            continue
        merged.append({**map_row, **{k: v for k, v in row.items() if not _is_blank(v)}})
    return merged


class ModbusPointConfig(PointConfig):
    address: int = Field(validation_alias=AliasChoices('address', 'point_address', 'Address', 'Point Address'))
    # Any spelling accepted by the proxy's parse_data_type: pymodbus names (UINT16), modbus_tk names (float,
    # string[8]), or struct formats (>f, 4H). A data_type of 'pad' marks registers to read but not publish.
    data_type: str = Field(validation_alias=AliasChoices('data_type', 'Data Type', 'data_format', 'Data Format',
                                                         'modbus_register', 'Modbus Register', 'type', 'Type'))
    # Number of registers (or coils); usually implied by data_type. Required for strings without a length.
    count: int | None = Field(default=None, validation_alias=AliasChoices('count', 'Count', 'length', 'Length'))
    # Defaults from data_type and writable when omitted: booleans go to coil/discrete_input, all else to
    # holding/input.
    table: Table | None = Field(default=None, validation_alias=AliasChoices('table', 'Table'))
    word_order: WordOrder | None = Field(default=None, validation_alias=AliasChoices('word_order', 'Word Order'))
    # Legacy spelling of word_order='little'.
    mixed_endian: bool = Field(default=False, validation_alias=AliasChoices('mixed_endian', 'Mixed Endian', 'mixed'))
    string_encoding: str = Field(default='utf-8', validation_alias=AliasChoices('string_encoding', 'String Encoding'))
    default_value: Any = Field(default=None, validation_alias=AliasChoices('default_value', 'Default Value'))
    description: str = Field(default='', validation_alias=AliasChoices('description', 'Description'))
    # Kept for modbus_tk registry compatibility; a synonym for reference_point_name.
    register_name: str = Field(default='', validation_alias=AliasChoices('register_name', 'Register Name'))
    # TODO: transform is not yet implemented; it should be handled by the base driver for all interfaces.
    transform: str = Field(default='', validation_alias=AliasChoices('transform', 'Transform'))

    @model_validator(mode='before')
    @classmethod
    def _pads_are_not_points(cls, data):
        """A pad row describes registers to read through, not a point.

        The platform builds its equipment tree from these configs before the interface sees them, so the row marks
        itself inactive and never-polled here. It is then never scheduled, published, or requested from the interface.
        """
        if isinstance(data, dict):
            data_type = next((data[k] for k in _DATA_TYPE_KEYS if k in data), None)
            if isinstance(data_type, str) and is_pad_type(data_type):
                overridden = ('active', 'data_source', 'Data Source', 'writable', 'Writable')
                data = {**{k: v for k, v in data.items() if k not in overridden},
                        'active': False, 'data_source': 'never', 'writable': False}
        return data

    @model_validator(mode='before')
    @classmethod
    def _register_name_as_point_name(cls, data):
        """A row with a Register Name but no Volttron Point Name is published under its Register Name.

        Volttron Point Name is always used when present. modbus_tk registries commonly carried both, but a map file
        used directly as the registry has only Register Name.
        """
        if isinstance(data, dict) and _first_value(data, _POINT_NAME_KEYS) is None:
            register_name = register_name_of(data)
            if register_name is not None:
                data = {k: v for k, v in data.items() if k not in _POINT_NAME_KEYS}
                data['volttron_point_name'] = register_name
        return data

    @field_validator('address', mode='before')
    @classmethod
    def _parse_address(cls, v):
        """Addresses may be decimal or, as in modbus_tk register maps, hexadecimal such as 0x200."""
        if isinstance(v, str):
            v = v.strip()
            if v.lower().startswith('0x'):
                return int(v, 16)
        return v

    @field_validator('table', mode='before')
    @classmethod
    def _normalize_table(cls, v):
        v = _lower_or_none(v)
        if v is None or isinstance(v, Table):
            return v
        try:
            return TABLE_ALIASES[v.replace(' ', '_').replace('-', '_')]
        except KeyError:
            raise ValueError(f"Unknown Modbus table {v!r}. Use one of: {', '.join(t.value for t in Table)}")

    @field_validator('word_order', mode='before')
    @classmethod
    def _normalize_word_order(cls, v):
        return _lower_or_none(v)

    @field_validator('count', mode='before')
    @classmethod
    def _empty_count(cls, v):
        return None if v == '' else v

    @property
    def is_pad(self) -> bool:
        return is_pad_type(self.data_type)


class ModbusRemoteConfig(RemoteConfig):
    transport_protocol: TransportProtocol = Field(default=TransportProtocol.tcp,
                                                  validation_alias=AliasChoices('transport_protocol', 'transport'))
    device_address: str
    # None resolves to 802 for TLS and 502 otherwise. Ignored for serial.
    port: int | None = None
    unit_id: int = Field(default=1, validation_alias=AliasChoices('unit_id', 'device_id', 'slave_id'))
    addressing: Addressing = Field(default=Addressing.offset)
    # Default word order for multi-register values; a point may override it.
    word_order: WordOrder = Field(default=WordOrder.big, validation_alias=AliasChoices('word_order', 'endian'))
    # Largest run of unconfigured registers the proxy may read through to merge two requests into one.
    max_gap: int = Field(default=0, ge=0)
    # Device timeout: how long the proxy waits for the device to answer one request before retrying.
    timeout: float = Field(default=3.0, gt=0)
    retries: int = Field(default=3, ge=0)
    # How long to wait for the proxy's reply to a read or write. Must exceed the device timeout: behind a shared
    # gateway a request queues until the other units' requests (including their timeout-and-retry cycles) finish.
    # Defaults to three full timeout-and-retry cycles, and at least 30 seconds.
    reply_timeout: float | None = Field(default=None, gt=0)
    # How long to wait for the proxy process to start and register before declaring setup failed.
    registration_timeout: float = Field(default=30.0, gt=0)
    # Serial settings.
    baudrate: int = Field(default=9600, validation_alias=AliasChoices('baudrate', 'baud_rate'))
    bytesize: int = Field(default=8, ge=5, le=8)
    parity: Parity = Field(default=Parity.none)
    stopbits: StopBits = Field(default=StopBits.one, validation_alias=AliasChoices('stopbits', 'stop_bits'))
    # All Modbus devices share one proxy process unless a group is named here.
    proxy_group: str | None = None
    # Optional modbus_tk-style register map (normally a config:// reference to a CSV, resolved to its rows by the
    # configuration store). Registry rows lacking an address or data type are completed from the map row with the
    # same Register Name; see merge_register_map. Never required when the registry is complete on its own.
    register_map: list[dict] | None = None

    @field_validator('register_map', mode='before')
    @classmethod
    def _normalize_register_map(cls, v, info):
        return normalize_register_map(v, str((info.data or {}).get('device_address', '')))

    @field_validator('addressing', mode='before')
    @classmethod
    def _normalize_addressing(cls, v):
        v = _lower_or_none(v)
        return 'offset' if v in (None, 'exact') else v

    @field_validator('parity', 'word_order', 'transport_protocol', mode='before')
    @classmethod
    def _lower(cls, v):
        return _lower_or_none(v) if isinstance(v, str) else v

    @property
    def resolved_reply_timeout(self) -> float:
        if self.reply_timeout is not None:
            return self.reply_timeout
        return max(30.0, 3 * self.timeout * (self.retries + 1))

    @property
    def resolved_port(self) -> int:
        if self.port is not None:
            return self.port
        return 802 if self.transport_protocol is TransportProtocol.tls else 502

    def device_fields(self) -> dict:
        """The fields every proxy message uses to identify this device's client."""
        fields = {'device_address': self.device_address, 'device_type': self.transport_protocol.value}
        if self.transport_protocol is not TransportProtocol.serial:
            fields['port'] = self.resolved_port
        return fields

    def client_options(self) -> dict:
        """pymodbus client settings for REGISTER_DEVICE."""
        options = {'timeout': self.timeout, 'retries': self.retries}
        if self.transport_protocol is TransportProtocol.serial:
            options.update(baudrate=self.baudrate, bytesize=self.bytesize, parity=self.parity.pymodbus,
                           stopbits=self.stopbits.value)
        return options

    def proxy_key(self) -> tuple:
        """Selects the proxy process. Constant by default so all Modbus devices share one."""
        return ('modbus',) if self.proxy_group is None else ('modbus', self.proxy_group)
