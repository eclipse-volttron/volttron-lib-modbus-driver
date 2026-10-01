"""Platform-level test of the Modbus driver against a local modbus_tk server.

A real VOLTTRON platform (see the ``platform`` fixture) runs the Platform Driver, which polls a modbus_tk TCP server
started here through the Modbus Protocol Proxy. Points cover every struct data type in both byte orders, including the
legacy '<' little-endian forms, and are read and written through the Platform Driver's ``vdrv`` command-line tool.
"""
import csv
import json
import logging
import socket
import time

from random import randint
from struct import pack, unpack

import pytest

from . import helpers
from .client import Client, Field
from .conftest import PLATFORM_DRIVER
from .server import Server

logger = logging.getLogger(__name__)

DEVICE_TOPIC = 'devices/modbus'


def get_rand_ip_and_port():

    def is_port_open(ip, port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result = sock.connect_ex((ip, port))
        return result == 0

    def get_rand_port(ip=None, min_ip=5000, max_ip=6000):
        port = randint(min_ip, max_ip)
        if ip:
            while is_port_open(ip, port):
                port = randint(min_ip, max_ip)
        return port

    ip = "127.0.0.{}".format(randint(1, 254))
    port = get_rand_port(ip)
    return ip + ":{}".format(port)


IP, _port = get_rand_ip_and_port().split(":")
PORT = int(_port)

# Register values dictionary for testing set_point and get_point
REGISTERS_DICT = {
    "BigUShort": 2**16 - 1,
    "BigUInt": 2**32 - 1,
    "BigULong": 2**64 - 1,
    "BigShort": -(2**16) // 2,
    "BigInt": -(2**32) // 2,
    "BigFloat": -1234.0,
    "BigLong": -(2**64) // 2,
    "LittleUShort": 0,
    "LittleUInt": 0,
    "LittleULong": 0,
    "LittleShort": (2**16) // 2 - 1,
    "LittleInt": (2**32) // 2 - 1,
    "LittleFloat": 1.0,
    "LittleLong": (2**64) // 2 - 1
}

REGISTRY_CONFIG = [{"Volttron Point Name": "BigUShort", "Units": "PPM", "Modbus Register": ">H", "Writable": "TRUE",
                    "Point Address": "0"},
                   {"Volttron Point Name": "BigUInt", "Units": "PPM", "Modbus Register": ">I", "Writable": "TRUE",
                    "Point Address": "1"},
                   {"Volttron Point Name": "BigULong", "Units": "PPM", "Modbus Register": ">Q", "Writable": "TRUE",
                    "Point Address": "3"},
                   {"Volttron Point Name": "BigShort", "Units": "PPM", "Modbus Register": ">h", "Writable": "TRUE",
                    "Point Address": "7"},
                   {"Volttron Point Name": "BigInt", "Units": "PPM", "Modbus Register": ">i", "Writable": "TRUE",
                    "Point Address": "8"},
                   {"Volttron Point Name": "BigFloat", "Units": "PPM", "Modbus Register": ">f", "Writable": "TRUE",
                    "Point Address": "10"},
                   {"Volttron Point Name": "BigLong", "Units": "PPM", "Modbus Register": ">q", "Writable": "TRUE",
                    "Point Address": "12"},
                   {"Volttron Point Name": "LittleUShort", "Units": "PPM", "Modbus Register": "<H",
                    "Writable": "TRUE", "Point Address": "100"},
                   {"Volttron Point Name": "LittleUInt", "Units": "PPM", "Modbus Register": "<I",
                    "Writable": "TRUE", "Point Address": "101"},
                   {"Volttron Point Name": "LittleULong", "Units": "PPM", "Modbus Register": "<Q",
                    "Writable": "TRUE", "Point Address": "103"},
                   {"Volttron Point Name": "LittleShort", "Units": "PPM", "Modbus Register": "<h",
                    "Writable": "TRUE", "Point Address": "107"},
                   {"Volttron Point Name": "LittleInt", "Units": "PPM", "Modbus Register": "<i", "Writable": "TRUE",
                    "Point Address": "108"},
                   {"Volttron Point Name": "LittleFloat", "Units": "PPM", "Modbus Register": "<f",
                    "Writable": "TRUE", "Point Address": "110"},
                   {"Volttron Point Name": "LittleLong", "Units": "PPM", "Modbus Register": "<q",
                    "Writable": "TRUE", "Point Address": "112"}]

# Polls are scheduled on a hyperperiod, the least common multiple of the intervals in a group; keep it short so the
# nested device below is polled several times within a test.
POLL_INTERVAL = 10

# Legacy form of the device configuration (driver_config, slave_id); the driver still accepts it.
DRIVER_CONFIG = {
    "driver_config": {
        "device_address": IP,
        "port": PORT,
        "slave_id": 1
    },
    "driver_type": "modbus",
    "registry_config": "config://modbus.csv",
    "interval": POLL_INTERVAL,
    "timezone": "UTC"
}


# A modbus_tk-style configuration for a second device on unit 2 of the same server: a registry with only point names
# and register names, completed from a register map with hexadecimal addresses. One registry row has no Volttron Point
# Name (it is published under its Register Name) and one names a register missing from the map (dropped with a warning).
# Its topic is nested beneath the first device's topic, and it polls quickly: the first device's remote must neither
# register nor poll the nested device's points (see EquipmentTree.device_points).
TK_DEVICE_TOPIC = f'{DEVICE_TOPIC}/meter'
TK_POLL_INTERVAL = 5
TK_REGISTRY_CONFIG = [{"Volttron Point Name": "Big Float", "Register Name": "big_float"},
                      {"Volttron Point Name": "", "Register Name": "big_ushort"},
                      {"Volttron Point Name": "Ghost", "Register Name": "not_in_map"}]
TK_REGISTER_MAP = [{"Register Name": "big_float", "Address": "0xA", "Type": "float", "Units": "PPM", "Writable": "TRUE"},
                   {"Register Name": "big_ushort", "Address": "0", "Type": "uint16", "Units": "PPM", "Writable": "TRUE"},
                   {"Register Name": "unused", "Address": "0x64", "Type": "uint16", "Units": "", "Writable": "FALSE"}]
TK_DRIVER_CONFIG = {
    "driver_config": {
        "device_address": IP,
        "port": PORT,
        "slave_id": 2,
        "register_map": "config://modbus_tk_map.csv"
    },
    "driver_type": "modbus",
    "registry_config": "config://modbus_tk.csv",
    "interval": TK_POLL_INTERVAL,
    "timezone": "UTC"
}


def topic(point_name: str) -> str:
    return f'{DEVICE_TOPIC}/{point_name}'


def write_csv(path, rows):
    with open(path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture(scope="module")
def configured_driver(platform, tmp_path_factory):
    """Store the registry and device configuration and wait for the device to register with the proxy."""
    config_dir = tmp_path_factory.mktemp('config')
    registry = config_dir / 'modbus.csv'
    with open(registry, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(REGISTRY_CONFIG[0]))
        writer.writeheader()
        writer.writerows(REGISTRY_CONFIG)
    device = config_dir / 'modbus.config'
    device.write_text(json.dumps(DRIVER_CONFIG))
    platform.store_config(PLATFORM_DRIVER, 'modbus.csv', registry, csv=True)
    platform.store_config(PLATFORM_DRIVER, DEVICE_TOPIC, device)
    summary = platform.wait_for_log(r'Modbus .* unit 1 holding table: 14 points, 0 pads, (\d+) request\(s\) per poll')
    logger.info(summary)
    return platform


@pytest.fixture(scope="module")
def configured_tk_driver(configured_driver, tmp_path_factory):
    """Store a modbus_tk-style registry, register map and device configuration for unit 2."""
    platform = configured_driver
    config_dir = tmp_path_factory.mktemp('config_tk')
    write_csv(config_dir / 'modbus_tk.csv', TK_REGISTRY_CONFIG)
    write_csv(config_dir / 'modbus_tk_map.csv', TK_REGISTER_MAP)
    device = config_dir / 'modbus_tk.config'
    device.write_text(json.dumps(TK_DRIVER_CONFIG))
    platform.store_config(PLATFORM_DRIVER, 'modbus_tk_map.csv', config_dir / 'modbus_tk_map.csv', csv=True)
    platform.store_config(PLATFORM_DRIVER, 'modbus_tk.csv', config_dir / 'modbus_tk.csv', csv=True)
    platform.store_config(PLATFORM_DRIVER, TK_DEVICE_TOPIC, device)
    summary = platform.wait_for_log(r'Modbus .* unit 2 holding table: 2 points, 0 pads, (\d+) request\(s\) per poll')
    logger.info(summary)
    return platform


class PPSPi32Client(Client):
    """
    Define some registers to PPSPi32Client
    """

    def __init__(self, *args, **kwargs):
        super(PPSPi32Client, self).__init__(*args, **kwargs)

    byte_order = helpers.BIG_ENDIAN
    addressing = helpers.ADDRESS_OFFSET

    BigUShort = Field("BigUShort", 0, helpers.USHORT, 'PPM', 2, helpers.no_op,
                      helpers.REGISTER_READ_WRITE, helpers.OP_MODE_READ_WRITE)
    BigUInt = Field("BigUInt", 1, helpers.UINT, 'PPM', 2, helpers.no_op,
                    helpers.REGISTER_READ_WRITE, helpers.OP_MODE_READ_WRITE)
    BigULong = Field("BigULong", 3, helpers.UINT64, 'PPM', 2, helpers.no_op,
                     helpers.REGISTER_READ_WRITE, helpers.OP_MODE_READ_WRITE)
    BigShort = Field("BigShort", 7, helpers.SHORT, 'PPM', 2, helpers.no_op,
                     helpers.REGISTER_READ_WRITE, helpers.OP_MODE_READ_WRITE)
    BigInt = Field("BigInt", 8, helpers.INT, 'PPM', 2, helpers.no_op, helpers.REGISTER_READ_WRITE,
                   helpers.OP_MODE_READ_WRITE)
    BigFloat = Field("BigFloat", 10, helpers.FLOAT, 'PPM', 2, helpers.no_op,
                     helpers.REGISTER_READ_WRITE, helpers.OP_MODE_READ_WRITE)
    BigLong = Field("BigLong", 12, helpers.INT64, 'PPM', 2, helpers.no_op,
                    helpers.REGISTER_READ_WRITE, helpers.OP_MODE_READ_WRITE)
    LittleUShort = Field("LittleUShort", 100, helpers.USHORT, 'PPM', 2, helpers.no_op,
                         helpers.REGISTER_READ_WRITE, helpers.OP_MODE_READ_WRITE)
    LittleUInt = Field("LittleUInt", 101, helpers.UINT, 'PPM', 2, helpers.no_op,
                       helpers.REGISTER_READ_WRITE, helpers.OP_MODE_READ_WRITE)
    LittleULong = Field("LittleULong", 103, helpers.UINT64, 'PPM', 2, helpers.no_op,
                        helpers.REGISTER_READ_WRITE, helpers.OP_MODE_READ_WRITE)
    LittleShort = Field("LittleShort", 107, helpers.SHORT, 'PPM', 2, helpers.no_op,
                        helpers.REGISTER_READ_WRITE, helpers.OP_MODE_READ_WRITE)
    LittleInt = Field("LittleInt", 108, helpers.INT, 'PPM', 2, helpers.no_op,
                      helpers.REGISTER_READ_WRITE, helpers.OP_MODE_READ_WRITE)
    LittleFloat = Field("LittleFloat", 110, helpers.FLOAT, 'PPM', 2, helpers.no_op,
                        helpers.REGISTER_READ_WRITE, helpers.OP_MODE_READ_WRITE)
    LittleLong = Field("LittleLong", 112, helpers.INT64, 'PPM', 2, helpers.no_op,
                       helpers.REGISTER_READ_WRITE, helpers.OP_MODE_READ_WRITE)


@pytest.fixture(scope="module")
def modbus_server():
    """One server for the module: test_default_values runs first and sees the zeros, test_set_point then writes."""
    modbus_server = Server(address=IP, port=PORT)
    modbus_server.define_slave(1, PPSPi32Client, unsigned=True)
    modbus_server.define_slave(2, PPSPi32Client, unsigned=True)     # The modbus_tk-style device.

    # Set values for registers from server as the default values
    modbus_server.set_values(1, PPSPi32Client().field_by_name("BigUShort"), 0)
    modbus_server.set_values(1, PPSPi32Client().field_by_name("BigUInt"), 0)
    modbus_server.set_values(1, PPSPi32Client().field_by_name("BigULong"), 0)
    modbus_server.set_values(1, PPSPi32Client().field_by_name("BigShort"), 0)
    modbus_server.set_values(1, PPSPi32Client().field_by_name("BigInt"), 0)
    modbus_server.set_values(1, PPSPi32Client().field_by_name("BigFloat"), 0)
    modbus_server.set_values(1, PPSPi32Client().field_by_name("BigLong"), 0)
    modbus_server.set_values(1,
                             PPSPi32Client().field_by_name("LittleUShort"),
                             unpack('<H', pack('>H', 0)))
    modbus_server.set_values(1,
                             PPSPi32Client().field_by_name("LittleUInt"),
                             unpack('<HH', pack('>I', 0)))
    modbus_server.set_values(1,
                             PPSPi32Client().field_by_name("LittleULong"),
                             unpack('<HHHH', pack('>Q', 0)))
    modbus_server.set_values(1,
                             PPSPi32Client().field_by_name("LittleShort"),
                             unpack('<H', pack('>h', 0)))
    modbus_server.set_values(1,
                             PPSPi32Client().field_by_name("LittleInt"),
                             unpack('<HH', pack('>i', 0)))
    modbus_server.set_values(1,
                             PPSPi32Client().field_by_name("LittleFloat"),
                             unpack('<HH', pack('>f', 0)))
    modbus_server.set_values(1,
                             PPSPi32Client().field_by_name("LittleLong"),
                             unpack('<HHHH', pack('>q', 0)))

    modbus_server.start()
    time.sleep(1)
    yield modbus_server
    modbus_server.stop()


def test_default_values(modbus_server, configured_driver):
    """
    By default server setting, all registers values are 0
    """
    default_values = configured_driver.get(DEVICE_TOPIC)
    assert set(default_values) == {topic(name) for name in REGISTERS_DICT}
    assert all(value == 0 for value in default_values.values()), default_values


def test_set_point(modbus_server, configured_driver):
    for key, value in REGISTERS_DICT.items():
        assert configured_driver.set_point(topic(key), value) == value
        assert configured_driver.get_point(topic(key)) == value

    all_values = configured_driver.get(DEVICE_TOPIC)
    assert all_values == {topic(name): value for name, value in REGISTERS_DICT.items()}


def test_modbus_tk_registry_and_map(modbus_server, configured_tk_driver):
    """A registry completed from a register map polls and writes like any other; the unmatched row was dropped."""
    platform = configured_tk_driver
    values = platform.get(TK_DEVICE_TOPIC)
    assert set(values) == {f'{TK_DEVICE_TOPIC}/Big Float', f'{TK_DEVICE_TOPIC}/big_ushort'}, values
    assert all(value == 0 for value in values.values()), values
    assert platform.set_point(f'{TK_DEVICE_TOPIC}/Big Float', -1234.0) == -1234.0
    assert platform.get_point(f'{TK_DEVICE_TOPIC}/Big Float') == -1234.0
    assert platform.set_point(f'{TK_DEVICE_TOPIC}/big_ushort', 42) == 42
    assert platform.get_point(f'{TK_DEVICE_TOPIC}/big_ushort') == 42
    assert platform.wait_for_log(r"dropping registry row 'Ghost'.*no register_map row is named 'not_in_map'", timeout=5)
    # Unit 1's registers are untouched by writes to unit 2.
    assert platform.get_point(topic('BigFloat')) != -1234.0 or REGISTERS_DICT['BigFloat'] == -1234.0


def test_nested_device_is_polled_only_by_its_own_remote(modbus_server, configured_tk_driver):
    """After a restart, with both devices loaded from the store, only the nested device's own remote polls its points.

    Poll sets are built from each remote's point set when the Platform Driver starts. Before
    EquipmentTree.device_points, the enclosing device's remote also claimed the nested device's points (which its
    interface does not have), logged a failure for each on every poll, and published an empty values dict for the nested
    device. Waits three hyperperiods after both devices re-register so that several polls of each occur.
    """
    platform = configured_tk_driver
    since = platform.log_size()
    platform.restart_agent(PLATFORM_DRIVER, 'driver')
    assert platform.wait_for_log(r'unit 1 holding table: 14 points', since=since)
    assert platform.wait_for_log(r'unit 2 holding table: 2 points', since=since)
    time.sleep(3 * POLL_INTERVAL)
    failures = [line for line in platform.log_since(since).splitlines()
                if ('Failed to poll' in line or 'Point not configured' in line or 'No values were returned' in line)
                and 'meter' in line]
    assert not failures, '\n'.join(failures)
    assert platform.get_point(f'{TK_DEVICE_TOPIC}/big_ushort') == 42
