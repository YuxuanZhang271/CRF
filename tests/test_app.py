"""Exercise HTTP routes without starting any camera or robot workers."""

import io
import json
import sys
import threading
import unittest
from http.client import HTTPResponse
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from app import _Handler
from flex_endo_robo.control_node import ControlNode


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

    def test_state_and_config_remain_available(self):
        status, _, body = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertFalse(state["arm_enabled"])
        self.assertFalse(state["joints_received"])
        self.assertIn("flex_model_bend_point", state)
        status, _, body = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual([category["id"] for category in json.loads(body)["categories"]],
                         [0, 39, 41, 46, 56])

    def test_preview_rejects_motion_and_bad_json(self):
        self.assertEqual(self.request("POST", "/api/cmd/reset", "{}")[0], 503)
        self.assertEqual(self.request("POST", "/api/key", "[]")[0], 400)

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
