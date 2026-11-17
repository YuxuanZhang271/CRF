"""Coordinate Flex motion with current camera observations and UR5 feedback."""

import math
import time

import numpy as np

from . import flex_ipc as ipc


BEND_KEYS = ("flex_left", "flex_right", "flex_up", "flex_down")
ROLL_KEYS = ("flex_roll_minus", "flex_roll_plus")
FLEX_KEYS = BEND_KEYS + ROLL_KEYS
POSITION_TIMEOUT_S = 30.0
KEY_TIMEOUT_S = 0.65
STRAIGHT_BEND_DEG = 0.25
TRACK_ARM_SPEED_RAD_S = math.radians(3)
PULL_OUT_ALIGN_SPEED_RAD_S = math.radians(4)
PULL_OUT_ALIGN_TOLERANCE_RAD = math.radians(0.35)
PULL_OUT_RETRACT_SPEED_M_S = 0.050
# Match the manual segment and tracking increments in the native driver.
TRACK_SPEED_MULTIPLIER = 3.0
HORIZON_ROLL_STEP_DEG = 0.80
JOG_SEGMENT_SECONDS = 0.20
JOG_BEND_RATE_DEG_S = (1.5 * 150_000.) / (68_000. * 4. * math.pi / 180.)
JOG_ROLL_RATE_DEG_S = (1.5 * 56_250.) / (123_000. * (100. / 40.) / 360.)
TRACK_BEND_STEP_DEG = 0.32 * TRACK_SPEED_MULTIPLIER
TRACK_ROLL_STEP_DEG = 1.0 * TRACK_SPEED_MULTIPLIER
HORIZON_DEADBAND_DEG = 0.15
HORIZON_VERTICAL_ENTER = math.sin(math.radians(2.0))
HORIZON_VERTICAL_EXIT = math.sin(math.radians(4.0))
TRACK_HORIZON_TOLERANCE_DEG = 1.0
PIVOT_TOLERANCE_M = 0.002
MIN_FLEX_BEND_DEG = 30
DEFAULT_FLEX_BEND_DEG = 60
MAX_FLEX_BEND_DEG = 90  # Match the native driver's maximum travel envelope.
HEARTBEAT_LOST = "Flex heartbeat lost. Motion stopped. Waiting to reconnect."
DRIVER_DISCONNECTED = "Flex driver disconnected or restarted."


class FlexControl:
    def __init__(self, client, camera, pull_out_clearance=None, *, camera_roll_offset_deg=0.):
        self.client = client
        self.camera = camera
        self.pull_out_clearance = pull_out_clearance
        self.camera_roll_offset_deg = camera_roll_offset_deg
        angle = math.radians(camera_roll_offset_deg)
        c, s = math.cos(angle), math.sin(angle)
        self._image_to_flex = np.array(((c, -s), (s, c)))
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
        self._horizon_y = None
        self._horizon_near_vertical = False
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
            self.client.command(ipc.IDLE)
            raise ValueError("Could not send the Flex input recheck. Check the driver connection.")
        self._startup_recheck = (s.driver, request, time.monotonic() + 2.0)
        self.message = "Rechecking Flex startup inputs…"
        return True

    def _available_for_request(self):
        s = self.status
        return s.action == ipc.HORIZON or (s.action == ipc.IDLE and not s.moving)

    @property
    def bend_limit_max(self):
        s = self.status
        return MAX_FLEX_BEND_DEG if s is None or s.flags & ipc.WIDE_BEND_RANGE else DEFAULT_FLEX_BEND_DEG

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
        if key in ROLL_KEYS and not self.status.flags & ipc.COUPLED_ROLL_SUPPORTED:
            raise ValueError("Rebuild and restart the Flex driver to enable coordinated roll jogging.")
        self.cancel_tracking()
        if self.procedure or self.pending:
            self.cancel("Flex positioning canceled by manual input")
            return False
        self.keys[key] = time.monotonic()
        if key in ROLL_KEYS:
            self._horizon_paused = True
            self._zero_hold_joints = None
        elif not any(k in self.keys for k in ROLL_KEYS):
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
        timeout = POSITION_TIMEOUT_S
        if action in (ipc.POSITION, ipc.RETURN_ZERO):
            s = self.status
            travel = math.hypot(s.bend_x - parameters.get("x", 0.),
                                s.bend_y - parameters.get("y", 0.),
                                s.roll - parameters.get("roll", 0.))
            timeout += 2 * travel / s.speed_limit
        self.pending = (request, action, time.monotonic() + timeout)
        self._owned = True
        self.message = ""
        if self.client.send():
            return True
        self.client.command(ipc.IDLE)
        self.pending = None
        self._owned = False
        self.message = "Could not send the Flex command. Try again."
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

    def calibrate(self, bend_limit=None):
        if (not self.ready(calibrated=False) or not self.stop_acknowledged()
                or not self._available_for_request() or self.keys or self.pending
                or self.procedure or self.tracking):
            raise ValueError("Calibration requires stationary Flex hardware with all motion controls released.")
        if self.status.flags & ipc.SETUP_ENCODER_MISMATCH:
            raise ValueError("Setup travel and encoder feedback disagree. Check the encoder wiring, direction, and pulse scale.")
        if bend_limit is not None and not self.status.flags & ipc.BEND_LIMIT_SUPPORTED:
            raise ValueError("Rebuild and restart the Flex driver to adjust the bend limit.")
        if bend_limit is not None and bend_limit > self.bend_limit_max:
            raise ValueError("Rebuild and restart the Flex driver to use the expanded bend range.")
        self.resume_horizon()
        return self._request(ipc.CALIBRATE, x=0 if bend_limit is None else bend_limit)

    def set_bend_limit(self, degrees, *, speed=None):
        s = self.status
        if not self.ready(calibrated=False) or not s.flags & ipc.BEND_LIMIT_SUPPORTED:
            raise ValueError("A connected Flex driver with adjustable limits is required.")
        if s.bend_limit == 0:
            raise ValueError("Use CALIBRATE to set the first Flex bend limit.")
        if degrees > self.bend_limit_max:
            raise ValueError("Rebuild and restart the Flex driver to use the expanded bend range.")
        if speed is not None:
            if speed not in ipc.SPEED_PRESETS_DEG_S:
                raise ValueError("Choose a Flex speed preset.")
            if not s.flags & ipc.COMBINED_LIMITS_SUPPORTED:
                raise ValueError("Rebuild and restart the Flex driver to apply the bend and speed limits together.")
        if (not self.stop_acknowledged() or not self._available_for_request()
                or self.keys or self.pending or self.procedure or self.tracking):
            raise ValueError("Wait for Flex to finish moving before applying the bend limit.")
        if math.hypot(s.bend_x, s.bend_y) > degrees + 1e-6:
            raise ValueError("Bring Flex inside the requested bend limit first.")
        return self._request(ipc.SET_BEND_LIMIT, x=degrees, y=speed if speed is not None else 0.)

    def set_speed_limit(self, speed):
        if type(speed) not in (int, float) or not math.isfinite(speed) or speed < ipc.MIN_SPEED_DEG_S:
            raise ValueError("Enter a Flex speed of at least 0.1°/s.")
        if (not self.ready(calibrated=False) or not self.stop_acknowledged()
                or not self._available_for_request() or self.keys or self.pending
                or self.procedure or self.tracking):
            raise ValueError("Release the Flex controls and wait for Flex to stop before changing speed.")
        return self._request(ipc.SET_SPEED_LIMIT, x=speed)

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
        if not self.status.flags & ipc.TRACK_HORIZON_SUPPORTED:
            raise ValueError("Rebuild and restart the Flex driver for horizon-constrained tracking.")
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
        horizon_error = self._horizon_error(node, q_now) if obs.valid else 0.
        if obs.valid and not outside_cone and abs(horizon_error) > TRACK_HORIZON_TOLERANCE_DEG:
            self._tracking_command = None
            if self.status.action == ipc.TRACK:
                self.client.command(ipc.IDLE)
                self._owned = False
                self.tracking_state = "Leveling horizon"
            else:
                correcting = self._hold_horizon(node, horizon_error)
                self.tracking_state = ("Leveling horizon" if correcting else
                                       "Tracking limited · Flex cannot level the horizon within its travel limits")
            return
        if not obs.valid or obs.centered:
            self._tracking_limit = None
            self._tracking_command = None
            if outside_cone:
                self._wait_at_tracking_limit("UR5 outside the RCM cone")
                return
            self.tracking_state = "Centered" if obs.valid else "Waiting for target"
            if obs.valid:
                self._hold_horizon(node, horizon_error)
            else:
                self.client.command(ipc.IDLE)
                self._owned = False
            return

        # Convert image right/down to tendon and arm steering coordinates.
        error = np.array((obs.dx, obs.dy), dtype=float)
        norm = float(np.linalg.norm(error))
        if not math.isfinite(norm) or not norm:
            self.cancel_tracking("Tracking stopped · invalid target coordinates")
            return
        direction = self._image_to_flex @ (error / norm)
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
            required_roll = self._horizon_error(node, q_now, x, y, TRACK_BEND_STEP_DEG)
            roll = self._horizon_roll_command(required_roll, TRACK_ROLL_STEP_DEG)
            if abs(required_roll - roll * TRACK_ROLL_STEP_DEG) <= TRACK_HORIZON_TOLERANCE_DEG:
                self.client.command(ipc.TRACK, frame=obs.frame, x=x, y=y, roll=roll)
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
            self._wait_at_tracking_limit("no available RCM tilt toward the target", node if not outside_cone else None)
            return
        angle = min(TRACK_ARM_SPEED_RAD_S, node.arm_linear_speed_m_s / depth) * node.dt
        axis_target = math.cos(angle) * axis + math.sin(angle) * tangent / length
        seed_inside = node._inside_rcm_cone(axis, ref)
        if not seed_inside:
            if np.dot(axis_target - axis, ref) <= 1e-8:
                self._wait_at_tracking_limit("UR5 outside the RCM cone")
                return
        elif not node._inside_rcm_cone(axis_target, ref):
            radial = axis_target - np.dot(axis_target, ref) * ref
            cone = node.rcm_cone_half_angle
            axis_target = math.cos(cone) * ref + math.sin(cone) * radial / np.linalg.norm(radial)
            if np.dot(axis_target - axis, tangent) <= 1e-8:
                self._wait_at_tracking_limit("UR5 RCM cone reached", node)
                return
        q_target = node.kin.ik_along_axis(pivot + depth * axis_target, axis_target, q_seed)

        def validate_start(q):
            start_axis, _, start_tip = node.kin.tube_axis_in_base(q)
            return node._rcm_pivot_is_valid(start_axis, start_tip, depth)

        horizon_limit = TRACK_HORIZON_TOLERANCE_DEG
        if outside_cone:
            horizon_limit = max(horizon_limit, abs(horizon_error) - 1e-6)
        horizon_blocked = False

        def validate_target(q):
            nonlocal horizon_blocked
            target_axis, _, target_tip = node.kin.tube_axis_in_base(q)
            allowed = (node._inside_rcm_cone(target_axis, ref) if seed_inside
                       else np.dot(target_axis - axis, ref) > 1e-8)
            if not allowed or not node._rcm_pivot_is_valid(target_axis, target_tip, depth):
                return False
            horizon_blocked = abs(self._horizon_error(node, q)) > horizon_limit
            return not horizon_blocked

        if q_target is None or not node._send_servo(
                q_target, q_now, node.dt, validate=validate_target, validate_start=validate_start):
            reason = "arm assist would tilt the horizon" if horizon_blocked else "no feasible RCM correction"
            self._wait_at_tracking_limit(reason, node if not outside_cone else None)
            return
        if outside_cone:
            self.client.command(ipc.IDLE)
            self._owned = False
            self.tracking_state = "Returning UR5 to the RCM cone"
        else:
            self.tracking_state = "Following with arm assist"
            self._hold_horizon(node, horizon_error)

    def _wait_at_tracking_limit(self, reason, node=None):
        self._tracking_command = None
        if node is not None:
            self._hold_horizon(node)
        else:
            self.client.command(ipc.IDLE)
            self._owned = False
        self.tracking_state = f"Tracking limited · {reason}. Waiting for target to move."

    def _valid_arm_pose(self, node, q):
        return node._rcm_pose_is_valid(q)

    def _horizon_direction(self, rotation):
        forward = rotation[:, 2]
        horizontal = math.hypot(*forward[:2])
        threshold = HORIZON_VERTICAL_EXIT if self._horizon_near_vertical else HORIZON_VERTICAL_ENTER
        previous_y = self._horizon_y
        if horizontal <= threshold:
            # Gravity cannot define image roll at a vertical view. Carry
            # the last image direction into the new viewing plane.
            if previous_y is not None:
                y = previous_y - np.dot(previous_y, forward) * forward
                length = np.linalg.norm(y)
                if length > 1e-6:
                    return y / length, True
            angle = math.radians(self.camera_roll_offset_deg)
            return math.cos(angle) * rotation[:, 1] - math.sin(angle) * rotation[:, 0], True

        y = np.array((-forward[1], forward[0], 0.)) / horizontal
        # Either sign is level. Keep the sign continuous across the pole.
        if previous_y is not None and np.dot(y, previous_y) < 0.:
            y = -y
        return y, False

    def _horizon_error(self, node, q, x=0., y=0., bend_step=0.):
        s = self.status
        scale = max(1., math.hypot(x, y))
        x_next, y_next = s.bend_x + x * bend_step / scale, s.bend_y + y * bend_step / scale
        rotation = node.kin.physical_flex_camera_rotation(q, x_next, y_next, s.roll)
        target_y, _ = self._horizon_direction(rotation)
        delta = math.atan2(-np.dot(target_y, rotation[:, 0]), np.dot(target_y, rotation[:, 1]))
        return (math.degrees(delta) - self.camera_roll_offset_deg + 180.) % 360. - 180.

    def _horizon_roll_command(self, delta, roll_step):
        roll = self.status.roll
        limit = self._roll_limit()
        bounded = min(limit - roll, max(-limit - roll, delta))
        if abs(bounded) <= HORIZON_DEADBAND_DEG:
            return 0.
        return float(np.clip(bounded / roll_step, -1., 1.))

    def _horizon_correction(self, node, x, y, bend_step, roll_step):
        if self._horizon_paused or not node._arm_ready():
            return 0.
        delta = self._horizon_error(node, node.joint_positions.copy(), x, y, bend_step)
        return self._horizon_roll_command(delta, roll_step)

    def _hold_horizon(self, node, error=None):
        s = self.status
        roll = (self._horizon_correction(node, 0., 0., 0., HORIZON_ROLL_STEP_DEG) if error is None
                else self._horizon_roll_command(error, HORIZON_ROLL_STEP_DEG))
        if roll:
            self.client.command(ipc.HORIZON, roll=roll)
            self._owned = s.result != ipc.LIMIT or s.sequence != self.client.sequence
        else:
            self.client.command(ipc.IDLE)
            self._owned = False
        return self._owned

    def _roll_limit(self):
        s = self.status
        return 20. if s.planned_feedback and not s.flags & ipc.COUPLED_ROLL_SUPPORTED else 180.

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
        tendon_angle = distal + math.radians(self.camera_roll_offset_deg)
        roll = (math.degrees(base) - self.camera_roll_offset_deg + 180.) % 360. - 180.
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
        if any(node._key_state.values()) or node._arm_moving is not False or node._teleop_servo_active:
            raise ValueError("Release the arm controls and wait for the UR5 to stop before starting Pull Out.")
        if (self.keys or self.pending or self.procedure or self.tracking
                or not self.stop_acknowledged() or not self._available_for_request()):
            raise ValueError("Wait for Flex to be idle before starting Pull Out.")
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
        flex_at_zero = self._straight() and bool(self.status.flags & ipc.AT_SAVED_ZERO)
        if not flex_at_zero and not self.return_to_zero():
            return False
        self._horizon_paused = True
        self._zero_hold_joints = None
        if flex_at_zero:
            self.client.command(ipc.IDLE)
        sign = -math.copysign(1., alignment)
        align_rate = min(PULL_OUT_ALIGN_SPEED_RAD_S, node.arm_linear_speed_m_s / max(depth, 1e-6))
        self.procedure = {
            "stage": "prepare", "depth": depth, "offset": axial_offset, "flex_zero": flex_at_zero,
            "end": sign * self.pull_out_clearance, "sign": sign,
            "deadline": max(
                self.pending[2] if self.pending else 0.,
                time.monotonic() + POSITION_TIMEOUT_S + 2 * node.rcm_cone_half_angle / align_rate,
            ),
            "retract_distance": abs(axial_offset + sign * self.pull_out_clearance - depth),
            "retract_speed": min(PULL_OUT_RETRACT_SPEED_M_S, node.arm_linear_speed_m_s),
        }
        self.message = "Pull Out · preparing RCM and Flex"
        return True

    def confirm_pull_out(self, node):
        if (not self.procedure or self.procedure["stage"] != "confirm"
                or not self._rcm_ready(node) or not self.ready() or not self._straight()
                or node._arm_moving is not False):
            raise ValueError("Wait for the UR5 and Flex to finish resetting before confirming.")
        q = node.joint_positions.copy()
        axis, _, _ = node.kin.tube_axis_in_base(q)
        if (float(np.dot(axis, node.rcm_ref_axis)) < math.cos(PULL_OUT_ALIGN_TOLERANCE_RAD)
                or not node._rcm_pose_is_valid(q, self.procedure["depth"])):
            raise ValueError("The UR5 moved away from the reset pose. Start Pull Out again.")
        return self._begin_pull_out_retraction()

    def _begin_pull_out_retraction(self):
        self.procedure["stage"] = "retract"
        self.procedure["deadline"] = (time.monotonic() + POSITION_TIMEOUT_S
                                      + 2 * self.procedure["retract_distance"] / self.procedure["retract_speed"])
        self.message = "Pull Out · retracting"
        return True

    def _straight(self):
        return (not self.pending and not self.status.moving and self.status.action == ipc.IDLE
                and math.hypot(self.status.bend_x, self.status.bend_y) <= STRAIGHT_BEND_DEG)

    def _step_pull_out(self, node):
        st = self.procedure
        if not self._rcm_ready(node) or not self.ready():
            self.cancel("Pull Out stopped · UR5 or Flex feedback unavailable")
            return
        stage = st["stage"]
        if time.monotonic() >= st["deadline"]:
            phase = {"prepare": "RCM/Flex reset", "confirm": "straightness confirmation", "retract": "withdrawal"}[stage]
            self.cancel(f"Pull Out stopped · {phase} timed out")
            return
        q = node.joint_positions.copy()
        axis, _, tip = node.kin.tube_axis_in_base(q)
        if not self._valid_arm_pose(node, q):
            self.cancel("Pull Out stopped · UR5 outside the RCM envelope")
            return
        ref, pivot = node.rcm_ref_axis, node.rcm_world_point
        actual_error = math.acos(float(np.clip(np.dot(axis, ref), -1., 1.)))
        flex_ready = st["flex_zero"] and self._straight()
        aligned = actual_error <= PULL_OUT_ALIGN_TOLERANCE_RAD
        if stage == "confirm":
            if (not flex_ready or not aligned or node._arm_moving is not False
                    or not node._rcm_pivot_is_valid(axis, tip, st["depth"])):
                self.cancel("Pull Out stopped · reset pose changed before confirmation")
            return
        # Keep the target advancing from the last nearby servo command while
        # RTDE follows it. The measured pose still gates every phase change.
        q_seed = node._q_cmd
        if q_seed is None or np.max(np.abs(q_seed - q)) > node.TELEOP_RESYNC_RAD:
            q_seed = q
        if stage == "prepare":
            depth = st["depth"]
            if not node._rcm_pivot_is_valid(axis, tip, depth):
                self.cancel("Pull Out stopped · insertion depth changed during reset")
                return
            if aligned:
                if node._arm_moving is not False or node._teleop_servo_active:
                    return
                if not flex_ready:
                    self.message = "Pull Out · RCM aligned, waiting for Flex zero"
                elif self.status.planned_feedback:
                    st["stage"] = "confirm"
                    st["deadline"] = time.monotonic() + POSITION_TIMEOUT_S
                    self.message = "Pull Out · verify the scope is physically straight"
                else:
                    self._begin_pull_out_retraction()
                return
            seed_axis = node.kin.tube_axis_in_base(q_seed)[0] if q_seed is not q else axis
            angle = math.acos(float(np.clip(np.dot(seed_axis, ref), -1., 1.)))
            step = min(PULL_OUT_ALIGN_SPEED_RAD_S, node.arm_linear_speed_m_s / max(depth, 1e-6)) * node.dt
            if angle <= step:
                axis_target = ref
            else:
                cross = np.cross(seed_axis, ref)
                axis_target = node.kin.rotate_about(seed_axis, cross / np.linalg.norm(cross), step)
            self.message = ("Pull Out · aligning to RCM" if flex_ready else
                            "Pull Out · aligning RCM and returning Flex to zero")
        else:
            if not flex_ready:
                self.cancel("Pull Out stopped · Flex straightness lost")
                return
            if actual_error > math.radians(1):
                self.cancel("Pull Out stopped · RCM alignment lost")
                return
            axis_target = ref
            end = node.kin.straight_camera_position_in_base(q)
            current = float(np.dot(end - pivot, ref))
            remaining = st["sign"] * (st["end"] - current)
            if remaining <= 0.001:
                if node._arm_moving is False and not node._teleop_servo_active:
                    self.procedure = None
                    self.message = "Pull Out complete"
                return
            end_seed = node.kin.straight_camera_position_in_base(q_seed) if q_seed is not q else end
            commanded = float(np.dot(end_seed - pivot, ref))
            step = st["retract_speed"] * node.dt
            command_remaining = st["sign"] * (st["end"] - commanded)
            next_end = (st["end"] if command_remaining <= step else commanded + st["sign"] * step)
            depth = next_end + st["offset"]
        p_target = pivot + depth * axis_target
        q_target = node.kin.ik_along_axis(p_target, axis_target, q_seed)
        if q_target is None or not node._send_servo(
                q_target, q, node.dt, validate=lambda target: node._rcm_pose_is_valid(target, depth),
                validate_start=node._rcm_pose_is_valid):
            self.cancel("Pull Out stopped · RCM correction unavailable")

    def tick(self, node):
        old = self._driver
        s = self.client.poll()
        self._driver = s.driver if s is not None and self.client.ready else None
        exclusive = bool(self.tracking or self.procedure or
                         (self.pending and self.pending[1] in (ipc.POSITION, ipc.RETURN_ZERO)))
        if old is not None and old != self._driver:
            issue = DRIVER_DISCONNECTED
            if s is not None and s.driver == old:
                issue = "Flex motion blocked by GTS fault" if s.fault else HEARTBEAT_LOST
            self.cancel(issue)
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
        if self.message in (HEARTBEAT_LOST, DRIVER_DISCONNECTED):
            self.message = "Flex reconnected. Press a movement control to resume."
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
                         ipc.RETURN_ZERO: "Return to Flex zero", ipc.SET_BEND_LIMIT: "Bend limit update",
                         ipc.SET_SPEED_LIMIT: "Speed limit update"}[action]
                if action == ipc.SET_SPEED_LIMIT:
                    self.message = ("Flex speed limit applied."
                                    if result == ipc.DONE else f"{label} {ipc.RESULTS[result]}.")
                elif action == ipc.SET_BEND_LIMIT:
                    if result == ipc.DONE:
                        self.message = "Flex limits applied."
                    elif result == ipc.LIMIT:
                        self.message = "Bring Flex inside the requested bend limit first."
                    else:
                        self.message = f"{label} {ipc.RESULTS[result]}."
                elif result != ipc.DONE:
                    self.cancel(f"{label} {ipc.RESULTS[result]}")
                elif self.procedure and action == ipc.RETURN_ZERO:
                    self.procedure["flex_zero"] = True
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
        if self.ready() and node._arm_ready() and not self._horizon_paused:
            rotation = node.kin.physical_flex_camera_rotation(
                node.joint_positions.copy(), s.bend_x, s.bend_y, s.roll)
            self._horizon_y, self._horizon_near_vertical = self._horizon_direction(rotation)
        else:
            self._horizon_y, self._horizon_near_vertical = None, False
        obs = self.camera.observation()
        if self.tracking:
            self._step_tracking(node, obs)
        elif self.procedure:
            self._step_pull_out(node)
        elif self.keys and self.jog_ready() and not self.pending and self.stop_acknowledged():
            if any(k in self.keys for k in ROLL_KEYS):
                minus, plus = (int(k in self.keys) for k in ROLL_KEYS)
                self.client.command(ipc.ROLL_JOG, roll=plus - minus)
            else:
                left, right, up, down = (int(k in self.keys) for k in BEND_KEYS)
                x, y = right - left, down - up
                bend_step = min(s.speed_limit, JOG_BEND_RATE_DEG_S) * JOG_SEGMENT_SECONDS
                roll_step = min(s.speed_limit, JOG_ROLL_RATE_DEG_S) * JOG_SEGMENT_SECONDS
                if x or y:
                    direction = math.hypot(x, y)
                    along = (s.bend_x * x + s.bend_y * y) / direction
                    perpendicular = (s.bend_x * y - s.bend_y * x) / direction
                    to_limit = math.sqrt(max(0., s.bend_limit**2 - perpendicular**2)) - along
                    bend_step = min(bend_step, max(0., to_limit))
                roll = (self._horizon_correction(node, x, y, bend_step, roll_step)
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
        bend = math.hypot(s.bend_x, s.bend_y) if s else None
        plane = math.radians(s.roll) + math.atan2(-s.bend_x, s.bend_y) if s else None
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
            "bend": bend,
            "bend_point": [bend * math.sin(plane), -bend * math.cos(plane)] if s else None,
            "roll": s.roll if s else None,
            "tendons": list(s.tendons) if s and (s.calibrated or planned_zero) else None,
            "bend_limit": s.bend_limit if s else None,
            "bend_limit_default": DEFAULT_FLEX_BEND_DEG,
            "bend_limit_max": self.bend_limit_max,
            "bend_limit_supported": bool(s and s.flags & ipc.BEND_LIMIT_SUPPORTED),
            "speed_limit_deg_s": s.speed_limit if s else None,
            "moving": s.moving if s and self.client.ready else None,
            "fault": s.fault if s else 0,
            "fault_detail": s.fault_detail if s else "",
            "startup_recheck": bool(s and s.flags & ipc.STARTUP_RECHECK),
            "recheck_pending": self._startup_recheck is not None,
            "setup_encoder_mismatch": bool(s and s.flags & ipc.SETUP_ENCODER_MISMATCH),
            "roll_jog_supported": bool(s and s.flags & ipc.COUPLED_ROLL_SUPPORTED),
            "message": self.message,
            "tracking": self.tracking is not None,
            "tracking_horizon_supported": bool(s and s.flags & ipc.TRACK_HORIZON_SUPPORTED),
            "tracking_state": self.tracking_state,
            "pull_out_enabled": self.pull_out_clearance is not None,
            "pull_out_confirm": bool(self.procedure and self.procedure["stage"] == "confirm"),
        }
