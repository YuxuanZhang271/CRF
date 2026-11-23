"""Exercise HTTP routes without starting any camera or robot workers."""

import io
import logging
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import Future
from http.client import HTTPResponse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from app import _Handler
from flex_endo_robo.control_node import ControlNode
from status import Recorder, Status
from keyboard_listener import KeyboardListener
from tasks import TaskExecutor, TaskState
from diagnostics import DiagnosticLog


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = SimpleNamespace()
        cls.server.node = ControlNode("127.0.0.1", 0, Path(__file__).resolve().parents[1]
                                      / "src/flex_endo_robo/ur5_endo_flex.urdf")
        cls.server.relay = None
        cls.server.stopping = False
        cls.server.interrupting = False
        cls.server.camera = SimpleNamespace(status=lambda: {
            "category": None, "revision": 0, "ready": False, "error": None,
        })

    def setUp(self):
        self.server.tasks = TaskState()
        self.temp = tempfile.TemporaryDirectory(prefix='recording-test-',
                                                 dir=Path(__file__).resolve().parents[1])
        self.server.recorder = Recorder(Status(self.server, {'camera_only': True}), self.temp.name)
        self.server.executor = TaskExecutor(self.server)
        self.server.node.task_executor = self.server.executor
        self.server.keyboard = KeyboardListener()

    def tearDown(self):
        self.server.recorder.close()
        self.temp.cleanup()

    def request(self, method, path, body=None):
        payload = body.encode() if body is not None else b""
        headers = (f"{method} {path} HTTP/1.0\r\nHost: localhost\r\n"
                   f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n")
        output = bytearray()
        connection = SimpleNamespace(
            makefile=lambda *args: io.BytesIO(headers.encode() + payload),
            sendall=output.extend,
            settimeout=lambda timeout: None,
        )
        _Handler(connection, ("127.0.0.1", 0), self.server)
        response = HTTPResponse(SimpleNamespace(makefile=lambda *args: io.BytesIO(output)))
        response.begin()
        return response.status, response.getheader("Content-Type"), response.read()

    def test_dashboard_loads_external_assets(self):
        status, content_type, html = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "text/html; charset=utf-8")
        for filename, mime in (("dashboard.css", "text/css"),
                               ("dashboard.js", "text/javascript")):
            path = f"/static/{filename}"
            self.assertIn(path.encode(), html)
            status, content_type, body = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertTrue(content_type.startswith(mime))
            self.assertGreater(len(body), 0)

    def test_only_explicit_static_routes_are_served(self):
        for path in ("/static/../app.py", "/static/app.py", "/missing"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)

    def test_diagnostic_feed_and_browser_terminal_forwarding(self):
        feed = DiagnosticLog()
        try:
            feed.emit(logging.LogRecord('Tasks', logging.ERROR, __file__, 1, 'Full task failure', (), None))
            with patch.object(self.server, 'diagnostics', feed, create=True):
                status, _, body = self.request('GET', '/api/diagnostics?after=0')
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)['messages'][0]['message'], 'Full task failure')
                self.assertEqual(json.loads(self.request('GET', '/api/diagnostics?after=1')[2])['messages'], [])
                self.assertEqual(self.request('GET', '/api/diagnostics?after=bad')[0], 400)
            with self.assertLogs('WindowsApp', level='INFO') as logs:
                status, _, _ = self.request('POST', '/api/recording/console',
                    json.dumps({'source': 'APP', 'level': 'info', 'message': 'Browser message without recording'}))
            self.assertEqual(status, 200)
            self.assertIn('Browser message without recording', logs.output[0])
            self.assertTrue(logs.records[0].from_dashboard)
        finally:
            feed.close()

    def test_state_and_config_remain_available(self):
        status, _, body = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertFalse(state["arm_enabled"])
        self.assertFalse(state["joints_received"])
        self.assertIn("flex_model_bend_point", state)
        self.assertEqual(state['task']['id'], 0)
        status, _, body = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual([category["id"] for category in json.loads(body)["categories"]],
                         [0, 39, 41, 46, 56])
        self.assertEqual([task['id'] for task in json.loads(body)['tasks']], list(range(1, 16)))

    def test_task_selection_switching_and_interrupt_clear_held_inputs(self):
        self.server.node._key_state['arm_up'] = True
        for task_id in range(1, 16):
            with patch.object(self.server.executor, 'activate'):
                status, _, body = self.request('POST', '/api/task', json.dumps({'id': task_id}))
            self.assertEqual(status, 200)
            task = json.loads(body)['task']
            self.assertEqual(task['id'], task_id)
            self.assertFalse(task['simulated'])
            self.assertFalse(any(self.server.node._key_state.values()))
            self.assertEqual(json.loads(self.request('GET', '/api/state')[2])['task'], task)
        self.assertEqual(self.request('POST', '/api/cmd/interrupt', '{}')[0], 200)
        self.assertEqual(self.server.tasks.snapshot()['id'], 0)
        self.assertFalse(self.server.stopping)

    def test_active_task_blocks_direct_controls_recording_and_native_shortcuts(self):
        self.assertEqual(self.request('POST', '/api/task', '{"id":3}')[0], 200)
        for path, body in (
                ('/api/cmd/bird_eye', {}), ('/api/cmd/clean_lens', {}),
                ('/api/cmd/pull_out', {}), ('/api/cmd/reset', {}), ('/api/cmd/stop', {}),
                ('/api/mode/arm', {}), ('/api/flex/calibrate', {}),
                ('/api/flex/return_zero', {}), ('/api/flex/recheck_startup', {}),
                ('/api/limits/arm', {'degrees': 20}), ('/api/limits/flex', {'degrees': 40}),
                ('/api/speed/flex', {'speed': 5}), ('/api/recording/start', {}),
                ('/api/recording/stop', {'confirmed': True}),
                ('/api/key', {'key': 'arm_up', 'down': True}),
                ('/api/key/heartbeat', {'held': ['arm_up']})):
            with self.subTest(path=path):
                self.assertEqual(self.request('POST', path, json.dumps(body))[0], 409)
        state = json.loads(self.request('POST', '/api/keyboard',
            '{"client":"task-test","enabled":true}')[2])
        self.assertEqual(state['commands'], [])
        self.assertFalse(self.server.stopping)
        # Key releases remain accepted with hardware enabled, even while TASK is active.
        with patch.object(self.server, 'relay', Mock()):
            self.assertEqual(self.request('POST', '/api/key', '{"key":"arm_up","down":false}')[0], 200)

    def test_browser_cannot_claim_task_completion(self):
        self.request('POST', '/api/task', '{"id":3}')
        task = self.server.tasks.snapshot()
        self.assertEqual(self.request('POST', '/api/task/complete',
            json.dumps({'id': 3, 'revision': task['revision']}))[0], 409)
        self.assertEqual(self.server.tasks.snapshot()['id'], 3)

    def test_native_alt_t_can_cancel_active_tracking(self):
        self.server.tasks.select(16)
        with patch.object(self.server.keyboard, 'poll', return_value={
                'available': True, 'commands': ['tracking']}) as poll:
            status, _, body = self.request('POST', '/api/keyboard',
                '{"client":"tracking-panel","enabled":true}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['commands'], ['tracking'])
        poll.assert_called_once_with('tracking-panel', True)

    def test_invalid_tasks_and_tracking_are_rejected(self):
        for task_id in (None, True, '1', 1.5, -1, 17):
            self.assertEqual(self.request('POST', '/api/task', json.dumps({'id': task_id}))[0], 400)
        self.assertEqual(self.request('POST', '/api/flex/tracking', '{"enabled":true}')[0], 503)
        self.assertEqual(self.server.tasks.snapshot()['id'], 16)
        self.assertIsNotNone(self.server.tasks.snapshot()['error'])

    def test_listen_shortcut_remains_available_during_tasks(self):
        self.server.tasks.select(3)
        with patch.object(self.server.keyboard, 'poll', return_value={
                'available': True, 'commands': ['record', 'listen', 'tracking']}) as poll:
            status, _, body = self.request('POST', '/api/keyboard',
                '{"client":"voice-panel","enabled":true}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['commands'], ['listen'])
        poll.assert_called_once_with('voice-panel', True)

    def test_failed_stop_keeps_task_locked_until_successful_interrupt(self):
        failure = Future()
        failure.set_result('UR5 did not confirm stop.')
        success = Future()
        success.set_result(None)
        with patch.object(self.server, 'relay', Mock()), \
                patch.object(self.server.node, 'cmd_interrupt', side_effect=[failure, success]):
            self.assertEqual(self.request('POST', '/api/task', '{"id":12}')[0], 503)
            task = self.server.tasks.snapshot()
            self.assertEqual(task['id'], 12)
            self.assertIn('UR5', task['error'])
            self.assertEqual(self.request('POST', '/api/cmd/reset', '{}')[0], 409)
            self.assertEqual(self.request('POST', '/api/task/complete',
                json.dumps({'id': 12, 'revision': task['revision']}))[0], 409)
            self.assertEqual(self.request('POST', '/api/cmd/interrupt', '{}')[0], 200)
        self.assertEqual(self.server.tasks.snapshot()['id'], 0)
        self.assertIsNone(self.server.tasks.snapshot()['error'])

    def test_interrupt_takes_priority_while_task_entry_waits_for_stop(self):
        result = Future()
        entered = threading.Event()
        responses = []

        def interrupt(_):
            entered.set()
            return result

        with patch.object(self.server, 'relay', Mock()), \
                patch.object(self.server.node, 'cmd_interrupt', side_effect=interrupt):
            worker = threading.Thread(target=lambda: responses.append(
                self.request('POST', '/api/task', '{"id":5}')[0]))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.request('POST', '/api/cmd/interrupt', '{}')[0], 202)
                self.assertEqual(self.server.tasks.snapshot()['id'], 0)
                self.assertEqual(self.request('POST', '/api/cmd/reset', '{}')[0], 409)
            finally:
                result.set_result(None)
                worker.join(2)
        self.assertEqual(responses, [200])
        self.assertEqual(self.server.tasks.snapshot()['id'], 0)
        self.assertFalse(self.server.interrupting)

    def test_task_transitions_are_recorded(self):
        self.request('POST', '/api/recording/start', '{}')
        self.request('POST', '/api/task', '{"id":3}')
        self.assertEqual(self.server.recorder.source.snapshot()['robot']['task']['id'], 3)
        self.request('POST', '/api/cmd/interrupt', '{}')
        session = self.server.recorder.status()['session']
        self.request('POST', '/api/recording/stop', '{"confirmed":true}')
        rows = [json.loads(line) for path in Path(session).glob('*/events.jsonl')
                for line in path.read_text(encoding='utf-8').splitlines()]
        self.assertEqual([row['data']['id'] for row in rows if row['type'] == 'task'], [3, 3, 0])

    def test_preview_rejects_motion_and_bad_json(self):
        self.assertEqual(self.request("POST", "/api/cmd/reset", "{}")[0], 503)
        self.assertEqual(self.request("POST", "/api/key", "[]")[0], 400)

    def test_keyboard_bridge_in_preview(self):
        self.assertEqual(self.request('POST', '/api/keyboard', '{}')[0], 400)
        body = '{"client":"test-dashboard","enabled":true}'
        self.assertEqual(self.request('POST', '/api/keyboard', body)[0], 200)
        self.assertTrue(self.server.keyboard.key(ord('R'), True, True, True))
        state = json.loads(self.request('POST', '/api/keyboard', body)[2])
        self.assertEqual(state['commands'], ['record'])

    def test_recording_in_preview_requires_confirmation_and_audits_rejections(self):
        self.assertEqual(self.request('POST', '/api/recording/start', '{}')[0], 200)
        state = json.loads(self.request('GET', '/api/recording')[2])
        self.assertTrue(state['active'])
        self.assertEqual(self.request('POST', '/api/cmd/bird_eye', '{}')[0], 503)
        self.assertEqual(self.request('POST', '/api/recording/console',
                                      json.dumps({'source': 'APP', 'message': 'Ready', 'level': 'info'}))[0], 200)
        self.assertEqual(self.request('POST', '/api/recording/console',
                                      json.dumps({'source': 'APP', 'message': '✓' * 2000,
                                                  'level': 'info'}, ensure_ascii=False))[0], 200)
        self.assertEqual(self.request('POST', '/api/recording/stop', '{}')[0], 400)
        self.assertTrue(self.server.recorder.status()['active'])
        self.assertEqual(self.request('POST', '/api/recording/stop', '{"confirmed":true}')[0], 200)
        rows = [json.loads(line) for path in Path(state['session']).glob('*/events.jsonl')
                for line in path.read_text(encoding='utf-8').splitlines()]
        command = next(row['data'] for row in rows if row['type'] == 'command')
        result = next(row['data'] for row in rows if row['type'] == 'command_result')
        self.assertEqual(command['command'], 'BIRD-EYE')
        self.assertEqual(command['id'], result['id'])
        self.assertEqual(result['http_status'], 503)
        self.assertTrue(any(row['type'] == 'console' and row['data']['message'] == 'Ready' for row in rows))
        config = rows[0]['data']['config']
        self.assertIn('module_defaults', config)
        self.assertIn('rcm_cone_deg', config['control'])

    def test_recording_status_survives_stale_control_feedback(self):
        node = self.server.node
        original = node._snapshot_at
        node._snapshot_at = 0.
        node._running.set()
        try:
            self.assertEqual(self.request('POST', '/api/recording/start', '{}')[0], 200)
            self.assertEqual(self.request('GET', '/api/recording')[0], 200)
            self.assertEqual(self.request('GET', '/api/state')[0], 503)
        finally:
            node._running.clear()
            node._snapshot_at = original

    def test_state_does_not_wait_for_the_motion_lock(self):
        completed = threading.Event()
        statuses = []

        def read_state():
            try:
                statuses.append(self.request("GET", "/api/state")[0])
            finally:
                completed.set()

        with self.server.node.control_lock:
            reader = threading.Thread(target=read_state)
            reader.start()
            responsive = completed.wait(1.)
        reader.join()
        self.assertTrue(responsive, "State requests blocked behind motion work")
        self.assertEqual(statuses, [200])

    def test_stalled_coordinator_does_not_serve_an_old_ready_state(self):
        node = self.server.node
        original = node._snapshot_at
        node._snapshot_at = 0.
        node._running.set()
        try:
            self.assertEqual(self.request("GET", "/api/state")[0], 503)
        finally:
            node._running.clear()
            node._snapshot_at = original


if __name__ == "__main__":
    unittest.main()
