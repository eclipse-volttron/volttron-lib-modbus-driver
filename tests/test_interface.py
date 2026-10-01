"""Tests for the Modbus driver interface: configuration, register creation, and proxy messaging."""
import csv
import logging

import pytest

from gevent import Timeout

from volttron.driver.base.config import RemoteConfig
from volttron.driver.base.interfaces import DriverInterfaceError
from volttron.driver.interfaces.modbus.config import (Addressing, ModbusPointConfig, ModbusRemoteConfig, Parity,
                                                      Table, TransportProtocol, merge_register_map)
from volttron.driver.interfaces.modbus.modbus import Modbus, ModbusRegister

from tests.conftest import CATALYST_CSV, MODBUSTK_MAP_CSV, MODBUSTK_REGISTRY_CSV, TOPIC, point, read_csv, serialized


class TestPointConfig:
    def test_legacy_csv_headers_load(self):
        with open(CATALYST_CSV, newline='') as f:
            rows = [ModbusPointConfig(**row) for row in csv.DictReader(f)]
        stpt = next(r for r in rows if r.volttron_point_name == 'ReturnAirCO2Stpt')
        assert (stpt.address, stpt.data_type, stpt.writable, stpt.default_value) == (1011, '>f', True, '1000')
        assert stpt.table is None and stpt.word_order is None

    @pytest.mark.parametrize('spelling, table', [
        ('coil', Table.coil), ('Coils', Table.coil), ('discrete_output_coils', Table.coil),
        ('discrete input', Table.discrete_input), ('contact', Table.discrete_input),
        ('analog_input_registers', Table.input), ('Input Registers', Table.input),
        ('holding', Table.holding), ('analog-output-holding-registers', Table.holding), ('', None),
    ])
    def test_table_aliases(self, spelling, table):
        assert point('p', 1, table=spelling).table is table

    def test_unknown_table_rejected(self):
        with pytest.raises(ValueError, match='Unknown Modbus table'):
            point('p', 1, table='registers')

    def test_pad_detection(self):
        assert point('p', 1, 'pad').is_pad and point('p', 1, 'PAD[3]').is_pad and point('p', 1, 'reserved').is_pad
        assert not point('p', 1, 'uint16').is_pad

    @pytest.mark.parametrize('given, expected', [('0x200', 512), (' 0X20a ', 522), ('1001', 1001), (7, 7)])
    def test_hex_or_decimal_address(self, given, expected):
        assert point('p', given).address == expected

    @pytest.mark.parametrize('row', [
        {'Register Name': 'active_power_total', 'Address': '0x200', 'Type': 'float'},              # no column at all
        {'Volttron Point Name': '', 'Register Name': 'active_power_total', 'Address': 1, 'Type': 'float'},   # blank
        {'register_name': 'active_power_total', 'address': 1, 'data_type': 'float'},
    ])
    def test_register_name_used_when_point_name_missing(self, row):
        cfg = ModbusPointConfig(**row)
        assert cfg.volttron_point_name == 'active_power_total' and cfg.register_name == 'active_power_total'

    def test_volttron_point_name_preferred_over_register_name(self):
        cfg = ModbusPointConfig(**{'Volttron Point Name': 'Active Power Total', 'Register Name': 'active_power_total',
                                   'Address': '0x200', 'Type': 'float'})
        assert cfg.volttron_point_name == 'Active Power Total' and cfg.register_name == 'active_power_total'

    def test_point_name_still_required_without_register_name(self):
        with pytest.raises(ValueError, match='olttron'):
            ModbusPointConfig(**{'Address': 1, 'Type': 'float'})


class TestRemoteConfig:
    def test_defaults(self):
        cfg = ModbusRemoteConfig(driver_type='modbus', device_address='10.0.0.4')
        assert cfg.resolved_port == 502 and cfg.unit_id == 1 and cfg.addressing is Addressing.offset
        assert cfg.device_fields() == {'device_address': '10.0.0.4', 'device_type': 'tcp', 'port': 502}
        assert cfg.client_options() == {'timeout': 3.0, 'retries': 3}
        assert cfg.proxy_key() == ('modbus',)

    def test_tls_default_port_and_group(self):
        cfg = ModbusRemoteConfig(driver_type='modbus', device_address='h', transport_protocol='TLS', proxy_group='plant-b')
        assert cfg.resolved_port == 802 and cfg.proxy_key() == ('modbus', 'plant-b')

    def test_serial_options_use_pymodbus_spellings(self):
        cfg = ModbusRemoteConfig(driver_type='modbus', device_address='/dev/ttyUSB0', transport_protocol='serial',
                                 baud_rate=19200, parity='Even', stop_bits=2)
        assert cfg.device_fields() == {'device_address': '/dev/ttyUSB0', 'device_type': 'serial'}
        assert cfg.client_options() == {'timeout': 3.0, 'retries': 3, 'baudrate': 19200, 'bytesize': 8,
                                        'parity': 'E', 'stopbits': 2.0}
        assert Parity.none.pymodbus == 'N'

    @pytest.mark.parametrize('kwargs, expected', [
        ({}, 36.0),                                         # 3 * 3 s * (3 + 1)
        ({'timeout': 1.0, 'retries': 0}, 30.0),             # floor
        ({'timeout': 5.0, 'retries': 2}, 45.0),
        ({'reply_timeout': 5.0}, 5.0),                      # explicit wins
    ])
    def test_reply_timeout_derived_from_device_timeout(self, kwargs, expected):
        cfg = ModbusRemoteConfig(driver_type='modbus', device_address='h', **kwargs)
        assert cfg.resolved_reply_timeout == expected
        assert cfg.registration_timeout == 30.0

    def test_legacy_aliases(self):
        cfg = ModbusRemoteConfig(driver_type='modbus', device_address='h', slave_id=7, endian='little', addressing='exact')
        assert cfg.unit_id == 7 and cfg.word_order.value == 'little' and cfg.addressing is Addressing.offset

    @pytest.mark.parametrize('mode, table, given, expected', [
        ('offset', Table.holding, 1001, 1001),
        ('offset_plus', Table.holding, 1001, 1000),
        ('address', Table.holding, 41001, 1000),
        ('address', Table.coil, 1, 0),
        ('address', Table.discrete_input, 10005, 4),
        ('address', Table.input, 30001, 0),
    ])
    def test_addressing(self, mode, table, given, expected):
        assert Addressing(mode).resolve(given, table) == expected

    def test_addressing_out_of_range(self):
        with pytest.raises(ValueError, match='out of range'):
            Addressing.address.resolve(1001, Table.holding)


class TestCreateRegister:
    def test_float_from_legacy_row(self, make_interface):
        iface = make_interface()
        reg = iface.create_register(point('ReturnAirCO2', 1001, '>f', units='PPM'))
        assert isinstance(reg, ModbusRegister)
        assert (reg.table, reg.address, reg.spec.count, reg.read_only, reg.python_type) == (Table.input, 1001, 2, True, float)
        assert reg.get_register_type() == ('byte', True) and reg.get_units() == 'PPM'
        assert reg.spec_fields() == {'address': 1001, 'data_type': 'FLOAT32', 'count': 2, 'word_order': 'big'}

    @pytest.mark.parametrize('data_type, writable, table, kind', [
        ('>f', False, Table.input, 'byte'), ('>f', True, Table.holding, 'byte'),
        ('bool', False, Table.discrete_input, 'bit'), ('bool', True, Table.coil, 'bit'),
    ])
    def test_default_table(self, make_interface, data_type, writable, table, kind):
        reg = make_interface().create_register(point('p', 1, data_type, writable=writable))
        assert reg.table is table and reg.register_type == kind

    def test_explicit_table_and_string(self, make_interface):
        reg = make_interface().create_register(point('name', 20, 'string[8]', table='holding', string_encoding='ascii'))
        assert reg.table is Table.holding and reg.python_type is str and reg.spec.count == 4
        assert reg.spec_fields()['string_encoding'] == 'ascii'

    def test_count_override(self, make_interface):
        assert make_interface().create_register(point('p', 1, 'string', count=6)).spec.count == 6

    def test_word_order_precedence(self, make_interface):
        iface = make_interface(word_order='little')
        assert iface.create_register(point('a', 1)).spec.word_order == 'little'             # remote default
        assert iface.create_register(point('b', 3, word_order='big')).spec.word_order == 'big'   # point wins
        iface = make_interface()
        assert iface.create_register(point('c', 5, mixed_endian=True)).spec.word_order == 'little'

    def test_legacy_little_endian_struct_types(self, make_interface):
        reg = make_interface().create_register(point('p', 100, '<f', writable=True))
        assert reg.spec.byte_swap is True and reg.spec.word_order == 'little'
        assert reg.spec_fields() == {'address': 100, 'data_type': 'FLOAT32', 'count': 2, 'word_order': 'little',
                                     'byte_swap': True}
        reg = make_interface().create_register(point('q', 100, '<f', word_order='big'))
        assert reg.spec.byte_swap is True and reg.spec.word_order == 'big'

    def test_addressing_applied(self, make_interface):
        iface = make_interface(addressing='address')
        assert iface.create_register(point('p', 40011, '>f', writable=True)).address == 10

    def test_pad_row_is_kept_out_of_point_map(self, interface):
        assert TOPIC('gap') not in interface.point_map
        assert [(p.address, p.count) for p in interface.pads[Table.input]] == [(1005, 5)]

    def test_pad_row_marks_itself_inactive_for_the_platform(self):
        # The platform driver only schedules active points, so a pad row never becomes a phantom poll.
        for row in (dict(volttron_point_name='gap', address=1005, data_type='pad[5]', table='holding'),
                    {'Volttron Point Name': 'gap', 'Point Address': 1005, 'Modbus Register': 'PAD[2]', 'Table': 'holding',
                     'Writable': 'TRUE'}):
            cfg = ModbusPointConfig(**row)
            assert cfg.is_pad and cfg.active is False and cfg.data_source.value == 'never' and cfg.writable is False
        real = ModbusPointConfig(volttron_point_name='p', address=1, data_type='uint16')
        assert real.active is None and real.data_source.value == 'short_poll'

    def test_pad_row_requires_table(self, make_interface):
        with pytest.raises(ValueError, match='must name its table'):
            make_interface().create_register(point('gap', 1005, 'pad[5]'))
        iface = make_interface(addressing='offset_plus')
        reg = iface.create_register(point('gap', 11, 'pad[2]', table='Holding Registers'))
        assert reg.is_pad and reg.address == 10 and reg.spec.count == 2       # addressing applied to pads too

    def test_default_value_registered_for_revert(self, interface):
        assert interface._tracker.get_revert_value(TOPIC('CO2Stpt')) == 1000.0

    def test_bad_default_value_warns(self, make_interface, caplog):
        make_interface([point('p', 1, 'uint16', writable=True, default_value='lots')])
        assert 'bad default value' in caplog.text

    @pytest.mark.parametrize('kwargs, message', [
        (dict(data_type='>f', writable=True, table='input'), 'read-only'),
        (dict(data_type='uint16', table='coil'), 'holds booleans'),
        (dict(data_type='nonsense'), 'Unrecognized'),
        (dict(data_type='uint32', count=3), 'not a multiple'),
    ])
    def test_invalid_points(self, make_interface, kwargs, message):
        with pytest.raises(ValueError, match=message):
            make_interface().create_register(point('bad', 1, **kwargs))

    def test_metadata_types(self, interface):
        assert interface.point_map[TOPIC('FanStatus')].python_type is bool
        assert interface.point_map[TOPIC('Mode')].python_type is int


class TestRegisterMap:
    """modbus_tk configurations: an optional register map completes registry rows that lack address or type."""

    def test_complete_registry_never_consults_map(self):
        rows = read_csv(CATALYST_CSV)
        conflicting_map = [{'Register Name': r.get('Reference Point Name', ''), 'Address': '0x999', 'Type': 'uint16'}
                           for r in rows]
        assert merge_register_map(rows, None) == rows
        assert merge_register_map(rows, conflicting_map) == rows

    def test_two_column_registry_completed_from_map(self):
        merged = merge_register_map(read_csv(MODBUSTK_REGISTRY_CSV), read_csv(MODBUSTK_MAP_CSV), 'dev')
        configs = [ModbusPointConfig(**row) for row in merged]
        assert [c.volttron_point_name for c in configs][:3] == ['Active Power Total', 'Reactive Power Total',
                                                                 'Apparent Power Total']
        first = configs[0]
        assert (first.register_name, first.address, first.data_type, first.units, first.writable) == \
               ('active_power_total', 0x200, 'float', 'kW', True)
        assert configs[-1].address == 0x605 and configs[-1].data_type == 'uint16'

    def test_registry_row_overrides_map_but_blank_cells_do_not(self):
        registry = [{'Volttron Point Name': 'Power', 'Register Name': 'p', 'Units': 'MW', 'Writable': ''}]
        register_map = [{'Register Name': 'p', 'Address': '10', 'Type': 'float', 'Units': 'kW', 'Writable': 'TRUE'}]
        merged, = merge_register_map(registry, register_map)
        assert merged == {'Register Name': 'p', 'Address': '10', 'Type': 'float', 'Units': 'MW', 'Writable': 'TRUE',
                          'Volttron Point Name': 'Power'}

    def test_mixed_registry_completes_only_incomplete_rows(self):
        registry = [{'Volttron Point Name': 'Own', 'Address': '1', 'Type': 'uint16'},
                    {'Volttron Point Name': 'Mapped', 'Register Name': 'm'}]
        register_map = [{'Register Name': 'm', 'Address': '2', 'Type': 'float'},
                        {'Register Name': 'Own', 'Address': '99', 'Type': 'float'}]
        merged = merge_register_map(registry, register_map)
        assert merged[0] is registry[0]
        assert merged[1]['Address'] == '2' and merged[1]['Volttron Point Name'] == 'Mapped'

    def test_unmatched_rows_dropped_with_warning_and_rest_kept(self, caplog):
        registry = read_csv(MODBUSTK_REGISTRY_CSV)
        registry.insert(1, {'Volttron Point Name': 'Ghost', 'Register Name': 'not_in_map'})
        registry.append({'Volttron Point Name': 'Nameless', 'Register Name': ''})
        with caplog.at_level(logging.WARNING):
            merged = merge_register_map(registry, read_csv(MODBUSTK_MAP_CSV), '10.0.0.4')
        assert [r['Volttron Point Name'] for r in merged] == [r['Volttron Point Name'] for r in read_csv(MODBUSTK_REGISTRY_CSV)]
        messages = [rec.message for rec in caplog.records if 'dropping registry row' in rec.message]
        assert len(messages) == 2
        assert "'Ghost'" in messages[0] and "no register_map row is named 'not_in_map'" in messages[0]
        assert "'Nameless'" in messages[1] and 'no Register Name' in messages[1]

    def test_incomplete_rows_without_map_dropped_with_warning(self, caplog):
        registry = read_csv(MODBUSTK_REGISTRY_CSV)
        with caplog.at_level(logging.WARNING):
            assert merge_register_map(registry, None, '10.0.0.4') == []
        assert all('no register_map is configured' in rec.message for rec in caplog.records) and caplog.records

    def test_map_rows_without_register_name_are_ignored(self, caplog):
        register_map = [{'Register Name': '', 'Address': '1', 'Type': 'float'},
                        {'Register Name': 'p', 'Address': '2', 'Type': 'float'}]
        with caplog.at_level(logging.WARNING):
            merged, = merge_register_map([{'Register Name': 'p'}], register_map)
        assert merged['Address'] == '2'
        assert any('no Register Name' in rec.message for rec in caplog.records)

    def test_remote_config_accepts_rows_and_rejects_garbage(self):
        rows = read_csv(MODBUSTK_MAP_CSV)
        assert ModbusRemoteConfig(driver_type='modbus', device_address='h', register_map=rows).register_map == rows
        assert ModbusRemoteConfig(driver_type='modbus', device_address='h').register_map is None
        with pytest.raises(ValueError, match='register_map'):
            ModbusRemoteConfig(driver_type='modbus', device_address='h', register_map=[1, 2])

    def test_unresolved_reference_logs_error_and_is_ignored(self, caplog):
        with caplog.at_level(logging.ERROR):
            cfg = ModbusRemoteConfig(driver_type='modbus', device_address='h', register_map='config://missing.csv')
        assert cfg.register_map is None
        assert any("'config://missing.csv' was not resolved" in rec.message for rec in caplog.records)

    def test_interface_hook_uses_its_own_map(self, make_interface):
        iface = make_interface(register_map=read_csv(MODBUSTK_MAP_CSV))
        merged = iface.prepare_registry_config(read_csv(MODBUSTK_REGISTRY_CSV))
        assert len(merged) == 10 and merged[0]['Address'] == '0x200'

    def test_interface_hook_prefers_the_supplied_remote_config(self, make_interface):
        """During an update the platform passes the new remote config, which may carry a newer map."""
        iface = make_interface(register_map=read_csv(MODBUSTK_MAP_CSV))
        newer = RemoteConfig(driver_type='modbus', device_address='10.0.0.4',
                             register_map=[{'Register Name': 'active_power_total', 'Address': '5', 'Type': 'int16'}])
        merged = iface.prepare_registry_config([{'Volttron Point Name': 'P', 'Register Name': 'active_power_total'}], newer)
        assert merged == [{'Register Name': 'active_power_total', 'Address': '5', 'Type': 'int16',
                           'Volttron Point Name': 'P'}]

    def test_interface_without_map_passes_complete_rows_through(self, make_interface):
        iface = make_interface()
        rows = read_csv(CATALYST_CSV)
        assert iface.prepare_registry_config(rows, RemoteConfig(driver_type='modbus', device_address='10.0.0.4')) == rows

    def test_merged_rows_build_registers(self, make_interface):
        merged = merge_register_map(read_csv(MODBUSTK_REGISTRY_CSV), read_csv(MODBUSTK_MAP_CSV))
        iface = make_interface([ModbusPointConfig(**row) for row in merged])
        reg = iface.get_register_by_name(TOPIC('Active Power Total'))
        assert (reg.address, reg.python_type, reg.read_only) == (0x200, float, False)
        assert len(iface.get_register_names()) == 10


class TestSetup:
    def test_constructor_uses_shared_manager(self, interface, ppm):
        assert interface.ppm is ppm and ppm.started == 1

    def test_finalize_setup_registers_device_with_tables(self, interface, ppm):
        ppm.queue(serialized({'client': 'tcp:10.0.0.4:502',
                              'configured': {'input': {'specs': 1, 'pads': 1, 'blocks': [[1001, 2]]}}, 'cleared': []}))
        interface.finalize_setup(initial_setup=True)
        assert ppm.proxy_key == ('modbus',) and ppm.proxy_kwargs == {}
        [(method, payload)] = ppm.sent
        assert method == 'REGISTER_DEVICE'
        assert payload['device_address'] == '10.0.0.4' and payload['device_type'] == 'tcp' and payload['port'] == 502
        assert payload['timeout'] == 3.0 and payload['unit_id'] == 1 and payload['clear_others'] is True
        tables = payload['tables']
        assert set(tables) == {'input', 'holding', 'discrete_input', 'coil'}
        assert [s['address'] for s in tables['input']] == [1001, 1005]       # point, then pad
        assert tables['input'][1] == {'address': 1005, 'data_type': 'PAD', 'count': 5}
        assert tables['holding'] == [{'address': 1003, 'data_type': 'FLOAT32', 'count': 2, 'word_order': 'big'},
                                     {'address': 1010, 'data_type': 'UINT16', 'count': 1, 'word_order': 'big'}]
        assert tables['coil'] == [{'address': 5, 'data_type': 'BITS', 'count': 1, 'word_order': 'big'}]

    def test_registration_summary_logged(self, interface, ppm, caplog):
        ppm.queue(serialized({'configured': {'input': {'specs': 1, 'pads': 1, 'blocks': [[1001, 2], [1010, 1]]}}}))
        with caplog.at_level(logging.INFO):
            interface.finalize_setup()
        assert '1 points, 1 pads, 2 request(s) per poll' in caplog.text

    def test_registration_failure_warns(self, interface, ppm, caplog):
        ppm.queue(serialized({}, {'configure': "Invalid spec 0 for holding table on unit 1: overlaps"}))
        interface.finalize_setup()
        assert 'Failed to register Modbus device' in caplog.text and 'overlaps' in caplog.text

    def test_registration_wait_is_separate_from_device_timeout(self, make_interface, ppm):
        make_interface(timeout=1.0).finalize_setup()
        assert ppm.registration_wait == 30.0
        make_interface(registration_timeout=7.0).finalize_setup()
        assert ppm.registration_wait == 7.0

    def test_reply_wait_uses_reply_timeout(self, make_interface, ppm):
        from gevent.event import AsyncResult

        class RecordingResult(AsyncResult):
            waits = []

            def get(self, block=True, timeout=None):
                self.waits.append(timeout)
                return super().get(block, timeout)
        iface = make_interface([point('p', 1)], timeout=2.0, retries=1)
        iface.proxy_peer = ppm.peer
        reply = RecordingResult()
        reply.set(serialized({'1': 4.0}))
        ppm.queue(reply)
        assert iface.get_point(TOPIC('p')) == 4.0
        assert RecordingResult.waits == [30.0]           # max(30, 3 * 2 * 2) rather than the 2 s device timeout

    def test_proxy_group_in_key(self, make_interface, ppm):
        iface = make_interface(proxy_group='plant-b')
        iface.finalize_setup()
        assert ppm.proxy_key == ('modbus', 'plant-b')

    def test_unique_remote_id_identifies_the_unit(self):
        tcp = RemoteConfig(driver_type='modbus', device_address='10.0.0.4', unit_id=3)
        assert Modbus.unique_remote_id('rtu.config', tcp) == ('tcp', '10.0.0.4', 502, 3)
        serial = RemoteConfig(driver_type='modbus', device_address='/dev/ttyUSB0', transport_protocol='serial', unit_id=2)
        assert Modbus.unique_remote_id('meter.config', serial) == ('serial', '/dev/ttyUSB0', 2)

    def test_not_initialized(self, make_interface):
        iface = make_interface([point('p', 1)])
        with pytest.raises(DriverInterfaceError):
            iface.get_multiple_points([TOPIC('p')])
        with pytest.raises(DriverInterfaceError):
            iface.set_point(TOPIC('p'), 1)


class TestReads:
    def test_whole_table_poll_omits_queries(self, interface, ppm):
        ppm.queue(serialized({'1001': 412.5}))
        results, errors = interface.get_multiple_points([TOPIC('ReturnAirCO2')])
        assert results == {TOPIC('ReturnAirCO2'): 412.5} and errors == {}
        [(method, payload)] = ppm.sent
        assert method == 'READ_REGISTERS'
        assert payload == {'device_address': '10.0.0.4', 'device_type': 'tcp', 'port': 502, 'unit_id': 1,
                           'register_map': 'input', 'decode': True}

    def test_subset_poll_sends_queries_with_bridging_pads(self, make_interface, ppm):
        iface = make_interface([point('a', 1001), point('b', 1003), point('gap', 1005, 'pad[5]', table='input'),
                                point('c', 1010, 'uint16'), point('d', 1020, 'uint16'), point('far', 1030, 'pad[2]', table='input')])
        iface.proxy_peer = ppm.peer
        ppm.queue(serialized({'1001': 1.0, '1010': 42}))
        results, errors = iface.get_multiple_points([TOPIC('a'), TOPIC('c')])
        assert results == {TOPIC('a'): 1.0, TOPIC('c'): 42} and errors == {}
        assert ppm.payloads()[0]['queries'] == [[1001, 2], [1010, 1], [1005, 5]]   # pad between them, not the far one

    def test_points_grouped_per_table(self, interface, ppm):
        ppm.queue(serialized({'1001': 1.0}), serialized({'1003': 2.0, '1010': 7}), serialized({'3': True}),
                  serialized({'5': False}))
        topics = [TOPIC(n) for n in ('ReturnAirCO2', 'CO2Stpt', 'Mode', 'FanStatus', 'FanCmd')]
        results, errors = interface.get_multiple_points(topics)
        assert results == {TOPIC('ReturnAirCO2'): 1.0, TOPIC('CO2Stpt'): 2.0, TOPIC('Mode'): 7,
                           TOPIC('FanStatus'): True, TOPIC('FanCmd'): False}
        assert errors == {}
        assert [p['register_map'] for p in ppm.payloads()] == ['input', 'holding', 'discrete_input', 'coil']

    def test_request_errors_mapped_to_points(self, interface, ppm):
        # The proxy keys errors by the start of the failed request, which covered both holding points.
        ppm.queue(serialized({}, {'1003': 'ExceptionResponse(illegal address)'}))
        results, errors = interface.get_multiple_points([TOPIC('CO2Stpt'), TOPIC('Mode')])
        assert results == {}
        assert errors[TOPIC('CO2Stpt')] == 'Request starting at 1003 failed: ExceptionResponse(illegal address)'
        assert errors[TOPIC('Mode')].startswith('Request starting at 1003 failed')

    def test_missing_value_without_error(self, interface, ppm):
        ppm.queue(serialized({'1003': 2.0}))
        results, errors = interface.get_multiple_points([TOPIC('CO2Stpt'), TOPIC('Mode')])
        assert results == {TOPIC('CO2Stpt'): 2.0} and errors == {TOPIC('Mode'): 'No value returned by the Modbus Proxy.'}

    def test_unknown_topic(self, interface, ppm):
        results, errors = interface.get_multiple_points(['campus/building/rtu/Nope'])
        assert results == {} and errors == {'campus/building/rtu/Nope': 'Point not configured on device.'} and ppm.sent == []

    def test_unsendable_and_proxy_level_errors(self, interface, ppm):
        ppm.queue(False, b'{"status": "error", "method": "READ_REGISTERS", "error": "boom"}')
        _, errors = interface.get_multiple_points([TOPIC('ReturnAirCO2')])
        assert 'Unable to send request' in errors[TOPIC('ReturnAirCO2')]
        _, errors = interface.get_multiple_points([TOPIC('ReturnAirCO2')])
        assert errors[TOPIC('ReturnAirCO2')] == 'Modbus Proxy READ_REGISTERS failed: boom'

    def test_timeout_is_reported_per_point(self, interface, ppm, monkeypatch):
        def timed_out(*_, **__):
            raise Timeout()
        monkeypatch.setattr(interface, 'parse_proxy_response', timed_out)
        results, errors = interface.get_multiple_points([TOPIC('ReturnAirCO2'), TOPIC('Mode')])
        assert results == {} and all('Timeout' in e for e in errors.values()) and set(errors) == {TOPIC('ReturnAirCO2'), TOPIC('Mode')}

    def test_get_point(self, interface, ppm):
        ppm.queue(serialized({'1010': 3}), serialized({}, {'1010': 'nope'}))
        assert interface.get_point(TOPIC('Mode')) == 3
        with pytest.raises(RuntimeError, match='nope'):
            interface.get_point(TOPIC('Mode'))

    def test_poll_updates_revert_tracker(self, interface, ppm):
        ppm.queue(serialized({'1003': 950.0}))
        interface.get_multiple_points([TOPIC('CO2Stpt')])
        interface.set_multiple_points([])                          # nothing dirty yet
        assert interface._tracker.get_revert_value(TOPIC('CO2Stpt')) == 1000.0   # configured default wins


class TestWrites:
    def test_set_point_encodes_via_proxy(self, interface, ppm):
        ppm.queue(serialized([{'address': 1010, 'count': 1}], [None]))
        assert interface.set_point(TOPIC('Mode'), 4) == 4
        [(method, payload)] = ppm.sent
        assert method == 'WRITE_REGISTERS'
        assert payload == {'device_address': '10.0.0.4', 'device_type': 'tcp', 'port': 502, 'unit_id': 1,
                           'register_map': 'holding', 'encode': True, 'queries': [[1010, None, 4]]}
        assert TOPIC('Mode') in interface._tracker.dirty_points

    def test_set_point_read_only(self, interface, ppm):
        with pytest.raises(RuntimeError, match='read only'):
            interface.set_point(TOPIC('ReturnAirCO2'), 1)
        assert ppm.sent == []

    def test_set_point_error(self, interface, ppm):
        ppm.queue(serialized([None], ['Error encoding Modbus value: bad']))
        with pytest.raises(RuntimeError, match='bad'):
            interface.set_point(TOPIC('Mode'), 5)

    def test_values_coerced_to_point_type_before_sending(self, interface, ppm):
        # Command-line tools deliver strings; the proxy must receive the register's Python type.
        ppm.queue(serialized([{'address': 1010, 'count': 1}, {'address': 1003, 'count': 2}], [None, None]),
                  serialized([{'address': 5, 'count': 1}], [None]))
        results, errors = interface.set_multiple_points([(TOPIC('Mode'), '7'), (TOPIC('CO2Stpt'), '950'), (TOPIC('FanCmd'), 'true')])
        assert results == {TOPIC('Mode'): 7, TOPIC('CO2Stpt'): 950.0, TOPIC('FanCmd'): True} and errors == {}
        assert ppm.payloads('WRITE_REGISTERS')[0]['queries'] == [[1010, None, 7], [1003, None, 950.0]]
        assert ppm.payloads('WRITE_REGISTERS')[1]['queries'] == [[5, None, True]]

    def test_uncoercible_value_reported_without_sending(self, interface, ppm):
        results, errors = interface.set_multiple_points([(TOPIC('Mode'), 'seven')])
        assert results == {} and 'not valid for this point (int)' in errors[TOPIC('Mode')] and ppm.sent == []

    def test_set_multiple_points_batches_per_table(self, interface, ppm):
        ppm.queue(serialized([{'address': 1003, 'count': 2}, {'address': 1010, 'count': 1}], [None, None]),
                  serialized([{'address': 5, 'count': 1}], [None]))
        results, errors = interface.set_multiple_points([(TOPIC('CO2Stpt'), 900.0), (TOPIC('Mode'), 2),
                                                         (TOPIC('FanCmd'), True), (TOPIC('FanStatus'), True)])
        assert results == {TOPIC('CO2Stpt'): 900.0, TOPIC('Mode'): 2, TOPIC('FanCmd'): True}
        assert errors == {TOPIC('FanStatus'): 'Trying to write to a point configured read only.'}
        payloads = ppm.payloads('WRITE_REGISTERS')
        assert [p['register_map'] for p in payloads] == ['holding', 'coil']
        assert payloads[0]['queries'] == [[1003, None, 900.0], [1010, None, 2]]

    def test_partial_write_failure(self, interface, ppm):
        ppm.queue(serialized([{'address': 1003, 'count': 2}, None], [None, 'Count mismatch']))
        results, errors = interface.set_multiple_points([(TOPIC('CO2Stpt'), 900.0), (TOPIC('Mode'), 2)])
        assert results == {TOPIC('CO2Stpt'): 900.0} and errors == {TOPIC('Mode'): 'Count mismatch'}

    def test_whole_request_failure(self, interface, ppm):
        ppm.queue(b'{"status": "error", "method": "WRITE_REGISTERS", "error": "down"}')
        results, errors = interface.set_multiple_points([(TOPIC('CO2Stpt'), 900.0), (TOPIC('Mode'), 2)])
        assert results == {} and set(errors) == {TOPIC('CO2Stpt'), TOPIC('Mode')} and 'down' in errors[TOPIC('Mode')]

    def test_revert_point_writes_default(self, interface, ppm):
        ppm.queue(serialized([{'address': 1003, 'count': 2}], [None]))
        interface.set_point(TOPIC('CO2Stpt'), 800.0)
        ppm.queue(serialized([{'address': 1003, 'count': 2}], [None]))
        interface.revert_point(TOPIC('CO2Stpt'))
        assert ppm.payloads('WRITE_REGISTERS')[-1]['queries'] == [[1003, None, 1000.0]]
