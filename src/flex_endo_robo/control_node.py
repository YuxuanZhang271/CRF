#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run EndoFlex kinematics and send joint targets to the local RTDE relay."""
import json
import logging
import math
import socket
import threading
import time

import numpy as np

if __package__:
    from .kinematics import EndoscopeKinematics, HOME_DEG
else:
    from kinematics import EndoscopeKinematics, HOME_DEG

# ---------- copy of flex constants ----------
FLEX_N          = 8
FLEX_SEG_LEN    = 0.008
FLEX_ARC_MIN    = 0.030
FLEX_ARC_MAX    = 0.050
FLEX_MAX_BEND   = math.pi / 2.0
FLEX_TENDON_R   = 0.004
FLEX_TENDONS    = [(0,'R'), (90,'G'), (180,'B'), (270,'Y')]
FLEX_STRAIGHT_KAPPA_TOL = 1e-3
BIRD_EYE_HORIZON_DEG = 30.0
PULL_OUT_BEHIND_RCM_M = 0.10

TRACK_L_EFF            = FLEX_ARC_MAX - FLEX_SEG_LEN
TRACK_TIP_TO_FLEX_BASE = 0.59995
TRACK_W_SCALE          = 1e-6
TRACK_RECOVER_BEND_DEG = 55.0
TRACK_RECOVER_DONE_DEG = 50.0

TRACK_TARGETS = [
    ('LN_No3',  'LN3 Red', (1.00, 0.15, 0.15, 0.96)),
    ('LN_No4d', 'LN4d Grn', (0.12, 0.92, 0.25, 0.96)),
    ('LN_No6',  'LN6 Blu', (0.15, 0.48, 1.00, 0.96)),
    ('LGA_No7', 'LGA7 Dyn', (1.00, 0.86, 0.05, 0.98)),
]

log = logging.getLogger("ControlNode")
STATE_TIMEOUT_S = 0.3
KEY_TIMEOUT_S = 2.0
MAX_LINE_BYTES = 4096
DEFAULT_JOG_SPEED_SCALE = 10
JOG_SPEED_SCALES = (3, 5, DEFAULT_JOG_SPEED_SCALE, 15)

class ControlNode:
    """Non-ROS port of Ur5EndoFlexNode."""

    STEP_TICK_HZ        = 125.0          # ★ 与 servo_thread 同步
    ARM_LINEAR_VEL      = 0.025
    ARM_AXIS_VEL        = 0.025
    ARM_TILT_VEL_DEG_S  = 4.0
    FLEX_YAW_VEL        = 1.0
    FLEX_CURV_VEL       = 10.0
    FLEX_ARC_VEL        = 0.020
    TRAJ_DURATION_S     = 3.0
    RCM_CONE_HALF_ANGLE = math.radians(20.0)
    TELEOP_RESYNC_RAD   = 0.05

    # ★ 位置伺服单 tick 的最大关节速度限制（rad/s）；防止 IK 偶发跳变
    SERVO_MAX_JOINT_VEL = 2.0

    def __init__(self, relay_host, relay_port, urdf_path,
                 rcm_point_local=(0,0,-0.30),
                 tip_local=(0,0,0.20195),
                 standoff_m=0.105,
                 flex_state_callback=None,
                 jog_speed_scale=DEFAULT_JOG_SPEED_SCALE):
        if jog_speed_scale not in JOG_SPEED_SCALES:
            choices = ", ".join(map(str, JOG_SPEED_SCALES))
            raise ValueError(f"jog speed scale must be one of {choices}")
        self.dt = 1.0 / self.STEP_TICK_HZ
        self.jog_speed_scale = jog_speed_scale
        self.relay_host  = relay_host
        self.relay_port  = relay_port
        self.standoff    = standoff_m
        self.flex_state_callback = flex_state_callback

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
        self.joint_received  = False
        self._last_joint_at = float("-inf")
        self._motion_fault = None

        self.rcm_enabled      = False
        self.rcm_world_point  = None
        self.rcm_ref_axis     = None
        self.rcm_ref_depth    = None
        self.arm_mode         = 'linear'
        self.flex_mode        = 'fixed'

        self._q_cmd       = None
        self._teleop_servo_active = False       # ★ 位置伺服活跃标记
        self._servo_sent_this_tick = False

        self.flex_steer_yaw = 0.0
        self.flex_base_yaw  = 0.0
        self.flex_ext_yaw   = 0.0
        self.flex_arc_len   = FLEX_ARC_MAX
        self.flex_curvature = 0.0

        self._pull_out_state  = None
        self._flex_slew_target = None

        self._track_target      = None
        self._targets_state     = {}
        self._track_warned      = False
        self._track_locked      = False
        self._track_dist_m      = None
        self._track_aim_deg     = None
        self._track_zone        = None
        self._track_recover     = False
        self._track_status_msg  = None

        self._key_state = {
            'arm_up':False,'arm_down':False,'arm_left':False,'arm_right':False,
            'arm_fwd':False,'arm_bwd':False,
            'flex_w':False,'flex_s':False,'flex_a':False,'flex_d':False,
            'flex_r':False,'flex_f':False,
        }
        self._key_updated = {}

        # --- TCP ---
        self._sock         = None
        self._send_lock    = threading.Lock()
        self._recv_thread  = None
        self._tick_thread  = None
        self._running      = threading.Event()
        self._shutdown     = threading.Event()

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
                    if not isinstance(msg, dict) or msg.get("type") != "robot_state":
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
    def start(self):
        if self._running.is_set():
            return
        self._shutdown.clear()
        self._running.set()
        self._recv_thread = threading.Thread(target=self._connect, daemon=True,
                                             name="tcp-recv")
        self._tick_thread = threading.Thread(target=self._tick_loop, daemon=True,
                                             name="ctrl-tick")
        self._recv_thread.start()
        self._tick_thread.start()
        log.info(f"ControlNode started @ {self.STEP_TICK_HZ:.0f} Hz")

    def stop(self):
        with self.control_lock:
            self._running.clear()
            self._shutdown.set()
            with self._lock:
                sock = self._sock
            if sock is not None:
                self.cmd_stop()
                self._disconnect_socket(sock)
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
                sleep_t = next_t - now
                if sleep_t > 0.002:
                    time.sleep(sleep_t - 0.001)
                while time.monotonic() < next_t:
                    pass
            next_t += self.dt
            if next_t < time.monotonic():
                next_t = time.monotonic()
            try:
                with self.control_lock:
                    if self._running.is_set():
                        self._tick()
            except Exception as e:
                log.exception(f"tick error: {e}")

    # ==================================================================
    # Tick / helpers
    # ==================================================================

    def _tick(self):
        with self._lock:
            fault = self._motion_fault
        if fault and (self._teleop_servo_active or self._pull_out_state is not None
                      or self._track_target is not None):
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
                self._pull_out_state = None; self._flex_slew_target = None
                pull_out_active = False
                log.info("PULL OUT cancelled by manual key input.")
            tracking_active = self._track_target is not None
            if tracking_active and any_manual_key:
                self._clear_track_state_unlocked(); self._flex_slew_target = None
                tracking_active = False
                log.info("TRACKING cancelled by manual key input.")
            if any_manual_key and self._flex_slew_target is not None:
                self._flex_slew_target = None
                log.info("FLEX slew cancelled by manual key input.")
            if tracking_active and self._flex_slew_target is not None:
                self._flex_slew_target = None

        servo_was_active = self._teleop_servo_active
        self._servo_sent_this_tick = False
        try:
            self._step_controls(ks, pull_out_active, tracking_active)
        finally:
            if servo_was_active and not self._servo_sent_this_tick:
                self._send_servo_stop()

    def _step_controls(self, ks, pull_out_active, tracking_active):
        self._step_flex_slew()

        if pull_out_active:
            self._step_pull_out(); self._update_flex_state(); return
        if tracking_active:
            self._step_hybrid_rcm_tracking(); self._update_flex_state(); return

        arm_dy = arm_dz = arm_tilt_right = arm_tilt_up = 0.0
        with self._lock: mode = self.arm_mode

        jog_dt = self.dt * self.jog_speed_scale
        vertical = ks['arm_up'] - ks['arm_down']
        horizontal = ks['arm_right'] - ks['arm_left']
        arm_dx = (ks['arm_bwd'] - ks['arm_fwd']) * self.ARM_AXIS_VEL * jog_dt
        if mode == 'rcm':
            arm_tilt_up = vertical * self.ARM_TILT_VEL_DEG_S * jog_dt
            arm_tilt_right = horizontal * self.ARM_TILT_VEL_DEG_S * jog_dt
        else:
            arm_dz = vertical * self.ARM_LINEAR_VEL * jog_dt
            arm_dy = horizontal * self.ARM_LINEAR_VEL * jog_dt

        if arm_tilt_up or arm_tilt_right:
            self._arm_rcm_tilt(arm_tilt_right, arm_tilt_up, insertion_m=arm_dx)
        elif arm_dx or arm_dy or arm_dz:
            self._arm_linear_step(arm_dx, arm_dy, arm_dz)

        with self._lock:
            dk = (ks['flex_s'] - ks['flex_w']) * self.FLEX_CURV_VEL * self.dt
            dy = (ks['flex_a'] - ks['flex_d']) * self.FLEX_YAW_VEL * self.dt
            da = (ks['flex_r'] - ks['flex_f']) * self.FLEX_ARC_VEL * self.dt
            self.flex_arc_len = min(FLEX_ARC_MAX, max(FLEX_ARC_MIN, self.flex_arc_len + da))
            effective_length = self.flex_arc_len - FLEX_SEG_LEN
            self.flex_curvature = min(FLEX_MAX_BEND / effective_length,
                                      max(0.0, self.flex_curvature + dk))
            self.flex_steer_yaw = (self.flex_steer_yaw + dy + math.pi) % (2 * math.pi) - math.pi
        self._update_flex_state()

    def _arm_ready(self):
        with self._lock:
            return self.kin.ready and self._motion_fault is None and self._joint_state_fresh()

    def _joint_state_fresh(self):
        return (self.joint_received
                and time.monotonic() - self._last_joint_at <= STATE_TIMEOUT_S)

    def _arm_linear_step(self, dx_m, dy_m, dz_m):
        if not self._arm_ready(): return

        with self._lock:
            q_now = self.joint_positions.copy()
            rcm_on = self.rcm_enabled
            rcm_pt = None if self.rcm_world_point is None else self.rcm_world_point.copy()
        try:
            axis, _, p_tip = self.kin.tube_axis_in_base(q_now)
        except Exception as e:
            log.warning(f"FK failed in linear step: {e}"); return

        world_z = np.array([0.,0.,1.])
        tube_axis = axis / max(np.linalg.norm(axis), 1e-9)
        proj = tube_axis - np.dot(tube_axis, world_z)*world_z
        pn = np.linalg.norm(proj)
        if pn < 1e-6:
            left = np.array([0.,1.,0.])
        else:
            proj = proj/pn
            left = np.cross(world_z, proj)
            ln = np.linalg.norm(left)
            left = np.array([0.,1.,0.]) if ln < 1e-9 else left/ln

        delta = world_z*dz_m + left*dy_m + tube_axis*dx_m
        if np.linalg.norm(delta) < 1e-9: return
        p_target = p_tip + delta
        q_target = (self.kin.ik_with_rcm(p_target, rcm_pt, q_now)
                    if rcm_on and rcm_pt is not None
                    else self.kin.ik_position_only(p_target, q_now))
        if q_target is None: return
        q_target = self._auto_solve_wrist1(q_target)
        self._send_servo(q_target, q_now, self.dt)

    def _arm_rcm_tilt(self, angle_right_deg, angle_up_deg, insertion_m=0.0):
        if not self._arm_ready(): return

        with self._lock:
            rcm_on  = self.rcm_enabled
            rcm_pt  = None if self.rcm_world_point is None else self.rcm_world_point.copy()
            ref_axis = None if self.rcm_ref_axis is None else self.rcm_ref_axis.copy()
            q_now = self.joint_positions.copy()
        if not rcm_on or rcm_pt is None or ref_axis is None: return
        try: axis, _, p_tip = self.kin.tube_axis_in_base(q_now)
        except Exception: return
        distance = float(np.linalg.norm(p_tip - rcm_pt))
        if distance < 1e-6: return
        z_world = np.array([0.,0.,1.])
        new_axis = axis.copy()
        if angle_up_deg != 0.0:
            theta = math.radians(float(angle_up_deg))
            a_h = np.array([new_axis[0], new_axis[1], 0.])
            n = np.linalg.norm(a_h)
            pitch_axis = np.array([1.,0.,0.]) if n < 1e-6 else np.cross(a_h/n, z_world)
            pitch_axis /= max(np.linalg.norm(pitch_axis), 1e-9)
            new_axis = self.kin.rotate_about(new_axis, pitch_axis, -theta)
            new_axis /= max(np.linalg.norm(new_axis), 1e-9)
        if angle_right_deg != 0.0:
            theta = math.radians(float(angle_right_deg))
            new_axis = self.kin.rotate_about(new_axis, z_world, -theta)
            new_axis /= max(np.linalg.norm(new_axis), 1e-9)
        cos_a = float(np.clip(np.dot(new_axis, ref_axis), -1., 1.))
        if math.acos(cos_a) > self.RCM_CONE_HALF_ANGLE:
            if not insertion_m:
                return
            new_axis = (p_tip - rcm_pt) / distance
        if distance + insertion_m < 1e-6:
            return
        p_target = rcm_pt + (distance + insertion_m) * new_axis
        q_target = self.kin.ik_with_rcm(p_target, rcm_pt, q_now)
        if q_target is None: return
        q_target = self._auto_solve_wrist1(q_target)
        self._send_servo(q_target, q_now, self.dt)

    def _auto_solve_wrist1(self, q):
        q = np.array(q, float).copy()
        q[3] = math.radians(-270.0) - q[1] - q[2]
        return q

    # ---------- flex ----------
    def _set_flex_slew_target(self, curvature=None, steer_yaw=None, arc_len=None):
        self._flex_slew_target = {
            'curvature': self.flex_curvature if curvature is None else float(curvature),
            'steer_yaw': self.flex_steer_yaw if steer_yaw is None else float(steer_yaw),
            'arc_len':   self.flex_arc_len   if arc_len is None   else float(arc_len),
        }

    def _step_flex_slew(self):
        with self._lock:
            tgt = self._flex_slew_target
            if tgt is None: return
            a = self.flex_arc_len
            a_t = min(max(FLEX_ARC_MIN, tgt['arc_len']), FLEX_ARC_MAX)
            step_a = self.FLEX_ARC_VEL*self.dt
            a_new = a_t if abs(a_t-a) <= step_a else a + math.copysign(step_a, a_t-a)
            self.flex_arc_len = min(max(FLEX_ARC_MIN, a_new), FLEX_ARC_MAX)
            c = self.flex_curvature
            eff = max(1e-6, self.flex_arc_len - FLEX_SEG_LEN)
            kmax = FLEX_MAX_BEND/eff
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
            y_err = ((self.flex_steer_yaw - y_t + math.pi) % (2*math.pi)) - math.pi
            if (abs(self.flex_curvature - c_t) <= 1e-6
                    and abs(y_err) <= 1e-6
                    and abs(self.flex_arc_len - a_t) <= 1e-6):
                self._flex_slew_target = None

    def _flex_is_straight(self):
        with self._lock:
            return abs(self.flex_curvature) <= FLEX_STRAIGHT_KAPPA_TOL

    def _update_auto_horizon_twist_unlocked(self, theta_total):
        if self.kin.ready and self._joint_state_fresh():
            try:
                by, ey = self.kin.solve_horizon_flex_yaw(
                    q=self.joint_positions, steer_yaw=self.flex_steer_yaw,
                    theta_total=theta_total, flex_mode=self.flex_mode)
                self.flex_base_yaw = float(by); self.flex_ext_yaw = float(ey); return
            except Exception as e:
                log.warning(f"auto horizon twist failed: {e}")
        if self.flex_mode == 'fixed':
            self.flex_base_yaw = float(self.flex_steer_yaw)
            self.flex_ext_yaw  = float(-self.flex_steer_yaw)
        else:
            self.flex_base_yaw = float(self.flex_steer_yaw)
            self.flex_ext_yaw  = 0.0

    def _update_flex_state(self):
        with self._lock:
            eff = max(1e-6, self.flex_arc_len - FLEX_SEG_LEN)
            theta_total = min(FLEX_MAX_BEND, eff * max(0.0, self.flex_curvature))
            self._update_auto_horizon_twist_unlocked(theta_total)
            flex_snapshot = {
                'base_yaw':  float(self.flex_base_yaw),
                'ext_yaw':   float(self.flex_ext_yaw),
                'arc_len':   float(self.flex_arc_len),
                'curvature': float(self.flex_curvature),
                'theta_total': float(theta_total),
                'theta_seg':   float(theta_total / (FLEX_N - 1)),
                'd_seg':       float(eff / (FLEX_N - 1)),
                'steer_yaw':   float(self.flex_steer_yaw),
            }
        if self.flex_state_callback is not None:
            try: self.flex_state_callback(flex_snapshot)
            except Exception as e: log.warning(f"flex_state_callback raised: {e}")

    # ---------- modes ----------
    def toggle_arm_mode(self):
        with self._lock:
            if self.arm_mode == 'linear' and not self._arm_ready():
                return False
            self._pull_out_state = None
            self._flex_slew_target = None
            self._q_cmd = None
            tracking_was = self._track_target is not None
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
                if tracking_was:
                    self._clear_track_state_unlocked()
                    log.warning("TRACKING auto-cancelled (RCM off)")
        return True

    def _capture_rcm_unlocked(self):
        if not (self.kin.ready and self.joint_received):
            self.rcm_enabled = False; return
        q = self.joint_positions.copy()
        axis, p_rcm, p_tip = self.kin.tube_axis_in_base(q)
        self.rcm_world_point = p_rcm
        self.rcm_ref_axis = axis
        self.rcm_ref_depth = float(np.dot(p_tip - p_rcm, axis))
        self.rcm_enabled = True

    def toggle_flex_mode(self):
        with self._lock:
            self.flex_mode = 'free' if self.flex_mode == 'fixed' else 'fixed'
            m = self.flex_mode
            if m != 'fixed' and self._track_target is not None:
                self._clear_track_state_unlocked()
                log.warning("TRACKING auto-cancelled (flex not fixed)")
        self._update_flex_state()
        log.info(f"Flex mode -> {m}")

    # ---------- semi-auto ----------
    def cmd_home(self):
        if not self._arm_ready():
            return False
        with self._lock:
            self._cancel_motion()
            self._set_flex_slew_target(curvature=0., steer_yaw=0., arc_len=FLEX_ARC_MAX)
        self._update_flex_state()
        q_target = np.deg2rad(np.array(HOME_DEG, float))
        q_target = self._auto_solve_wrist1(q_target)
        return self._send_trajectory(q_target, traj_duration=self.TRAJ_DURATION_S)

    def cmd_reset(self):
        if not self._arm_ready():
            return False
        with self._lock:
            self._pull_out_state = None
            self._flex_slew_target = None
            tracking_was = self._track_target is not None
            self._clear_track_state_unlocked()
            self.arm_mode = 'linear'
            self.flex_mode = 'fixed'
            self.rcm_enabled = False
            self.rcm_world_point = None
            self.rcm_ref_axis = None
            self.rcm_ref_depth = None
        if tracking_was: log.warning("TRACKING auto-cancelled (RESET)")
        return self.cmd_home()

    def cmd_bird_eye_view(self):
        if not self._arm_ready():
            log.warning("BIRD-EYE waiting for URDF/joints")
            return False
        with self._lock:
            self._pull_out_state = None
            q_now = self.joint_positions.copy()
            eff = max(1e-6, self.flex_arc_len - FLEX_SEG_LEN)
            theta_t = self.kin.solve_bird_eye_bend_angle(
                q=q_now, target_horizon_deg=BIRD_EYE_HORIZON_DEG,
                max_bend_rad=FLEX_MAX_BEND)
            self._set_flex_slew_target(curvature=float(theta_t/eff), steer_yaw=0.)
        self._update_flex_state()
        log.info(f"BIRD-EYE {math.degrees(theta_t):.1f} deg")
        return True

    def cmd_pull_out(self):
        if not self._arm_ready():
            return False
        with self._lock:
            rcm_on   = (self.arm_mode == 'rcm' and self.rcm_enabled)
            rcm_pt   = None if self.rcm_world_point is None else self.rcm_world_point.copy()
            ref_axis = None if self.rcm_ref_axis is None else self.rcm_ref_axis.copy()
            q_now    = self.joint_positions.copy()
            self._clear_track_state_unlocked()
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
        if d_tip < 0.05: d_tip = float(self.kin.tube_length)
        tip_from_end = float(np.linalg.norm(p_tip_cur - p_end_cur))
        with self._lock:
            self._set_flex_slew_target(curvature=0., steer_yaw=0.)
            self._pull_out_state = {
                'stage':'align','axis_cmd': axis_cur.copy(),
                'd_tip_align': d_tip,'tip_from_end': tip_from_end,
                's_end_cmd': None,'waiting_logged': False}
        self._update_flex_state()
        log.info("PULL OUT started (align stage)")
        return True

    def _step_pull_out(self):
        if not self._arm_ready(): return
        with self._lock:
            st = self._pull_out_state
            if st is None: return
            if not (self.arm_mode == 'rcm' and self.rcm_enabled
                    and self.rcm_world_point is not None
                    and self.rcm_ref_axis is not None):
                self._pull_out_state = None; self._flex_slew_target = None; return
            rcm_pt = self.rcm_world_point.copy()
            ref_axis = self.rcm_ref_axis.copy()
            q_now = self.joint_positions.copy()
            self._set_flex_slew_target(curvature=0., steer_yaw=0.)
        try:
            axis_cur, _, p_tip_cur = self.kin.tube_axis_in_base(q_now)
            p_end_cur = self.kin.end_cylinder_in_base(q_now)
        except Exception as e:
            log.warning(f"PULL OUT FK: {e}")
            with self._lock: self._pull_out_state = None; self._flex_slew_target = None
            return

        if st['stage'] == 'align':
            axis_cmd = st['axis_cmd']
            err_cmd = math.acos(float(np.clip(np.dot(axis_cmd, ref_axis), -1., 1.)))
            step = math.radians(self.ARM_TILT_VEL_DEG_S)*self.dt
            if err_cmd <= step or err_cmd < 1e-3:
                axis_next = ref_axis.copy()
            else:
                rk = np.cross(axis_cmd, ref_axis); kn = float(np.linalg.norm(rk))
                axis_next = (ref_axis.copy() if kn < 1e-9
                             else self.kin.rotate_about(axis_cmd, rk/kn, step))
                axis_next /= max(float(np.linalg.norm(axis_next)), 1e-9)
            st['axis_cmd'] = axis_next
            p_tip_t = rcm_pt + st['d_tip_align']*axis_next
            q_t = self.kin.ik_along_axis(p_tip_t, axis_next, q_now)
            if q_t is not None:
                q_t = self._auto_solve_wrist1(q_t)
                self._send_servo(q_t, q_now, self.dt)     # ★ teleop 单步，用 servo

            err_actual = math.degrees(math.acos(float(np.clip(np.dot(axis_cur, ref_axis), -1., 1.))))
            aligned = (np.allclose(axis_next, ref_axis, atol=1e-3) and err_actual < 1.0)
            if not aligned: return
            if not self._flex_is_straight():
                if not st.get('waiting_logged', False):
                    log.info("PULL OUT aligned; waiting for flex straight")
                    st['waiting_logged'] = True
                return
            st['waiting_logged'] = False
            s_end_cur = float(np.dot(p_end_cur - rcm_pt, ref_axis))
            st['stage'] = 'retract'; st['s_end_cmd'] = s_end_cur
            log.info(f"PULL OUT align done (err={err_actual:.2f}°); retracting")
            return

        if st['stage'] == 'retract':
            s_end_cur = float(np.dot(p_end_cur - rcm_pt, ref_axis))
            if st['s_end_cmd'] is None: st['s_end_cmd'] = s_end_cur
            step_m = self.ARM_AXIS_VEL*self.dt
            s_end_next = (PULL_OUT_BEHIND_RCM_M if st['s_end_cmd']+step_m >= PULL_OUT_BEHIND_RCM_M
                          else st['s_end_cmd']+step_m)
            st['s_end_cmd'] = s_end_next
            p_tip_t = rcm_pt + (st['tip_from_end'] + s_end_next)*ref_axis
            q_t = self.kin.ik_along_axis(p_tip_t, ref_axis, q_now)
            if q_t is not None:
                q_t = self._auto_solve_wrist1(q_t)
                self._send_servo(q_t, q_now, self.dt)     # ★
            if s_end_next >= PULL_OUT_BEHIND_RCM_M - 1e-4 and s_end_cur >= PULL_OUT_BEHIND_RCM_M - 0.003:
                with self._lock:
                    self._pull_out_state = None; self._flex_slew_target = None
                log.info(f"PULL OUT complete ({s_end_cur*100:.1f} cm behind RCM)")

    def _cancel_motion(self):
        self._key_updated.clear()
        for key in self._key_state:
            self._key_state[key] = False
        self._pull_out_state = None
        self._flex_slew_target = None
        self._clear_track_state_unlocked()
        self._teleop_servo_active = False
        self._q_cmd = None

    def cmd_stop(self):
        self.cancel_motion()
        return self._send({"type": "stop"})

    def cancel_motion(self):
        with self._lock:
            self._cancel_motion()

    # ---------- tracking ----------
    def _clear_track_state_unlocked(self):
        self._track_target = None; self._track_zone = None; self._track_warned = False
        self._track_locked = False; self._track_recover = False
        self._track_dist_m = None;  self._track_aim_deg = None
        self._track_status_msg = None

    def set_targets(self, targets_dict):
        valid_keys = {key for key, _, _ in TRACK_TARGETS}
        targets = {}
        for key, value in targets_dict.items():
            if key not in valid_keys:
                raise ValueError(f"Unknown tracking target {key}")
            if not isinstance(value, list) or len(value) != 3:
                raise ValueError(f"{key} needs three coordinates")
            try:
                valid = all(type(v) in (int, float) and math.isfinite(v) for v in value)
            except OverflowError:
                valid = False
            if not valid:
                raise ValueError(f"{key} coordinates must be finite numbers")
            targets[key] = np.array(value, float)
        with self._lock:
            self._targets_state.update(targets)

    def start_tracking(self, key):
        if not isinstance(key, str) or key not in {k for k, _, _ in TRACK_TARGETS}:
            return False
        if not self._arm_ready():
            return False
        refusal = None; recovering = False; start_theta = 0.0
        with self._lock:
            rcm_ok = (self.arm_mode == 'rcm' and self.rcm_enabled
                      and self.rcm_world_point is not None)
            if not rcm_ok or self.flex_mode != 'fixed':
                refusal = 'requires RCM mode + fixed flex mode'
                self._track_status_msg = refusal
            else:
                self._pull_out_state = None; self._flex_slew_target = None
                self._clear_track_state_unlocked()
                self._track_target = key
                start_theta = math.degrees(min(FLEX_MAX_BEND, TRACK_L_EFF*self.flex_curvature))
                self._track_recover = start_theta > TRACK_RECOVER_BEND_DEG
                recovering = self._track_recover
        if refusal:
            log.warning(f"TRACKING refused for {key}: {refusal}")
            return False
        if recovering:
            log.info(f"TRACKING recovery: bend {start_theta:.1f}° > "
                     f"{TRACK_RECOVER_BEND_DEG:.0f}°")
        log.info(f"TRACKING started: {key}")
        return True

    def stop_tracking(self):
        with self._lock:
            if self._track_target is None: return
            self._clear_track_state_unlocked()
        log.info("TRACKING stopped")

    @staticmethod
    def _wdls_solve(J, e, w_rel):
        W = TRACK_W_SCALE * np.diag(np.asarray(w_rel, float))
        A = J.T@J + W
        try: return np.linalg.solve(A, J.T@e)
        except Exception: return None

    def _step_hybrid_rcm_tracking(self):
        if not self._arm_ready(): return
        with self._lock:
            key = self._track_target
            if key is None: return
            p_target = None if key not in self._targets_state else self._targets_state[key].copy()
            arm_mode = self.arm_mode; rcm_on = self.rcm_enabled
            rcm_pt = None if self.rcm_world_point is None else self.rcm_world_point.copy()
            ref_axis = None if self.rcm_ref_axis is None else self.rcm_ref_axis.copy()
            q_now = self.joint_positions.copy()
            kappa = float(self.flex_curvature)
            steer_yaw = float(self.flex_steer_yaw)
            arc_len = float(self.flex_arc_len)
            flex_mode = self.flex_mode
            standoff = float(self.standoff)
            already_warned = self._track_warned
            recovering = bool(self._track_recover)
            if p_target is None and not already_warned: self._track_warned = True
        if not (arm_mode == 'rcm' and rcm_on and rcm_pt is not None and ref_axis is not None):
            with self._lock: self._clear_track_state_unlocked()
            log.warning("TRACKING auto-cancelled (RCM gone)"); return
        if flex_mode != 'fixed':
            with self._lock: self._clear_track_state_unlocked()
            log.warning("TRACKING auto-cancelled (flex not fixed)"); return
        if p_target is None:
            if not already_warned: log.warning(f"TRACKING waiting for target '{key}'")
            return

        try:
            R_shaft, _ = self.kin.fk_matrix(q_now)
            _, _, p_tip = self.kin.tube_axis_in_base(q_now)
            d_ins = float(np.linalg.norm(p_tip - rcm_pt))
            p_cam, v_hat = self.kin.camera_pose_on_shaft(
                rcm_pt, R_shaft, d_ins, kappa, steer_yaw,
                arc_len=arc_len, flex_mode=flex_mode,
                tip_to_flex_base=TRACK_TIP_TO_FLEX_BASE)
        except Exception as e:
            log.warning(f"TRACKING FK: {e}"); return

        p_aim = p_cam + standoff*v_hat
        e_vec = p_target - p_aim
        err_norm = float(np.linalg.norm(e_vec))
        aim_vec = p_target - p_cam
        aim_norm = float(np.linalg.norm(aim_vec))
        aim_deg = (math.degrees(math.acos(float(np.clip(
            np.dot(v_hat, aim_vec/aim_norm), -1., 1.)))) if aim_norm > 1e-9 else 0.0)
        dist_m = float(np.linalg.norm(p_cam - p_target))

        theta_total = min(FLEX_MAX_BEND, TRACK_L_EFF*kappa)
        theta_deg = math.degrees(theta_total)
        if recovering and theta_deg <= TRACK_RECOVER_DONE_DEG:
            with self._lock: self._track_recover = False
            recovering = False
            log.info(f"TRACKING recovery done at {theta_deg:.1f}°")
        if theta_deg <= 50.0:
            zone = 1; w_rel = np.array([1.,1.,1e6,1e6,1.])
        elif theta_deg < 80.0:
            zone = 2; s = (theta_deg-50.0)/30.0
            wf = 1.0 + 20.0*s*s; wt = 1.0 + (1e6-1.0)*(1.0-s)**2
            w_rel = np.array([wf,wf,wt,wt,1.])
        else:
            zone = 3; w_rel = np.array([1.,1.,1.,1.,1.])

        max_dk = self.FLEX_CURV_VEL*self.dt
        max_dpsi = self.FLEX_YAW_VEL*self.dt
        max_dtilt = math.radians(self.ARM_TILT_VEL_DEG_S)*self.dt
        max_dins = self.ARM_AXIS_VEL*self.dt

        if recovering:
            d_kappa = -max_dk; d_psi = 0.; d_tilt = 0.; d_yaw = 0.; d_ins_step = 0.
        else:
            if err_norm < 1e-12:
                du = np.zeros(5)
            else:
                try:
                    J = self.kin.hybrid_rcm_jacobian(
                        rcm_pt, R_shaft, d_ins, kappa, steer_yaw, standoff,
                        arc_len=arc_len, flex_mode=flex_mode,
                        tip_to_flex_base=TRACK_TIP_TO_FLEX_BASE)
                except Exception as e:
                    log.warning(f"TRACKING jacobian: {e}"); return
                du = self._wdls_solve(J, e_vec, w_rel)
                if du is None:
                    log.warning("TRACKING singular"); return
                if zone == 3:
                    kx0 = kappa*math.cos(steer_yaw); ky0 = kappa*math.sin(steer_yaw)
                    if math.hypot(kx0+du[0], ky0+du[1]) > kappa + 1e-12:
                        w_lock = w_rel.copy(); w_lock[0]=1e4; w_lock[1]=1e4
                        du_lock = self._wdls_solve(J, e_vec, w_lock)
                        if du_lock is not None: du = du_lock
                if theta_total > math.radians(55.0) and kappa > 1e-9:
                    Jp = np.linalg.pinv(J); Nn = np.eye(5) - Jp@J
                    g = np.zeros(5)
                    gf = -TRACK_L_EFF*(theta_total - math.radians(45.0))
                    g[0] = gf*math.cos(steer_yaw); g[1] = gf*math.sin(steer_yaw)
                    du = du - 0.1*(Nn@g)

            kx0 = kappa*math.cos(steer_yaw); ky0 = kappa*math.sin(steer_yaw)
            ku = math.hypot(kx0+du[0], ky0+du[1])
            psi_u = (math.atan2(ky0+du[1], kx0+du[0]) if ku > 1e-12 else steer_yaw)
            d_kappa = float(np.clip(ku-kappa, -max_dk, max_dk))
            d_psi = float(np.clip(((psi_u-steer_yaw+math.pi) % (2*math.pi))-math.pi,
                                  -max_dpsi, max_dpsi))
            d_tilt = float(np.clip(du[2], -max_dtilt, max_dtilt))
            d_yaw  = float(np.clip(du[3], -max_dtilt, max_dtilt))
            d_ins_step = float(np.clip(du[4], -max_dins, max_dins))

        kappa_new = float(np.clip(kappa+d_kappa, 0., FLEX_MAX_BEND/TRACK_L_EFF))
        psi_new = (steer_yaw if kappa_new < 1e-6
                   else ((steer_yaw+d_psi+math.pi) % (2*math.pi))-math.pi)

        R_shaft_new = self.kin.rotate_shaft_frame(R_shaft, d_tilt=d_tilt, d_yaw=d_yaw)
        u_new = R_shaft_new[:,2]
        cone_deg = math.degrees(math.acos(float(np.clip(np.dot(u_new, ref_axis), -1., 1.))))
        if cone_deg > math.degrees(self.RCM_CONE_HALF_ANGLE):
            if abs(d_tilt)+abs(d_yaw) > 1e-12:
                log.warning(f"TRACKING RCM cone {cone_deg:.1f}°; arm frozen")
            d_tilt = 0.; d_yaw = 0.; u_new = R_shaft[:,2].copy()

        with self._lock:
            self.flex_curvature = kappa_new
            if kappa_new >= 1e-6: self.flex_steer_yaw = float(psi_new)

        if abs(d_tilt)+abs(d_yaw)+abs(d_ins_step) > 1e-9:
            d_new = d_ins + d_ins_step
            p_tip_t = rcm_pt + d_new*u_new
            q_t = self.kin.ik_with_rcm(p_tip_t, rcm_pt, q_now)
            if q_t is not None:
                q_t = self._auto_solve_wrist1(q_t)
                self._send_servo(q_t, q_now, self.dt)     # ★
            else:
                log.warning("TRACKING IK failed; flex only this tick")

        locked = (not recovering and aim_deg < 0.5
                  and abs(dist_m - standoff) < 0.0015)
        with self._lock:
            self._track_dist_m = dist_m; self._track_aim_deg = aim_deg
            self._track_zone = zone
            newly = locked and not self._track_locked
            if locked: self._track_locked = True
        if newly:
            log.info(f"TRACKING locked on {key}: d={dist_m*100:.1f}cm aim={aim_deg:.2f}°")

    # ---------- transport ----------
    def _send_servo(self, q_target, q_now, dt):
        q_target = np.asarray(q_target, float)
        if q_target.shape != (6,) or not np.isfinite(q_target).all():
            return False

        with self._lock:
            if not self._arm_ready():
                return False
            q_base = self._q_cmd
            if q_base is None or np.max(np.abs(q_base - q_now)) > self.TELEOP_RESYNC_RAD:
                q_base = q_now

        max_step = self.SERVO_MAX_JOINT_VEL * dt
        delta = q_target - q_base
        md = float(np.max(np.abs(delta)))
        if md > max_step:
            q_target = q_base + delta * (max_step / md)

        if not self._send({"type": "servo_joint", "positions": q_target.tolist()}):
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

    def _send_trajectory(self, q_target, traj_duration):
        with self._lock:
            if not self._arm_ready():
                return False
            q_base = self.joint_positions.copy()

        q_target = np.asarray(q_target, float)
        if q_target.shape != (6,) or not np.isfinite(q_target).all():
            return False
        max_delta = float(np.max(np.abs(q_target - q_base)))
        if max_delta < 1e-6:
            speed = 0.05
        else:
            speed = max_delta / float(traj_duration)
        speed = float(np.clip(speed, 0.02, 3.0))
        accel = float(np.clip(speed * 2.0, 0.05, 5.0))

        return self._send({
            "type": "move_joint",
            "positions": q_target.tolist(),
            "speed": speed, "accel": accel,
        })

    # ---------- key state ----------
    def set_key(self, key, down):
        if key not in self._key_state:
            return False
        if down and key.startswith("arm_") and not self._arm_ready():
            return False
        self._key_state[key] = down
        if down:
            self._key_updated[key] = time.monotonic()
        else:
            self._key_updated.pop(key, None)
        return True

    def refresh_keys(self, held):
        now = time.monotonic()
        for key in held:
            if key in self._key_updated and now - self._key_updated[key] <= KEY_TIMEOUT_S:
                self._key_updated[key] = now

    # ---------- state snapshot ----------
    def get_state_dict(self):
        with self._lock:
            fresh = self._joint_state_fresh()
            q = self.joint_positions.copy() if fresh else None
            eff = max(1e-6, self.flex_arc_len - FLEX_SEG_LEN)
            theta_total = min(FLEX_MAX_BEND, eff*max(0., self.flex_curvature))
            state = {
                'arm_mode': self.arm_mode,
                'flex_mode': self.flex_mode,
                'rcm_enabled': self.rcm_enabled,
                'rcm_point': None if self.rcm_world_point is None else self.rcm_world_point.tolist(),
                'joints_deg': (np.rad2deg(q).tolist() if q is not None else None),
                'flex_base_yaw': math.degrees(self.flex_base_yaw),
                'flex_arc_len':  self.flex_arc_len,
                'flex_ext_yaw':  math.degrees(self.flex_ext_yaw),
                'flex_curvature': self.flex_curvature,
                'flex_theta_total_deg': math.degrees(theta_total),
                'flex_theta_seg_deg':   math.degrees(theta_total/(FLEX_N-1)),
                'tracking': self._track_target,
                'tracking_dist_m':  self._track_dist_m,
                'tracking_aim_deg': self._track_aim_deg,
                'tracking_zone':    self._track_zone,
                'tracking_recovering': self._track_recover,
                'tracking_locked':     self._track_locked,
                'tracking_refusal':    self._track_status_msg,
                'relay_connected':     self._sock is not None,
                'joints_received':     fresh,
                'kinematics_ready':    self.kin.ready,
                'motion_fault':        self._motion_fault,
            }
            alpha = self.flex_base_yaw + self.flex_ext_yaw
            tendons = {}
            for phi_deg, name in FLEX_TENDONS:
                phi = math.radians(phi_deg)
                dL = -FLEX_TENDON_R*math.cos(phi - alpha)*theta_total
                tendons[name] = (self.flex_arc_len + dL, dL)
            state['tendons'] = {n:(L*1000, dL*1000) for n,(L,dL) in tendons.items()}
        return state
