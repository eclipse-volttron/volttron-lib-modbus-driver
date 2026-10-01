"""Platform-level test against a real Modbus demo board.

Skipped unless the MODBUS_TEST_IP environment variable names the board's address. See test_driver_local.py for the
same flow against a local simulated server.
"""
import csv
import json
import os

import pytest

from .conftest import PLATFORM_DRIVER

MODBUS_TEST_IP = "MODBUS_TEST_IP"
skip_msg = f"Env var {MODBUS_TEST_IP} not set. Please set the env var to the proper IP to run this integration test."
pytestmark = pytest.mark.skipif(os.environ.get(MODBUS_TEST_IP) is None, reason=skip_msg)

DEVICE_TOPIC = 'devices/modbus'

REGISTRY_CONFIG = [
    {"Volttron Point Name": "SupplyTemp", "Units": "degC", "Writable": "FALSE", "Point Address": "0", "Notes": "",
     "Modbus Register": ">H", "Multiplier": "100"},
    {"Volttron Point Name": "Demand", "Units": "%", "Writable": "FALSE", "Point Address": "26", "Notes": "",
     "Modbus Register": ">H", "Multiplier": "1"},
    {"Volttron Point Name": "SecondStageCoolingDemandSetPoint", "Units": "None", "Writable": "TRUE",
     "Point Address": "14", "Notes": "", "Modbus Register": ">H", "Multiplier": "1"},
]


@pytest.fixture(scope="module")
def configured_driver(platform, tmp_path_factory):
    config_dir = tmp_path_factory.mktemp('config')
    registry = config_dir / 'modbus.csv'
    with open(registry, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(REGISTRY_CONFIG[0]))
        writer.writeheader()
        writer.writerows(REGISTRY_CONFIG)
    device = config_dir / 'modbus.config'
    device.write_text(json.dumps({
        "driver_config": {"device_address": os.environ[MODBUS_TEST_IP], "port": 502, "slave_id": 8},
        "driver_type": "modbus",
        "registry_config": "config://modbus.csv",
        "interval": 5,
        "timezone": "US/Pacific",
    }))
    platform.store_config(PLATFORM_DRIVER, 'modbus.csv', registry, csv=True)
    platform.store_config(PLATFORM_DRIVER, DEVICE_TOPIC, device)
    platform.wait_for_log(r'Modbus .* table: .* request\(s\) per poll')
    return platform


def test_get_point(configured_driver):
    for point_name in ["SupplyTemp", "Demand", "SecondStageCoolingDemandSetPoint"]:
        point_val = configured_driver.get_point(f'{DEVICE_TOPIC}/{point_name}')
        print(f"Point: {point_name} has point value of {point_val}")
        assert isinstance(point_val, int)


def test_set_point(configured_driver):
    """Write a new value to the setpoint and read it back, then restore what the board had before."""
    point_topic = f'{DEVICE_TOPIC}/SecondStageCoolingDemandSetPoint'
    original = configured_driver.get_point(point_topic)
    new_value = (original + 1) % 2 ** 16                  # a 16-bit register; differ from the current value
    try:
        assert configured_driver.set_point(point_topic, new_value) == new_value
        assert configured_driver.get_point(point_topic) == new_value
    finally:
        configured_driver.set_point(point_topic, original)  # leave the board as we found it, even on failure
    assert configured_driver.get_point(point_topic) == original
