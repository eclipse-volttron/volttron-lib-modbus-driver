"""End-to-end harness: real interface -> real GeventProtocolProxyManager -> real Modbus proxy subprocess -> pymodbus
simulator. Run by test_end_to_end.py in its own process (gevent's monkey patching must happen before other imports).
Exit status is non-zero if any check fails."""
from gevent import monkey; monkey.patch_all()
import gevent, logging, socket, subprocess, sys, time
from pathlib import Path
logging.basicConfig(filename=__import__('os').environ.get('MODBUS_E2E_LOG', '/tmp/modbus_e2e.log'), level=logging.DEBUG, format='%(asctime)s %(name)s %(levelname)s %(message)s')
from volttron.driver.base.config import RemoteConfig
from volttron.driver.interfaces.modbus.modbus import Modbus
from volttron.driver.interfaces.modbus.config import ModbusPointConfig

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 15020
sim = subprocess.Popen([sys.executable, str(Path(__file__).with_name('modbus_simulator.py')), str(PORT)])
for _ in range(50):
    try: socket.create_connection(('127.0.0.1', PORT), timeout=0.2).close(); break
    except OSError: time.sleep(0.1)
else: sys.exit('simulator did not start')

class Core: spawn = staticmethod(gevent.spawn)
class Agent: core = Core()
Modbus.default_config = {}
T = 'campus/b/rtu/{}'.format
checks = []
def check(name, cond, detail=''):
    checks.append((name, bool(cond))); print(('PASS ' if cond else 'FAIL ') + name + (f'  {detail}' if detail else ''))

try:
    iface = Modbus(RemoteConfig(driver_type='modbus', device_address='127.0.0.1', port=PORT, unit_id=1, timeout=15), driver_agent=Agent())
    rows = [dict(volttron_point_name='ReturnAirCO2', address=1001, data_type='>f', units='PPM', writable=False),
            dict(volttron_point_name='Temp', address=1010, data_type='int16', units='F', writable=False),
            dict(volttron_point_name='CO2Stpt', address=1003, data_type='>f', units='PPM', writable=True, default_value='1000'),
            dict(volttron_point_name='Name', address=1005, data_type='string[6]', units='', writable=True),
            dict(volttron_point_name='padding', address=1008, data_type='pad[2]', units='', writable=False, table='holding'),
            dict(volttron_point_name='Mode', address=1010, data_type='uint16', units='', writable=True),
            dict(volttron_point_name='Missing', address=5000, data_type='uint16', units='', writable=True),   # illegal address on the device
            dict(volttron_point_name='FanStatus', address=3, data_type='bool', units='', writable=False),
            dict(volttron_point_name='FanCmd', address=5, data_type='bool', units='', writable=True)]
    for row in rows:
        iface.insert_register(iface.create_register(ModbusPointConfig(**row)), 'campus/b/rtu')
    t0 = time.time(); iface.finalize_setup(initial_setup=True); t_setup = time.time() - t0
    check('proxy launched and device registered', iface.proxy_peer is not None and iface.proxy_peer.socket_params is not None, f'{t_setup:.1f}s')

    t0 = time.time(); results, errors = iface.get_multiple_points(list(iface.point_map)); t_poll = time.time() - t0
    print('  results:', {k.split('/')[-1]: v for k, v in results.items()}); print('  errors:', {k.split('/')[-1]: v for k, v in errors.items()})
    check('poll: input floats/ints decoded', results.get(T('ReturnAirCO2')) == 412.5 and results.get(T('Temp')) == -7)
    check('poll: holding float, string, uint16 decoded', results.get(T('CO2Stpt')) == 1000.0 and results.get(T('Name')) == 'RTU-7' and results.get(T('Mode')) == 3)
    check('poll: coils decoded', results.get(T('FanStatus')) is True and results.get(T('FanCmd')) is False)
    check('poll: illegal address isolated to its point', T('Missing') in errors and len(errors) == 1, str(errors.get(T('Missing')))[:80])
    check('poll: pad read but not published', T('padding') not in results and T('padding') not in errors and len(results) == 7, f'{t_poll:.2f}s')

    check('get_point', iface.get_point(T('Mode')) == 3)
    check('set_point returns requested value', iface.set_point(T('Mode'), '7') == 7)     # string, as vctl sends it
    check('set_point visible on device', iface.get_point(T('Mode')) == 7)
    r, e = iface.set_multiple_points([(T('CO2Stpt'), 950.0), (T('Name'), 'AHU-2'), (T('FanCmd'), True)])
    check('set_multiple_points', r == {T('CO2Stpt'): 950.0, T('Name'): 'AHU-2', T('FanCmd'): True} and e == {}, str(e))
    results, _ = iface.get_multiple_points([T('CO2Stpt'), T('Name'), T('FanCmd')])
    check('writes visible on device', results == {T('CO2Stpt'): 950.0, T('Name'): 'AHU-2', T('FanCmd'): True}, str(results))
    try:
        iface.set_point(T('ReturnAirCO2'), 1); check('read-only write rejected', False)
    except Exception as ex:
        check('read-only write rejected', 'read only' in str(ex))
    try:
        iface.set_point(T('Missing'), 1); check('write to illegal address reports error', False)
    except Exception as ex:
        check('write to illegal address reports error', True, str(ex)[:80])

    iface.revert_all()
    results, _ = iface.get_multiple_points([T('CO2Stpt'), T('Mode'), T('Name'), T('FanCmd')])
    check('revert_all: default restored', results.get(T('CO2Stpt')) == 1000.0, str(results.get(T('CO2Stpt'))))
    check('revert_all: clean values restored', results.get(T('Mode')) == 3 and results.get(T('Name')) == 'RTU-7' and results.get(T('FanCmd')) is False, str(results))
finally:
    for peer in list(getattr(iface.ppm, 'peers', {}).values()) if 'iface' in dir() else []:
        proc = getattr(peer, 'process', None)
        if proc: proc.terminate()
    sim.terminate()
failed = [n for n, ok in checks if not ok]
print(f'\n{len(checks) - len(failed)}/{len(checks)} passed' + (f'; FAILED: {failed}' if failed else ''))
sys.exit(1 if failed else 0)
