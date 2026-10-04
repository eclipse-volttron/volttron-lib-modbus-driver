"""End-to-end harness for the slave role: one real proxy process serves a Modbus TCP unit for an interface in the slave
role, and a second interface in the master role reaches it through the same proxy. Run by test_end_to_end.py in its own
process (gevent's monkey patching must happen before other imports). Exit status is non-zero if any check fails."""
from gevent import monkey; monkey.patch_all()
import gevent, logging, os, sys, time
logging.basicConfig(filename=os.environ.get('MODBUS_E2E_LOG', '/tmp/modbus_e2e_server.log'), level=logging.DEBUG,
                    format='%(asctime)s %(name)s %(levelname)s %(message)s')
from volttron.driver.base.config import RemoteConfig
from volttron.driver.interfaces.modbus.modbus import Modbus
from volttron.driver.interfaces.modbus.config import ModbusPointConfig

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 15021
TRANSPORT = sys.argv[2] if len(sys.argv) > 2 else 'tcp'            # tcp, udp or tls


def make_certs(cert_dir):
    """A CA, a server certificate for 127.0.0.1 and a master certificate, all signed by the CA (tls only)."""
    import datetime, ipaddress
    from pathlib import Path
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    cert_dir = Path(cert_dir); cert_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    before, after = now - datetime.timedelta(days=1), now + datetime.timedelta(days=1)

    def write(name, cert, key):
        (cert_dir / f'{name}.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (cert_dir / f'{name}.key').write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        return str(cert_dir / f'{name}.pem'), str(cert_dir / f'{name}.key')
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'modbus e2e CA')])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(before).not_valid_after(after).add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(ca_key, hashes.SHA256()))
    ca_path, _ = write('ca', ca, ca_key)

    def leaf(name, usage, san=None):
        key = ec.generate_private_key(ec.SECP256R1())
        builder = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])).issuer_name(ca_name)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(before).not_valid_after(after)
                   .add_extension(x509.ExtendedKeyUsage([usage]), critical=False))
        if san:
            builder = builder.add_extension(x509.SubjectAlternativeName(san), critical=False)
        return write(name, builder.sign(ca_key, hashes.SHA256()), key)
    server_cert, server_key = leaf('server', ExtendedKeyUsageOID.SERVER_AUTH, [x509.IPAddress(ipaddress.ip_address('127.0.0.1'))])
    master_cert, master_key = leaf('master', ExtendedKeyUsageOID.CLIENT_AUTH)
    return {'ca': ca_path, 'server_cert': server_cert, 'server_key': server_key, 'master_cert': master_cert, 'master_key': master_key}


served_tls, master_tls = {}, {}
if TRANSPORT == 'tls':
    import tempfile
    certs = make_certs(tempfile.mkdtemp(prefix='modbus-tls-'))
    served_tls = dict(cert_path=certs['server_cert'], key_path=certs['server_key'], ca_path=certs['ca'], client_auth=True)
    master_tls = dict(ca_path=certs['ca'], cert_path=certs['master_cert'], key_path=certs['master_key'])


class Core: spawn = staticmethod(gevent.spawn)
class Agent:
    core = Core()
    def __init__(self): self.pushed = []
    def publish_push(self, result): self.pushed.append(dict(result))
    def flat(self):
        out = {}
        for values in self.pushed: out.update(values)
        return out


Modbus.default_config = {}
S = 'campus/b/served/{}'.format
M = 'campus/b/master/{}'.format
checks = []
def check(name, cond, detail=''):
    checks.append((name, bool(cond))); print(('PASS ' if cond else 'FAIL ') + name + (f'  {detail}' if detail else ''))
def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate(): return True
        gevent.sleep(0.05)
    return predicate()


SERVED_ROWS = [dict(volttron_point_name='ReturnAirCO2', address=1001, data_type='>f', units='PPM', default_value='412.5'),      # input
               dict(volttron_point_name='CO2Stpt', address=1003, data_type='>f', units='PPM', remote_writable=True, default_value='1000'),
               dict(volttron_point_name='Name', address=1005, data_type='string[6]', units='', table='holding', remote_writable=False, default_value='Rtu-1'),
               dict(volttron_point_name='Mode', address=1010, data_type='uint16', units='', remote_writable=True, default_value='3'),
               dict(volttron_point_name='FanStatus', address=3, data_type='bool', units='', default_value='TRUE'),                # discrete input
               dict(volttron_point_name='FanCmd', address=5, data_type='bool', units='', remote_writable=True, default_value='FALSE')]  # coil
MASTER_ROWS = [dict(volttron_point_name='ReturnAirCO2', address=1001, data_type='>f', units='PPM', writable=False),
               dict(volttron_point_name='CO2Stpt', address=1003, data_type='>f', units='PPM', writable=True),
               dict(volttron_point_name='Name', address=1005, data_type='string[6]', units='', writable=True),
               dict(volttron_point_name='Mode', address=1010, data_type='uint16', units='', writable=True),
               dict(volttron_point_name='FanStatus', address=3, data_type='bool', units='', writable=False),
               dict(volttron_point_name='FanCmd', address=5, data_type='bool', units='', writable=True)]

served = master = None
served_agent, master_agent = Agent(), Agent()
try:
    served = Modbus(RemoteConfig(driver_type='modbus', driver_role='slave', transport_protocol=TRANSPORT, bind_host='127.0.0.1', port=PORT, unit_id=7,
                                 **served_tls), driver_agent=served_agent)
    rows = served.prepare_registry_config([dict(r) for r in SERVED_ROWS], served.config)
    check('served rows default to data_source server and writable', all(r['data_source'] == 'server' and r['writable'] is True for r in rows))
    for row in rows:
        served.insert_register(served.create_register(ModbusPointConfig(**row)), 'campus/b/served')
    t0 = time.time(); served.finalize_setup(initial_setup=True); t_setup = time.time() - t0
    check(f'proxy launched and unit served over {TRANSPORT}', served.proxy_peer is not None and served.proxy_peer.socket_params is not None, f'{t_setup:.1f}s')
    check('served table pushed on registration', wait_for(lambda: S('FanCmd') in served_agent.flat())
          and served_agent.flat().get(S('ReturnAirCO2')) == 412.5 and served_agent.flat().get(S('Name')) == 'Rtu-1'
          and served_agent.flat().get(S('FanStatus')) is True, str(served_agent.flat()))

    master = Modbus(RemoteConfig(driver_type='modbus', transport_protocol=TRANSPORT, device_address='127.0.0.1', port=PORT, unit_id=7, timeout=5,
                                 **master_tls), driver_agent=master_agent)
    for row in MASTER_ROWS:
        master.insert_register(master.create_register(ModbusPointConfig(**row)), 'campus/b/master')
    master.finalize_setup(initial_setup=True)
    check('master registered with the same proxy', master.proxy_peer is served.proxy_peer and master.proxy_peer is not None)

    results, errors = master.get_multiple_points(list(master.point_map))
    print('  master poll:', {k.split('/')[-1]: v for k, v in results.items()}, errors)
    check('master reads the starting values', results.get(M('ReturnAirCO2')) == 412.5 and results.get(M('CO2Stpt')) == 1000.0
          and results.get(M('Name')) == 'Rtu-1' and results.get(M('Mode')) == 3 and results.get(M('FanStatus')) is True
          and results.get(M('FanCmd')) is False and errors == {}, str(errors))

    served_agent.pushed.clear()
    r, e = served.set_multiple_points([(S('ReturnAirCO2'), 450.25), (S('FanStatus'), False), (S('Mode'), 4)])
    check('platform writes served values, inputs included', r == {S('ReturnAirCO2'): 450.25, S('FanStatus'): False, S('Mode'): 4} and e == {}, str(e))
    check('served writes are reflected at once', served_agent.flat() == {S('ReturnAirCO2'): 450.25, S('FanStatus'): False, S('Mode'): 4}, str(served_agent.flat()))
    results, _ = master.get_multiple_points([M('ReturnAirCO2'), M('FanStatus'), M('Mode')])
    check('master sees the platform write', results == {M('ReturnAirCO2'): 450.25, M('FanStatus'): False, M('Mode'): 4}, str(results))
    check('served interface reads what the proxy holds', served.get_point(S('ReturnAirCO2')) == 450.25 and served.get_point(S('Mode')) == 4)

    served_agent.pushed.clear()
    r, e = master.set_multiple_points([(M('CO2Stpt'), 950.5), (M('FanCmd'), True), (M('Mode'), 5)])
    check('master writes remote-writable points', r == {M('CO2Stpt'): 950.5, M('FanCmd'): True, M('Mode'): 5} and e == {}, str(e))
    check('master writes are pushed to the served interface', wait_for(lambda: served_agent.flat() == {S('CO2Stpt'): 950.5, S('FanCmd'): True, S('Mode'): 5}),
          str(served_agent.flat()))
    results, _ = served.get_multiple_points([S('CO2Stpt'), S('FanCmd'), S('Mode')])
    check('served values hold the master write', results == {S('CO2Stpt'): 950.5, S('FanCmd'): True, S('Mode'): 5}, str(results))
    r, e = master.set_multiple_points([(M('Name'), 'Other')])
    check('write to a point that is not remote-writable is refused', r == {} and M('Name') in e, str(e))

    # A proxy crash: the manager relaunches it, both interfaces register again, and the served values come back from
    # the platform's memory (the tree values sent with the re-registration) rather than from the starting values.
    class Tree:
        def __init__(self, agent): self.agent = agent
        def get_node(self, topic):
            flat = self.agent.flat()
            return type('Node', (), {'last_value': flat[topic], 'last_updated': True})() if topic in flat else None
    served_agent.pushed.clear()
    served_agent.pushed.append({S('ReturnAirCO2'): 450.25, S('CO2Stpt'): 950.5, S('Mode'): 5, S('FanCmd'): True, S('FanStatus'): False, S('Name'): 'Rtu-1'})
    served.driver_agent.equipment_model = Tree(served_agent)
    old_peer = served.proxy_peer
    old_peer.process.terminate()
    relaunched = wait_for(lambda: served.proxy_peer is not None and served.proxy_peer is not old_peer
                          and served.proxy_peer.socket_params is not None and master.proxy_peer is served.proxy_peer, 20)
    check('proxy relaunched after a crash and both remotes set up again', relaunched)
    def master_reads():
        try:
            return bool(master.get_multiple_points([M('Mode')])[0])
        except Exception:
            return False
    wait_for(master_reads, 20)
    results, errors = master.get_multiple_points([M('ReturnAirCO2'), M('CO2Stpt'), M('Mode'), M('FanCmd')])
    check('served values survive the proxy restart from the platform memory', results == {M('ReturnAirCO2'): 450.25, M('CO2Stpt'): 950.5,
          M('Mode'): 5, M('FanCmd'): True}, f'{results} {errors}')
    served.driver_agent.equipment_model = None

    served.revert_all()
    results, _ = master.get_multiple_points([M('ReturnAirCO2'), M('CO2Stpt'), M('Mode'), M('FanCmd')])
    check('revert_all restores the starting values', results == {M('ReturnAirCO2'): 412.5, M('CO2Stpt'): 1000.0, M('Mode'): 3, M('FanCmd'): False}, str(results))
finally:
    for iface in (served, master):
        if iface is not None and iface.ppm is not None:
            for peer in list(getattr(iface.ppm, 'peers', {}).values()):
                proc = getattr(peer, 'process', None)
                if proc: proc.terminate()
failed = [n for n, ok in checks if not ok]
print(f'\n{len(checks) - len(failed)}/{len(checks)} passed' + (f'; FAILED: {failed}' if failed else ''))
sys.exit(1 if failed else 0)
