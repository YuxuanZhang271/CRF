"""Opt-in real IPC checks. The spawned native driver ALWAYS uses --simulate.

Set CRF_SIM_DRIVER to the built executable to run. No controller/serial I/O.
"""
import os
import subprocess
import sys
import time
import unittest
import json
import io
from http.client import HTTPResponse
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from flex_endo_robo.flex_endo import flex_ipc as ipc


@unittest.skipUnless(os.environ.get('CRF_SIM_DRIVER'), 'Set CRF_SIM_DRIVER for simulated native IPC checks')
class SimulatedDriverTests(unittest.TestCase):
    def test_http_holds_release_and_interrupt_with_real_coordinator(self):
        from app import _Handler
        from flex_endo_robo.control_node import ControlNode
        from flex_endo_robo.flex_endo.flex_control import FlexControl
        executable = Path(os.environ['CRF_SIM_DRIVER']).resolve()
        process = subprocess.Popen([str(executable), '--simulate', '--managed-session'],
                                    cwd=executable.parent, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(lambda: self.close_process(process))
        time.sleep(0.1)
        self.assertIsNone(process.poll(), 'Simulated driver did not acquire exclusive ownership')
        camera = SimpleNamespace(observation=lambda: None)
        client = ipc.FlexClient()
        self.addCleanup(client.close)
        client.start()
        deadline = time.monotonic() + 2.
        while client.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(client.status)
        self.assertTrue(client.status.flags & ipc.SIMULATED)
        root = Path(__file__).resolve().parents[1]
        node = ControlNode('127.0.0.1', 0, root / 'src/flex_endo_robo/ur5_endo_flex.urdf')
        node.flex = FlexControl(client, camera)
        node.start(connect_arm=False)
        self.addCleanup(node.stop)
        from tasks import TaskExecutor, TaskState
        server = SimpleNamespace(tasks=TaskState(), node=node, camera=camera, relay=None, stopping=False,
                                 interrupting=False, recorder=SimpleNamespace(event=lambda *a: None))
        server.executor = TaskExecutor(server)
        node.task_executor = server.executor

        def call(path, body=None):
            # Feed real HTTP bytes to the actual handler without network access.
            data = b'' if body is None else json.dumps(body).encode()
            method = 'GET' if body is None else 'POST'
            headers = (f'{method} {path} HTTP/1.0\r\nHost: localhost\r\n'
                       f'Content-Type: application/json\r\nContent-Length: {len(data)}\r\n\r\n')
            output = bytearray()
            connection = SimpleNamespace(makefile=lambda *a: io.BytesIO(headers.encode() + data),
                                          sendall=output.extend, settimeout=lambda *a: None)
            _Handler(connection, ('127.0.0.1', 0), server)
            response = HTTPResponse(SimpleNamespace(makefile=lambda *a: io.BytesIO(output)))
            response.begin()
            return response.status, json.loads(response.read())

        def wait_phase(phase, timeout=2.):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                code, state = call('/api/state')
                flex_state = state.get('flex_hardware', {})
                if (code == 200 and flex_state.get('connected') and flex_state.get('spray_phase') == phase
                        and (phase != 'idle' or not flex_state.get('spray_busy'))):
                    return state
                time.sleep(0.02)
            self.fail(f'HTTP pump phase did not become {phase}')

        wait_phase('idle')
        self.assertEqual(call('/api/key', {'key': 'flex_water', 'down': True})[0], 200)
        wait_phase('water')
        self.assertEqual(call('/api/cmd/reset', {})[0], 400)
        self.assertEqual(call('/api/key', {'key': 'flex_water', 'down': False})[0], 200)
        wait_phase('idle')
        self.assertEqual(call('/api/cmd/clean_lens', {})[0], 200)
        wait_phase('cleaning_water')
        code, result = call('/api/cmd/interrupt', {})
        self.assertEqual((code, result.get('flex')), (200, 'confirmed'))
        wait_phase('idle')
        self.assertEqual(call('/api/key', {'key': 'flex_gas', 'down': True})[0], 200)
        wait_phase('gas')
        # No browser heartbeat: the coordinator must terminate the held action.
        wait_phase('idle')
        for task_id, phase in ((14, 'water'), (15, 'gas')):
            code, response = call('/api/task', {'id': task_id})
            self.assertEqual(code, 200, response)
            wait_phase(phase)
            started = time.monotonic()
            state = wait_phase('idle', timeout=5.)
            self.assertGreaterEqual(time.monotonic() - started, 2.8)
            deadline = time.monotonic() + 1.
            while state['task']['id'] and time.monotonic() < deadline:
                time.sleep(.01)
                state = call('/api/state')[1]
            self.assertEqual(state['task']['id'], 0)
        code, response = call('/api/task', {'id': 14})
        self.assertEqual(code, 200, response)
        wait_phase('water')
        self.assertEqual(call('/api/cmd/interrupt', {})[0], 200)
        self.assertEqual(wait_phase('idle')['task']['id'], 0)
        self.assertEqual(call('/api/flex/calibrate', {'degrees': 60})[0], 200)
        deadline = time.monotonic() + 3.
        while time.monotonic() < deadline:
            with node.control_lock:
                calibrated = node.flex.status.calibrated and node.flex.pending is None
            if calibrated:
                break
            time.sleep(.01)
        self.assertTrue(calibrated, 'Wait for native calibration completion before selecting a task')
        wait_phase('idle')
        for task_id in (5, 6, 7, 8, 9, 10):
            before = call('/api/state')[1]['flex_hardware']
            code, response = call('/api/task', {'id': task_id})
            self.assertEqual(code, 200, response)
            time.sleep(.25)
            state = call('/api/state')[1]
            self.assertIsNone(state['task']['error'], state['task'])
            self.assertEqual(state['task']['id'], task_id)
            after = state['flex_hardware']
            if task_id in (9, 10):
                self.assertGreater(abs(after['roll'] - before['roll']), .01)
            else:
                self.assertGreater(sum(abs(a - b) for a, b in zip(
                    after['bend_point'], before['bend_point'])), .01)
            self.assertEqual(call('/api/cmd/interrupt', {})[0], 200)
            wait_phase('idle')
        code, response = call('/api/task', {'id': 12})
        self.assertEqual(code, 200, response)
        state = wait_phase('idle', timeout=5.)
        deadline = time.monotonic() + 1.
        while state['task']['id'] and time.monotonic() < deadline:
            time.sleep(.01)
            state = call('/api/state')[1]
        self.assertEqual(state['task']['id'], 0)

    def test_holds_cleaning_cancellation_and_watchdog_through_real_ipc(self):
        executable = Path(os.environ['CRF_SIM_DRIVER']).resolve()
        process = subprocess.Popen([str(executable), '--simulate', '--managed-session'],
                                    cwd=executable.parent, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(lambda: self.close_process(process))
        # Refuse to use any pre-existing driver's mappings if our own driver
        # could not acquire the shared hardware ownership mutex.
        time.sleep(0.1)
        self.assertIsNone(process.poll(), 'Simulated driver did not acquire exclusive ownership')
        client = ipc.FlexClient()
        self.addCleanup(client.close)
        client.start()

        def wait_for(predicate, timeout=2., publish=True):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                self.assertIsNone(process.poll(), 'Simulated driver exited unexpectedly')
                status = client.poll()
                if status is not None:
                    self.assertTrue(status.flags & ipc.SIMULATED, 'Never send intent to a physical driver')
                    if predicate(status):
                        return status
                    if publish:
                        client.send()
                time.sleep(0.01)
            self.fail(f'Timed out: last status {client.status}')

        client.command(ipc.IDLE)
        wait_for(lambda s: s.app == client.app)
        self.assertTrue(client.status.flags & ipc.WATER_SPRAY_READY)
        self.assertTrue(client.status.flags & ipc.GAS_SPRAY_READY)
        for action, phase in [(ipc.WATER_SPRAY, 1), (ipc.GAS_SPRAY, 2)]:
            client.request(action)
            wait_for(lambda s: s.spray_phase == phase)
            stop = client.request(ipc.STOP)
            wait_for(lambda s: s.request == stop and s.result == ipc.DONE and s.spray_phase == 0)
            client.command(ipc.IDLE)
            wait_for(lambda s: s.action == ipc.IDLE)

        clean = client.request(ipc.CLEAN_LENS)
        wait_for(lambda s: s.spray_phase == 3)
        water_at = time.monotonic()
        wait_for(lambda s: s.spray_phase == 5, timeout=4.)
        gas_at = time.monotonic()
        self.assertGreaterEqual(gas_at - water_at, 2.8)
        wait_for(lambda s: s.request == clean and s.result == ipc.DONE and s.spray_phase == 0, timeout=4.)
        self.assertGreaterEqual(time.monotonic() - gas_at, 2.8)

        client.command(ipc.IDLE)
        wait_for(lambda s: s.action == ipc.IDLE)
        client.request(ipc.CLEAN_LENS)
        wait_for(lambda s: s.spray_phase == 3)
        stop = client.request(ipc.STOP)
        wait_for(lambda s: s.request == stop and s.result == ipc.DONE and s.spray_phase == 0)
        client.command(ipc.IDLE)
        wait_for(lambda s: s.action == ipc.IDLE)
        client.request(ipc.WATER_SPRAY)
        wait_for(lambda s: s.spray_phase == 1)
        # The IPC worker continues running, but cannot refresh stale coordinator intent.
        wait_for(lambda s: s.app == 0 and s.spray_phase == 0, publish=False)
        client.command(ipc.IDLE)
        wait_for(lambda s: s.app == client.app)
        self.assertEqual(client.status.spray_phase, 0, 'Reconnect must not restart spraying')

    @staticmethod
    def close_process(process):
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=3.)


if __name__ == '__main__':
    unittest.main()
