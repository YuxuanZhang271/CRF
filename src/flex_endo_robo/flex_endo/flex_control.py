"""Coordinate Flex motion with current camera observations and UR5 feedback."""

import math
import time

import numpy as np

from . import flex_ipc as ipc


FLEX_KEYS = ("flex_left", "flex_right", "flex_up", "flex_down", "flex_ccw", "flex_cw")
POSITION_TIMEOUT_S = 30.0
KEY_TIMEOUT_S = 0.65
STRAIGHT_BEND_DEG = 0.25
TRACK_BEND_MARGIN_DEG = 0.5
TRACK_ARM_SPEED_RAD_S = math.radians(2)
PIVOT_TOLERANCE_M = 0.002


class FlexControl:
    def __init__(self, client, camera, pull_out_clearance=None):
        self.client = client
        self.camera = camera
        self.pull_out_clearance = pull_out_clearance
        self.keys = {}
        self.pending = None
        self.tracking = None
        self.tracking_state = "Off"
        self.procedure = None
        self.message = ""
        self._driver = None
        self._owned = False
        self.stop_request = None
        self._tracking_limit = None
        self._tracking_sequence = 0

    @property
    def status(self):
        return self.client.status

    def ready(self, *, calibrated=True, allow_setup=False):
        s = self.status
        return (self.client.ready and s is not None and not s.fault
                and (s.positioning_ready or not calibrated) and (allow_setup or not s.setup))

    def jog_ready(self):
        s = self.status
        return self.ready(calibrated=not (s and s.planned_feedback))

    def set_key(self, key, down):
        if not down:
            self.keys.pop(key, None)
            if not self.keys and self._owned and self.pending is None and not self.tracking:
                self.client.command(ipc.IDLE)
                self.client.send()
                self._owned = False
            return True
        if not self.jog_ready() or not self.stop_acknowledged():
            return False
        self.cancel_tracking()
        if self.procedure or self.pending:
            self.cancel("Flex positioning canceled by manual input")
            return False
        self.keys[key] = time.monotonic()
        return True

    def heartbeat(self, held):
        now = time.monotonic()
        for key in self.keys.keys() & set(held):
            if now - self.keys[key] <= KEY_TIMEOUT_S:
                self.keys[key] = now

    def _request(self, action, **parameters):
        request = self.client.request(action, **parameters)
        self.pending = (request, action, time.monotonic() + POSITION_TIMEOUT_S)
        self._owned = True
        self.message = ""
        if self.client.send():
            return True
        self.pending = None
        self.message = "Flex driver connection lost"
        return False

    def stop(self):
        self.tracking = None
        self.tracking_state = "Off"
        self._tracking_limit = None
        self.keys.clear()
        self.pending = self.procedure = None
        self._owned = False
        self.stop_request = self.client.request(ipc.STOP)
        return self.client.send()

    def stop_acknowledged(self):
        s = self.status
        return (self.stop_request is None or
                (s is not None and s.app == self.client.app and s.request == self.stop_request
                 and s.result == ipc.DONE and not s.moving))

    def cancel(self, message):
        self.stop()
        self.message = message

    def cancel_tracking(self, state="Off"):
        if self.tracking is None:
            return
        self.tracking = None
        self.tracking_state = state
        self._tracking_limit = None
        self.stop_request = self.client.request(ipc.STOP)
        self.client.send()
        self._owned = False

    def calibrate(self):
        if (not self.ready(calibrated=False, allow_setup=True) or not self.stop_acknowledged()
                or self.status.moving
                or self.status.action != ipc.IDLE or self.keys or self.pending
                or self.procedure or self.tracking):
            raise ValueError("Calibration requires stationary Flex hardware with all motion controls released.")
        if self.status.flags & ipc.SETUP_ENCODER_MISMATCH:
            raise ValueError("Setup travel and encoder feedback disagree. Check the encoder wiring, direction, and pulse scale.")
        return self._request(ipc.CALIBRATE)

    def return_to_zero(self):
        s = self.status
        if (not self.jog_ready() or not self.stop_acknowledged() or s.moving
                or s.action != ipc.IDLE or self.keys or self.pending or self.procedure or self.tracking):
            raise ValueError("Stop Flex motion and release the jog controls before returning to zero.")
        if not (s.positioning_ready or s.flags & ipc.PLANNED_ZERO):
            raise ValueError("Calibrate Flex first to record a zero position.")
        if not self._request(ipc.RETURN_ZERO):
            return False
        self.message = "Returning to planned Flex zero" if s.flags & ipc.PLANNED_ZERO else "Returning Flex to zero"
        return True

    def set_mode(self, mode):
        if mode not in ipc.MODES:
            raise ValueError("Choose fixed or free.")
        if (not self.ready() or not self.stop_acknowledged()
                or self.keys or self.pending or self.procedure):
            raise ValueError("Release Flex controls and wait for positioning to finish.")
        if self.tracking:
            self.cancel_tracking()
            raise ValueError("Tracking stopped. Select the mode once Flex is idle.")
        if self.status.moving or self.status.action != ipc.IDLE:
            raise ValueError("Wait for Flex to be stationary before changing mode.")
        return self._request(ipc.SET_MODE, mode=ipc.MODES.index(mode))

    def _rcm_ready(self, node):
        return (node._arm_ready() and node.arm_mode == "rcm" and node.rcm_enabled
                and node.rcm_world_point is not None and node.rcm_ref_axis is not None)

    def set_tracking(self, enabled, node):
        if not enabled:
            self.cancel_tracking()
            return True
        if self.tracking:
            return True
        if not self._rcm_ready(node):
            raise ValueError("Tracking requires an active UR5 RCM and fresh joint feedback.")
        if (not self.ready() or not self.stop_acknowledged() or self.status.moving
                or self.status.action != ipc.IDLE
                or self.keys or self.pending or self.procedure):
            raise ValueError("Tracking requires calibrated, idle Flex hardware.")
        obs = self.camera.observation()
        if obs is None or obs.category is None or not self._fresh(obs):
            raise ValueError("Select one YOLO category and wait for a fresh camera frame.")
        if not self._valid_arm_pose(node, node.joint_positions.copy()):
            raise ValueError("UR5 is outside the captured RCM envelope.")
        with node._lock:
            node._clear_track_state_unlocked()
        self.tracking = (obs.revision, obs.category)
        self.tracking_state = "Waiting for target"
        self.message = ""
        return True

    @staticmethod
    def _fresh(observation):
        return (observation is not None
                and 0 <= time.monotonic() - observation.captured_at <= ipc.VISION_TIMEOUT_S)

    def _step_tracking(self, node, obs):
        if not self._rcm_ready(node) or not self.ready():
            self.cancel_tracking("Tracking stopped · RCM or Flex feedback lost")
            return
        q_now = node.joint_positions.copy()
        if not self._valid_arm_pose(node, q_now):
            self.cancel_tracking("Tracking stopped · UR5 outside the RCM envelope")
            return
        if obs is None or (obs.revision, obs.category) != self.tracking or not self._fresh(obs):
            self.cancel_tracking("Tracking stopped · camera stream or category changed")
            return
        if not obs.valid or obs.centered:
            self.tracking_state = "Centered" if obs.valid else "Waiting for target"
            self._tracking_limit = None
            self.client.command(ipc.IDLE)
            self._owned = False
            return

        # The prototype maps image right/down to the two tendon axes. Both
        # the camera and actuator frame rotate with axis 5.
        error = np.array((obs.dx, obs.dy), dtype=float)
        norm = float(np.linalg.norm(error))
        if not math.isfinite(norm) or not norm:
            self.cancel_tracking("Tracking stopped · invalid target coordinates")
            return
        direction = error / norm
        s = self.status
        bend = np.array((s.bend_x, s.bend_y))
        # Camera frames can advance faster than the driver acknowledges them.
        if self._owned and s.result == ipc.LIMIT and s.sequence >= self._tracking_sequence:
            self._tracking_limit = direction
        blocked = self._tracking_limit is not None and np.dot(direction, self._tracking_limit) > 0.95
        if not blocked:
            self._tracking_limit = None
        next_bend = np.linalg.norm(bend + TRACK_BEND_MARGIN_DEG * direction)
        if not blocked and (next_bend < s.bend_limit or next_bend < np.linalg.norm(bend)):
            gain = min(1., max(0.15, norm / (0.12 * min(obs.width, obs.height))))
            x, y = gain * direction
            self.client.command(ipc.TRACK, frame=obs.frame, x=x, y=y,
                                roll=self._horizon_correction(node, x, y))
            if not self._owned:
                self._tracking_sequence = self.client.sequence
            self._owned = True
            self.tracking_state = "Following with Flex"
            return

        self.client.command(ipc.IDLE)
        self._owned = False
        if s.moving or s.action == ipc.TRACK:
            self.tracking_state = "Holding Flex before arm assist"
            return
        axis, _, tip = node.kin.tube_axis_in_base(q_now)
        pivot = node.rcm_world_point
        depth = float(np.dot(tip - pivot, axis))
        if depth <= 0:
            self.cancel_tracking("Tracking stopped · UR5 outside the RCM envelope")
            return
        camera_rotation = node.kin.physical_flex_camera_rotation(q_now, s.bend_x, s.bend_y, s.roll)
        right = -camera_rotation[:, 1]
        down = camera_rotation[:, 0]
        tangent = direction[0] * right + direction[1] * down
        tangent -= np.dot(tangent, axis) * axis
        length = float(np.linalg.norm(tangent))
        if length < 1e-6:
            self.cancel_tracking("Tracking limited · target cannot be centered")
            return
        angle = TRACK_ARM_SPEED_RAD_S * node.dt
        axis_target = math.cos(angle) * axis + math.sin(angle) * tangent / length
        if not node._inside_rcm_cone(axis_target, node.rcm_ref_axis):
            self.cancel_tracking("Tracking limited · target cannot be centered")
            return
        q_target = node.kin.ik_along_axis(pivot + depth * axis_target, axis_target, q_now)
        if q_target is None or not node._send_servo(
                q_target, q_now, node.dt, validate=lambda q: node._rcm_pose_is_valid(q, depth)):
            self.cancel_tracking("Tracking stopped · RCM correction unavailable")
            return
        self.tracking_state = "Following with arm assist"

    def _valid_arm_pose(self, node, q):
        return node._rcm_pose_is_valid(q)

    def _horizon_correction(self, node, x, y):
        s = self.status
        if s.mode != ipc.FIXED or not (x or y) or not node._arm_ready():
            return 0.
        q = node.joint_positions.copy()
        x_next, y_next = s.bend_x + x * 0.12, s.bend_y + y * 0.12
        radius = math.hypot(x_next, y_next)
        if radius < STRAIGHT_BEND_DEG:
            return 0.
        neutral, _ = node.kin.solve_horizon_flex_yaw(q, flex_mode="free")
        plane = math.radians(s.roll) + math.atan2(-x_next, y_next)
        target, _ = node.kin.solve_horizon_flex_yaw(
            q, steer_yaw=plane - neutral, theta_total=math.radians(radius), flex_mode="fixed")
        target = math.degrees(target)
        delta = (target - s.roll + 180.) % 360. - 180.
        limit = self._roll_limit()
        bounded = min(limit - s.roll, max(-limit - s.roll, delta))
        return float(np.clip(bounded / 0.40, -1., 1.))

    def _roll_limit(self):
        return 20. if self.status.planned_feedback else 180.

    def _position(self, x, y, roll):
        s = self.status
        if (not self.ready() or not self.stop_acknowledged()
                or self.pending or self.keys or self.procedure):
            raise ValueError("Wait for calibrated Flex hardware and release the jog controls.")
        if self.tracking:
            self.cancel_tracking()
            raise ValueError("Tracking stopped. Try the positioning command once Flex is idle.")
        if s.moving or s.action != ipc.IDLE:
            raise ValueError("Wait for Flex to stop before positioning.")
        if (not all(map(math.isfinite, (x, y, roll))) or abs(roll) > self._roll_limit()
                or math.hypot(x, y) > s.bend_limit + 1e-6):
            raise ValueError("Requested Flex position is outside the hardware limits.")
        return self._request(ipc.POSITION, x=x, y=y, roll=roll)

    def bird_eye(self, node):
        if not node._arm_ready() or not self.ready():
            raise ValueError("Bird-eye requires a fresh UR5 pose and calibrated Flex hardware.")
        if any(node._key_state.values()):
            raise ValueError("Release arm controls before starting Bird-eye.")
        q = node.joint_positions.copy()
        r, _ = node.kin.flex_base_frame_in_base(q)
        pitch = math.asin(float(np.clip(-r[2, 2], -1., 1.)))
        required = math.radians(30) - pitch
        if required < -1e-6 or required > math.radians(self.status.bend_limit) + 1e-6:
            raise ValueError("The 30° downward view is outside the available bend range.")
        bend = max(0., math.degrees(required))
        base, distal = node.kin.solve_horizon_flex_yaw(
            q, steer_yaw=0., theta_total=max(0., required),
            flex_mode=ipc.MODES[self.status.mode])
        # The physical bending-plane yaw is base plus distal compensation.
        # Tendon x points image-right and tendon y points image-down.
        tendon_angle = distal
        roll = math.degrees(base)
        if abs(roll) > self._roll_limit():
            raise ValueError("Bird-eye needs more base roll than is available at this UR5 pose.")
        if not self._position(-bend * math.sin(tendon_angle), bend * math.cos(tendon_angle), roll):
            return False
        self.message = "Bird-eye · moving Flex"
        return True

    def pull_out(self, node):
        if self.pull_out_clearance is None:
            raise ValueError("Configure the bench-verified pull-out clearance before enabling Pull Out.")
        if not self._rcm_ready(node) or not self.ready():
            raise ValueError("Pull Out requires active RCM and calibrated Flex feedback.")
        if any(node._key_state.values()):
            raise ValueError("Release arm controls before starting Pull Out.")
        q = node.joint_positions.copy()
        axis, _, tip = node.kin.tube_axis_in_base(q)
        if not self._valid_arm_pose(node, q):
            raise ValueError("UR5 pose is outside the captured RCM envelope.")
        end = node.kin.straight_camera_position_in_base(q)
        axial_offset = float(np.dot(tip - end, axis))
        if np.linalg.norm((tip - end) - axial_offset * axis) > PIVOT_TOLERANCE_M:
            raise ValueError("The modeled straight distal tip is outside the tube axis.")
        depth = float(np.dot(tip - node.rcm_world_point, axis))
        rotation, _ = node.kin.flex_base_frame_in_base(q)
        alignment = float(np.dot(rotation[:, 2], axis))
        if abs(alignment) < 0.98:
            raise ValueError("Pull Out requires aligned camera and shaft axes.")
        if not self.return_to_zero():
            return False
        sign = -math.copysign(1., alignment)
        self.procedure = {
            "stage": "straighten", "depth": depth, "offset": axial_offset,
            "end": sign * self.pull_out_clearance, "sign": sign,
            "deadline": time.monotonic() + POSITION_TIMEOUT_S,
        }
        self.message = "Pull Out · straightening Flex"
        return True

    def confirm_pull_out(self, node):
        if (not self.procedure or self.procedure["stage"] != "confirm"
                or not self._rcm_ready(node) or not self.ready() or not self._straight()):
            raise ValueError("Start Pull Out and wait for Flex to reach its saved zero before confirming.")
        return self._begin_pull_out_alignment()

    def _begin_pull_out_alignment(self):
        self.procedure["stage"] = "align"
        self.procedure["deadline"] = time.monotonic() + POSITION_TIMEOUT_S
        self.message = "Pull Out · aligning to RCM"
        return True

    def _straight(self):
        return (not self.pending and not self.status.moving and self.status.action == ipc.IDLE
                and math.hypot(self.status.bend_x, self.status.bend_y) <= STRAIGHT_BEND_DEG)

    def _step_pull_out(self, node):
        st = self.procedure
        if not self._rcm_ready(node) or not self.ready() or time.monotonic() >= st["deadline"]:
            self.cancel("Pull Out stopped · feedback unavailable or timed out")
            return
        q = node.joint_positions.copy()
        axis, _, tip = node.kin.tube_axis_in_base(q)
        if not self._valid_arm_pose(node, q):
            self.cancel("Pull Out stopped · UR5 outside the RCM envelope")
            return
        ref, pivot = node.rcm_ref_axis, node.rcm_world_point
        actual_error = math.acos(float(np.clip(np.dot(axis, ref), -1., 1.)))
        straight = self._straight()
        if st["stage"] == "straighten":
            if straight and self.status.planned_feedback:
                st["stage"] = "confirm"
                st["deadline"] = time.monotonic() + POSITION_TIMEOUT_S
                self.message = "Pull Out · verify the scope is physically straight"
            elif straight:
                self._begin_pull_out_alignment()
            return
        if st["stage"] == "confirm":
            if not straight:
                self.cancel("Pull Out stopped · Flex moved before confirmation")
            return
        if not straight:
            self.cancel("Pull Out stopped · straightness lost")
            return
        if st["stage"] == "align":
            if actual_error < math.radians(0.35):
                st["stage"] = "retract"
                self.message = "Pull Out · retracting"
                return
            cross = np.cross(axis, ref)
            axis_target = node.kin.rotate_about(axis, cross / np.linalg.norm(cross),
                                                min(actual_error, TRACK_ARM_SPEED_RAD_S * node.dt))
            depth = st["depth"]
        else:
            if actual_error > math.radians(1):
                self.cancel("Pull Out stopped · RCM alignment lost")
                return
            axis_target = ref
            end = node.kin.straight_camera_position_in_base(q)
            current = float(np.dot(end - pivot, ref))
            remaining = st["sign"] * (st["end"] - current)
            if remaining <= 0.001:
                self.procedure = None
                self.message = "Pull Out complete"
                return
            step = st["sign"] * min(remaining, node.ARM_AXIS_VEL * node.dt)
            depth = current + step + st["offset"]
        p_target = pivot + depth * axis_target
        q_target = node.kin.ik_along_axis(p_target, axis_target, q)
        if q_target is None or not node._send_servo(
                q_target, q, node.dt, validate=lambda target: node._rcm_pose_is_valid(target, depth)):
            self.cancel("Pull Out stopped · RCM correction unavailable")

    def tick(self, node):
        old, owned = self._driver, self._owned
        s = self.client.poll()
        self._driver = s.driver if s else None
        exclusive = bool(self.tracking or self.procedure or
                         (self.pending and self.pending[1] in (ipc.POSITION, ipc.RETURN_ZERO)))
        if old != self._driver or (s is not None and not s.fault and not self.client.ready):
            if old is not None or owned or self.tracking or self.procedure:
                self.cancel("Flex motion blocked by GTS fault" if s and s.fault
                            else "Flex driver disconnected or restarted")
            if s is None or not self.client.ready:
                self.stop_request = None
        elif s is not None and s.fault and self.stop_request is not None:
            self.stop_request = None
        if s is None:
            return exclusive
        if self.client.ready and self.message == "Flex driver disconnected or restarted":
            self.message = ""
        if self.stop_request is not None and self.stop_acknowledged():
            self.stop_request = None
            self.client.command(ipc.IDLE)
        if s.fault or (s.setup and (self.tracking or self.procedure or self.keys)):
            if self.tracking or self.procedure or self.keys or self.pending:
                self.cancel("Flex driver fault or local Xbox ownership")
        now = time.monotonic()
        self.keys = {k: t for k, t in self.keys.items() if now - t <= KEY_TIMEOUT_S}
        if self.pending:
            request, action, deadline = self.pending
            result = s.result if s.app == self.client.app and s.request == request else ipc.NO_RESULT
            if now >= deadline:
                self.cancel("Flex command timed out")
            elif action == ipc.POSITION and not node._arm_ready():
                self.cancel("Flex positioning stopped · UR5 feedback unavailable")
            elif result in (ipc.DONE, ipc.REFUSED, ipc.CANCELED, ipc.LIMIT):
                self.pending = None
                self._owned = False
                self.client.command(ipc.IDLE)
                label = {ipc.POSITION: "Bird-eye", ipc.CALIBRATE: "Calibration",
                         ipc.RETURN_ZERO: "Return to Flex zero",
                         ipc.SET_MODE: "Mode change"}[action]
                if result != ipc.DONE:
                    self.cancel(f"{label} {ipc.RESULTS[result]}")
                elif not self.procedure:
                    if s.planned_feedback and action == ipc.RETURN_ZERO:
                        self.message = "Returned to planned Flex zero"
                    elif s.planned_feedback and action == ipc.POSITION:
                        self.message = "Bird-eye · planned position reached"
                    else:
                        self.message = f"{label} complete"
        obs = self.camera.observation()
        if self.tracking:
            self._step_tracking(node, obs)
        elif self.procedure:
            self._step_pull_out(node)
        elif self.keys and self.jog_ready() and not self.pending and self.stop_acknowledged():
            left, right, up, down, ccw, cw = (int(k in self.keys) for k in FLEX_KEYS)
            x, y, roll = right - left, down - up, cw - ccw
            if not roll and s.positioning_ready:
                roll = self._horizon_correction(node, x, y)
            self.client.command(ipc.JOG, x=x, y=y, roll=roll)
            self._owned = True
        elif self._owned and not self.pending:
            self.client.command(ipc.IDLE)
            self._owned = False
        if self.keys and s.result == ipc.LIMIT and s.sequence == self.client.sequence:
            self.keys.clear()
            self.client.command(ipc.IDLE)
            self._owned = False
            self.message = "Flex limit reached · release jog controls"
        if not self.client.send(obs) and exclusive:
            self.cancel("Flex shared memory connection lost")
            if node._servo_sent_this_tick:
                node._send_servo_stop()
        return exclusive

    def snapshot(self):
        s = self.status
        manual_bench = bool(s and s.flags & ipc.MANUAL_BENCH)
        planned_zero = bool(s and s.flags & ipc.PLANNED_ZERO)
        has_pose = s and (s.calibrated or s.setup or s.planned_feedback)
        return {
            "connected": self.client.ready,
            "driver_online": s is not None,
            "calibrated": bool(s and s.calibrated),
            "positioning_ready": bool(s and s.positioning_ready),
            "setup": bool(s and s.setup),
            "controller": bool(s and s.flags & ipc.XBOX),
            "simulated": bool(s and s.flags & ipc.SIMULATED),
            "manual_bench": manual_bench,
            "planned_feedback": bool(s and s.planned_feedback),
            "planned_zero": planned_zero,
            "busy": bool(self.pending or self.procedure or (s and s.moving)),
            "mode": ipc.MODES[s.mode] if s else None,
            "bend": math.hypot(s.bend_x, s.bend_y) if has_pose else None,
            "roll": s.roll if has_pose else None,
            "tendons": list(s.tendons) if s and (s.calibrated or planned_zero) else None,
            "bend_limit": s.bend_limit if s else None,
            "fault": s.fault if s else 0,
            "fault_detail": s.fault_detail if s else "",
            "setup_encoder_mismatch": bool(s and s.flags & ipc.SETUP_ENCODER_MISMATCH),
            "message": self.message,
            "tracking": self.tracking is not None,
            "tracking_state": self.tracking_state,
            "pull_out_enabled": self.pull_out_clearance is not None,
            "pull_out_confirm": bool(self.procedure and self.procedure["stage"] == "confirm"),
        }
