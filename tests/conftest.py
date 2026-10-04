"""Shared fixtures for the Modbus interface test suite. These run without a platform, a proxy, or a device."""
import csv
import os

from pathlib import Path
from unittest import mock

import pytest

from volttron.driver.base.testing import FakePPM, build_interface, serialized      # noqa: F401  (re-exported for tests)
from volttron.driver.interfaces.modbus.modbus import Modbus, ModbusPointConfig

from tests.platform_cli import PlatformCLI

TESTS_DIR = Path(__file__).parent
CATALYST_CSV = TESTS_DIR.parent / 'modbus_example_registry.csv'
# A modbus_tk configuration: a two-column registry (Volttron Point Name, Register Name) completed from a register map.
MODBUSTK_REGISTRY_CSV = TESTS_DIR / 'modbustk_registry.csv'
MODBUSTK_MAP_CSV = TESTS_DIR / 'modbustk_map.csv'


def read_csv(path) -> list[dict]:
    with open(path, newline='') as f:
        return list(csv.DictReader(f))


def point(name, address, data_type='>f', writable=False, **extra):
    return ModbusPointConfig(volttron_point_name=name, address=address, data_type=data_type, writable=writable,
                             units=extra.pop('units', 'F'), **extra)


@pytest.fixture
def ppm():
    return FakePPM()


@pytest.fixture
def make_interface(ppm):
    """Build a real Modbus interface whose proxy manager is the FakePPM."""
    def build(points=(), base_topic='campus/building/rtu', **remote):
        remote = {'driver_type': 'modbus', 'device_address': '10.0.0.4', **remote}
        return build_interface(Modbus, remote, driver_agent=mock.Mock(), ppm=ppm, points=points, base_topic=base_topic)
    return build


@pytest.fixture
def interface(make_interface):
    """Two floats at 1001 and 1003, a pad over 1005-1009, a uint16 at 1010, and two coils."""
    iface = make_interface([
        point('ReturnAirCO2', 1001),
        point('CO2Stpt', 1003, writable=True, default_value='1000'),
        point('gap', 1005, 'pad[5]', table='input'),
        point('Mode', 1010, 'uint16', writable=True),
        point('FanStatus', 3, 'bool'),
        point('FanCmd', 5, 'bool', writable=True),
    ])
    iface.proxy_peer = iface.ppm.peer
    return iface


TOPIC = 'campus/building/rtu/{}'.format


# ---------------------------------------------------------------------------- platform-level tests
# The Platform Driver project, when this interface is checked out inside the modular monorepo; otherwise the PyPI name.
_LOCAL_PLATFORM_DRIVER = Path(__file__).resolve().parents[3] / 'platform-driver-agent'
PLATFORM_DRIVER_SOURCE = str(_LOCAL_PLATFORM_DRIVER) if (_LOCAL_PLATFORM_DRIVER / 'pyproject.toml').exists() \
    else 'volttron-platform-driver'
PLATFORM_DRIVER = 'platform.driver'


@pytest.fixture(scope='module')
def platform(tmp_path_factory):
    """A real VOLTTRON platform with the Platform Driver installed, driven through vctl and vdrv.

    Slow (a few minutes) because the platform builds a poetry project of the environment on first start. Set
    MODBUS_SKIP_PLATFORM_TESTS=1 to skip these tests.
    """
    if os.environ.get('MODBUS_SKIP_PLATFORM_TESTS'):
        pytest.skip('MODBUS_SKIP_PLATFORM_TESTS is set')
    if not PlatformCLI.available():
        pytest.skip('volttron, vctl and vdrv must be installed in the test environment')
    cli = PlatformCLI(tmp_path_factory.mktemp('platform') / 'volttron_home')
    cli.start()
    try:
        cli.install_agent(PLATFORM_DRIVER_SOURCE, vip_identity=PLATFORM_DRIVER, tag='driver')
        yield cli
    finally:
        cli.shutdown()
