"""CB3 Freedrive ownership, acknowledgements and cleanup without robot connections."""

import io
import json
import sys
import threading
import time
import unittest
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from http.client import HTTPResponse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from app import _Handler
from flex_endo_robo.control_node import ControlNode
from flex_endo_robo.ur5_rtde_server import FREEDRIVE_LEASE_S, PERIOD, Relay
from tasks import TaskState


class FreedriveTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.
        self.clock = patch('flex_endo_robo.ur5_rtde_server.time.monotonic', side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.control = Mock()
        for name in ('teachMode', 'endTeachMode', 'setWatchdog', 'kickWatchdog', 'isConnected'):
            getattr(self.control, name).return_value = True
        self.relay = Relay(self.control, None, 'unused', Mock())
        self.sample()

    def sample(self, velocity=0.):
        self.relay.telemetry = (self.now, {'joints': [0.] * 6}, [velocity] * 6)

    def enter(self):
        result = self.relay.request_freedrive(True, 'owner')
        self.relay._tick()
        self.assertIsNone(result.result(timeout=0))

    def settle(self):
        for _ in range(4):
            self.now += 2 * PERIOD
            self.sample()
            self.relay._tick()

    def test_cb3_pair_is_acknowledged_and_exit_waits_for_standstill(self):
        self.enter()
        self.control.teachMode.assert_called_once()
        self.control.freedriveMode.assert_not_called()
        self.control.setWatchdog.assert_called_once_with(2.)
        self.assertTrue(self.relay.state()[1]['freedrive_active'])
        result = self.relay.request_freedrive(False, None)
        self.sample(0.02)  # Hand guiding can be moving when the operator exits.
        self.relay._tick()
        self.control.endTeachMode.assert_called_once()
        self.assertFalse(result.done())
        self.assertTrue(self.relay.freedrive_busy())
        self.settle()
        self.assertIsNone(result.result(timeout=0))
        self.assertFalse(self.relay.freedrive_busy())
        self.control.moveJ.assert_not_called()
        self.control.servoJ.assert_not_called()

    def test_pending_and_active_freedrive_discard_conflicting_motion(self):
        result = self.relay.request_freedrive(True, 'owner')
        for active in (False, True):
            if active:
                self.relay._tick()
            for kind, args in (('move_joint', ([1.] * 6, .5, .5)),
                               ('reset_joint', ([1.] * 6, .5, .5)),
                               ('servo_joint', ([1.] * 6,))):
                self.relay.submit(kind, args)
            self.assertIsNone(self.relay.pending_move)
            self.assertIsNone(self.relay.servo_target)
            with self.assertRaises(ValueError):
                self.relay.set_speed_limit(25.)
        self.assertTrue(result.done())

    def test_entry_requires_fresh_stationary_idle_feedback(self):
        for blocked in ('moving', 'stale', 'move', 'servo', 'handoff', 'fault', 'closed'):
            with self.subTest(blocked=blocked):
                relay = Relay(self.control, None, 'unused', Mock())
                relay.telemetry = (self.now, {'joints': [0.] * 6}, [0.] * 6)
                if blocked == 'moving':
                    relay.telemetry = (self.now, {'joints': [0.] * 6}, [.1] * 6)
                elif blocked == 'stale':
                    relay.telemetry = (self.now - 1., {'joints': [0.] * 6}, [0.] * 6)
                elif blocked == 'move':
                    relay.pending_move = (([1.] * 6, .5, .5), self.now)
                elif blocked == 'servo':
                    relay.servo_target = ([1.] * 6, self.now, None, 'servo_joint')
                elif blocked == 'handoff':
                    relay.handoff_deadline = self.now + 1.
                else:
                    setattr(relay, blocked, True)
                with self.assertRaises(ValueError):
                    relay.request_freedrive(True, 'owner')
        self.control.teachMode.assert_not_called()

    def test_only_matching_live_lease_can_keep_freedrive_active(self):
        self.enter()
        self.now += FREEDRIVE_LEASE_S / 2
        self.assertFalse(self.relay.refresh_freedrive('other-browser'))
        self.assertTrue(self.relay.refresh_freedrive('owner'))
        self.now += FREEDRIVE_LEASE_S / 2
        self.sample()
        self.relay._tick()
        self.assertEqual(self.relay.mode, 'freedrive')
        self.now += FREEDRIVE_LEASE_S
        self.assertFalse(self.relay.refresh_freedrive('owner'))
        self.sample()
        self.relay._tick()
        self.control.endTeachMode.assert_called_once()
        self.settle()
        self.assertFalse(self.relay.freedrive_busy())

    def test_expired_entry_never_enables_hand_guiding(self):
        result = self.relay.request_freedrive(True, 'owner')
        self.now += FREEDRIVE_LEASE_S + .01
        self.sample()
        self.relay._tick()
        self.assertIn('expired', result.result(timeout=0))
        self.control.teachMode.assert_not_called()

    def test_interrupt_exits_and_confirms_standstill(self):
        self.enter()
        result = self.relay.submit('interrupt')
        self.relay._tick()
        self.assertFalse(result.done())
        self.settle()
        self.assertIsNone(result.result(timeout=0))
        self.assertFalse(self.relay.freedrive_busy())

    def test_stop_cancels_pending_entry(self):
        entry = self.relay.request_freedrive(True, 'owner')
        self.relay.submit('stop')
        self.relay._tick()
        self.assertIn('cancelled', entry.result(timeout=0))
        self.control.teachMode.assert_not_called()

    def test_keyboard_release_cancels_entry_before_controller_dispatch(self):
        entry = self.relay.request_freedrive(True, 'owner')
        exit_result = self.relay.request_freedrive(False, None)
        self.assertIn('cancelled', entry.result(timeout=0))
        self.relay._tick()
        self.assertIsNone(exit_result.result(timeout=0))
        self.control.teachMode.assert_not_called()
        self.assertFalse(self.relay.freedrive_busy())

    def test_keyboard_release_during_entry_waits_for_exit_acknowledgement(self):
        exit_result = None

        def release_while_controller_is_entering():
            nonlocal exit_result
            exit_result = self.relay.request_freedrive(False, None)
            return True

        self.control.teachMode.side_effect = release_while_controller_is_entering
        entry = self.relay.request_freedrive(True, 'owner')
        self.relay._tick()
        self.assertIn('cancelled', entry.result(timeout=0))
        self.assertFalse(exit_result.done(), 'Entry acknowledgement must not consume the queued exit')
        self.assertIs(self.relay.request_freedrive(False, None), exit_result)
        self.relay._tick()
        self.control.endTeachMode.assert_called_once()
        self.settle()
        self.assertIsNone(exit_result.result(timeout=0))
        self.assertFalse(self.relay.freedrive_busy())

    def test_dashboard_stop_runs_even_if_watchdog_keepalive_fails(self):
        self.enter()
        self.control.kickWatchdog.return_value = False
        self.now += .2
        result = self.relay.submit('estop')
        with patch('flex_endo_robo.ur5_rtde_server.dashboard_stop') as stop:
            self.relay._tick()
        stop.assert_called_once_with('unused')
        self.assertIsNone(result.result(timeout=0))

    def test_rejected_entry_latches_fault_and_attempts_exit(self):
        result = self.relay.request_freedrive(True, 'owner')
        self.control.teachMode.return_value = False
        shutdown = threading.Event()
        shutdown.wait = lambda delay: shutdown.set()
        self.relay.motion_loop(shutdown)
        self.assertIn('rejected', result.result(timeout=0))
        self.assertIn('rejected', self.relay.motion_fault())
        self.control.endTeachMode.assert_called_once()

    def test_rejected_exit_uses_dashboard_fallback(self):
        self.enter()
        self.control.endTeachMode.return_value = False
        result = self.relay.request_freedrive(False, None)
        shutdown = threading.Event()
        shutdown.wait = lambda delay: shutdown.set()
        with patch('flex_endo_robo.ur5_rtde_server.dashboard_stop') as stop:
            self.relay.motion_loop(shutdown)
        self.assertIn('unconfirmed', result.result(timeout=0))
        stop.assert_called_once_with('unused')
        self.assertIsNone(self.relay.mode)

    def test_worker_shutdown_exits_active_freedrive(self):
        self.enter()
        shutdown = threading.Event()
        shutdown.set()
        self.relay.motion_loop(shutdown)
        self.control.endTeachMode.assert_called_once()
        self.assertTrue(self.relay.closed)

    def test_watchdog_keeps_running_after_normal_control_resumes(self):
        self.enter()
        self.relay.request_freedrive(False, None)
        self.relay._tick()
        self.settle()
        self.control.kickWatchdog.reset_mock()
        self.now += .2
        self.sample()
        self.relay._tick()
        self.control.kickWatchdog.assert_called_once()
        self.assertFalse(self.relay.freedrive_busy())


class FreedriveHttpTests(unittest.TestCase):
    def setUp(self):
        urdf = Path(__file__).resolve().parents[1] / 'src/flex_endo_robo/ur5_endo_flex.urdf'
        self.node = ControlNode('127.0.0.1', 0, urdf)
        self.node.joint_received = True
        self.node._last_joint_at = time.monotonic()
        self.node._arm_moving = False
        self.relay = Relay(Mock(), None, 'unused', self.node.kin)
        self.relay.telemetry = (time.monotonic(), {'joints': [0.] * 6}, [0.] * 6)
        self.server = SimpleNamespace(node=self.node, relay=self.relay, stopping=False,
                                      interrupting=False, tasks=TaskState())

    def request(self, path, body):
        payload = json.dumps(body).encode()
        headers = (f'POST {path} HTTP/1.0\r\nHost: localhost\r\nContent-Type: application/json\r\n'
                   f'Content-Length: {len(payload)}\r\n\r\n')
        output = bytearray()
        connection = SimpleNamespace(makefile=lambda *a: io.BytesIO(headers.encode() + payload),
                                     sendall=output.extend, settimeout=lambda timeout: None)
        _Handler(connection, ('127.0.0.1', 0), self.server)
        response = HTTPResponse(SimpleNamespace(makefile=lambda *a: io.BytesIO(output)))
        response.begin()
        return response.status, json.loads(response.read())

    def test_validation_disabled_arm_task_and_motion_guards(self):
        for body in ({}, {'enabled': 1}, {'enabled': True}, {'enabled': True, 'lease': ''}):
            self.assertEqual(self.request('/api/cmd/freedrive', body)[0], 400)
        body = {'enabled': True, 'lease': 'browser'}
        self.server.relay = None
        self.assertEqual(self.request('/api/cmd/freedrive', body)[0], 503)
        self.server.relay = self.relay
        self.server.tasks.select(3)
        self.assertEqual(self.request('/api/cmd/freedrive', body)[0], 409)
        self.server.tasks.select(0)
        self.node._key_state['arm_up'] = True
        self.assertEqual(self.request('/api/cmd/freedrive', body)[0], 409)

    def test_entry_clears_old_rcm_and_motion_targets(self):
        self.node.arm_mode = 'rcm'
        self.node.rcm_enabled = True
        self.node.rcm_world_point = np.ones(3)
        self.node._q_cmd = np.ones(6)
        done = Future()
        done.set_result(None)
        with patch.object(self.relay, 'request_freedrive', return_value=done):
            status, result = self.request('/api/cmd/freedrive', {'enabled': True, 'lease': 'browser'})
        self.assertEqual(status, 200)
        self.assertTrue(result['ok'])
        self.assertEqual(self.node.arm_mode, 'linear')
        self.assertFalse(self.node.rcm_enabled)
        self.assertIsNone(self.node.rcm_world_point)
        self.assertIsNone(self.node._q_cmd)
        self.assertFalse(self.node._arm_ready())
        self.assertFalse(self.node.set_key('arm_up', True))
        self.assertFalse(self.node.set_key('flex_s', True))

    def test_manual_routes_and_tasks_are_blocked_but_release_is_allowed(self):
        self.relay.mode = 'freedrive'
        for path, body in (('/api/cmd/reset', {}), ('/api/mode/arm', {}),
                           ('/api/cmd/clean_lens', {}), ('/api/task', {'id': 3}),
                           ('/api/key', {'key': 'arm_up', 'down': True})):
            with self.subTest(path=path):
                self.assertIn(self.request(path, body)[0], (400, 409))
        self.assertEqual(self.request('/api/key', {'key': 'arm_up', 'down': False})[0], 200)

    def test_heartbeat_cannot_enter_freedrive(self):
        self.assertEqual(self.request('/api/freedrive/heartbeat', {'lease': 'browser'})[0], 409)
        self.assertIsNone(self.relay.mode)
        self.relay.request_freedrive(True, 'browser')
        self.assertEqual(self.request('/api/freedrive/heartbeat', {'lease': 'other'})[0], 409)
        self.assertEqual(self.request('/api/freedrive/heartbeat', {'lease': 'browser'})[0], 200)

    def test_unconfirmed_entry_reports_error_and_requests_stop(self):
        result = Mock()
        result.result.side_effect = FutureTimeoutError
        with patch.object(self.relay, 'request_freedrive', return_value=result), \
                patch.object(self.relay, 'submit') as stop:
            status, body = self.request('/api/cmd/freedrive', {'enabled': True, 'lease': 'browser'})
        self.assertEqual(status, 504)
        self.assertIn('did not confirm', body['error'])
        stop.assert_called_once_with('stop')

    def test_uncalibrated_stationary_flex_does_not_prevent_hand_guiding(self):
        flex = self.node.flex = Mock()
        flex.ready.return_value = True
        flex.status.moving = False
        flex.snapshot.return_value = {'busy': False}
        flex.tracking = None
        flex.procedure = None
        flex.spray_busy = False
        done = Future()
        done.set_result(None)
        with patch.object(self.relay, 'request_freedrive', return_value=done):
            self.assertEqual(self.request('/api/cmd/freedrive', {'enabled': True, 'lease': 'browser'})[0], 200)
        flex.ready.assert_called_once_with(calibrated=False)
        flex.stop.assert_called_once()


if __name__ == '__main__':
    unittest.main()
