"""Coordinate Flex motion with current camera observations and UR5 feedback."""

import math
import time

import numpy as np

from . import flex_ipc as ipc


FLEX_KEYS = ("flex_left", "flex_right", "flex_up", "flex_down")
POSITION_TIMEOUT_S = 30.0
KEY_TIMEOUT_S = 0.65
STRAIGHT_BEND_DEG = 0.25
TRACK_ARM_SPEED_RAD_S = math.radians(3)
PULL_OUT_ALIGN_SPEED_RAD_S = math.radians(2)
# Match the per-command increments in the native Flex driver.
JOG_SPEED_MULTIPLIER = 1.5
TRACK_SPEED_MULTIPLIER = 3.0
HORIZON_ROLL_STEP_DEG = 0.80
JOG_BEND_STEP_DEG = 0.24 * JOG_SPEED_MULTIPLIER
JOG_ROLL_STEP_DEG = HORIZON_ROLL_STEP_DEG * JOG_SPEED_MULTIPLIER
TRACK_BEND_STEP_DEG = 0.32 * TRACK_SPEED_MULTIPLIER
TRACK_ROLL_STEP_DEG = 1.0 * TRACK_SPEED_MULTIPLIER
HORIZON_DEADBAND_DEG = 0.15
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
        self._startup_recheck = None
        self._tracking_limit = None
        self._tracking_command = None
        self._horizon_paused = False
        self._zero_hold_joints = None

    @property
    def status(self):
        return self.client.status

    def ready(self, *, calibrated=True):
        s = self.status
        return (self.client.ready and s is not None and not s.fault
                and (s.positioning_ready or not calibrated))

    def jog_ready(self):
        return (self.ready(calibrated=False)
                and not (self.status.flags & ipc.SETUP_ENCODER_MISMATCH))

    def connection_issue(self):
        s = self.status
        if s is None:
            return "Flex driver offline. Check the launcher console."
        if s.fault:
            detail = s.fault_detail or f"GTS fault {s.fault}"
            return f"Flex blocked by {detail}. See the Flex state panel."
        if not self.client.ready:
            return "Waiting for the Flex driver to acknowledge the session."
        return None

    def recheck_startup_inputs(self):
        s = self.status
        if self._startup_recheck is not None:
            raise ValueError("A Flex input recheck is already pending.")
        if s is None or not (s.flags & ipc.STARTUP_RECHECK):
            raise ValueError("A Flex startup input recheck is unavailable. See the driver console.")
        request = self.client.request(ipc.RECHECK_STARTUP)
        if not self.client.send():
            raise ValueError("Could not send the Flex input recheck. Check the driver connection.")
        self._startup_recheck = (s.driver, request, time.monotonic() + 2.0)
        self.message = "Rechecking Flex startup inputs…"
        return True

    def _available_for_request(self):
        s = self.status
        return s.action == ipc.HORIZON or (s.action == ipc.IDLE and not s.moving)

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
        self.resume_horizon()
        return True

    def resume_horizon(self):
        self._horizon_paused = False
        self._zero_hold_joints = None

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
        self._horizon_paused = True
        self._zero_hold_joints = None
        self.tracking = None
        self.tracking_state = "Off"
        self._tracking_limit = None
        self._tracking_command = None
        self.keys.clear()
        self.pending = self.procedure = None
        self._startup_recheck = None
        self._owned = False
        if not self.client.ready:
            self.stop_request = None
            self.client.command(ipc.IDLE)
            return False
        self.stop_request = self.client.request(ipc.STOP)
        return self.client.send()

    def stop_acknowledged(self, request=None):
        request = self.stop_request if request is None else request
        s = self.status
        return (request is None or
                (s is not None and s.app == self.client.app and s.request == request
                 and s.result == ipc.DONE and not s.fault and not s.moving))

    def cancel(self, message):
        self.stop()
        self.message = message

    def cancel_tracking(self, state="Off"):
        if self.tracking is None:
            return
        self.tracking = None
        self.tracking_state = state
        self._tracking_limit = None
        self._tracking_command = None
        self.stop_request = self.client.request(ipc.STOP)
        self.client.send()
        self._owned = False

    def calibrate(self):
        if (not self.ready(calibrated=False) or not self.stop_acknowledged()
                or not self._available_for_request() or self.keys or self.pending
                or self.procedure or self.tracking):
            raise ValueError("Calibration requires stationary Flex hardware with all motion controls released.")
        if self.status.flags & ipc.SETUP_ENCODER_MISMATCH:
            raise ValueError("Setup travel and encoder feedback disagree. Check the encoder wiring, direction, and pulse scale.")
        self.resume_horizon()
        return self._request(ipc.CALIBRATE)

    def return_to_zero(self):
        s = self.status
        if (not self.jog_ready() or not self.stop_acknowledged() or not self._available_for_request()
                or self.keys or self.pending or self.procedure or self.tracking):
            raise ValueError("Stop Flex motion and release the jog controls before returning to zero.")
        if not (s.positioning_ready or s.flags & ipc.PLANNED_ZERO):
            raise ValueError("Calibrate Flex first to record a zero position.")
        if not self._request(ipc.RETURN_ZERO):
            return False
        self._horizon_paused = True
        self._zero_hold_joints = None
        self.message = "Returning to planned Flex zero" if s.flags & ipc.PLANNED_ZERO else "Returning Flex to zero"
        return True

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
        if (not self.ready() or not self.stop_acknowledged() or not self._available_for_request()
                or self.keys or self.pending or self.procedure):
            raise ValueError("Tracking requires calibrated, idle Flex hardware.")
        obs = self.camera.observation()
        if obs is None or obs.category is None or not self._fresh(obs):
            raise ValueError("Select one YOLO category and wait for a fresh camera frame.")
        with node._lock:
            node._clear_track_state_unlocked()
        self.tracking = (obs.revision, obs.category)
        self.resume_horizon()
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
        if obs is None or (obs.revision, obs.category) != self.tracking or not self._fresh(obs):
            self.cancel_tracking("Tracking stopped · camera stream or category changed")
            return
        q_now = node.joint_positions.copy()
        axis_now, _, tip_now = node.kin.tube_axis_in_base(q_now)
        ref = node.rcm_ref_axis
        pivot = node.rcm_world_point
        depth = float(np.dot(tip_now - pivot, axis_now))
        outside_cone = not node._inside_rcm_cone(axis_now, ref)
        if depth <= 0 or not node._rcm_pivot_is_valid(axis_now, tip_now):
            self._wait_at_tracking_limit("UR5 is off the captured RCM pivot")
            return
        if not obs.valid or obs.centered:
            self._tracking_limit = None
            self._tracking_command = None
            if outside_cone:
                self._wait_at_tracking_limit("UR5 outside the RCM cone")
                return
            self.tracking_state = "Centered" if obs.valid else "Waiting for target"
            if obs.valid:
                self._hold_horizon(node)
            else:
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
        gain = min(1., max(0.15, norm / (0.12 * min(obs.width, obs.height))))
        step = gain * direction
        # Remember the last rejected Flex correction. A changed direction,
        # smaller step, or changed Flex pose can fit within the travel limits.
        if (self._tracking_command is not None and s.result == ipc.LIMIT
                and s.sequence == self._tracking_command[0]):
            self._tracking_limit = self._tracking_command[1:]
        blocked = False
        if self._tracking_limit is not None:
            limited_bend, limited_roll, limited_step = self._tracking_limit
            blocked = (np.linalg.norm(bend - limited_bend) < 0.02
                       and abs(s.roll - limited_roll) < 0.1
                       and np.linalg.norm(step - limited_step) < 0.02)
            if not blocked:
                self._tracking_limit = None
        current_bend = np.linalg.norm(bend)
        next_bend = np.linalg.norm(bend + TRACK_BEND_STEP_DEG * step)
        if not outside_cone and not blocked and (next_bend <= s.bend_limit or next_bend < current_bend):
            x, y = step
            self.client.command(ipc.TRACK, frame=obs.frame, x=x, y=y,
                                roll=self._horizon_correction(
                                    node, x, y, TRACK_BEND_STEP_DEG, TRACK_ROLL_STEP_DEG))
            if self._tracking_command is None or self.client.sequence != self._tracking_command[0]:
                self._tracking_command = (self.client.sequence, bend, s.roll, step)
            self._owned = True
            self.tracking_state = "Following with Flex"
            return

        self._tracking_command = None
        if s.action == ipc.TRACK or (s.moving and (outside_cone or s.action != ipc.HORIZON)):
            self.client.command(ipc.IDLE)
            self._owned = False
            self.tracking_state = "Holding Flex before arm assist"
            return
        # Advance from the last nearby servo target so RTDE lag does not reduce
        # each correction to a single step from the measured pose.
        q_seed = q_now
        axis = axis_now
        q_cmd = node._q_cmd
        if q_cmd is not None and np.max(np.abs(q_cmd - q_now)) <= node.TELEOP_RESYNC_RAD:
            cmd_axis, _, cmd_tip = node.kin.tube_axis_in_base(q_cmd)
            usable_direction = (np.dot(cmd_axis, ref) >= np.dot(axis_now, ref) if outside_cone
                                else node._inside_rcm_cone(cmd_axis, ref))
            if usable_direction and node._rcm_pivot_is_valid(cmd_axis, cmd_tip, depth):
                q_seed, axis = q_cmd, cmd_axis
        camera_rotation = node.kin.physical_flex_camera_rotation(q_seed, s.bend_x, s.bend_y, s.roll)
        right = -camera_rotation[:, 1]
        down = camera_rotation[:, 0]
        image_tangent = direction[0] * right + direction[1] * down
        # Rotate the camera toward the detection. Only the component of that
        # rotation perpendicular to the shaft can change its RCM tilt.
        tangent = np.cross(np.cross(camera_rotation[:, 2], image_tangent), axis)
        length = float(np.linalg.norm(tangent))
        if length < 1e-6:
            self._wait_at_tracking_limit("no available RCM tilt toward the target")
            return
        angle = TRACK_ARM_SPEED_RAD_S * node.dt
        axis_target = math.cos(angle) * axis + math.sin(angle) * tangent / length
        seed_inside = node._inside_rcm_cone(axis, ref)
        if not seed_inside:
            if np.dot(axis_target - axis, ref) <= 1e-8:
                self._wait_at_tracking_limit("UR5 outside the RCM cone")
                return
        elif not node._inside_rcm_cone(axis_target, ref):
            radial = axis_target - np.dot(axis_target, ref) * ref
            cone = node.RCM_CONE_HALF_ANGLE
            axis_target = math.cos(cone) * ref + math.sin(cone) * radial / np.linalg.norm(radial)
            if np.dot(axis_target - axis, tangent) <= 1e-8:
                self._wait_at_tracking_limit("UR5 RCM cone reached")
                return
        q_target = node.kin.ik_along_axis(pivot + depth * axis_target, axis_target, q_seed)
        def validate_start(q):
            start_axis, _, start_tip = node.kin.tube_axis_in_base(q)
            return node._rcm_pivot_is_valid(start_axis, start_tip, depth)

        def validate_target(q):
            target_axis, _, target_tip = node.kin.tube_axis_in_base(q)
            allowed = (node._inside_rcm_cone(target_axis, ref) if seed_inside
                       else np.dot(target_axis - axis, ref) > 1e-8)
            return allowed and node._rcm_pivot_is_valid(target_axis, target_tip, depth)

        if q_target is None or not node._send_servo(
                q_target, q_now, node.dt, validate=validate_target, validate_start=validate_start):
            self._wait_at_tracking_limit("no feasible RCM correction")
            return
        if outside_cone:
            self.client.command(ipc.IDLE)
            self._owned = False
            self.tracking_state = "Returning UR5 to the RCM cone"
        else:
            self.tracking_state = "Following with arm assist"
            self._hold_horizon(node)

    def _wait_at_tracking_limit(self, reason):
        self._tracking_command = None
        self.client.command(ipc.IDLE)
        self._owned = False
        self.tracking_state = f"Tracking limited · {reason}. Waiting for target to move."

    def _valid_arm_pose(self, node, q):
        return node._rcm_pose_is_valid(q)

    def _horizon_correction(self, node, x, y, bend_step, roll_step):
        s = self.status
        if self._horizon_paused or not node._arm_ready():
            return 0.
        q = node.joint_positions.copy()
        scale = max(1., math.hypot(x, y))
        x_next, y_next = s.bend_x + x * bend_step / scale, s.bend_y + y * bend_step / scale
        radius = math.hypot(x_next, y_next)
        neutral, _ = node.kin.solve_horizon_flex_yaw(q)
        if radius < STRAIGHT_BEND_DEG:
            target = neutral
        else:
            plane = math.radians(s.roll) + math.atan2(-x_next, y_next)
            target, _ = node.kin.solve_horizon_flex_yaw(
                q, steer_yaw=plane - neutral, theta_total=math.radians(radius))
        target = math.degrees(target)
        delta = (target - s.roll + 180.) % 360. - 180.
        limit = self._roll_limit()
        bounded = min(limit - s.roll, max(-limit - s.roll, delta))
        if abs(bounded) <= HORIZON_DEADBAND_DEG:
            return 0.
        return float(np.clip(bounded / roll_step, -1., 1.))

    def _hold_horizon(self, node):
        s = self.status
        roll = self._horizon_correction(node, 0., 0., 0., HORIZON_ROLL_STEP_DEG)
        if roll:
            self.client.command(ipc.HORIZON, roll=roll)
            self._owned = s.result != ipc.LIMIT or s.sequence != self.client.sequence
        else:
            self.client.command(ipc.IDLE)
            self._owned = False

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
        if not self._available_for_request():
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
        base, distal = node.kin.solve_horizon_flex_yaw(q, theta_total=max(0., required))
        # The physical bending-plane yaw is base plus distal compensation.
        # Tendon x points image-right and tendon y points image-down.
        tendon_angle = distal
        roll = math.degrees(base)
        if abs(roll) > self._roll_limit():
            raise ValueError("Bird-eye needs more base roll than is available at this UR5 pose.")
        if not self._position(-bend * math.sin(tendon_angle), bend * math.cos(tendon_angle), roll):
            return False
        self.resume_horizon()
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
                                                min(actual_error, PULL_OUT_ALIGN_SPEED_RAD_S * node.dt))
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
        old = self._driver
        s = self.client.poll()
        self._driver = s.driver if s is not None and self.client.ready else None
        exclusive = bool(self.tracking or self.procedure or
                         (self.pending and self.pending[1] in (ipc.POSITION, ipc.RETURN_ZERO)))
        if old is not None and old != self._driver:
            self.cancel("Flex motion blocked by GTS fault" if s and s.fault
                        else "Flex driver disconnected or restarted")
        if self._startup_recheck is not None:
            driver, request, deadline = self._startup_recheck
            if s is None or s.driver != driver:
                self.message = "Flex driver connection lost during the input recheck."
            elif s.request == request and s.result in (ipc.DONE, ipc.REFUSED):
                self.message = ("GTS startup inputs are clear. Waiting for the drives to settle."
                                if s.result == ipc.DONE else
                                "GTS still reports a fault. Check Flex drive power and the alarm and limit inputs.")
            elif time.monotonic() >= deadline:
                self.message = "Flex input recheck timed out. Check the driver console and restart the session."
            else:
                self.client.send()
                return exclusive
            self._startup_recheck = None
        if self._driver is None:
            if s is not None:
                # The driver waits for neutral intent and drive settling before it
                # acknowledges this app. Keep the heartbeat neutral until then.
                self.client.command(ipc.IDLE)
                self.client.send()
            return exclusive
        if self.message == "Flex driver disconnected or restarted":
            self.message = ""
        elif self.message == "GTS startup inputs are clear. Waiting for the drives to settle." and not s.fault:
            self.message = "GTS startup inputs are clear."
        if s.fault:
            self.stop_request = None
            if self.tracking or self.procedure or self.keys or self.pending:
                self.cancel("Flex driver fault")
        if self.stop_request is not None and self.stop_acknowledged():
            self.stop_request = None
            self.client.command(ipc.IDLE)
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
                         ipc.RETURN_ZERO: "Return to Flex zero"}[action]
                if result != ipc.DONE:
                    self.cancel(f"{label} {ipc.RESULTS[result]}")
                elif not self.procedure:
                    if action == ipc.RETURN_ZERO and node._arm_ready():
                        self._zero_hold_joints = node.joint_positions.copy()
                    if s.planned_feedback and action == ipc.RETURN_ZERO:
                        self.message = "Returned to planned Flex zero"
                    elif s.planned_feedback and action == ipc.POSITION:
                        self.message = "Bird-eye · planned position reached"
                    else:
                        self.message = f"{label} complete"
        if (self._zero_hold_joints is not None and node._arm_ready()
                and np.max(np.abs(node.joint_positions - self._zero_hold_joints))
                > math.radians(HORIZON_DEADBAND_DEG)):
            self.resume_horizon()
        obs = self.camera.observation()
        if self.tracking:
            self._step_tracking(node, obs)
        elif self.procedure:
            self._step_pull_out(node)
        elif self.keys and self.jog_ready() and not self.pending and self.stop_acknowledged():
            left, right, up, down = (int(k in self.keys) for k in FLEX_KEYS)
            x, y = right - left, down - up
            roll = (self._horizon_correction(node, x, y, JOG_BEND_STEP_DEG, JOG_ROLL_STEP_DEG)
                    if s.positioning_ready else 0.)
            self.client.command(ipc.JOG, x=x, y=y, roll=roll)
            self._owned = True
        elif (not self.pending and self.stop_request is None and self.ready()
              and s.action in (ipc.IDLE, ipc.HORIZON) and not self._horizon_paused):
            self._hold_horizon(node)
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
        return {
            "connected": self.client.ready,
            "driver_online": s is not None,
            "calibrated": bool(s and s.calibrated),
            "positioning_ready": bool(s and s.positioning_ready),
            "simulated": bool(s and s.flags & ipc.SIMULATED),
            "manual_bench": manual_bench,
            "planned_feedback": bool(s and s.planned_feedback),
            "planned_zero": planned_zero,
            "busy": bool(self.keys or self.pending or self.procedure or
                         (s and s.action != ipc.HORIZON and (s.moving or s.action == ipc.JOG))),
            "bend": math.hypot(s.bend_x, s.bend_y) if s else None,
            "roll": s.roll if s else None,
            "tendons": list(s.tendons) if s and (s.calibrated or planned_zero) else None,
            "bend_limit": s.bend_limit if s else None,
            "fault": s.fault if s else 0,
            "fault_detail": s.fault_detail if s else "",
            "startup_recheck": bool(s and s.flags & ipc.STARTUP_RECHECK),
            "recheck_pending": self._startup_recheck is not None,
            "setup_encoder_mismatch": bool(s and s.flags & ipc.SETUP_ENCODER_MISMATCH),
            "message": self.message,
            "tracking": self.tracking is not None,
            "tracking_state": self.tracking_state,
            "pull_out_enabled": self.pull_out_clearance is not None,
            "pull_out_confirm": bool(self.procedure and self.procedure["stage"] == "confirm"),
        }
