#!/usr/bin/env python3
"""Bridge newline-delimited Windows commands and UR5 state over TCP."""

import argparse
import json
import logging
import math
import select
import socket
import threading
import time


UR5_IP = "192.168.1.10"
TCP_HOST = "192.168.55.1"
TCP_PORT = 9000
UR_DASHBOARD_PORT = 29999
PERIOD = 1.0 / 125.0
SERVO_LOOKAHEAD = 0.1
SERVO_GAIN = 300.0
MOTION_STALE_S = 0.3
STOP_TIMEOUT_S = 1.0
STATIONARY_RAD_S = 1e-3
MAX_LINE_BYTES = 4096

log = logging.getLogger("Jetson")


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
        self.telemetry = None

        # The motion worker owns the RTDE control interface.
        self.mode = None
        self.handoff_deadline = None
        self.quiet_sample = None
        self.fault = None
        self.last_servo_at = float("-inf")

    def submit(self, kind, args=()):
        with self.lock:
            now = time.monotonic()
            if kind in ("stop", "estop"):
                self.pending_move = None
                self.servo_target = None
                if self.pending_stop != "estop":
                    self.pending_stop = kind
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

    def _tick(self):
        with self.lock:
            stop, self.pending_stop = self.pending_stop, None
            move = self.pending_move
            servo = self.servo_target
            sample = self.telemetry

        if stop == "estop":
            self.fault = "Dashboard stop requested; restart the relay after checking the robot"
            try:
                dashboard_stop(self.robot_ip)
            except (OSError, RuntimeError):
                log.exception("Dashboard stop failed")
                self.fault = "Dashboard stop failed; check the robot and Jetson log"
                self._stop_motion()
            else:
                self.mode = None
                log.warning("Dashboard confirmed the program stopped")
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
                # Keep the discrete command while the joints decelerate.
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
                    if self.fault is None:
                        self.fault = str(error)
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
            try:
                self._stop_motion()
            except Exception:
                log.exception("RTDE stop during shutdown failed")


def handle_client(conn, relay):
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    conn.settimeout(0.1)
    buffer = b""
    last_state_at = None
    while True:
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--robot-ip", default=UR5_IP)
    parser.add_argument("--host", default=TCP_HOST)
    parser.add_argument("--port", type=int, default=TCP_PORT)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[Jetson] %(asctime)s - %(message)s")

    try:
        from rtde_control import RTDEControlInterface
        from rtde_receive import RTDEReceiveInterface
    except ImportError:
        log.error("Install ur-rtde in the Jetson Python environment")
        return 1

    try:
        with socket.socket() as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((args.host, args.port))
            server.listen(1)
            receive = RTDEReceiveInterface(args.robot_ip)
            control = RTDEControlInterface(args.robot_ip)
            relay = Relay(control, receive, args.robot_ip)
            shutdown = threading.Event()
            worker = threading.Thread(target=relay.motion_loop, args=(shutdown,))
            receiver = threading.Thread(target=relay.receive_loop, args=(shutdown,))
            log.info("Connected to UR5 %s; listening on %s:%d",
                     args.robot_ip, args.host, args.port)
            receiver.start()
            worker.start()
            try:
                while True:
                    conn, addr = server.accept()
                    with conn:
                        log.info("Windows connected from %s", addr)
                        try:
                            handle_client(conn, relay)
                        except (OSError, ValueError) as error:
                            log.info("Client connection ended: %s", error)
                        finally:
                            relay.submit("stop")
                            log.info("Windows disconnected from %s", addr)
            finally:
                shutdown.set()
                worker.join()
                receiver.join()
    except KeyboardInterrupt:
        log.info("Relay stopped")
    except (OSError, RuntimeError) as error:
        log.error("Relay failed: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
