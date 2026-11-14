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

UR5_IP = "192.168.1.10"
TCP_HOST = "127.0.0.1"     # Local loopback
TCP_PORT = 9000
UR_DASHBOARD_PORT = 29999
PERIOD = 1.0 / 125.0
SERVO_LOOKAHEAD = 0.1
SERVO_GAIN = 300.0
MOTION_STALE_S = 0.3
STOP_TIMEOUT_S = 1.0
STATIONARY_RAD_S = 1e-3
MAX_LINE_BYTES = 4096

log = logging.getLogger("RTDE-Relay")

def joint_vector(values):
    if not isinstance(values, (list, tuple)) or len(values) != 6:
        raise ValueError("expected six values")
    if not all(type(value) in (int, float) and math.isfinite(value) for value in values):
        raise ValueError("expected finite numbers")
    return [float(value) for value in values]

def parse_command(line):
    cmd = json.loads(line)
    if not isinstance(cmd, dict):
        raise ValueError("expected a JSON object")
    kind = cmd.get("type")
    if kind in ("stop", "estop"):
        return kind, ()
    if kind == "servo_joint":
        return kind, (joint_vector(cmd.get("positions")),)
    if kind == "move_joint":
        speed = cmd.get("speed", 0.5)
        accel = cmd.get("accel", 0.5)
        if not (type(speed) in (int, float) and 0 < speed <= 3.0
                and type(accel) in (int, float) and 0 < accel <= 5.0):
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
    def __init__(self, control, receive, robot_ip):
        self.control = control
        self.receive = receive
        self.robot_ip = robot_ip
        self.lock = threading.Lock()
        self.pending_move = None
        self.servo_target = None
        self.pending_stop = None
        self.dashboard_result = None
        self.closed = False
        self.telemetry = None

        self.mode = None
        self.handoff_deadline = None
        self.quiet_sample = None
        self.fault = None
        self.last_servo_at = float("-inf")

    def submit(self, kind, args=()):
        with self.lock:
            if self.closed:
                if kind == "estop":
                    raise RuntimeError("RTDE relay has stopped")
                return
            now = time.monotonic()
            if kind in ("stop", "estop"):
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
            elif self.fault is not None:
                return
            elif kind == "move_joint":
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
                self.servo_target = (target, now)

    def motion_fault(self):
        with self.lock:
            return self.fault

    def state(self):
        with self.lock:
            if self.telemetry is None:
                return None
            sample_at, message, _ = self.telemetry
            return sample_at, dict(message, motion_fault=self.fault)

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
        self.mode = None
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
            dashboard_stop(self.robot_ip)
        except (OSError, RuntimeError) as error:
            log.exception("Dashboard Stop failed")
            failure = f"Dashboard Stop failed: {error}"
            with self.lock:
                self.fault = failure
                self.dashboard_result.set_result(failure)
            self._stop_motion()
        else:
            self.mode = None
            with self.lock:
                self.fault = "PolyScope confirmed Dashboard Stop. Check the robot before restarting the app."
                self.dashboard_result.set_result(None)
            log.warning("Dashboard confirmed the program stopped")

    def _tick(self):
        with self.lock:
            stop, self.pending_stop = self.pending_stop, None
            move = self.pending_move
            servo = self.servo_target
            sample = self.telemetry

        if stop == "estop":
            self._execute_dashboard_stop()
        elif stop == "stop":
            self._stop_motion()

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

        if move is not None:
            args, updated_at = move
            fresh = now - updated_at <= STOP_TIMEOUT_S + MOTION_STALE_S
            if fresh and self.mode is not None:
                self._stop_motion()
                return
            if fresh and max(map(abs, sample[2])) >= STATIONARY_RAD_S:
                return
            with self.lock:
                if self.pending_stop or self.pending_move is not move:
                    return
                self.pending_move = None
                self.servo_target = None
            if fresh:
                self.mode = "move"
                if not self.control.moveJ(*args, True):
                    raise RuntimeError("moveJ was rejected")
                log.info("moveJ dispatched")
            else:
                log.warning("Discarded an expired move_joint command")
            return

        if servo is not None:
            target, updated_at = servo
            if now - updated_at <= MOTION_STALE_S:
                if self.mode == "move":
                    self._stop_motion()
                    return
                with self.lock:
                    if self.pending_stop or self.pending_move is not None:
                        return
                self.mode = "servo"
                if not self.control.servoJ(target, 0.0, 0.0, PERIOD,
                                           SERVO_LOOKAHEAD, SERVO_GAIN):
                    raise RuntimeError("servoJ was rejected")
                self.last_servo_at = updated_at
                return

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
                        relay.submit(*parse_command(line))
                    except (ValueError, OverflowError) as error:
                        log.warning("Ignoring invalid command: %s", error)

        telemetry = relay.state()
        if telemetry is not None:
            sent_at, state = telemetry
            if sent_at != last_state_at and time.monotonic() - sent_at < MOTION_STALE_S:
                conn.sendall((json.dumps(state, allow_nan=False) + "\n").encode("utf-8"))
                last_state_at = sent_at

@contextmanager
def open_relay(robot_ip, host, port):
    """Bind the local port and initialize RTDE before starting any workers."""
    try:
        from rtde_control import RTDEControlInterface
        from rtde_receive import RTDEReceiveInterface
    except ImportError as error:
        raise RuntimeError("Install ur-rtde in the crf environment: python -m pip install ur-rtde") from error

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
        yield server, Relay(control, receive, robot_ip)


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
