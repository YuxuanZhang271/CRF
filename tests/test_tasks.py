"""Executable task ownership and completion checks, with no hardware I/O."""
import math
import sys
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from tasks import LOOK_DIRECTIONS, TaskExecutor, TaskState
from flex_endo_robo.control_node import ControlNode
from flex_endo_robo.flex_endo import flex_ipc as ipc
from flex_endo_robo.flex_endo.flex_control import FlexControl
from flex_endo_robo.kinematics import rcm_axis_from_angles

TEST_ARM_JOINTS = np.deg2rad([0., -120., -120., -30., 90., 45.])


class MemoryClient:
    app, ready = 42, True

    def __init__(self):
        flags = (ipc.CALIBRATED | ipc.COUPLED_ROLL_SUPPORTED | ipc.TRACK_HORIZON_SUPPORTED
                 | ipc.DIRECTIONAL_LOOK_SUPPORTED | ipc.TIMED_SPRAY_SUPPORTED
                 | ipc.WATER_SPRAY_READY | ipc.GAS_SPRAY_READY)
        self.status = ipc.FlexStatus(7, 1, self.app, 0, 0, ipc.IDLE, flags,
            ipc.NO_RESULT, 0, 0., 0., 0., 60., (0.,) * 4)
        self.sequence = 0
        self.command = Mock(side_effect=self._command)

    def _command(self, action, **kwargs):
        self.sequence += 1

    def request(self, action, **kwargs):
        self.command(action, **kwargs)
        self.status = replace(self.status, action=action, sequence=self.sequence,
                              request=self.sequence, result=ipc.ACCEPTED)
        return self.sequence

    def send(self, observation=None):
        return True

    def poll(self):
        return self.status


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.node = ControlNode('127.0.0.1', 0, Path(__file__).resolve().parents[1] /
            'src/flex_endo_robo/ur5_endo_flex.urdf')
        self.server = SimpleNamespace(node=self.node, tasks=TaskState(), relay=None,
            interrupting=False, stopping=False, recorder=Mock())
        self.runner = TaskExecutor(self.server)
        self.node.task_executor = self.runner
        self.camera = SimpleNamespace(observation=lambda: SimpleNamespace(frame=3,
            revision=1, category=39, valid=True, centered=True, captured_at=time.monotonic()))

    def hardware(self, arm=True):
        self.client = MemoryClient()
        self.node.flex = FlexControl(self.client, self.camera)
        if arm:
            self.server.relay = Mock()
            self.node.joint_received = True
            self.node._last_joint_at = time.monotonic()
            self.node._arm_moving = False
            self.node.joint_positions = TEST_ARM_JOINTS.copy()
            self.node.arm_mode = 'rcm'
            self.node._capture_rcm_unlocked()
        return self.node.flex

    def start(self, task_id):
        task = self.server.tasks.select(task_id)
        self.runner.activate(task_id, task['revision'])

    def done(self):
        self.client.status = replace(self.client.status, result=ipc.DONE,
            spray_phase=0, flags=self.client.status.flags & ~ipc.MOVING)

    def test_forward_backward_are_owned_and_refreshed_by_coordinator(self):
        self.hardware()
        self.node.flex = None
        for task_id, key in ((1, 'arm_fwd'), (2, 'arm_bwd')):
            self.node.cancel_motion()
            self.start(task_id)
            self.assertTrue(self.node._key_state[key])
            before = self.node._key_updated[key]
            self.runner.tick()
            self.assertGreaterEqual(self.node._key_updated[key], before)
            self.runner.cancel()
            self.node.cancel_motion()
            self.runner.tick()
            self.assertFalse(any(self.node._key_state.values()))

    def test_zoom_runs_on_server_and_holds_at_boundary(self):
        for task_id, initial, expected in ((3, 1.99, 2.), (4, 1.01, 1.)):
            self.server.tasks.update(zoom=initial)
            self.start(task_id)
            for _ in range(6):
                self.runner.active['last_at'] -= .016
                self.runner.tick()
            self.assertEqual(self.server.tasks.snapshot()['zoom'], expected)
            self.assertEqual(self.server.tasks.snapshot()['phase'], 'limited')

    def test_look_tasks_use_directional_controller_without_yolo_selection(self):
        f = self.hardware()
        for task_id, direction in LOOK_DIRECTIONS.items():
            f.tracking = None
            self.start(task_id)
            self.assertEqual(f.task_direction, direction)
            self.assertEqual(f.tracking, (0, 0))
            self.runner.tick()
            self.assertEqual(self.server.tasks.snapshot()['id'], task_id)
            self.runner.cancel()
        f.stop()
        self.assertIsNone(f.task_direction)

    def test_directional_flex_uses_native_look_action_without_fabricating_vision(self):
        f = self.hardware()
        self.start(8)
        n = self.node
        with patch.object(f, '_horizon_error', return_value=0.), \
                patch.object(n, '_rcm_pivot_is_valid', return_value=True), \
                patch.object(n, '_inside_rcm_cone', return_value=True):
            f.tick(n)
        commands = [call.args[0] for call in self.client.command.call_args_list]
        self.assertIn(ipc.DIRECTIONAL_LOOK, commands)
        self.assertNotIn(ipc.TRACK, commands)

    def test_look_without_arm_stops_and_warns_at_flex_edge(self):
        f = self.hardware(arm=False)
        self.client.status = replace(self.client.status, bend_x=60.)
        self.start(8)
        f.tick(self.node)
        self.runner.tick()
        self.assertEqual(self.server.tasks.snapshot()['phase'], 'limited')
        self.assertIn('UR5 assistance unavailable', self.server.tasks.snapshot()['message'])
        self.assertEqual(self.client.command.call_args.args[0], ipc.IDLE)

    def test_uncalibrated_zero_limit_does_not_mean_zero_available_travel(self):
        f = self.hardware(arm=False)
        self.client.status = replace(self.client.status, bend_limit=0.,
            flags=self.client.status.flags & ~ipc.CALIBRATED)
        self.start(8)
        f.tick(self.node)
        self.runner.tick()
        self.assertEqual(self.client.command.call_args.args[0], ipc.JOG)
        self.assertEqual(self.server.tasks.snapshot()['phase'], 'running')

    def test_roll_is_continuous_until_edge_and_interrupt_clears_intent(self):
        f = self.hardware(arm=False)
        for task_id, key, roll in ((9, 'flex_roll_minus', -180.), (10, 'flex_roll_plus', 180.)):
            f.keys.clear()
            self.client.status = replace(self.client.status, roll=0.)
            self.start(task_id)
            self.assertIn(key, f.keys)
            self.client.status = replace(self.client.status, roll=roll)
            self.runner.tick()
            self.assertNotIn(key, f.keys)
            self.assertEqual(self.server.tasks.snapshot()['phase'], 'limited')
        self.runner.cancel()
        f.stop()
        self.assertFalse(f.keys)

    def test_reset_waits_for_both_arm_and_native_flex_completion(self):
        f = self.hardware()
        n = self.node
        reference = n.rcm_ref_axis.copy()
        pivot = n.rcm_world_point.copy()
        captured_depth = n.rcm_ref_depth
        # Preserve an insertion depth different from the initially captured one.
        depth = captured_depth + .015
        tilted_axis = rcm_axis_from_angles([5., 0.], reference)
        tilted = n.kin.ik_along_axis(pivot + depth * tilted_axis, tilted_axis, n.joint_positions)
        self.assertIsNotNone(tilted)
        n.joint_positions = tilted
        with patch.object(n, 'cmd_reset', side_effect=AssertionError('Never run the default joint reset')):
            self.start(12)
        self.assertEqual(self.client.status.action, ipc.RETURN_ZERO)
        self.done()
        self.runner.tick()
        self.assertEqual(self.server.tasks.snapshot()['id'], 12)
        self.assertAlmostEqual(self.runner.active['rcm_depth'], depth, places=5)
        sent = []
        with patch.object(n, '_send', side_effect=lambda command: sent.append(command) or True):
            for _ in range(1000):
                n._last_joint_at = time.monotonic()
                before = len(sent)
                n._tick()
                frame = sent[before:]
                servo = [command for command in frame if command['type'] == 'servo_joint']
                if servo:
                    self.assertFalse(any(command['type'] == 'stop' for command in frame),
                        'The coordinator must not stop a servo target sent by the reset task')
                    n.joint_positions = np.array(servo[-1]['positions'])
                    axis, _, tip = n.kin.tube_axis_in_base(n.joint_positions)
                    self.assertTrue(n._rcm_pose_is_valid(n.joint_positions, depth))
                    self.assertAlmostEqual(float(np.dot(tip - pivot, axis)), depth, places=5)
                    self.assertIn('rcm', servo[-1])
                if self.server.tasks.snapshot()['id'] == 0:
                    break
        self.assertEqual(self.server.tasks.snapshot()['id'], 0)
        self.assertTrue(any(command['type'] == 'servo_joint' for command in sent))
        self.assertFalse(any(command['type'] == 'reset_joint' for command in sent))
        self.assertEqual(n.arm_mode, 'rcm')
        self.assertTrue(n.rcm_enabled)
        np.testing.assert_array_equal(n.rcm_ref_axis, reference)
        np.testing.assert_array_equal(n.rcm_world_point, pivot)
        self.assertEqual(n.rcm_ref_depth, captured_depth)
        self.assertTrue(f._horizon_paused)

    def test_reset_in_linear_mode_is_rejected_before_flex_moves(self):
        self.hardware()
        self.node.arm_mode = 'linear'
        with self.assertRaisesRegex(ValueError, 'active UR5 RCM'):
            self.start(12)
        self.client.command.assert_not_called()

    def test_reset_waits_for_flex_after_rcm_tip_is_already_aligned(self):
        self.hardware()
        self.start(12)
        self.runner.tick()
        self.assertEqual(self.server.tasks.snapshot()['id'], 12)
        self.done()
        self.runner.tick()
        self.assertEqual(self.server.tasks.snapshot()['id'], 0)

    def test_bird_eye_uses_existing_procedure_and_returns_to_zero_at_goal(self):
        f = self.hardware()
        with patch.object(self.node, 'cmd_bird_eye_view', return_value=True) as bird:
            f.pending = (7, ipc.POSITION, time.monotonic() + 30.)
            self.start(11)
        bird.assert_called_once()
        self.runner.tick()
        self.assertEqual(self.server.tasks.snapshot()['id'], 11)
        self.client.status = replace(self.client.status, request=7, result=ipc.DONE)
        self.runner.tick()
        self.assertEqual(self.server.tasks.snapshot()['phase'], 'idle')
        self.assertEqual(self.server.tasks.snapshot()['id'], 0)

    def test_spray_tasks_use_native_timing_and_only_finish_after_outputs_stop(self):
        for task_id, action in ((13, ipc.CLEAN_LENS), (14, ipc.WATER_SPRAY), (15, ipc.GAS_SPRAY)):
            f = self.hardware(arm=False)
            self.start(task_id)
            self.assertEqual(self.client.status.action, action)
            if task_id != 13:
                self.assertEqual(self.client.command.call_args.kwargs['x'], 3.)
                self.assertIsNone(f.spray_key_at)
            self.client.status = replace(self.client.status, result=ipc.DONE, spray_phase=6)
            self.runner.tick()
            self.assertEqual(self.server.tasks.snapshot()['id'], task_id)
            self.done()
            self.runner.tick()
            self.assertEqual(self.server.tasks.snapshot()['id'], 0)

    def test_tracking_is_executable_and_lost_tracking_cannot_unlock_manual_controls(self):
        f = self.hardware()
        self.start(16)
        self.assertEqual(f.tracking, (1, 39))
        self.runner.tick()
        f.cancel_tracking('Tracking stopped · camera lost')
        with patch.object(self.node, 'cmd_stop') as stop:
            self.runner.tick()
        stop.assert_called_once()
        self.assertEqual(self.server.tasks.snapshot()['id'], 16)
        self.assertIn('camera lost', self.server.tasks.snapshot()['error'])

    def test_faults_timeouts_and_stale_invocations_stop_execution(self):
        f = self.hardware(arm=False)
        self.start(14)
        self.runner.active['deadline'] = time.monotonic() - 1.
        with patch.object(self.node, 'cmd_stop') as stop:
            self.runner.tick()
        stop.assert_called_once()
        self.assertIsNotNone(self.server.tasks.snapshot()['error'])
        self.start(3)
        self.server.tasks.select(0)
        zoom = self.server.tasks.snapshot()['zoom']
        self.runner.tick()
        self.assertIsNone(self.runner.active)
        self.assertEqual(self.server.tasks.snapshot()['zoom'], zoom)

    def test_old_native_driver_cannot_start_unbounded_task_spray(self):
        self.hardware(arm=False)
        self.client.status = replace(self.client.status,
            flags=self.client.status.flags & ~ipc.TIMED_SPRAY_SUPPORTED)
        with self.assertRaisesRegex(ValueError, 'Rebuild'):
            self.start(14)
        self.client.command.assert_not_called()

    def test_refused_continuous_movement_is_an_error(self):
        self.hardware(arm=False)
        self.start(9)
        self.client.status = replace(self.client.status, sequence=self.client.sequence, result=ipc.REFUSED)
        with patch.object(self.node, 'cmd_stop'):
            self.runner.tick()
        self.assertIn('refused', self.server.tasks.snapshot()['error'])


if __name__ == '__main__':
    unittest.main()
