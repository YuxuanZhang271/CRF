#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run EndoFlex kinematics and send joint targets to the local RTDE relay."""
import json
import logging
import math
import socket
import threading
import time
from concurrent.futures import Future

import numpy as np

from .flex_endo.flex_control import FLEX_KEYS, POSITION_TIMEOUT_S, PULL_OUT_BEHIND_RCM_M, PULL_OUT_RETRACT_SPEED_M_S
from .flex_endo.flex_ipc import DEFAULT_SPEED_DEG_S, MIN_SPEED_DEG_S, POSITION
from .kinematics import EndoscopeKinematics, rcm_tip_angles
from .ur5_rtde_server import DEFAULT_LINEAR_SPEED_MM_S, SERVO_RESYNC_RAD, limit_servo_target

# Modeled Flex geometry; physical motor limits live in the native driver.
FLEX_N = 8
FLEX_SEG_LEN = 0.008
FLEX_ARC_MIN = 0.030
FLEX_ARC_MAX = 0.050
FLEX_MAX_BEND = math.pi / 2.0
MIN_RCM_CONE_DEG = 10
MAX_RCM_CONE_DEG = 30
FLEX_TENDON_R = 0.004
FLEX_TENDONS = [(0,'R'), (90,'G'), (180,'B'), (270,'Y')]
FLEX_STRAIGHT_KAPPA_TOL = 1e-3
BIRD_EYE_HORIZON_DEG = 30.0
BIRD_EYE_TOLERANCE_DEG = 1.0

FLEX_MAX_EFFECTIVE_LENGTH = FLEX_ARC_MAX - FLEX_SEG_LEN

log = logging.getLogger("ControlNode")
STATE_TIMEOUT_S = 0.3
KEY_TIMEOUT_S = 2.0
MAX_LINE_BYTES = 4096
DEFAULT_JOG_SPEED_SCALE = 10
JOG_SPEED_SCALES = (3, 5, DEFAULT_JOG_SPEED_SCALE, 15)


class ControlNode:
    """Coordinate arm jogging, RCM procedures, and hardware or modeled Flex."""

    STEP_TICK_HZ = 125.0          # Match the RTDE servo frequency.
    ARM_TILT_VEL_DEG_S = 4.0
    FLEX_YAW_VEL = 1.0
    FLEX_CURV_VEL = 10.0
    FLEX_ARC_VEL = 0.020
    RCM_CONE_HALF_ANGLE = math.radians(MIN_RCM_CONE_DEG)
    TELEOP_RESYNC_RAD = SERVO_RESYNC_RAD

    def __init__(self, relay_host, relay_port, urdf_path,
                 rcm_point_local=(0,0,-0.30),
                 tip_local=(0,0,0.20195),
                 jog_speed_scale=DEFAULT_JOG_SPEED_SCALE, flex=None):
        if jog_speed_scale not in JOG_SPEED_SCALES:
            choices = ", ".join(map(str, JOG_SPEED_SCALES))
            raise ValueError(f"jog speed scale must be one of {choices}")
        self.dt = 1.0 / self.STEP_TICK_HZ
        self.jog_speed_scale = jog_speed_scale
        self.arm_linear_speed_m_s = DEFAULT_LINEAR_SPEED_MM_S / 1000.
        self.relay_host = relay_host
        self.relay_port = relay_port
        self.flex = flex
        self.flex_stop_delivered = False

        # --- kinematics ---
        self.kin = EndoscopeKinematics(
            base_link="base_link", tip_link="endoscope_link",
            rcm_point_local=np.array(rcm_point_local, float),
            tip_local=np.array(tip_local, float),
            logger=log)
        try:
            with open(urdf_path, "r", encoding="utf-8") as f:
                urdf_str = f.read()
            if not self.kin.setup_from_urdf(urdf_str):
                log.error("kinematics.setup_from_urdf returned False")
        except Exception as e:
            log.error(f"URDF load failed ({urdf_path}): {e}")

        # --- state ---
        self.control_lock = threading.RLock()
        self._lock = threading.RLock()
        self.joint_positions = np.zeros(6)
        self.joint_received = False
        self._last_joint_at = float("-inf")
        self._motion_fault = None
        self._arm_moving = None

        self.rcm_enabled = False
        self.rcm_world_point = None
        self.rcm_ref_axis = None
        self.rcm_ref_depth = None
        self.arm_mode = 'linear'
        self.rcm_cone_half_angle = self.RCM_CONE_HALF_ANGLE

        self._q_cmd = None
        self._teleop_servo_active = False       # Whether servo targets are being sent.
        self._servo_sent_this_tick = False

        self.flex_steer_yaw = 0.0
        self.flex_base_yaw = 0.0
        self.flex_ext_yaw = 0.0
        self.flex_arc_len = FLEX_ARC_MAX
        self.flex_curvature = 0.0
        self.flex_max_bend = FLEX_MAX_BEND
        self.flex_model_speed_deg_s = DEFAULT_SPEED_DEG_S
        self._model_bend = np.zeros(2)
        self._last_model_update = time.monotonic()

        self._pull_out_state = None
        self._pull_out_complete_joints = None
        self._flex_slew_target = None
        self._model_horizon_paused = False

        self._key_state = {
            'arm_up':False,'arm_down':False,'arm_left':False,'arm_right':False,
            'arm_fwd':False,'arm_bwd':False,
            'flex_w':False,'flex_s':False,'flex_a':False,'flex_d':False,
            'flex_r':False,'flex_f':False,
        }
        self._key_updated = {}

        # --- TCP ---
        self._sock = None
        self._send_lock = threading.Lock()
        self._recv_thread = None
        self._interrupt_id = 0
        self._interrupt_result = None
        self._tick_thread = None
        self._running = threading.Event()
        self._shutdown = threading.Event()
        self._snapshot_lock = threading.Lock()
        self._snapshot_at = time.monotonic()
        self._snapshot = self.get_state_dict()

    # ---------------- TCP plumbing ----------------
    def _connect(self):
        while self._running.is_set():
            try:
                with socket.create_connection((self.relay_host, self.relay_port),
                                              timeout=2.0) as sock:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    sock.settimeout(0.1)
                    with self.control_lock, self._lock:
                        if not self._running.is_set():
                            return
                        self._sock = sock
                        self._last_joint_at = time.monotonic()
                    log.info("Connected to relay %s:%d", self.relay_host, self.relay_port)
                    try:
                        self._recv_loop(sock)
                    finally:
                        with self.control_lock, self._lock:
                            self._sock = None
                            self.joint_received = False
                            self._last_joint_at = float("-inf")
                            self._motion_fault = None
                            self._arm_moving = None
                            self._finish_arm_interrupt("UR5 relay disconnected during INTERRUPT.")
                            self._cancel_motion()
                            self.arm_mode = 'linear'
                            self.rcm_enabled = False
                            self.rcm_world_point = None
                            self.rcm_ref_axis = None
                            self.rcm_ref_depth = None
            except (OSError, ValueError) as e:
                if self._running.is_set():
                    log.warning("Relay connection lost or unavailable: %s", e)
            self._shutdown.wait(3.0)

    def _recv_loop(self, sock):
        buf = b""
        while self._running.is_set():
            if time.monotonic() - self._last_joint_at > STATE_TIMEOUT_S:
                raise TimeoutError("joint state timed out")
            try:
                data = sock.recv(MAX_LINE_BYTES)
            except TimeoutError:
                continue
            if not data:
                return
            lines = (buf + data).split(b"\n")
            buf = lines.pop()
            if len(buf) > MAX_LINE_BYTES:
                raise ValueError("unterminated robot state exceeds size limit")
            for line in lines:
                if len(line) > MAX_LINE_BYTES:
                    raise ValueError("robot state exceeds size limit")
                try:
                    msg = json.loads(line)
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("type") == "interrupt_result":
                        with self._lock:
                            if msg.get("request") == self._interrupt_id:
                                self._finish_arm_interrupt(msg.get("error"))
                        continue
                    if msg.get("type") != "robot_state":
                        continue
                    q = msg.get("joints")
                    if not isinstance(q, list) or len(q) != 6:
                        continue
                    if not all(type(v) in (int, float) and math.isfinite(v) for v in q):
                        continue
                except (ValueError, OverflowError):
                    continue
                with self._lock:
                    self.joint_positions = np.array(q, float)
                    self.joint_received = True
                    self._last_joint_at = time.monotonic()
                    fault = msg.get("motion_fault")
                    self._motion_fault = fault if isinstance(fault, str) else None
                    moving = msg.get("moving")
                    self._arm_moving = moving if type(moving) is bool else None
                    linear = msg.get("arm_linear_speed_mm_s", DEFAULT_LINEAR_SPEED_MM_S)
                    if type(linear) in (int, float) and math.isfinite(linear) and linear > 0:
                        self.arm_linear_speed_m_s = linear / 1000.

    def _send(self, obj):
        with self._send_lock:
            with self._lock:
                sock = self._sock
            if sock is None:
                log.warning(f"send skipped (not connected): {obj.get('type')}")
                return False
            try:
                sock.sendall((json.dumps(obj, allow_nan=False) + "\n").encode("utf-8"))
                return True
            except OSError as e:
                log.warning(f"send failed: {e}")
                self._disconnect_socket(sock)
                return False

    @staticmethod
    def _disconnect_socket(sock):
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    # ---------------- Lifecycle ----------------
    def start(self, connect_arm=True):
        if self._running.is_set():
            return
        self._shutdown.clear()
        if self.flex is not None:
            self.flex.client.start()
        self._running.set()
        self._recv_thread = threading.Thread(target=self._connect, daemon=True,
                                             name="tcp-recv")
        self._tick_thread = threading.Thread(target=self._tick_loop, daemon=True,
                                             name="ctrl-tick")
        if connect_arm:
            self._recv_thread.start()
        self._tick_thread.start()
        log.info(f"ControlNode started @ {self.STEP_TICK_HZ:.0f} Hz")

    def stop(self):
        with self.control_lock:
            self._running.clear()
            self._shutdown.set()
            with self._lock:
                sock = self._sock
            self.cmd_stop()
            if sock is not None:
                self._disconnect_socket(sock)
            if self.flex is not None:
                self.flex.client.close()
        for t in (self._recv_thread, self._tick_thread):
            if t and t.is_alive():
                t.join()

    # ---------------- Tick loop ----------------
    def _tick_loop(self):
        """Schedule control ticks at an 8 ms interval."""
        next_t = time.monotonic()
        while self._running.is_set():
            now = time.monotonic()
            if now < next_t:
                if self._shutdown.wait(next_t - now):
                    break
            next_t += self.dt
            if next_t < time.monotonic():
                next_t = time.monotonic()
            try:
                with self.control_lock:
                    if self._running.is_set():
                        self._tick()
                        if time.monotonic() - self._snapshot_at >= 0.05:
                            snapshot = self.get_state_dict()
                            with self._snapshot_lock:
                                self._snapshot = snapshot
                                self._snapshot_at = time.monotonic()
            except Exception as e:
                log.exception(f"tick error: {e}")
                with self.control_lock:
                    self.cmd_stop()

    # ==================================================================
    # Tick / helpers
    # ==================================================================

    def _tick(self):
        with self._lock:
            fault = self._motion_fault
        if fault and (self._teleop_servo_active or self._pull_out_state is not None
                      or (self.flex is not None and (self.flex.procedure or self.flex.tracking))):
            self.cmd_stop()
        now = time.monotonic()
        for key, updated_at in list(self._key_updated.items()):
            if now - updated_at > KEY_TIMEOUT_S:
                self.set_key(key, False)
        ks = self._key_state

        any_manual_key = any(ks.values())

        with self._lock:
            pull_out_active = self._pull_out_state is not None
            if pull_out_active and any_manual_key:
                self._pull_out_state = None
                self._flex_slew_target = None
                pull_out_active = False
                log.info("PULL OUT cancelled by manual key input.")
            if any_manual_key and self._flex_slew_target is not None:
                self._flex_slew_target = None
                log.info("FLEX slew cancelled by manual key input.")

        servo_was_active = self._teleop_servo_active
        self._servo_sent_this_tick = False
        try:
            if self.flex is None or not self.flex.tick(self):
                self._step_controls(ks, pull_out_active)
        finally:
            if servo_was_active and not self._servo_sent_this_tick:
                self._send_servo_stop()

    def _step_controls(self, ks, pull_out_active):
        self._step_flex_slew()

        if pull_out_active:
            self._step_pull_out()
            self._update_flex_state()
            return

        with self._lock:
            mode = self.arm_mode

        vertical = ks['arm_up'] - ks['arm_down']
        horizontal = ks['arm_right'] - ks['arm_left']
        axial = ks['arm_bwd'] - ks['arm_fwd']
        if axial or horizontal or vertical:
            kind = "jog_tip" if mode == 'rcm' and (horizontal or vertical) else "jog_linear"
            self._send_arm_jog(kind, (axial, horizontal, vertical))

        if self.flex is not None:
            return
        with self._lock:
            dk = (ks['flex_s'] - ks['flex_w']) * self.FLEX_CURV_VEL * self.dt
            dy = (ks['flex_a'] - ks['flex_d']) * self.FLEX_YAW_VEL * self.dt
            da = (ks['flex_r'] - ks['flex_f']) * self.FLEX_ARC_VEL * self.dt
            self.flex_arc_len = min(FLEX_ARC_MAX, max(FLEX_ARC_MIN, self.flex_arc_len + da))
            effective_length = self.flex_arc_len - FLEX_SEG_LEN
            self.flex_curvature = min(self.flex_max_bend / effective_length,
                                      max(0.0, self.flex_curvature + dk))
            self.flex_steer_yaw = (self.flex_steer_yaw + dy + math.pi) % (2 * math.pi) - math.pi
        self._update_flex_state()

    def _arm_ready(self):
        with self._lock:
            return self.kin.ready and self._motion_fault is None and self._joint_state_fresh()

    def _joint_state_fresh(self):
        return (self.joint_received
                and time.monotonic() - self._last_joint_at <= STATE_TIMEOUT_S)

    def _inside_rcm_cone(self, axis, ref_axis):
        return float(np.dot(axis, ref_axis)) >= math.cos(self.rcm_cone_half_angle) - 1e-10

    def _rcm_pose_is_valid(self, joints, depth=None):
        axis, _, tip = self.kin.tube_axis_in_base(joints)
        return (self._inside_rcm_cone(axis, self.rcm_ref_axis)
                and self._rcm_pivot_is_valid(axis, tip, depth))

    def _rcm_pivot_is_valid(self, axis, tip, depth=None):
        offset = tip - self.rcm_world_point
        return (np.linalg.norm(np.cross(offset, axis)) <= 0.002
                and (depth is None or abs(float(np.dot(offset, axis)) - depth) <= 0.002))

    def _auto_solve_wrist1(self, q):
        q = np.array(q, float).copy()
        q[3] = math.radians(-270.0) - q[1] - q[2]
        return q

    # ---------- flex ----------
    def _set_flex_slew_target(self, curvature=None, steer_yaw=None, arc_len=None):
        self._model_horizon_paused = False
        self._flex_slew_target = {
            'curvature': self.flex_curvature if curvature is None else float(curvature),
            'steer_yaw': self.flex_steer_yaw if steer_yaw is None else float(steer_yaw),
            'arc_len':   self.flex_arc_len   if arc_len is None   else float(arc_len),
        }

    def _step_flex_slew(self):
        with self._lock:
            tgt = self._flex_slew_target
            if tgt is None:
                return
            a = self.flex_arc_len
            a_t = min(max(FLEX_ARC_MIN, tgt['arc_len']), FLEX_ARC_MAX)
            step_a = self.FLEX_ARC_VEL*self.dt
            a_new = a_t if abs(a_t-a) <= step_a else a + math.copysign(step_a, a_t-a)
            self.flex_arc_len = min(max(FLEX_ARC_MIN, a_new), FLEX_ARC_MAX)
            c = self.flex_curvature
            eff = max(1e-6, self.flex_arc_len - FLEX_SEG_LEN)
            kmax = self.flex_max_bend/eff
            c_t = min(max(0.0, tgt['curvature']), kmax)
            step_c = self.FLEX_CURV_VEL*self.dt
            c_new = c_t if abs(c_t-c) <= step_c else c + math.copysign(step_c, c_t-c)
            self.flex_curvature = min(max(0.0, c_new), kmax)
            y = self.flex_steer_yaw
            y_t = tgt['steer_yaw']
            dy = ((y_t-y+math.pi) % (2*math.pi)) - math.pi
            step_y = self.FLEX_YAW_VEL*self.dt
            y_new = y_t if abs(dy) <= step_y else y + math.copysign(step_y, dy)
            self.flex_steer_yaw = (y_new + math.pi) % (2*math.pi) - math.pi

    def _flex_is_straight(self):
        with self._lock:
            return abs(self.flex_curvature) <= FLEX_STRAIGHT_KAPPA_TOL

    def _update_auto_horizon_twist_unlocked(self, theta_total):
        if self._model_horizon_paused:
            return
        if self.kin.ready and self._joint_state_fresh():
            try:
                by, ey = self.kin.solve_horizon_flex_yaw(
                    q=self.joint_positions, steer_yaw=self.flex_steer_yaw,
                    theta_total=theta_total)
                self.flex_base_yaw = float(by)
                self.flex_ext_yaw = float(ey)
                return
            except Exception as e:
                log.warning(f"auto horizon twist failed: {e}")
        self.flex_base_yaw = float(self.flex_steer_yaw)
        self.flex_ext_yaw = float(-self.flex_steer_yaw)

    def _update_flex_state(self):
        with self._lock:
            eff = max(1e-6, self.flex_arc_len - FLEX_SEG_LEN)
            theta_total = min(self.flex_max_bend, eff * max(0.0, self.flex_curvature))
            if self.flex is None:
                theta_total = self._limit_model_flex_step(theta_total, eff)
            else:
                self._update_auto_horizon_twist_unlocked(theta_total)

    def _limit_model_flex_step(self, theta, length):
        now = time.monotonic()
        budget = math.radians(self.flex_model_speed_deg_s) * min(self.dt, now - self._last_model_update)
        self._last_model_update = now
        target = theta * np.array([math.cos(self.flex_steer_yaw), math.sin(self.flex_steer_yaw)])
        delta = target - self._model_bend
        bend_distance = float(np.linalg.norm(delta))
        bend_change = min(budget, bend_distance)
        if bend_distance > 0.:
            self._model_bend += delta * (bend_change / bend_distance)
        theta = float(np.linalg.norm(self._model_bend))
        self.flex_curvature = theta / length
        if theta > 1e-9:
            self.flex_steer_yaw = math.atan2(self._model_bend[1], self._model_bend[0])
        previous_roll = self.flex_base_yaw
        self._update_auto_horizon_twist_unlocked(theta)
        requested_roll = (self.flex_base_yaw - previous_roll + math.pi) % (2 * math.pi) - math.pi
        roll_budget = math.sqrt(max(0., budget**2 - bend_change**2))
        limited_roll = float(np.clip(requested_roll, -roll_budget, roll_budget))
        self.flex_base_yaw = (previous_roll + limited_roll + math.pi) % (2 * math.pi) - math.pi
        self.flex_ext_yaw += requested_roll - limited_roll
        target = self._flex_slew_target
        if target is not None:
            arc = min(FLEX_ARC_MAX, max(FLEX_ARC_MIN, target['arc_len']))
            curvature = min(self.flex_max_bend / length, max(0., target['curvature']))
            yaw_error = (self.flex_steer_yaw - target['steer_yaw'] + math.pi) % (2 * math.pi) - math.pi
            if (abs(self.flex_arc_len - arc) <= 1e-6 and abs(self.flex_curvature - curvature) <= 1e-6
                    and abs(yaw_error) <= 1e-6):
                self._flex_slew_target = None
        return theta

    def set_model_flex_speed(self, speed):
        if type(speed) not in (int, float) or not math.isfinite(speed) or speed < MIN_SPEED_DEG_S:
            raise ValueError("Enter a Flex speed of at least 0.1°/s.")
        if self._flex_slew_target or self._pull_out_state or any(self._key_state.values()):
            raise ValueError("Wait for modeled Flex motion to stop before changing speed.")
        self.flex_model_speed_deg_s = speed
        return True

    # ---------- modes ----------
    def set_rcm_cone_limit(self, degrees, *, speed=None, relay=None):
        angle = math.radians(degrees)
        with self._lock:
            if (not self._arm_ready() or self._arm_moving is not False or self._teleop_servo_active
                    or any(self._key_state.values()) or self._pull_out_state
                    or (self.flex and (self.flex.tracking or self.flex.procedure))):
                raise ValueError("Wait for the arm to be stationary and release the controls before changing its limit.")
            if self.rcm_enabled:
                axis, _, _ = self.kin.tube_axis_in_base(self.joint_positions)
                if float(np.dot(axis, self.rcm_ref_axis)) < math.cos(angle) - 1e-10:
                    raise ValueError("Move the arm inside the requested RCM limit before applying it.")
            if speed is not None and not relay.set_speed_limit(speed):
                return False
            self.rcm_cone_half_angle = angle
        return True

    def set_model_bend_limit(self, degrees, *, speed=None):
        angle = math.radians(degrees)
        with self._lock:
            if (any(self._key_state.values()) or self._pull_out_state
                    or self._flex_slew_target is not None):
                raise ValueError("Wait for modeled Flex motion to stop before changing its limit.")
            bend = self.flex_curvature * max(self.flex_arc_len - FLEX_SEG_LEN, FLEX_MAX_EFFECTIVE_LENGTH)
            if bend > angle + 1e-10:
                raise ValueError("Bring Flex inside the requested bend limit first.")
            if speed is not None:
                self.set_model_flex_speed(speed)
            self.flex_max_bend = angle
        return True

    def toggle_arm_mode(self):
        if self.flex is not None and (self.flex.tracking or self.flex.procedure):
            self.flex.cancel("Coordinated motion stopped · arm mode changed")
            if self._teleop_servo_active:
                self._send_servo_stop()
        with self._lock:
            if self.arm_mode == 'linear' and not self._arm_ready():
                return False
            self._pull_out_state = None
            self._flex_slew_target = None
            self._q_cmd = None
            if self.arm_mode == 'linear':
                self.arm_mode = 'rcm'
                self._capture_rcm_unlocked()
                log.info("Arm mode -> RCM (captured)")
            else:
                self.arm_mode = 'linear'
                self.rcm_enabled = False
                self.rcm_world_point = None
                self.rcm_ref_axis = None
                self.rcm_ref_depth = None
                log.info("Arm mode -> linear")
        return True

    def stop_camera_tracking(self):
        if self.flex is not None and self.flex.tracking:
            self.flex.cancel_tracking()
            if self._teleop_servo_active:
                self._send_servo_stop()

    def _capture_rcm_unlocked(self):
        if not (self.kin.ready and self.joint_received):
            self.rcm_enabled = False
            return
        q = self.joint_positions.copy()
        axis, p_rcm, p_tip = self.kin.tube_axis_in_base(q)
        self.rcm_world_point = p_rcm
        self.rcm_ref_axis = axis
        self.rcm_ref_depth = float(np.dot(p_tip - p_rcm, axis))
        self.rcm_enabled = True

    # ---------- semi-auto ----------
    def cmd_reset(self):
        if not self._arm_ready():
            raise ValueError("RESET requires fresh UR5 joint feedback and a ready arm.")
        target = np.deg2rad([0., -120., -120., -30., 90., 45.])
        with self._lock:
            coordinated = self.flex is not None and (self.flex.tracking or self.flex.procedure)
            self._cancel_motion(include_flex=bool(coordinated))
            self.arm_mode = 'linear'
            self.rcm_enabled = False
            self.rcm_world_point = None
            self.rcm_ref_axis = None
            self.rcm_ref_depth = None
            distance = float(np.max(np.abs(target - self.joint_positions)))
        if distance < 1e-6:
            return self._send({"type": "stop"})
        return self._send({"type": "reset_joint", "positions": target.tolist()})

    def cmd_bird_eye_view(self):
        if self.flex is not None:
            return self.flex.bird_eye(self)
        if not self._arm_ready():
            log.warning("BIRD-EYE waiting for URDF/joints")
            return False
        with self._lock:
            self._pull_out_state = None
            q_now = self.joint_positions.copy()
            eff = max(1e-6, self.flex_arc_len - FLEX_SEG_LEN)
            theta_t = self.kin.solve_bird_eye_bend_angle(
                q=q_now, target_horizon_deg=BIRD_EYE_HORIZON_DEG,
                max_bend_rad=self.flex_max_bend)
            self._set_flex_slew_target(curvature=float(theta_t/eff), steer_yaw=0.)
        self._update_flex_state()
        log.info(f"BIRD-EYE {math.degrees(theta_t):.1f} deg")
        return True

    @property
    def pull_out_active(self):
        return self._pull_out_state is not None or (self.flex is not None and self.flex.procedure is not None)

    def cmd_pull_out(self):
        if self.flex is not None:
            return self.flex.pull_out(self)
        if self._pull_out_state is not None:
            if self._pull_out_state['stage'] != 'confirm':
                raise ValueError('Pull Out is already moving. Wait for the ready indication.')
            if not self._model_pull_out_ready(self._pull_out_state):
                raise ValueError('The zero pose changed. Start Pull Out again.')
            self._pull_out_state['stage'] = 'retract'
            log.info('PULL OUT confirmed; retracting')
            return True
        if not self._arm_ready():
            return False
        if self._arm_moving is not False or any(self._key_state.values()):
            raise ValueError('Release the controls and wait for the UR5 to stop before starting Pull Out.')
        with self._lock:
            self._pull_out_complete_joints = None
            rcm_on = (self.arm_mode == 'rcm' and self.rcm_enabled)
            rcm_pt = None if self.rcm_world_point is None else self.rcm_world_point.copy()
            ref_axis = None if self.rcm_ref_axis is None else self.rcm_ref_axis.copy()
            q_now = self.joint_positions.copy()
        if not (rcm_on and rcm_pt is not None and ref_axis is not None):
            log.warning("PULL OUT requires RCM mode")
            return False
        try:
            axis_cur, _, p_tip_cur = self.kin.tube_axis_in_base(q_now)
            p_end_cur = self.kin.end_cylinder_in_base(q_now)
        except Exception as e:
            log.warning(f"PULL OUT FK failed: {e}")
            return False
        d_tip = float(np.linalg.norm(p_tip_cur - rcm_pt))
        if d_tip < 0.05:
            d_tip = float(self.kin.tube_length)
        tip_from_end = float(np.linalg.norm(p_tip_cur - p_end_cur))
        with self._lock:
            self._set_flex_slew_target(curvature=0., steer_yaw=0.)
            self._pull_out_state = {
                'stage':'align','axis_cmd': axis_cur.copy(),
                'd_tip_align': d_tip,'tip_from_end': tip_from_end,
                'end': PULL_OUT_BEHIND_RCM_M,
                's_end_cmd': None,'waiting_logged': False}
        self._update_flex_state()
        log.info("PULL OUT started (align stage)")
        return True

    def _step_pull_out(self):
        if not self._arm_ready():
            self._pull_out_state = self._flex_slew_target = None
            return
        with self._lock:
            st = self._pull_out_state
            if st is None:
                return
            if not (self.arm_mode == 'rcm' and self.rcm_enabled
                    and self.rcm_world_point is not None
                    and self.rcm_ref_axis is not None):
                self._pull_out_state = None
                self._flex_slew_target = None
                return
            rcm_pt = self.rcm_world_point.copy()
            ref_axis = self.rcm_ref_axis.copy()
            q_now = self.joint_positions.copy()
            self._set_flex_slew_target(curvature=0., steer_yaw=0.)
        if st['stage'] == 'confirm':
            if not self._model_pull_out_ready(st):
                self._pull_out_state = self._flex_slew_target = None
                log.warning('PULL OUT stopped: zero pose changed or confirmation timed out')
            return
        try:
            axis_cur, _, p_tip_cur = self.kin.tube_axis_in_base(q_now)
            p_end_cur = self.kin.end_cylinder_in_base(q_now)
        except Exception as e:
            log.warning(f"PULL OUT FK: {e}")
            with self._lock:
                self._pull_out_state = None
                self._flex_slew_target = None
            return

        if st['stage'] == 'align':
            axis_cmd = st['axis_cmd']
            err_cmd = math.acos(float(np.clip(np.dot(axis_cmd, ref_axis), -1., 1.)))
            step = min(math.radians(self.ARM_TILT_VEL_DEG_S),
                       self.arm_linear_speed_m_s / max(st['d_tip_align'], 1e-6)) * self.dt
            if err_cmd <= step or err_cmd < 1e-3:
                axis_next = ref_axis.copy()
            else:
                rk = np.cross(axis_cmd, ref_axis)
                kn = float(np.linalg.norm(rk))
                axis_next = (ref_axis.copy() if kn < 1e-9
                             else self.kin.rotate_about(axis_cmd, rk/kn, step))
                axis_next /= max(float(np.linalg.norm(axis_next)), 1e-9)
            st['axis_cmd'] = axis_next
            err_actual = math.degrees(math.acos(float(np.clip(np.dot(axis_cur, ref_axis), -1., 1.))))
            aligned = (np.allclose(axis_next, ref_axis, atol=1e-3) and err_actual < 1.0)
            if not aligned:
                p_tip_t = rcm_pt + st['d_tip_align']*axis_next
                q_t = self.kin.ik_along_axis(p_tip_t, axis_next, q_now)
                if q_t is not None:
                    q_t = self._auto_solve_wrist1(q_t)
                    self._send_servo(q_t, q_now, self.dt, validate=self._rcm_pose_is_valid)
                return
            if not self._flex_is_straight():
                if not st.get('waiting_logged', False):
                    log.info("PULL OUT aligned; waiting for flex straight")
                    st['waiting_logged'] = True
                return
            st['waiting_logged'] = False
            if self._arm_moving is not False or self._teleop_servo_active:
                return
            s_end_cur = float(np.dot(p_end_cur - rcm_pt, ref_axis))
            st['stage'] = 'confirm'
            st['deadline'] = time.monotonic() + POSITION_TIMEOUT_S
            st['confirm_depth'] = float(np.dot(p_tip_cur - rcm_pt, ref_axis))
            st['s_end_cmd'] = s_end_cur
            log.info('PULL OUT at zero. Verify straightness and path, then press PULL OUT again.')
            return

        if st['stage'] == 'retract':
            s_end_cur = float(np.dot(p_end_cur - rcm_pt, ref_axis))
            if s_end_cur >= st['end'] - 0.003:
                if self._arm_moving is not False or self._teleop_servo_active:
                    return
                with self._lock:
                    self._pull_out_complete_joints = q_now.copy()
                    self._pull_out_state = None
                    self._flex_slew_target = None
                log.info(f"PULL OUT complete ({s_end_cur*100:.1f} cm behind RCM)")
                return
            if st['s_end_cmd'] is None: st['s_end_cmd'] = s_end_cur
            step_m = min(PULL_OUT_RETRACT_SPEED_M_S, self.arm_linear_speed_m_s) * self.dt
            s_end_next = min(st['end'], st['s_end_cmd'] + step_m)
            st['s_end_cmd'] = s_end_next
            p_tip_t = rcm_pt + (st['tip_from_end'] + s_end_next)*ref_axis
            q_t = self.kin.ik_along_axis(p_tip_t, ref_axis, q_now)
            if q_t is not None:
                q_t = self._auto_solve_wrist1(q_t)
                self._send_servo(q_t, q_now, self.dt, validate=self._rcm_pose_is_valid)

    def _model_pull_out_ready(self, st):
        if (not self._arm_ready() or not self.rcm_enabled or self.arm_mode != 'rcm'
                or self._arm_moving is not False or not self._flex_is_straight()
                or self._teleop_servo_active or any(self._key_state.values())
                or time.monotonic() >= st['deadline']):
            return False
        axis, _, tip = self.kin.tube_axis_in_base(self.joint_positions)
        return (float(np.dot(axis, self.rcm_ref_axis)) > math.cos(math.radians(1.))
                and self._rcm_pivot_is_valid(axis, tip, st['confirm_depth']))

    def _cancel_motion(self, *, include_flex=True):
        if include_flex and self.flex is not None:
            self.flex_stop_delivered = self.flex.stop()
        for key in self._key_state:
            if include_flex or key.startswith('arm_'):
                self._key_state[key] = False
                self._key_updated.pop(key, None)
        self._pull_out_state = None
        self._pull_out_complete_joints = None
        if include_flex:
            self._flex_slew_target = None
        self._teleop_servo_active = False
        self._q_cmd = None

    def cmd_stop(self):
        self.cancel_motion()
        arm_ok = self._send({"type": "stop"}) if self._recv_thread and self._recv_thread.ident else True
        return arm_ok and (self.flex is None or self.flex_stop_delivered)

    def cmd_interrupt(self, interrupt_arm):
        self.cancel_motion()
        self._model_horizon_paused = True
        if not interrupt_arm:
            return None
        with self._lock:
            self._interrupt_id += 1
            request = self._interrupt_id
            result = self._interrupt_result = Future()
        if not self._send({"type": "interrupt", "request": request}):
            with self._lock:
                self._finish_arm_interrupt("The UR5 relay did not receive INTERRUPT.")
        return result

    def _finish_arm_interrupt(self, error):
        if self._interrupt_result is not None:
            self._interrupt_result.set_result(error)
            self._interrupt_result = None

    def cancel_motion(self):
        with self._lock:
            self._cancel_motion()

    # ---------- transport ----------
    def _rcm_constraint(self):
        if self.rcm_enabled and self.rcm_ref_axis is not None:
            return {"axis": self.rcm_ref_axis.tolist(), "pivot": self.rcm_world_point.tolist(),
                    "half_angle": self.rcm_cone_half_angle}
        return None

    def _send_arm_jog(self, kind, direction):
        scale = min(1., self.jog_speed_scale / DEFAULT_JOG_SPEED_SCALE)
        command = {"type": kind, "direction": [component * scale for component in direction]}
        with self._lock:
            if not self._arm_ready():
                return
            rcm = self._rcm_constraint()
            if self.arm_mode == 'rcm':
                if rcm is None:
                    return
                command["rcm"] = rcm
        if self._send(command):
            with self._lock:
                # The relay owns the joint reference for held jogs.
                self._q_cmd = None
                self._teleop_servo_active = True
                self._servo_sent_this_tick = True

    def _servo_reference(self, measured):
        reference = self._q_cmd
        if reference is None or np.max(np.abs(reference - measured)) > self.TELEOP_RESYNC_RAD:
            return measured
        return reference

    def _send_servo(self, q_target, q_now, dt, validate=None, *, validate_start=None):
        q_target = np.asarray(q_target, float)
        if q_target.shape != (6,) or not np.isfinite(q_target).all():
            return False

        with self._lock:
            if not self._arm_ready():
                return False
            q_base = self._servo_reference(q_now)
            rcm = self._rcm_constraint() if validate is not None else None
        q_target = limit_servo_target(self.kin, q_base, q_target, q_now, dt,
                                      self.arm_linear_speed_m_s)

        if validate is not None and (not (validate_start or validate)(q_now) or not validate(q_target)):
            return False
        command = {"type": "servo_joint", "positions": q_target.tolist()}
        if rcm is not None:
            command["rcm"] = rcm
        if not self._send(command):
            return False
        with self._lock:
            self._q_cmd = q_target.copy()
            self._teleop_servo_active = True
            self._servo_sent_this_tick = True
        return True

    def _send_servo_stop(self):
        with self._lock:
            self._teleop_servo_active = False
            self._q_cmd = None
        self._send({"type": "stop"})

    # ---------- key state ----------
    def set_key(self, key, down):
        if self.flex is not None and key in FLEX_KEYS:
            if down and any(self._key_state.values()):
                return False
            if down and (self.flex.tracking or self.flex.procedure) and self._teleop_servo_active:
                self._send_servo_stop()
            return self.flex.set_key(key, down)
        if key not in self._key_state:
            return False
        if self.flex is not None and key.startswith("flex_"):
            return False
        if down and key.startswith("arm_") and not self._arm_ready():
            return False
        if down and self.flex is not None:
            if self.flex.tracking or self.flex.procedure or self.flex.pending or self.flex.keys:
                self.flex.cancel("Coordinated motion stopped · manual arm input")
                if self._teleop_servo_active:
                    self._send_servo_stop()
            if key.startswith("arm_"):
                self.flex.resume_horizon()
        self._key_state[key] = down
        if down:
            self._model_horizon_paused = False
            self._key_updated[key] = time.monotonic()
        else:
            self._key_updated.pop(key, None)
        return True

    def refresh_keys(self, held):
        if self.flex is not None:
            self.flex.heartbeat(held)
        now = time.monotonic()
        for key in held:
            if key in self._key_updated and now - self._key_updated[key] <= KEY_TIMEOUT_S:
                self._key_updated[key] = now

    # ---------- state snapshot ----------
    def _procedure_status(self, q, theta_total):
        bird = 'unavailable'
        pitch = None
        if q is not None and self.kin.ready and self._motion_fault is None:
            if self.flex is not None:
                s = self.flex.status
                if self.flex.ready():
                    rotation = self.kin.physical_flex_camera_rotation(q, s.bend_x, s.bend_y, s.roll)
                    pitch = math.degrees(math.asin(float(np.clip(-rotation[2, 2], -1., 1.))))
                    level_error = self.flex._horizon_error(self, q)
                    moving = bool(self.flex.pending and self.flex.pending[1] == POSITION)
                    settled = not s.moving and self._arm_moving is False
                else:
                    rotation = None
            else:
                bend = math.degrees(theta_total)
                rotation = self.kin.physical_flex_camera_rotation(
                    q, -bend * math.sin(self.flex_ext_yaw), bend * math.cos(self.flex_ext_yaw),
                    math.degrees(self.flex_base_yaw))
                pitch = math.degrees(math.asin(float(np.clip(-rotation[2, 2], -1., 1.))))
                level_error = math.degrees(math.asin(float(np.clip(rotation[2, 1], -1., 1.))))
                moving = self._flex_slew_target is not None and not self.pull_out_active
                settled = not moving and not any(self._key_state.values()) and self._arm_moving is False
            if rotation is not None:
                at_goal = (abs(pitch - BIRD_EYE_HORIZON_DEG) <= BIRD_EYE_TOLERANCE_DEG
                           and abs(level_error) <= BIRD_EYE_TOLERANCE_DEG and settled)
                bird = 'moving' if moving else 'at_position' if at_goal else 'idle'
        if self.pull_out_active and (q is None or self._motion_fault is not None):
            pull_out = 'unavailable'
        elif self.flex is not None:
            pull_out = self.flex.pull_out_state(self)
        elif self._pull_out_state:
            pull_out = {'align': 'preparing', 'confirm': 'ready', 'retract': 'retracting'}[self._pull_out_state['stage']]
        elif (q is not None and self._pull_out_complete_joints is not None
              and self._arm_moving is False and self._flex_is_straight()
              and np.max(np.abs(q - self._pull_out_complete_joints)) < math.radians(.1)):
            pull_out = 'complete'
        else:
            pull_out = 'idle'
        return {'bird_eye_state': bird, 'camera_pitch_down_deg': pitch, 'pull_out_state': pull_out}

    def get_state_snapshot(self):
        """HTTP readers never wait for motion planning or device communication."""
        with self._snapshot_lock:
            if self._running.is_set() and time.monotonic() - self._snapshot_at > STATE_TIMEOUT_S:
                raise TimeoutError("Control state is stale; waiting for the coordinator.")
            return {**self._snapshot, 'snapshot_monotonic_s': self._snapshot_at}

    def get_state_dict(self):
        with self._lock:
            fresh = self._joint_state_fresh()
            q = self.joint_positions.copy() if fresh else None
            tip_angles = rcm_insertion_m = rcm_outside = None
            if q is not None and self.kin.ready and self.arm_mode == 'rcm' and self.rcm_enabled:
                axis, _, tip = self.kin.tube_axis_in_base(q)
                offset = tip - self.rcm_world_point
                tip_angles = rcm_tip_angles(axis, self.rcm_ref_axis).tolist()
                rcm_outside = not self._inside_rcm_cone(axis, self.rcm_ref_axis)
                # FWD inserts along the negative shaft axis. A pure tilt leaves this axial depth unchanged.
                rcm_insertion_m = self.rcm_ref_depth - float(np.dot(offset, axis))
            eff = max(1e-6, self.flex_arc_len - FLEX_SEG_LEN)
            theta_total = min(self.flex_max_bend, eff*max(0., self.flex_curvature))
            state = {
                'arm_mode': self.arm_mode,
                'rcm_enabled': self.rcm_enabled,
                'rcm_point': None if self.rcm_world_point is None else self.rcm_world_point.tolist(),
                'rcm_ref_axis': None if self.rcm_ref_axis is None else self.rcm_ref_axis.tolist(),
                'rcm_ref_depth_m': self.rcm_ref_depth,
                'rcm_cone_deg': round(math.degrees(self.rcm_cone_half_angle)),
                'rcm_cone_max_deg': MAX_RCM_CONE_DEG,
                'arm_linear_speed_mm_s': self.arm_linear_speed_m_s * 1000.,
                'pull_out_tilt_speed_deg_s': self.ARM_TILT_VEL_DEG_S,
                'jog_speed_scale': self.jog_speed_scale,
                'rcm_tip_angles': tip_angles,
                'rcm_outside_cone': rcm_outside,
                'rcm_insertion_m': rcm_insertion_m,
                'pull_out_active': self.pull_out_active,
                'joints_deg': (np.rad2deg(q).tolist() if q is not None else None),
                'flex_base_yaw': math.degrees(self.flex_base_yaw),
                'flex_arc_len':  self.flex_arc_len,
                'flex_ext_yaw':  math.degrees(self.flex_ext_yaw),
                'flex_curvature': self.flex_curvature,
                'flex_theta_total_deg': math.degrees(theta_total),
                'flex_theta_seg_deg':   math.degrees(theta_total/(FLEX_N-1)),
                'flex_model_bend_limit': round(math.degrees(self.flex_max_bend)),
                'flex_model_bend_max_deg': round(math.degrees(FLEX_MAX_BEND)),
                'flex_model_speed_deg_s': self.flex_model_speed_deg_s,
                'relay_connected':     self._sock is not None,
                'joints_received':     fresh,
                'arm_moving':          self._arm_moving if fresh else None,
                'kinematics_ready':    self.kin.ready,
                'motion_fault':        self._motion_fault,
            }
            state['endoscope_link_pose_base'] = None
            if q is not None and self.kin.ready:
                rotation, position = self.kin.fk_matrix(q)
                state['endoscope_link_pose_base'] = {
                    'position_m': position.tolist(), 'rotation_matrix': rotation.tolist()}
            alpha = self.flex_base_yaw + self.flex_ext_yaw
            bend = state['flex_theta_total_deg']
            state['flex_model_bend_point'] = [bend * math.sin(alpha), -bend * math.cos(alpha)]
            tendons = {}
            for phi_deg, name in FLEX_TENDONS:
                phi = math.radians(phi_deg)
                dL = -FLEX_TENDON_R*math.cos(phi - alpha)*theta_total
                tendons[name] = (self.flex_arc_len + dL, dL)
            state['tendons'] = {n:(L*1000, dL*1000) for n,(L,dL) in tendons.items()}
            if self.flex is not None:
                state['flex_hardware'] = self.flex.snapshot()
            state.update(self._procedure_status(q, theta_total))
        return state
