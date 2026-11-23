#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Global web UI for the FlexEndoRobo controls and SCOPE camera.

Usage:
    python src/app.py --robot-ip 192.168.1.10
"""
import argparse
import json
import logging
import math
import threading
import time
import uuid
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from flex_endo_robo.control_node import (
    DEFAULT_JOG_SPEED_SCALE,
    FLEX_MAX_BEND,
    JOG_SPEED_SCALES,
    MAX_RCM_CONE_DEG,
    MIN_RCM_CONE_DEG,
    ControlNode,
)
from flex_endo_robo.flex_endo.flex_control import MAX_FLEX_BEND_DEG, MIN_FLEX_BEND_DEG, FlexControl
from flex_endo_robo.flex_endo.flex_ipc import MIN_SPEED_DEG_S, SPEED_PRESETS_DEG_S, FlexClient
from flex_endo_robo.ur5_rtde_server import TCP_HOST, TCP_PORT, UR5_IP, open_relay, serve_relay
from scope.camera import CAMERA_INDEX, CameraFeed
from scope.yolo import CATEGORIES, COLORS, MODEL_PATH
from status import COMMAND_NAMES, RECORDINGS_DIR, Recorder, Status, app_state
from keyboard_listener import KeyboardListener
from tasks import (
    ARM_INTERRUPT_CONFIRM_TIMEOUT_S, FLEX_STOP_CONFIRM_TIMEOUT_S,
    TASK_NAMES, TaskCoordinator, TaskExecutor, TaskState,
)
from llm import LLMService
from recording_web import RecordingArchive, serve_recording
from diagnostics import DiagnosticLog, RunLog

log = logging.getLogger("WindowsApp")
DASHBOARD_CONFIRM_TIMEOUT_S = 10.0
STATIC_DIR = Path(__file__).with_name("static")
STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/static/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
    "/static/theme.js": ("theme.js", "text/javascript; charset=utf-8"),
    "/static/keyboard.js": ("keyboard.js", "text/javascript; charset=utf-8"),
    "/static/llm.js": ("llm.js", "text/javascript; charset=utf-8"),
    "/imgs/ABML_logo.png": (Path(__file__).resolve().parents[1] / "imgs" / "ABML_logo.png", "image/png"),
}


def angle_limit(body, minimum, maximum, name):
    degrees = body.get('degrees')
    if type(degrees) not in (int, float) or not minimum <= degrees <= maximum or degrees != int(degrees):
        raise ValueError(f'{name} must be a whole number from {minimum}° to {maximum}°.')
    return int(degrees)



def parse_flex_speed_limit(body):
    speed = body.get('speed')
    if type(speed) in (int, float):
        try:
            if math.isfinite(speed) and speed >= MIN_SPEED_DEG_S:
                return float(speed)
        except OverflowError:
            pass
    raise ValueError('Enter a finite FlexEndo speed of at least 0.1°/s.')


class _Handler(BaseHTTPRequestHandler):
    timeout = 2.0

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        if getattr(self, '_audit', None) is not None:
            self.server.recorder.event('command_result', {
                **self._audit, 'http_status': code, 'response': obj,
            })
        self._send(code, 'application/json', json.dumps(obj).encode('utf-8'))

    def do_GET(self):
        url = urlsplit(self.path)
        archive = getattr(self.server, 'archive', None)
        if archive is not None and serve_recording(self, url, archive):
            return
        asset = STATIC_ROUTES.get(url.path)
        if asset is not None:
            filename, content_type = asset
            self._send(200, content_type, (STATIC_DIR / filename).read_bytes())
        elif url.path == '/api/state':
            try:
                state = app_state(self.server)
            except TimeoutError as error:
                return self._json({'error': str(error)}, 503)
            self._json(state)
        elif url.path == '/api/recording':
            self._json(self.server.recorder.status())
        elif url.path in ('/api/llm', '/api/diagnostics'):
            try:
                after = int(parse_qs(url.query).get('after', ['0'])[0])
                if after < 0:
                    raise ValueError()
            except ValueError:
                return self._json({'error': 'Expected a nonnegative event cursor.'}, 400)
            if url.path == '/api/diagnostics':
                diagnostics = getattr(self.server, 'diagnostics', None)
                return self._json(diagnostics.snapshot(after) if diagnostics else {'sequence': 0, 'messages': []})
            service = getattr(self.server, 'llm', None)
            self._json(service.snapshot(after) if service else {
                'enabled': False, 'phase': 'unavailable', 'error': 'LLM service unavailable.',
                'sequence': 0, 'events': []})
        elif url.path == '/api/config':
            self._json({
                'tasks': [{'id': i, 'label': name}
                          for i, name in enumerate(TASK_NAMES) if 1 <= i <= 15],
                'categories': [
                    {'id': class_id, 'label': label.title(), 'color': '#%02x%02x%02x' % COLORS[class_id][::-1]}
                    for class_id, label in CATEGORIES.items()
                ],
            })
        elif url.path == '/api/camera/state':
            self._json(self.server.camera.status())
        elif url.path == '/api/camera/stream':
            try:
                revision = int(parse_qs(url.query).get('revision', [''])[0])
            except ValueError:
                return self._json({'error': 'Expected a camera revision'}, 400)
            self._stream_camera(revision)
        else:
            self._send(404, 'text/plain', b'not found')

    def _stream_camera(self, revision):
        state = self.server.camera.status()
        if revision != state['revision'] or not state['ready']:
            return self._json({'error': 'Camera frame unavailable'}, 503)
        self.send_response(200)
        self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            for jpeg in self.server.camera.frames(revision):
                header = f'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n\r\n'
                self.wfile.write(header.encode('ascii') + jpeg + b'\r\n')
        except OSError:
            pass

    def do_POST(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
            maximum = 16384 if self.path == '/api/recording/console' else 4096
            if not 0 <= n <= maximum:
                raise ValueError("Invalid Content-Length")
            if n and self.headers.get_content_type() != 'application/json':
                raise ValueError("Expected application/json")
            body = json.loads(self.rfile.read(n).decode('utf-8')) if n else {}
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object")
        except (ValueError, TimeoutError) as error:
            return self._json({'error': str(error)}, 400)

        origin = self.headers.get('Origin')
        if origin is not None and origin != f"http://{self.headers.get('Host')}":
            return self._json({'error': 'Request must come from the control panel'}, 403)

        if self.path == '/api/llm/listen':
            try:
                return self._json(self.server.llm.set_enabled(body.get('enabled')))
            except (ValueError, RuntimeError) as error:
                return self._json({'error': str(error)}, 409)

        if self.path in ('/api/recording/start', '/api/recording/stop'):
            # Serialize recording changes with task entry, including direct API calls.
            with self.server.node.control_lock:
                if self._task_locked():
                    return self._json({'error': 'Left-column controls require TASK 0.'}, 409)
                return self._recording_command(body)
        if self.path == '/api/keyboard':
            if (not isinstance(body.get('client'), str) or not 1 <= len(body['client']) <= 100
                    or type(body.get('enabled')) is not bool):
                return self._json({'error': 'Expected keyboard client and enabled flag.'}, 400)
            task = self.server.tasks.snapshot()
            tracking_cancel = (task['id'] == 16 and not task['error']
                               and not self.server.stopping and not self.server.interrupting)
            state = self.server.keyboard.poll(body['client'], body['enabled'])
            if self._task_locked() and not tracking_cancel:
                state['commands'] = [c for c in state['commands'] if c == 'listen']
            elif tracking_cancel:
                state['commands'] = [c for c in state['commands'] if c in ('listen', 'tracking')]
            return self._json(state)
        if self.path == '/api/recording/console':
            if (not all(isinstance(body.get(key), str) for key in ('source', 'message', 'level'))
                    or len(body['message']) > 3000):
                return self._json({'error': 'Expected a console source, message and level.'}, 400)
            self.server.recorder.event('console', {**body, 'origin': 'dashboard'})
            level = {'error': logging.ERROR, 'warning': logging.WARNING}.get(body['level'], logging.INFO)
            log.log(level, '[%s] %s', body['source'][:80], body['message'],
                    extra={'from_dashboard': True})
            return self._json({'ok': True})

        self._control_command(body)

    def _task_locked(self):
        task = self.server.tasks.snapshot()
        return (task['id'] != 0 or bool(task['error']) or self.server.interrupting
                or self.server.stopping)

    def _recording_command(self, body):
        if self.path == '/api/recording/start':
            if self.server.stopping:
                return self._json({'error': 'Control session is stopping.'}, 409)
            try:
                return self._json({'ok': True, **self.server.recorder.start()})
            except (OSError, RuntimeError, ValueError) as error:
                return self._json({'error': str(error)}, 503)
        if self.path == '/api/recording/stop':
            if body.get('confirmed') is not True:
                return self._json({'error': 'Confirm before stopping the recording.'}, 400)
            try:
                state = self.server.recorder.stop()
                return self._json({'ok': state['error'] is None, **state})
            except RuntimeError as error:
                return self._json({'error': str(error)}, 503)

    def _control_command(self, body):
        if self.path == '/api/freedrive/heartbeat':
            lease = body.get('lease')
            if not isinstance(lease, str) or not 1 <= len(lease) <= 100:
                return self._json({'error': 'Expected a Freedrive lease.'}, 400)
            relay = self.server.relay
            accepted = relay is not None and relay.refresh_freedrive(lease)
            return self._json({'ok': accepted}, 200 if accepted else 409)
        if getattr(self.server, 'recorder', None) is not None:
            self._audit = {'id': uuid.uuid4().hex, 'path': self.path,
                           'command': COMMAND_NAMES.get(self.path, self.path), 'body': body}
            if self.path == '/api/flex/calibrate':
                try:
                    self._audit['pose_at_request'] = app_state(self.server)
                except TimeoutError as error:
                    self._audit['pose_at_request'] = {'error': str(error)}
            self.server.recorder.event('command', self._audit)

        if self.path == '/api/cmd/freedrive':
            return self._freedrive_command(body)

        if self.path == '/api/camera/category':
            if 'category' not in body:
                return self._json({'error': 'Expected a category ID or null'}, 400)
            try:
                with self.server.node.control_lock:
                    if self.server.tasks.snapshot()['id'] == 16:
                        return self._json({'error': 'Use INTERRUPT before changing the tracked category.'}, 409)
                    prior = self.server.camera.status()['revision']
                    state = self.server.camera.select_category(body['category'])
                    if (self.server.node.flex and self.server.node.flex.task_direction is None
                            and state['revision'] != prior):
                        self.server.node.stop_camera_tracking()
            except ValueError as error:
                return self._json({'error': str(error)}, 400)
            return self._json(state)

        if self.path == '/api/cmd/interrupt':
            return self._interrupt_motion()
        if self.path == '/api/task':
            task_id = body.get('id')
            if type(task_id) is not int or not 0 <= task_id <= 16:
                return self._json({'error': 'Expected a task ID from 0 to 16.'}, 400)
            zoom = body.get('zoom')
            if zoom is not None and (type(zoom) not in (int, float) or not 1 <= zoom <= 2):
                return self._json({'error': 'Expected a zoom ratio from 1 to 2.'}, 400)
            return self._interrupt_motion(task_id, zoom)
        if self.path == '/api/task/complete':
            return self._json({'error': 'Tasks complete from device feedback. Use INTERRUPT to cancel.'}, 409)
        if self.path == '/api/flex/tracking':
            if type(body.get('enabled')) is not bool:
                return self._json({'error': 'Expected a boolean enabled value.'}, 400)
            return self._interrupt_motion(16 if body['enabled'] else 0)

        # A release may always clean up ownership; heartbeats cannot revive a held key.
        release = self.path == '/api/key' and body.get('down') is False
        if self.path == '/api/cmd/stop':
            return self._stop_session()
        with self.server.node.control_lock:
            if self._task_locked() and not release:
                return self._json({'error': 'Left-column controls require TASK 0. Use INTERRUPT.'}, 409)

        if self.server.relay is None and self.server.node.flex is None:
            return self._json({'error': 'Robot controls are disabled in camera preview.'}, 503)

        with self.server.node.control_lock:
            if self._task_locked() and not release:
                return self._json({'error': 'Left-column controls require TASK 0. Use INTERRUPT.'}, 409)
            if self.server.stopping:
                return self._json({'error': 'Control session is stopping.'}, 503)
            if self.server.interrupting and not release:
                return self._json({'error': 'Waiting for motion to stop.'}, 409)
            try:
                accepted = self._dispatch(body)
            except ValueError as error:
                return self._json({'error': str(error)}, 400)
            except KeyError:
                return self._json({'error': 'Unknown endpoint'}, 404)
        if accepted is False:
            if self.path == '/api/cmd/reset':
                return self._json({'error': 'The UR5 relay did not receive RESET.'}, 409)
            return self._json({'error': 'Command unavailable. Check the current mode and diagnostics.'}, 409)
        self._json({'ok': True})

    def _task_event(self, task, reason):
        self.server.recorder.event('task', {**task, 'reason': reason})
        log.info('TASK %s · %s (%s)', task['id'], task['name'], reason)

    def _freedrive_command(self, body):
        enabled, lease = body.get('enabled'), body.get('lease')
        if type(enabled) is not bool or (enabled and (
                not isinstance(lease, str) or not 1 <= len(lease) <= 100)):
            return self._json({'error': 'Expected enabled (boolean) and a Freedrive lease for entry.'}, 400)
        node, relay = self.server.node, self.server.relay
        with node.control_lock:
            if relay is None:
                return self._json({'error': 'The UR5 is disabled for this session.'}, 503)
            if enabled and self._task_locked():
                return self._json({'error': 'Freedrive requires TASK 0. Use INTERRUPT.'}, 409)
            if self.server.stopping or self.server.interrupting:
                return self._json({'error': 'Waiting for motion to stop.'}, 409)
            if enabled:
                flex = node.flex
                if (not node._arm_ready() or node._arm_moving is not False
                        or any(node._key_state.values()) or node._teleop_servo_active
                        or node.pull_out_active or (flex and (
                            not flex.ready(calibrated=False) or flex.status.moving
                            or flex.snapshot()['busy'] or flex.tracking or flex.spray_busy))):
                    return self._json({'error': 'Stop motion and release the controls before entering Freedrive.'}, 409)
            try:
                result = relay.request_freedrive(enabled, lease)
            except ValueError as error:
                return self._json({'error': str(error)}, 409)
            if enabled:
                node.prepare_freedrive()
        try:
            failure = result.result(timeout=ARM_INTERRUPT_CONFIRM_TIMEOUT_S)
        except FutureTimeoutError:
            relay.submit('stop')
            return self._json({'error': 'The UR5 did not confirm the Freedrive change. Stop requested.'}, 504)
        return self._json({'ok': not bool(failure), 'error': failure}, 503 if failure else 200)

    def _interrupt_motion(self, task_id=0, zoom=None):
        body, code = TaskCoordinator(self.server).select(task_id, zoom)
        self._json(body, code)

    def _stop_session(self):
        node = self.server.node
        with node.control_lock:
            if self._task_locked():
                return self._json({'error': 'Left-column controls require TASK 0. Use INTERRUPT.'}, 409)
            if self.server.stopping:
                return self._json({'error': 'Control session is already stopping.'}, 503)
            self.server.stopping = True
            self.server.executor.cancel()
            node.cancel_motion()
            flex_request = (node.flex.stop_request
                            if node.flex is not None and node.flex_stop_delivered else None)
        try:
            self._confirm_stop(flex_request)
        finally:
            self.server.shutdown()

    def _confirm_stop(self, flex_request):
        relay = self.server.relay
        try:
            result = relay.submit('estop') if relay is not None else None
        except RuntimeError as error:
            return self._json({'error': str(error), 'flex_stop_sent': flex_request is not None}, 503)
        flex_state = self._wait_for_flex_stop(flex_request)
        flex_stopped = flex_state != 'unconfirmed'
        if result is None:
            return self._json({'ok': True, 'arm': 'disabled', 'flex': flex_state} if flex_stopped else
                              {'error': 'Flex did not confirm Stop.', 'arm': 'disabled', 'flex': flex_state},
                              200 if flex_stopped else 503)
        try:
            failure = result.result(timeout=DASHBOARD_CONFIRM_TIMEOUT_S)
        except FutureTimeoutError:
            return self._json({'error': 'PolyScope did not confirm Dashboard Stop before the timeout.',
                               'flex': flex_state}, 504)
        if failure:
            return self._json({'error': failure, 'flex': flex_state}, 502)
        if not flex_stopped:
            return self._json({'error': 'PolyScope stopped. Flex did not confirm Stop.'}, 503)
        self._json({'ok': True, 'arm': 'confirmed', 'flex': flex_state})

    def _wait_for_flex_stop(self, flex_request, *, idle=False):
        flex = self.server.node.flex
        if flex is None:
            return 'disabled'
        if flex_request is not None:
            deadline = time.monotonic() + FLEX_STOP_CONFIRM_TIMEOUT_S
            while time.monotonic() < deadline:
                with self.server.node.control_lock:
                    if (flex.stop_acknowledged(flex_request) and
                            (not idle or (flex.status.action == 0 and flex.stop_request is None))):
                        return 'confirmed'
                time.sleep(0.01)
        return 'unconfirmed'

    def _dispatch(self, body):
        node = self.server.node
        flex = node.flex
        release = self.path == '/api/key' and body.get('down') is False
        if (self.server.relay is not None and self.server.relay.freedrive_busy() is True
                and not release):
            raise ValueError('Exit Freedrive before moving or changing robot settings.')
        if self.path == '/api/cmd/clean_lens':
            if flex is None:
                raise ValueError('The Flex hardware interface is disabled.')
            return flex.clean_lens(node)
        if flex is not None and flex.spray_busy and not (
                self.path == '/api/key/heartbeat' or
                (self.path == '/api/key' and body.get('down') is False)):
            raise ValueError('Release the spray control or cancel CLEAN LENS before moving or changing settings.')
        if self.path in ('/api/speed/arm/linear', '/api/limits/arm'):
            if self.server.relay is None:
                raise ValueError('The arm is disabled for this session.')
            if self.path == '/api/limits/arm' and 'degrees' in body:
                degrees = angle_limit(body, MIN_RCM_CONE_DEG, MAX_RCM_CONE_DEG, 'RCM tip limit')
                if 'speed' in body and body['speed'] is None:
                    raise ValueError('Choose a UR5 speed preset.')
                return node.set_rcm_cone_limit(degrees, speed=body.get('speed'), relay=self.server.relay)
            if (any(node._key_state.values()) or node.pull_out_active
                    or (flex and flex.tracking)):
                raise ValueError('Finish the current motion and release the controls before changing speed.')
            return self.server.relay.set_speed_limit(body.get('speed'))
        if self.path == '/api/speed/flex':
            speed = parse_flex_speed_limit(body)
            return flex.set_speed_limit(speed) if flex else node.set_model_flex_speed(speed)
        if self.path == '/api/limits/flex':
            speed = parse_flex_speed_limit(body) if 'speed' in body else None
            if speed is not None and speed not in SPEED_PRESETS_DEG_S:
                raise ValueError('Choose a FlexEndo speed preset.')
            if speed is not None and 'degrees' not in body:
                return flex.set_speed_limit(speed) if flex else node.set_model_flex_speed(speed)
            maximum = MAX_FLEX_BEND_DEG if flex else round(math.degrees(FLEX_MAX_BEND))
            degrees = angle_limit(body, MIN_FLEX_BEND_DEG, maximum, 'Flex bend limit')
            return flex.set_bend_limit(degrees, speed=speed) if flex else node.set_model_bend_limit(degrees, speed=speed)
        if flex is not None and (
                self.path in ('/api/cmd/bird_eye', '/api/cmd/pull_out', '/api/flex/calibrate',
                              '/api/flex/return_zero', '/api/flex/confirm_pull_out')
                or (self.path == '/api/flex/tracking' and body.get('enabled') is True)
                or (self.path == '/api/key' and body.get('down') is True and
                    isinstance(body.get('key'), str) and body['key'].startswith('flex_'))):
            issue = flex.connection_issue()
            if issue:
                raise ValueError(issue)
        if self.path.startswith('/api/flex/'):
            if node.flex is None:
                raise ValueError("The Flex hardware interface is disabled.")
            if self.path == '/api/flex/calibrate':
                degrees = (angle_limit(body, MIN_FLEX_BEND_DEG, MAX_FLEX_BEND_DEG, 'Flex bend limit')
                           if 'degrees' in body else None)
                return node.flex.calibrate(degrees)
            if self.path == '/api/flex/return_zero':
                return node.flex.return_to_zero()
            if self.path == '/api/flex/recheck_startup':
                return node.flex.recheck_startup_inputs()
            if self.path == '/api/flex/confirm_pull_out':
                return node.flex.confirm_pull_out(node)
        if self.path == '/api/key':
            key, down = body.get('key'), body.get('down')
            if not isinstance(key, str) or type(down) is not bool:
                raise ValueError("Expected a key name and a boolean down value")
            return node.set_key(key, down)
        if self.path == '/api/key/heartbeat':
            held = body.get('held')
            if not isinstance(held, list) or not all(isinstance(key, str) for key in held):
                raise ValueError("Expected a list of held keys")
            return node.refresh_keys(held)
        if self.path == '/api/cmd/pull_out':
            return node.cmd_pull_out()
        commands = {
            '/api/mode/arm': node.toggle_arm_mode,
            '/api/cmd/reset': node.cmd_reset,
            '/api/cmd/bird_eye': node.cmd_bird_eye_view,
        }
        return commands[self.path]()

    def log_message(self, *args):
        pass


def main():
    logging.basicConfig(level=logging.INFO,
                        format='[Windows] %(asctime)s - %(message)s')
    with RunLog(Path(__file__).resolve().parents[1] / 'logs') as run_log:
        log.info('Run warnings/errors log: %s', run_log.path)
        try:
            return _run_app()
        except KeyboardInterrupt:
            return 0
        except Exception:
            log.exception('Windows app failed')
            return 1


def _run_app():

    ap = argparse.ArgumentParser()
    ap.add_argument('--urdf', default=Path(__file__).with_name('flex_endo_robo') / 'ur5_endo_flex.urdf',
                    help='Path to ur5_endo_flex.urdf')
    ap.add_argument('--robot-ip', default=UR5_IP, help='UR5 IP address')
    ap.add_argument('--relay-port', type=int, default=TCP_PORT, help='Internal RTDE relay port')
    ap.add_argument('--port-web', type=int, default=8080)
    ap.add_argument('--host-web', default='127.0.0.1')
    ap.add_argument('--rcm-local', nargs=3, type=float, default=[0.0, 0.0, -0.30])
    ap.add_argument('--tip-local', nargs=3, type=float, default=[0.0, 0.0, 0.20195])
    ap.add_argument('--jog-speed-scale', type=float, default=DEFAULT_JOG_SPEED_SCALE,
                    choices=JOG_SPEED_SCALES,
                    help='Manual arm speed factor relative to the speed limits (10 uses the full limit)')
    ap.add_argument('--camera-index', type=int, default=CAMERA_INDEX, help='DirectShow camera index')
    ap.add_argument('--camera-roll-offset-deg', type=float, default=0.0,
                    help='Measured optical roll relative to the modeled Flex camera, about its forward axis')
    ap.add_argument('--model', type=Path, default=MODEL_PATH, help='Path to YOLO weights')
    ap.add_argument('--recordings-dir', type=Path, default=RECORDINGS_DIR,
                    help='Session recording output directory (default: recordings in repository root)')
    ap.add_argument('--llm-config', type=Path, default=Path(__file__).with_name('llm') / 'settings.json')
    ap.add_argument('--microphone', help='Input device name; default: system microphone')
    run_mode = ap.add_mutually_exclusive_group()
    run_mode.add_argument('--camera-only', action='store_true', help='Passive camera preview')
    run_mode.add_argument('--flex-only', action='store_true', help='Connect Flex hardware without opening RTDE')
    run_mode.add_argument('--arm-only', action='store_true', help='Use RTDE and modeled Flex without a motor driver')
    args = ap.parse_args()
    if not -180.0 <= args.camera_roll_offset_deg <= 180.0:
        ap.error('--camera-roll-offset-deg must be between -180 and 180')

    try:
        with ExitStack() as resources:
            srv = resources.enter_context(ThreadingHTTPServer((args.host_web, args.port_web), _Handler))
            srv.diagnostics = DiagnosticLog().start()
            resources.callback(srv.diagnostics.close)
            srv.stopping = False
            srv.interrupting = False
            srv.tasks = TaskState()
            srv.archive = RecordingArchive(args.recordings_dir)
            srv.relay = None
            relay_port = args.relay_port
            if not (args.camera_only or args.flex_only):
                relay_server, srv.relay = resources.enter_context(open_relay(
                    args.robot_ip, TCP_HOST, relay_port, urdf_path=args.urdf,
                    rcm_point_local=args.rcm_local, tip_local=args.tip_local))
                relay_port = relay_server.getsockname()[1]
            srv.camera = resources.enter_context(CameraFeed(args.camera_index, args.model))
            flex = (None if args.camera_only or args.arm_only else
                    FlexControl(FlexClient(), srv.camera,
                                camera_roll_offset_deg=args.camera_roll_offset_deg))
            srv.node = ControlNode(
                relay_host=TCP_HOST,
                relay_port=relay_port,
                urdf_path=args.urdf,
                rcm_point_local=args.rcm_local,
                tip_local=args.tip_local,
                jog_speed_scale=args.jog_speed_scale,
                flex=flex,
            )
            srv.recorder = Recorder(Status(srv, vars(args)), args.recordings_dir)
            srv.executor = TaskExecutor(srv)
            srv.node.task_executor = srv.executor
            srv.keyboard = KeyboardListener().start()
            resources.callback(srv.keyboard.close)
            srv.camera.recorder = srv.recorder
            resources.callback(srv.recorder.close)
            if srv.relay is not None:
                relay_shutdown = threading.Event()
                relay_thread = threading.Thread(
                    name='rtde-relay', target=serve_relay,
                    args=(relay_server, srv.relay, relay_shutdown),
                )
                relay_thread.start()
                resources.callback(relay_thread.join)
                resources.callback(relay_shutdown.set)
            resources.callback(srv.node.stop)
            srv.llm = LLMService(srv, args.llm_config, args.microphone)
            resources.callback(srv.llm.close)
            srv.node.start(connect_arm=srv.relay is not None)
            srv.llm.start()
            log.info("Web panel: http://%s:%d", args.host_web, srv.server_port)
            try:
                srv.serve_forever()
            finally:
                log.info("Shutting down...")
    except KeyboardInterrupt:
        pass
    except (OSError, RuntimeError, OverflowError) as error:
        log.exception("Windows app failed: %s", error)
        return 1
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
