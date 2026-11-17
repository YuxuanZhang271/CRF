#!/usr/bin/env python3
"""RTDE Bridge for local execution (runs in background thread)."""

import json
import logging
import math
import select
import socket
import threading
import time
from concurrent.futures import Future
from contextlib import ExitStack, contextmanager
from pathlib import Path

import numpy as np

if __package__:
    from .kinematics import EndoscopeKinematics, rcm_axis_from_angles, rcm_tip_angles
else:
    from kinematics import EndoscopeKinematics, rcm_axis_from_angles, rcm_tip_angles

UR5_IP = "192.168.1.10"
TCP_HOST = "127.0.0.1"     # Local loopback
TCP_PORT = 9000
UR_DASHBOARD_PORT = 29999
PERIOD = 1.0 / 125.0
LINEAR_JOG_PERIOD = 4 * PERIOD
TIP_JOG_PERIOD = 6 * PERIOD
JOG_PERIODS = {"jog_linear": LINEAR_JOG_PERIOD, "jog_tip": TIP_JOG_PERIOD}
SERVO_LOOKAHEAD = 0.05
SERVO_GAIN = 300.0
MOTION_STALE_S = 0.3
STOP_TIMEOUT_S = 1.0
STATIONARY_RAD_S = 1e-3
MAX_LINE_BYTES = 4096
DEFAULT_LINEAR_SPEED_MM_S = 25.
LINEAR_SPEED_PRESETS_MM_S = tuple(DEFAULT_LINEAR_SPEED_MM_S * factor for factor in (0.5, 1., 2., 5.))
SERVO_MAX_JOINT_VEL = 2.0
MAX_MOVE_JOINT_VEL = 3.0
MAX_MOVE_JOINT_ACCEL = 5.0
RESET_SPEED_FACTOR = 1.5
SERVO_RESYNC_RAD = 0.05
SERVO_TARGET_LEAD_RAD = 0.04
RCM_COMMAND_MARGIN_DEG = 0.1
RCM_EDGE_APPROACH_S = 0.12

log = logging.getLogger("RTDE-Relay")

def limit_servo_target(kin, reference, target, measured, elapsed, linear):
    delta = np.asarray(target) - reference
    distance = np.abs(delta)
    remaining = SERVO_TARGET_LEAD_RAD + np.sign(delta) * (measured - reference)
    # Keep the reference advancing with the jog while leaving enough room
    # for telemetry lag. Once the robot catches up, advancement resumes.
    lead_scale = np.divide(remaining, distance, out=np.ones_like(delta), where=distance > 0.)
    scale = max(0., min(1., float(np.min(lead_scale)),
                        SERVO_MAX_JOINT_VEL * elapsed / max(float(np.max(distance)), 1e-12)))
    return kin.limit_tip_step(reference, reference + delta * scale, elapsed, linear)


def joint_vector(values, size=6):
    if not isinstance(values, (list, tuple)) or len(values) != size:
        raise ValueError(f"expected {size} values")
    if not all(type(value) in (int, float) and math.isfinite(value) for value in values):
        raise ValueError("expected finite numbers")
    return [float(value) for value in values]

def parse_command(line):
    cmd = json.loads(line)
    if not isinstance(cmd, dict):
        raise ValueError("expected a JSON object")
    kind = cmd.get("type")
    if not isinstance(kind, str):
        raise ValueError("unknown command type")
    if kind in ("stop", "estop"):
        return kind, ()
    if kind == "interrupt":
        request = cmd.get("request")
        if type(request) is not int or request <= 0:
            raise ValueError("expected an interrupt request ID")
        return kind, (request,)
    if kind == "servo_joint" or kind in JOG_PERIODS:
        manual_jog = kind in JOG_PERIODS
        values = (joint_vector(cmd.get("direction"), 3) if manual_jog
                  else joint_vector(cmd.get("positions")))
        if manual_jog and not 0. < max(map(abs, values)) <= 1.:
            raise ValueError("invalid jog direction")
        rcm = cmd.get("rcm")
        if kind == "jog_tip" and (rcm is None or not any(values[1:])):
            raise ValueError("tip jog requires an RCM constraint and a tilt direction")
        if rcm is None:
            return kind, (values,)
        if not isinstance(rcm, dict):
            raise ValueError("expected an RCM constraint")
        axis = np.asarray(joint_vector(rcm.get("axis"), 3))
        pivot = np.asarray(joint_vector(rcm.get("pivot"), 3))
        angle = rcm.get("half_angle")
        if (abs(np.linalg.norm(axis) - 1.) > 1e-6
                or type(angle) not in (int, float) or not 0 < angle <= math.pi / 2):
            raise ValueError("invalid RCM axis or half-angle")
        if kind == "jog_linear" and any(values[1:]):
            raise ValueError("RCM linear jogs must follow the shaft axis")
        return kind, (values, (axis, pivot, math.cos(angle)))
    if kind == "reset_joint":
        speed = min(MAX_MOVE_JOINT_VEL, SERVO_MAX_JOINT_VEL * RESET_SPEED_FACTOR)
        return kind, (joint_vector(cmd.get("positions")), speed,
                      MAX_MOVE_JOINT_ACCEL, 2.0 * RESET_SPEED_FACTOR)
    if kind == "move_joint":
        speed = cmd.get("speed", 0.5)
        accel = cmd.get("accel", 0.5)
        if not (type(speed) in (int, float) and 0 < speed <= MAX_MOVE_JOINT_VEL
                and type(accel) in (int, float) and 0 < accel <= MAX_MOVE_JOINT_ACCEL):
            raise ValueError("move_joint speed or acceleration is out of range")
        return kind, (joint_vector(cmd.get("positions")), speed, accel)
    if kind == "move_joint_vel":
        duration = cmd.get("duration", PERIOD)
        if not (type(duration) in (int, float) and 0 < duration <= MOTION_STALE_S):
            raise ValueError("move_joint_vel duration is out of range")
        return kind, (joint_vector(cmd.get("velocities")), max(duration, PERIOD))
    raise ValueError("unknown command type")

def dashboard_stop(robot_ip):
    with socket.create_connection((robot_ip, UR_DASHBOARD_PORT), timeout=2.0) as conn:
        with conn.makefile("rb") as response:
            if not response.readline(MAX_LINE_BYTES).startswith(b"Connected:"):
                raise RuntimeError("unexpected Dashboard greeting")
            conn.sendall(b"stop\n")
            reply = response.readline(MAX_LINE_BYTES).decode("utf-8", errors="replace").strip()
            if reply.lower() != "stopped":
                raise RuntimeError(f"Dashboard stop reply: {reply!r}")

class Relay:
    def __init__(self, control, receive, robot_ip, kinematics):
        self.control = control
        self.receive = receive
        self.robot_ip = robot_ip
        self.kin = kinematics
        self.lock = threading.Lock()
        self.pending_move = None
        self.servo_target = None
        self.pending_stop = None
        self.interrupt_result = None
        self.dashboard_result = None
        self.closed = False
        self.telemetry = None

        self.mode = None
        self.handoff_deadline = None
        self.quiet_sample = None
        self.fault = None
        self.last_servo_at = float("-inf")
        self.last_servo_joints = None
        self.last_servo_sent_at = None
        self.move_target = None
        self.move_tolerance = 0.
        self.linear_speed_limit_mm_s = DEFAULT_LINEAR_SPEED_MM_S

    def set_speed_limit(self, value):
        if type(value) not in (int, float) or value not in LINEAR_SPEED_PRESETS_MM_S:
            raise ValueError("Choose a UR5 speed preset of 12.5, 25, 50, or 125 mm/s.")
        with self.lock:
            sample = self.telemetry
            if (self.closed or self.fault or self.pending_stop or self.mode or self.handoff_deadline
                    or self.pending_move or self.servo_target or sample is None
                    or time.monotonic() - sample[0] > MOTION_STALE_S
                    or max(map(abs, sample[2])) >= STATIONARY_RAD_S):
                raise ValueError("Wait for the UR5 to stop and release its controls before changing speed.")
            self.linear_speed_limit_mm_s = float(value)
        return True

    def submit(self, kind, args=()):
        with self.lock:
            if self.closed:
                if kind in ("estop", "interrupt"):
                    raise RuntimeError("RTDE relay has stopped")
                return
            now = time.monotonic()
            if kind in ("stop", "estop", "interrupt"):
                self.pending_move = None
                self.servo_target = None
                if kind == "estop":
                    if self.dashboard_result is None or self.dashboard_result.done():
                        self.dashboard_result = Future()
                        self.fault = "Waiting for PolyScope to confirm Dashboard Stop"
                        self.pending_stop = "estop"
                    return self.dashboard_result
                if self.pending_stop is None:
                    self.pending_stop = "stop"
                if kind == "interrupt":
                    if self.interrupt_result is None:
                        self.interrupt_result = Future()
                    return self.interrupt_result
            elif self.fault is not None or self.interrupt_result is not None:
                return
            elif kind in ("move_joint", "reset_joint"):
                self.servo_target = None
                self.pending_move = (args, now)
            else:
                if kind == "move_joint_vel":
                    if self.telemetry is None or now - self.telemetry[0] > MOTION_STALE_S:
                        return
                    positions = self.telemetry[1]["joints"]
                    velocities, duration = args
                    target = joint_vector([q + qd * duration for q, qd in zip(positions, velocities)])
                else:
                    target = args[0]
                constraint = args[1] if kind != "move_joint_vel" and len(args) > 1 else None
                self.servo_target = (target, now, constraint, kind)

    def _linear_jog_target(self, reference, measured, direction, constraint, linear, elapsed):
        shaft, _, tip = self.kin.tube_axis_in_base(reference)
        axial, lateral, vertical = direction
        if constraint is None:
            up = np.array([0., 0., 1.])
            left = np.cross(up, shaft)
            length = float(np.linalg.norm(left))
            left = left / length if length >= 1e-6 else np.array([0., 1., 0.])
            delta = axial * shaft + lateral * left + vertical * up
            origin = tip
        else:
            axis, pivot, limit = constraint
            measured_axis, _, _ = self.kin.tube_axis_in_base(measured)
            if min(np.dot(shaft, axis), np.dot(measured_axis, axis)) < limit - 1e-10:
                return None
            depth = float(np.dot(tip - pivot, shaft))
            origin = pivot + depth * shaft
            delta = axial * shaft
        max_speed = self.kin.tip_reach_bound * 6 * SERVO_MAX_JOINT_VEL
        delta *= min(linear, max_speed) * elapsed / max(1., float(np.linalg.norm(delta)))
        if constraint is not None and depth + np.dot(delta, shaft) <= 1e-6:
            return None

        # Use a shorter step when the requested endpoint is unreachable.
        for fraction in (1., 0.5, 0.25):
            target = self.kin.ik_along_axis(origin + fraction * delta, shaft, reference)
            if target is not None:
                return target
        return None

    def _tip_jog_target(self, reference, measured, direction, constraint, linear, elapsed):
        center, pivot, limit = constraint
        actual_shaft, _, actual_tip = self.kin.tube_axis_in_base(measured)
        shaft, _, tip = self.kin.tube_axis_in_base(reference)
        actual_alignment = float(np.dot(actual_shaft, center))
        alignment = float(np.dot(shaft, center))
        recovering = actual_alignment < limit - 1e-10
        if (recovering and alignment < actual_alignment) or (not recovering and alignment < limit - 1e-10):
            reference, shaft, tip = measured, actual_shaft, actual_tip
        depth = float(np.dot(tip - pivot, shaft))
        if depth <= 1e-6:
            return None

        axial, right, up = direction
        tilt_rate = min(linear / depth, 6 * SERVO_MAX_JOINT_VEL)
        tilt = math.degrees(tilt_rate * elapsed) / max(1., math.hypot(right, up))
        step = np.array([right, up]) * tilt
        current = rcm_tip_angles(shaft, center)
        target = current + step
        insertion = 0. if recovering else axial * min(
            linear, self.kin.tip_reach_bound * 6 * SERVO_MAX_JOINT_VEL) * elapsed
        if depth + insertion <= 1e-6:
            return None

        if recovering:
            actual = rcm_tip_angles(actual_shaft, center)
            if np.linalg.norm(actual + step) >= np.linalg.norm(actual) - 1e-8:
                return None
        else:
            radius = float(np.linalg.norm(current))
            requested = float(np.linalg.norm(target))
            soft_limit = math.degrees(math.acos(limit)) - RCM_COMMAND_MARGIN_DEG
            allowed = radius + max(0., soft_limit - radius) * min(1., elapsed / RCM_EDGE_APPROACH_S)
            if requested > allowed:
                target *= allowed / requested

        for fraction in (1., 0.5, 0.25):
            angles = current + fraction * (target - current)
            axis = rcm_axis_from_angles(angles, center)
            q = self.kin.ik_along_axis(pivot + (depth + fraction * insertion) * axis, axis, reference)
            if q is not None:
                return q
        return None

    def _valid_rcm_step(self, reference, target, measured, constraint):
        ref_axis, pivot, limit = constraint
        alignments = []
        # Check the measured pose and the actual interpolated command, since
        # the relay may rate-limit it again from a different servo target.
        for q in (measured, reference, (reference + target) / 2., target):
            axis, _, tip = self.kin.tube_axis_in_base(q)
            if np.linalg.norm(np.cross(tip - pivot, axis)) > 0.002:
                return False
            alignments.append(float(np.dot(axis, ref_axis)))
        actual, start, midpoint, end = alignments
        tol = 1e-10
        if midpoint < min(start, limit) - tol:
            return False
        if end >= limit - tol:
            return True
        # From an overshoot, the rate-limited step can still be outside.
        # Permit it only when it reduces the angle from both start poses.
        return max(actual, start) < limit - tol and end > max(actual, start) + tol

    def motion_fault(self):
        with self.lock:
            return self.fault

    def state(self):
        with self.lock:
            if self.telemetry is None:
                return None
            sample_at, message, _ = self.telemetry
            return sample_at, dict(message, motion_fault=self.fault,
                                   arm_linear_speed_mm_s=self.linear_speed_limit_mm_s)

    def receive_loop(self, shutdown):
        previous_stamp = None
        while not shutdown.is_set():
            started = time.monotonic()
            try:
                if self.receive.isConnected():
                    stamp = self.receive.getTimestamp()
                    if math.isfinite(stamp) and stamp != previous_stamp:
                        joints = joint_vector(self.receive.getActualQ())
                        velocities = joint_vector(self.receive.getActualQd())
                        pose = joint_vector(self.receive.getActualTCPPose())
                        with self.lock:
                            self.telemetry = (time.monotonic(), {
                                "type": "robot_state",
                                "joints": joints,
                                "tcp_pose": pose,
                                "moving": max(map(abs, velocities)) >= STATIONARY_RAD_S,
                                "timestamp": time.time(),
                            }, velocities)
                        previous_stamp = stamp
            except (RuntimeError, ValueError, OverflowError) as error:
                log.debug("RTDE feedback unavailable: %s", error)
            shutdown.wait(max(0, started + PERIOD - time.monotonic()))

    def _stop_motion(self):
        if self.mode == "servo":
            if self.control.servoStop(3.0) is False:
                raise RuntimeError("servoStop was rejected")
        elif self.mode == "move":
            self.control.stopJ(3.0)
        else:
            return
        with self.lock:
            self.mode = None
            self.move_target = self.last_servo_joints = self.last_servo_sent_at = None
            self.handoff_deadline = time.monotonic() + STOP_TIMEOUT_S
            self.quiet_sample = None

    def _update_handoff(self, sample):
        if self.handoff_deadline is None:
            return
        now = time.monotonic()
        if now >= self.handoff_deadline:
            raise RuntimeError("joint velocity did not settle after stopping")
        if sample is None:
            return
        sample_at, _, velocities = sample
        if sample_at < self.handoff_deadline - STOP_TIMEOUT_S:
            return
        if max(map(abs, velocities)) >= STATIONARY_RAD_S:
            self.quiet_sample = None
        elif self.quiet_sample is None:
            self.quiet_sample = sample_at
        elif sample_at - self.quiet_sample >= 2 * PERIOD:
            self.handoff_deadline = None

    def _execute_dashboard_stop(self):
        try:
            self._stop_motion()
        except (OSError, RuntimeError):
            log.exception("RTDE stop failed before Dashboard Stop")
        try:
            dashboard_stop(self.robot_ip)
        except (OSError, RuntimeError) as error:
            log.exception("Dashboard Stop failed")
            failure = f"Dashboard Stop failed: {error}"
            with self.lock:
                self.fault = failure
                self.dashboard_result.set_result(failure)
        else:
            self.mode = None
            with self.lock:
                self.fault = "PolyScope confirmed Dashboard Stop. Check the robot before restarting the app."
                self.dashboard_result.set_result(None)
            log.warning("Dashboard confirmed the program stopped")

    def _finish_interrupt(self, result, error):
        with self.lock:
            if result is not None and self.interrupt_result is result:
                self.interrupt_result = None
                result.set_result(error)

    def _tick(self):
        with self.lock:
            stop, self.pending_stop = self.pending_stop, None
            move = self.pending_move
            servo = self.servo_target
            sample = self.telemetry
            interrupt = self.interrupt_result
            linear = self.linear_speed_limit_mm_s / 1000.

        if stop == "estop":
            self._finish_interrupt(interrupt, "STOP took over the UR5 shutdown.")
            self._execute_dashboard_stop()
            return
        elif stop == "stop":
            self._stop_motion()
            if interrupt is not None and self.handoff_deadline is None:
                self.handoff_deadline = time.monotonic() + STOP_TIMEOUT_S
                self.quiet_sample = None

        if interrupt is not None:
            self._update_handoff(sample)
            if self.handoff_deadline is None:
                self._finish_interrupt(interrupt, None)
            return
        if stop or self.fault:
            return

        now = time.monotonic()
        self._update_handoff(sample)
        if sample is None or now - sample[0] > MOTION_STALE_S:
            if self.mode is not None:
                raise RuntimeError("RTDE feedback timed out during motion")
            return
        if not self.control.isConnected():
            raise RuntimeError("RTDE control connection lost")
        if self.handoff_deadline is not None:
            return
        measured = np.asarray(sample[1]["joints"])
        stationary = max(map(abs, sample[2])) < STATIONARY_RAD_S
        if (self.mode == "move" and stationary
                and np.max(np.abs(measured - self.move_target)) < self.move_tolerance
                and not self.control.getAsyncOperationProgressEx().isAsyncOperationRunning()):
            with self.lock:
                self.mode = None
                self.move_target = None

        if move is not None:
            args, updated_at = move
            fresh = now - updated_at <= STOP_TIMEOUT_S + MOTION_STALE_S
            if fresh and self.mode is not None:
                self._stop_motion()
                return
            if fresh and not stationary:
                return
            if fresh:
                target, speed, accel, *ramp = args
                delta = np.asarray(target) - measured
                distance = float(np.max(np.abs(delta)))
                if distance:
                    samples = max(1, min(128, math.ceil(float(np.sum(np.abs(delta))) / 0.05)))
                    travel = self.kin.tip_travel_bound(measured, delta, samples)
                    speed = min(speed, linear * distance / max(travel, 1e-12))
            with self.lock:
                if self.pending_stop or self.pending_move is not move:
                    return
                self.pending_move = None
                self.servo_target = None
                if fresh and distance:
                    self.mode = "move"
                    self.move_target = target
                    self.move_tolerance = min(0.002, distance / 2)
            if fresh and distance:
                accel_per_speed = ramp[0] if ramp else 2.0
                if not self.control.moveJ(target, speed, min(accel, accel_per_speed * speed), True):
                    raise RuntimeError("moveJ was rejected")
                log.info("moveJ dispatched")
            elif not fresh:
                log.warning("Discarded an expired move_joint command")
            return

        if servo is not None:
            target, updated_at, constraint, kind = servo
            if now - updated_at <= MOTION_STALE_S:
                if self.mode == "move":
                    self._stop_motion()
                    return
                with self.lock:
                    if self.pending_stop or self.pending_move is not None or self.servo_target is not servo:
                        return
                if kind in JOG_PERIODS and updated_at <= self.last_servo_at:
                    # A held jog must receive fresh intent before advancing.
                    return
                reference = self.last_servo_joints
                if reference is None:
                    reference = measured
                elif np.max(np.abs(reference - measured)) > SERVO_RESYNC_RAD:
                    self._stop_motion()
                    return
                sent_at = time.monotonic()
                # Held jog targets and servoJ use the same duration.
                # Delayed RTDE calls never accumulate a larger motion step.
                duration = JOG_PERIODS.get(kind, PERIOD)
                elapsed = (duration if self.last_servo_sent_at is None
                           else min(duration, sent_at - self.last_servo_sent_at))
                if kind == "jog_linear":
                    target = self._linear_jog_target(reference, measured, target, constraint, linear, elapsed)
                elif kind == "jog_tip":
                    target = self._tip_jog_target(reference, measured, target, constraint, linear, elapsed)
                if target is None:
                    self._stop_motion()
                    return
                target = limit_servo_target(self.kin, reference, target, measured, elapsed, linear)
                if constraint is not None and not self._valid_rcm_step(reference, target, measured, constraint):
                    self._stop_motion()
                    return
                with self.lock:
                    if self.pending_stop or self.pending_move is not None:
                        return
                    self.mode = "servo"
                if not self.control.servoJ(target.tolist(), 0.0, 0.0, duration,
                                           SERVO_LOOKAHEAD, SERVO_GAIN):
                    raise RuntimeError("servoJ was rejected")
                self.last_servo_at = updated_at
                self.last_servo_joints = target
                self.last_servo_sent_at = sent_at
                return

            with self.lock:
                if self.servo_target is servo:
                    self.servo_target = None
        if self.mode == "servo" and now - self.last_servo_at > MOTION_STALE_S:
            self._stop_motion()

    def motion_loop(self, shutdown):
        next_tick = time.monotonic()
        try:
            while not shutdown.is_set():
                try:
                    self._tick()
                except Exception as error:
                    with self.lock:
                        first_fault = self.fault is None
                        if first_fault:
                            self.fault = str(error)
                        interrupt = self.interrupt_result
                    self._finish_interrupt(interrupt, f"UR5 interrupt failed: {error}")
                    if interrupt is not None:
                        self.handoff_deadline = self.quiet_sample = None
                    if first_fault:
                        log.exception("RTDE relay fault; motion disabled until relay restart")
                        try:
                            self._stop_motion()
                        except Exception:
                            log.exception("RTDE stop failed")
                    else:
                        log.exception("RTDE operation failed after motion was disabled")
                next_tick = max(next_tick + PERIOD, time.monotonic())
                shutdown.wait(next_tick - time.monotonic())
        finally:
            with self.lock:
                self.closed = True
                dashboard_pending = self.pending_stop == "estop"
                self.pending_stop = None
                interrupt = self.interrupt_result
            self._finish_interrupt(interrupt, "UR5 relay stopped during INTERRUPT.")
            if dashboard_pending:
                try:
                    self._execute_dashboard_stop()
                except Exception:
                    log.exception("Dashboard Stop during shutdown failed")
            try:
                self._stop_motion()
            except Exception:
                log.exception("RTDE stop during shutdown failed")

def handle_client(conn, relay, shutdown):
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    conn.settimeout(0.1)
    buffer = b""
    last_state_at = None
    interrupt = None
    while not shutdown.is_set():
        readable, _, _ = select.select([conn], [], [], PERIOD)
        if readable:
            data = conn.recv(MAX_LINE_BYTES)
            if not data:
                return
            lines = (buffer + data).split(b"\n")
            buffer = lines.pop()
            if len(buffer) > MAX_LINE_BYTES:
                raise ValueError("unterminated command exceeds size limit")
            for line in lines:
                if len(line) > MAX_LINE_BYTES:
                    raise ValueError("command exceeds size limit")
                if line.strip():
                    try:
                        kind, args = parse_command(line)
                        if kind == "interrupt":
                            interrupt = (args[0], relay.submit(kind))
                        else:
                            relay.submit(kind, args)
                    except (ValueError, OverflowError) as error:
                        log.warning("Ignoring invalid command: %s", error)

        if interrupt is not None and interrupt[1].done():
            request, result = interrupt
            response = {"type": "interrupt_result", "request": request, "error": result.result()}
            conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
            interrupt = None
        telemetry = relay.state()
        if telemetry is not None:
            sent_at, state = telemetry
            if sent_at != last_state_at and time.monotonic() - sent_at < MOTION_STALE_S:
                conn.sendall((json.dumps(state, allow_nan=False) + "\n").encode("utf-8"))
                last_state_at = sent_at

@contextmanager
def open_relay(robot_ip, host, port, *, urdf_path=None,
               rcm_point_local=(0., 0., -0.30), tip_local=(0., 0., 0.20195)):
    """Bind the local port and initialize RTDE before starting any workers."""
    try:
        from rtde_control import RTDEControlInterface
        from rtde_receive import RTDEReceiveInterface
    except ImportError as error:
        raise RuntimeError("Install ur-rtde in the crf environment: python -m pip install ur-rtde") from error

    path = Path(urdf_path) if urdf_path is not None else Path(__file__).with_name("ur5_endo_flex.urdf")
    kin = EndoscopeKinematics("base_link", "endoscope_link", rcm_point_local, tip_local, log)
    if not kin.setup_from_urdf(path.read_text(encoding="utf-8")):
        raise RuntimeError("UR5 tip kinematics are required for speed limiting")
    with socket.socket() as server, ExitStack() as connections:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen(1)
        server.settimeout(0.2)

        log.info("Connecting to UR5 at %s...", robot_ip)
        try:
            receive = RTDEReceiveInterface(robot_ip)
            connections.callback(receive.disconnect)
            control = RTDEControlInterface(robot_ip)
            connections.callback(control.disconnect)
            if not (receive.isConnected() and control.isConnected()):
                raise RuntimeError("RTDE interfaces did not connect")
        except (OSError, RuntimeError) as error:
            raise RuntimeError(f"Could not initialize RTDE for {robot_ip}: {error}") from error
        log.info("RTDE connected. Relay listening on %s:%d", *server.getsockname())
        yield server, Relay(control, receive, robot_ip, kin)


def serve_relay(server, relay, shutdown_event):
    workers = []
    try:
        for name, target in (("rtde-motion", relay.motion_loop), ("rtde-receive", relay.receive_loop)):
            worker = threading.Thread(name=name, target=target, args=(shutdown_event,))
            worker.start()
            workers.append(worker)
        while not shutdown_event.is_set():
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue
            with conn:
                log.info("ControlNode connected from %s", addr)
                try:
                    handle_client(conn, relay, shutdown_event)
                except (OSError, ValueError) as error:
                    log.info("Client connection ended: %s", error)
                finally:
                    relay.submit("stop")
                    log.info("ControlNode disconnected")
    except (OSError, RuntimeError) as error:
        with relay.lock:
            if relay.fault is None:
                relay.fault = f"RTDE relay stopped: {error}"
        raise
    finally:
        shutdown_event.set()
        for worker in workers:
            worker.join()


def main():
    logging.basicConfig(level=logging.INFO, format="[RTDE] %(asctime)s - %(message)s")
    try:
        with open_relay(UR5_IP, TCP_HOST, TCP_PORT) as (server, relay):
            serve_relay(server, relay, threading.Event())
    except KeyboardInterrupt:
        pass
    except (OSError, RuntimeError) as error:
        log.error("Relay failed: %s", error)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
