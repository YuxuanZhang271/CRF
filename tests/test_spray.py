"""Pump coordination and protocol tests with no device connections."""
import sys
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from flex_endo_robo.flex_endo import flex_ipc as ipc
from flex_endo_robo.flex_endo.flex_control import FlexControl, KEY_TIMEOUT_S


class Client:
    app = 42
    ready = True
    sequence = 0

    def __init__(self):
        self.status = ipc.FlexStatus(7, 1, 42, 0, 0, ipc.IDLE,
                                     ipc.WATER_SPRAY_READY | ipc.GAS_SPRAY_READY,
                                     ipc.NO_RESULT, 0, 0., 0., 0., 0., (0.,) * 4)
        self.command = Mock(side_effect=lambda action, **kw: setattr(
            self, 'status', replace(self.status, action=action)))

    def request(self, action, **kwargs):
        self.sequence += 1
        self.status = replace(self.status, action=action, request=self.sequence, result=ipc.ACCEPTED)
        return self.sequence

    def poll(self):
        return self.status

    def send(self, *args):
        return True


class SprayTests(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.flex = FlexControl(self.client, SimpleNamespace(observation=lambda: None))
        self.node = SimpleNamespace(pull_out_active=False, _key_state={}, _recv_thread=None,
                                    _arm_ready=lambda: False, _motion_fault=None,
                                    _servo_sent_this_tick=False)

    def test_individual_spray_requires_no_calibration_or_ur5_and_release_stops(self):
        for key, action in [('flex_water', ipc.WATER_SPRAY), ('flex_gas', ipc.GAS_SPRAY)]:
            self.assertTrue(self.flex.set_spray_key(key, True, self.node))
            self.assertEqual(self.client.status.action, action)
            self.assertTrue(self.flex.tick(self.node))
            self.assertTrue(self.flex.set_spray_key(key, False, self.node))
            self.assertEqual(self.client.status.action, ipc.STOP)
            self.assertIsNone(self.flex.spray)
            self.client.status = replace(self.client.status, action=ipc.IDLE, result=ipc.DONE)
            self.flex.tick(self.node)

    def test_live_heartbeats_allow_long_hold_but_expiry_cannot_be_revived(self):
        self.flex.set_spray_key('flex_water', True, self.node)
        for _ in range(30):
            next_time = self.flex.spray_key_at + 0.5
            with patch('flex_endo_robo.flex_endo.flex_control.time.monotonic', return_value=next_time):
                self.flex.heartbeat(['flex_water'])
                self.assertTrue(self.flex.tick(self.node))
                self.assertIsNotNone(self.flex.spray)
        expired = self.flex.spray_key_at + KEY_TIMEOUT_S + 0.01
        with patch('flex_endo_robo.flex_endo.flex_control.time.monotonic', return_value=expired):
            self.flex.heartbeat(['flex_water'])
            self.flex.tick(self.node)
        self.assertIsNone(self.flex.spray)
        self.assertEqual(self.client.status.action, ipc.STOP)

    def test_clean_lens_completion_and_repeated_click_cancellation(self):
        self.flex.clean_lens(self.node)
        self.client.status = replace(self.client.status, spray_phase=3)
        self.assertTrue(self.flex.tick(self.node))
        self.assertIn('3 s', self.flex.snapshot()['spray_message'])
        self.flex.clean_lens(self.node)
        self.assertEqual(self.client.status.action, ipc.STOP)
        self.assertFalse(self.flex.stop_acknowledged())
        self.client.status = replace(self.client.status, result=ipc.DONE)
        self.assertFalse(self.flex.stop_acknowledged(), 'A pump phase must block stop confirmation')
        self.client.status = replace(self.client.status, spray_phase=0)
        self.assertTrue(self.flex.stop_acknowledged())
        self.flex.tick(self.node)
        self.flex.clean_lens(self.node)
        self.client.status = replace(self.client.status, result=ipc.DONE, action=ipc.IDLE)
        self.flex.tick(self.node)
        self.assertIsNone(self.flex.spray)
        self.assertIn('complete', self.flex.spray_message)

    def test_capability_and_exclusive_motion_rules(self):
        self.client.status = replace(self.client.status, flags=ipc.WATER_SPRAY_READY)
        with self.assertRaisesRegex(ValueError, 'Gas pump unavailable'):
            self.flex.clean_lens(self.node)
        self.node._key_state = {'arm_up': True}
        with self.assertRaises(ValueError):
            self.flex.set_spray_key('flex_water', True, self.node)
        self.node._key_state.clear()
        self.flex.set_spray_key('flex_water', True, self.node)
        self.assertFalse(self.flex.set_key('flex_up', True))
        with self.assertRaises(ValueError):
            self.flex.set_spray_key('flex_gas', True, self.node)
        self.flex.set_spray_key('flex_gas', False, self.node)
        self.assertIsNotNone(self.flex.spray, 'Releasing a different button cannot stop the owner')

    def test_serial_failure_is_reported_and_interrupt_clears_held_intent(self):
        self.flex.clean_lens(self.node)
        self.client.status = replace(self.client.status, result=ipc.REFUSED, spray_error=-206)
        self.flex.tick(self.node)
        self.assertIn('serial', self.flex.spray_message)
        self.assertIsNone(self.flex.spray)

    def test_protocol_decodes_pump_phase_and_fault(self):
        api = SimpleNamespace(now=lambda: 100)
        with patch.object(ipc, '_Windows', return_value=SimpleNamespace(create_mutex=lambda *a: 1, close=lambda *a: None)), \
                patch.object(ipc.ct, 'get_last_error', return_value=0):
            client = ipc.FlexClient()
        data = ipc.STATUS.pack(*ipc._header(ipc.STATUS), 7, 100, 42, 1, 1,
                               ipc.CLEAN_LENS, 0, ipc.WATER_SPRAY_READY, ipc.ACCEPTED, 0, 0,
                               0., 0., 0., 0., 0., 0., 0., 0., 10., 4, -206)
        client._api.now = api.now
        client._maps['Status'] = SimpleNamespace(copy=lambda: data, close=lambda: None)
        try:
            status = client._read_status()
            self.assertEqual((status.spray_phase, status.spray_error), (4, -206))
        finally:
            client.close()


if __name__ == '__main__':
    unittest.main()
