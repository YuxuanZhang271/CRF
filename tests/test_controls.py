"""Regression coverage for control cleanup, using no device connections."""

import math
import json
import sys
import time
import unittest
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flex_endo_robo.control_node import ControlNode, KEY_TIMEOUT_S
from flex_endo_robo.flex_endo import flex_ipc as ipc
from flex_endo_robo.flex_endo.flex_control import FlexControl
from flex_endo_robo.kinematics import rcm_axis_from_angles, rcm_tip_angles
from flex_endo_robo.ur5_rtde_server import Relay, parse_command

URDF = Path(__file__).resolve().parents[1] / "src/flex_endo_robo/ur5_endo_flex.urdf"


class MemoryFlexClient:
    """Accept requests and let tests advance native completion explicitly."""

    app = 42
    ready = True

    def __init__(self):
        self.status = ipc.FlexStatus(7, 1, self.app, 0, 0, ipc.IDLE,
                                     ipc.CALIBRATED, ipc.NO_RESULT, 0,
                                     0., 0., 0., 60., (0.,) * 4)
        self.sequence = 0
        self.command = Mock()

    def request(self, action, **parameters):
        self.sequence += 1
        self.status = replace(self.status, action=action, request=self.sequence,
                              result=ipc.ACCEPTED, flags=ipc.CALIBRATED | ipc.MOVING)
        return self.sequence

    def poll(self):
        return self.status

    def send(self, observation=None):
        return True


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.node = ControlNode("127.0.0.1", 0, URDF)

    def test_urdf_and_axis_conversions(self):
        self.assertTrue(self.node.kin.ready)
        reference = np.array([0., 0., 1.])
        for angles in ([0., 0.], [5., -3.], [-10., 4.]):
            axis = rcm_axis_from_angles(angles, reference)
            np.testing.assert_allclose(rcm_tip_angles(axis, reference), angles, atol=1e-10)
        joints = np.deg2rad([0., -120., -120., -30., 90., 45.])
        axis, _, tip = self.node.kin.tube_axis_in_base(joints)
        solved = self.node.kin.ik_along_axis(tip, axis, joints)
        np.testing.assert_allclose(solved, joints, atol=1e-7)

    def test_model_controls_and_state_snapshot(self):
        self.node.set_model_flex_speed(20.)
        self.node.set_model_bend_limit(45)
        next_tick = self.node._last_model_update + self.node.dt
        with patch("flex_endo_robo.control_node.time.monotonic", return_value=next_tick):
            self.assertTrue(self.node.set_key("flex_s", True))
            self.node._tick()
        state = self.node.get_state_dict()
        self.assertGreater(state["flex_theta_total_deg"], 0.)
        self.assertLessEqual(state["flex_theta_total_deg"], 45.)
        self.assertEqual(state["flex_model_speed_deg_s"], 20.)
        self.assertEqual(len(state["tendons"]), 4)
        with self.assertRaises(ValueError):
            self.node.set_model_flex_speed(10.)

    def test_expired_keys_and_cancel_clear_motion_intent(self):
        self.node.set_key("flex_s", True)
        self.node._key_updated["flex_s"] = time.monotonic() - KEY_TIMEOUT_S - 1.
        self.node._tick()
        self.assertFalse(self.node._key_state["flex_s"])
        self.node._pull_out_state = {"stage": "align"}
        self.node._flex_slew_target = {"curvature": 1.}
        self.node._teleop_servo_active = True
        self.node._q_cmd = np.ones(6)
        self.node.cancel_motion()
        self.assertIsNone(self.node._pull_out_state)
        self.assertIsNone(self.node._flex_slew_target)
        self.assertIsNone(self.node._q_cmd)
        self.assertFalse(self.node._teleop_servo_active)
        self.assertFalse(any(self.node._key_state.values()))

    def test_cancel_delivers_physical_flex_stop(self):
        flex = Mock()
        flex.stop.return_value = True
        self.node.flex = flex
        self.node.cancel_motion()
        flex.stop.assert_called_once_with()
        self.assertTrue(self.node.flex_stop_delivered)

    def test_camera_tracking_uses_current_observation(self):
        observation = SimpleNamespace(revision=7, category=39, captured_at=time.monotonic())
        camera = SimpleNamespace(observation=lambda: observation)
        status = SimpleNamespace(fault=0, positioning_ready=True, moving=False,
                                 action=ipc.IDLE, flags=ipc.TRACK_HORIZON_SUPPORTED)
        flex = FlexControl(SimpleNamespace(status=status, ready=True), camera)
        node = SimpleNamespace(_arm_ready=lambda: True, arm_mode="rcm", rcm_enabled=True,
                               rcm_world_point=np.zeros(3), rcm_ref_axis=np.array([0., 0., 1.]))
        self.assertTrue(flex.set_tracking(True, node))
        self.assertEqual(flex.tracking, (7, 39))
        self.assertEqual(flex.tracking_state, "Waiting for target")

    def test_model_limit_rejects_current_bend_outside_requested_range(self):
        self.node.flex_curvature = math.radians(60) / (self.node.flex_arc_len - 0.008)
        with self.assertRaises(ValueError):
            self.node.set_model_bend_limit(30)

    def test_ipc_layout_sizes_match_native_protocol(self):
        self.assertEqual(ipc.COMMAND.size, 96)
        self.assertEqual(ipc.STATUS.size, 152)
        self.assertEqual(ipc.VISION.size, 88)

    def test_profile_timeout_has_a_readable_driver_diagnostic(self):
        status = replace(MemoryFlexClient().status, fault=-112)
        self.assertIn("planned motion did not settle", status.fault_detail)
        self.assertIn("encoder did not follow", replace(status, fault=-113).fault_detail)

    def test_device_resets_complete_independently_in_either_order(self):
        for flex_first in (False, True):
            with self.subTest(flex_first=flex_first):
                client = MemoryFlexClient()
                flex = self.node.flex = FlexControl(client, SimpleNamespace(observation=lambda: None))
                self.node.joint_positions = np.deg2rad([10., -120., -120., -30., 90., 45.])
                self.node.joint_received = True
                self.node._last_joint_at = time.monotonic()
                control = Mock()
                relay = Relay(control, None, "unused", self.node.kin)
                relay.telemetry = (time.monotonic(), {"joints": self.node.joint_positions.tolist()}, [0.] * 6)

                def send(command):
                    relay.submit(*parse_command(json.dumps(command)))
                    return True

                with patch.object(self.node, "_send", side_effect=send):
                    if flex_first:
                        self.assertTrue(flex.return_to_zero())
                    self.assertTrue(self.node.cmd_reset())
                    relay._tick()
                    if not flex_first:
                        self.assertTrue(flex.return_to_zero())
                    self.node._tick()
                    self.assertEqual(relay.mode, "move")
                    self.assertIsNotNone(flex.pending)
                    control.stopJ.assert_not_called()
                    client.status = replace(client.status, action=ipc.IDLE, result=ipc.DONE,
                                            flags=ipc.CALIBRATED)
                    self.node._tick()
                    self.assertIsNone(flex.pending)
                    self.assertEqual(relay.mode, "move")
                    control.stopJ.assert_not_called()

    def test_flex_return_to_zero_does_not_suppress_held_arm_controls(self):
        client = MemoryFlexClient()
        flex = self.node.flex = FlexControl(client, SimpleNamespace(observation=lambda: None))
        self.assertTrue(flex.return_to_zero())
        self.node._key_state["arm_up"] = True
        with patch.object(self.node, "_send_arm_jog") as jog:
            self.node._tick()
        jog.assert_called_once()

    def test_arm_reset_still_cancels_a_coordinated_procedure(self):
        client = MemoryFlexClient()
        flex = self.node.flex = FlexControl(client, None)
        flex.procedure = {"stage": "prepare"}
        with patch.object(self.node, "_arm_ready", return_value=True), \
                patch.object(self.node, "_send", return_value=True):
            self.assertTrue(self.node.cmd_reset())
        self.assertIsNone(flex.procedure)
        self.assertEqual(client.status.action, ipc.STOP)


if __name__ == "__main__":
    unittest.main()
