import json
import time
import unittest
from services.printpilot.bridge import telemetry as tm


class AMS(unittest.TestCase):
    params = dict(ams_id=2, slot_id=1, filament_id='GFL99', setting_id='GFSA00', tray_type='PLA', tray_color='123456FF', nozzle_temp_min=190, nozzle_temp_max=230)

    def report(self, **changes):
        tray = dict(id='1', tag_uid='00000000', tray_info_idx='GFL99', setting_id='GFSA00', tray_type='PLA', tray_color='123456FF', remain=50, nozzle_temp_min='190', nozzle_temp_max='230')
        tray.update(changes)
        return dict(gcode_state='IDLE', task_id='task', subtask_id='sub', project_id='project', ams={'tray_exist_bits': '200', 'tray_now': '9', 'ams': [{'id': '2', 'humidity': '3', 'tray': [tray]}]})

    def client(self):
        c = tm.TelemetryClient('192.0.2.1', 'code', serial='SN')
        c.merge(json.dumps({'print': self.report()}).encode())
        return c

    def test_payload_matches_native(self):
        p = tm.command_payload('ams_filament_setting', self.params, '7')['print']
        self.assertEqual((p['ams_id'], p['slot_id'], p['tray_id'], p['tray_info_idx']), (2, 1, 1, 'GFL99'))
        for key, value in [('ams_id', True), ('slot_id', 4), ('tray_color', '#fff'), ('nozzle_temp_min', 400), ('filament_id', '')]:
            with self.assertRaises(ValueError):
                tm.command_payload('ams_filament_setting', dict(self.params, **{key: value}), '7')

    def test_nested_deltas_and_ids(self):
        c = self.client()
        c.merge(json.dumps({'print': {'ams': {'ams': [{'id': '2', 'tray': [{'id': '1', 'remain': 20}]}]}}}).encode())
        t = c.snapshot()
        slot = t['ams']['units'][0]['slots'][0]
        self.assertEqual((slot['material'], slot['remaining'], slot['rfid'], slot['active']), ('PLA', 20, False, True))
        self.assertEqual(t['task_id'], 'task')
        c.merge(b'{"print":{"ams":{"ams":[]}}}')
        self.assertEqual(c.snapshot()['ams']['units'], [])

    def test_unknown_and_rfid_rejected_without_publish(self):
        for tag in [None, '', 'DEADBEEF']:
            c = self.client()
            c.merge(json.dumps({'print': self.report(tag_uid=tag)}).encode())
            self.assertFalse(c.send_command('ams_filament_setting', self.params, .01)[0])
        c = self.client()
        c.merge(b'{"print":{"gcode_state":"RUNNING"}}')
        self.assertFalse(c.send_command('ams_filament_setting', self.params, .01)[0])
        c = self.client()
        c._ams_updated_at = 0
        self.assertFalse(c.send_command('ams_filament_setting', self.params, .01)[0])

    def execute(self, ack, readback):
        c = self.client()
        class Sock:
            def sendall(inner, packet):
                if b'ams_filament_setting' in packet:
                    seq = next(iter(c._waiters))
                    c.merge(json.dumps({'print': dict(ack, sequence_id=seq)}).encode())
        c._sock = Sock()
        def refresh(timeout):
            if readback is not None:
                c.merge(json.dumps({'print': readback}).encode())
            return True
        c.refresh = refresh
        return c.send_command('ams_filament_setting', self.params, .06)

    def test_explicit_ack_and_complete_readback_required(self):
        ack = dict(command='ams_filament_setting', result='success')
        self.assertTrue(self.execute(ack, self.report())[0])
        without_setting = self.report()
        del without_setting['ams']['ams'][0]['tray'][0]['setting_id']
        self.assertTrue(self.execute(ack, without_setting)[0])
        self.assertFalse(self.execute(ack, self.report(nozzle_temp_max='240'))[0])
        self.assertFalse(self.execute(ack, self.report(setting_id='wrong'))[0])
        self.assertTrue(self.execute({'command': 'ams_filament_setting'}, self.report())[0])  # native echo ACK; readback decides
        self.assertFalse(self.execute(ack, None)[0])
        self.assertFalse(self.execute(ack, self.report(tray_color='FFFFFFFF'))[0])
        self.assertFalse(self.execute(ack, {'ams': {'ams': [{'id': '2', 'tray': [{'id': '1', 'remain': 49}]}]}})[0])

    def test_ack_does_not_refresh_stale_state(self):
        c = self.client()
        before = c._updated_at
        c.merge(b'{"print":{"command":"ams_filament_setting","result":"success","tray_color":"FFFFFFFF"}}')
        self.assertEqual(c._updated_at, before)
        self.assertEqual(c.snapshot()['ams']['units'][0]['slots'][0]['color'], '123456FF')

    def test_missing_external_and_bad_values(self):
        t = tm.normalize({'nozzle_temper': float('inf'), 'vt_tray': {'tag_uid': '0', 'tray_type': 'PETG', 'remain': -1}})
        self.assertNotIn('nozzle_temp', t)
        self.assertEqual(t['ams']['units'], [])
        self.assertFalse(t['ams']['external']['rfid'])
        self.assertIsNone(t['ams']['external']['remaining'])

    def test_live_a1_sanitized_fields(self):
        # Captured A1 units use zero tag_uid with tray_is_bbl_bits set;
        # brand/profile bits do not prove a physical RFID tag exists.
        report = {'gcode_state': 'FINISH', 'ams': {'tray_exist_bits': 'ff', 'tray_is_bbl_bits': 'ff',
            'ams': [{'id': '0', 'humidity': '5', 'humidity_raw': '0', 'tray': [
                {'id': '0', 'tag_uid': '0000000000000000', 'tray_uuid': '0' * 32,
                 'tray_info_idx': 'GFG00', 'tray_type': 'PETG', 'tray_color': 'FFFFFFFF', 'remain': 100}]},
                {'id': '1', 'humidity': '5', 'tray': [
                    {'id': '3', 'tag_uid': '0' * 16, 'tray_type': 'PLA', 'tray_color': 'C52C18FF', 'remain': -1}]}]},
            'vt_tray': {'id': '254', 'tag_uid': '0' * 16, 'tray_type': 'PLA'}}
        ams = tm.normalize(report)['ams']
        self.assertEqual([u['id'] for u in ams['units']], [0, 1])
        self.assertEqual(ams['units'][0]['humidity_unit'], 'level')
        self.assertFalse(ams['units'][0]['slots'][0]['rfid'])
        self.assertTrue(ams['units'][1]['slots'][0]['present'])
        self.assertIsNone(ams['units'][1]['slots'][0]['remaining'])
        self.assertEqual(ams['external']['id'], 0)

    def test_removed_units_and_replug_do_not_revive_cached_material(self):
        c = self.client()
        c.merge(b'{"print":{"ams":{"ams_exist_bits":"0"}}}')
        self.assertEqual(c.snapshot()['ams']['units'], [])
        c.merge(b'{"print":{"ams":{"ams_exist_bits":"4"}}}')
        unit = c.snapshot()['ams']['units'][0]
        self.assertEqual(unit['id'], 2)
        self.assertEqual(unit['slots'][0]['material'], '')
        self.assertTrue(unit['slots'][0]['rfid'])
        self.assertTrue(unit['slots'][0]['present'])

    def test_presence_bits_show_unknown_slots_and_units(self):
        report = self.report()
        report['ams']['ams_exist_bits'] = '5'
        report['ams']['tray_exist_bits'] = '20f'
        units = tm.normalize(report)['ams']['units']
        self.assertEqual([u['id'] for u in units], [0, 2])
        self.assertEqual([s['id'] for s in units[0]['slots']], [0, 1, 2, 3])
        self.assertTrue(all(s['present'] and s['rfid'] and not s['rfid_known'] for s in units[0]['slots']))
        self.assertFalse(units[1]['slots'][0]['rfid'])

    def test_native_echo_ack_without_result_then_readback(self):
        echo = dict(command='ams_filament_setting', ams_id=2, slot_id=1, tray_id=1)
        self.assertTrue(self.execute(echo, self.report())[0])
        self.assertFalse(self.execute(echo, None)[0])
        self.assertFalse(self.execute(dict(command='ams_filament_setting', result='fail', reason='busy'), self.report())[0])
        self.assertFalse(self.execute(dict(command='ams_filament_setting', err_code=83935249), self.report())[0])
        self.assertFalse(self.execute(dict(command='push_status'), self.report())[0])

    def test_dynamic_flow_fields_are_normalized(self):
        def slot(**changes):
            return next(s for s in tm.normalize(self.report(**changes))['ams']['units'][0]['slots'] if s['id'] == 1)
        got = slot(k=0.048000000417, n=1, cali_idx=730)
        self.assertEqual((got['flow_k'], got['flow_n'], got['cali_idx']), (0.048, 1.0, 730))
        got = slot(k='bad', cali_idx=-1)
        self.assertEqual((got['flow_k'], got['cali_idx']), (None, -1))
        self.assertIsNone(slot(k=True)['flow_k'])

    def test_string_zero_error_code_is_success(self):
        ack = dict(command='ams_filament_setting', result='success', err_code='0')
        self.assertTrue(self.execute(ack, self.report())[0])
        self.assertTrue(tm.interpret_reply({'result': 'success', 'err_code': '0'})[0])

    def test_stale_idle_slot_requests_fresh_status_before_edit(self):
        c = self.client()
        c._slot_updated.clear()
        refreshed = []
        def refresh(timeout):
            refreshed.append(timeout)
            c.merge(json.dumps({'print': self.report()}).encode())
            return True
        c.refresh = refresh
        # No socket is configured, so reaching the connection check proves
        # preflight refreshed and accepted the formerly stale manual slot.
        ok, reason = c.send_command('ams_filament_setting', self.params, .01)
        self.assertFalse(ok)
        self.assertEqual(refreshed, [3.0])
        self.assertIn('MQTT', reason)
