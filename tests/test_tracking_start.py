"""Voice category selection must reach fresh inference before task activation."""

import asyncio
from concurrent.futures import Future
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from llm import LLMService
from scope.camera import CameraFeed
from scope.yolo import Observation
from tasks import TaskState


class TrackingStartTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.camera = CameraFeed()
        self.server = SimpleNamespace(camera=self.camera, tasks=TaskState(), recorder=Mock(),
            executor=Mock(), relay=None, interrupting=False, stopping=False,
            node=SimpleNamespace(control_lock=threading.RLock(), cmd_interrupt=Mock(return_value=None),
                                 flex_stop_delivered=False, flex=None))
        self.service = LLMService(self.server)
        self.service._desired = True
        self.service._generation = 1

    def command(self, intent='track', target='bottle'):
        return {'type': 'command.final', 'segment_id': 1, 'revision': 1,
                'slots': {'I': intent, 'T': target, 'P': 'none'}}

    def observation(self, *, revision=None, age=0., valid=False):
        return Observation(1, time.monotonic() - age,
            self.camera.status()['revision'] if revision is None else revision,
            39, 32, 32, valid, False, 0., 0.)

    def publish(self, observation):
        with self.camera._condition:
            self.camera._observation = observation
            self.camera._condition.notify_all()

    async def wait_entered(self, event):
        self.assertTrue(await asyncio.to_thread(event.wait, 1.))

    async def test_waits_after_stop_ack_for_matching_fresh_inference_without_holding_motion_lock(self):
        stop_entered, camera_entered = threading.Event(), threading.Event()
        ack = Future()
        self.server.relay = Mock()
        self.server.relay.freedrive_busy.return_value = False
        def interrupt(_):
            stop_entered.set()
            return ack
        self.server.node.cmd_interrupt.side_effect = interrupt
        original_wait = self.camera.wait_for_observation
        def wait(*args, **kwargs):
            camera_entered.set()
            return original_wait(*args, **kwargs)
        with patch.object(self.camera, 'wait_for_observation', side_effect=wait) as waiter:
            operation = asyncio.create_task(self.service._dispatch(self.command(), 1, 0))
            try:
                await self.wait_entered(stop_entered)
                waiter.assert_not_called()
                ack.set_result(None)
                await self.wait_entered(camera_entered)
                self.assertTrue(self.server.node.control_lock.acquire(blocking=False))
                self.server.node.control_lock.release()
                self.publish(self.observation(revision=0))
                await asyncio.sleep(.03)
                self.server.executor.activate.assert_not_called()
                self.publish(self.observation(age=1.))
                await asyncio.sleep(.03)
                self.server.executor.activate.assert_not_called()
                self.publish(self.observation(valid=False))  # Fresh frame may have no visible target yet.
                await asyncio.wait_for(operation, 1.)
            finally:
                if not ack.done():
                    ack.set_result(None)
                if not operation.done():
                    operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
        self.server.executor.activate.assert_called_once_with(16, self.server.tasks.snapshot()['revision'])
        self.assertIsNone(self.server.tasks.snapshot()['error'])
        self.assertTrue(self.service.snapshot()['events'][-1]['result']['ok'])

    async def test_stop_cancels_camera_wait_without_late_activation_or_overwriting_stop(self):
        entered = threading.Event()
        original = self.camera.wait_for_observation
        def wait(*args, **kwargs):
            entered.set()
            return original(*args, **kwargs)
        with patch.object(self.camera, 'wait_for_observation', side_effect=wait):
            operation = asyncio.create_task(self.service._dispatch(self.command(), 1, 0))
            await self.wait_entered(entered)
            self.service._generation += 1
            await self.service._dispatch(self.command('stop', 'none'), 2)
            await asyncio.wait_for(operation, 1.)
        self.server.executor.activate.assert_not_called()
        self.assertEqual(self.server.tasks.snapshot()['id'], 0)
        self.assertIsNone(self.server.tasks.snapshot()['error'])
        self.assertFalse(self.server.interrupting)
        self.assertFalse(self.service.snapshot()['events'][-1]['result']['ok'])

    async def test_camera_timeout_reports_readiness_failure_without_starting_tracking(self):
        original = self.camera.wait_for_observation
        def wait(*args, **kwargs):
            return original(*args, timeout=.03, **kwargs)
        with patch.object(self.camera, 'wait_for_observation', side_effect=wait):
            await self.service._dispatch(self.command(), 1, 0)
        self.server.executor.activate.assert_not_called()
        self.assertIn('Timed out waiting for a fresh YOLO observation for bottle',
                      self.server.tasks.snapshot()['error'])
        self.assertFalse(self.server.interrupting)

    async def test_changed_category_cannot_start_tracking_a_different_target(self):
        entered = threading.Event()
        original = self.camera.wait_for_observation
        def wait(*args, **kwargs):
            entered.set()
            return original(*args, **kwargs)
        with patch.object(self.camera, 'wait_for_observation', side_effect=wait):
            operation = asyncio.create_task(self.service._dispatch(self.command(), 1, 0))
            await self.wait_entered(entered)
            self.camera.select_category(41)
            await asyncio.wait_for(operation, 1.)
        self.server.executor.activate.assert_not_called()
        self.assertEqual(self.server.tasks.snapshot()['id'], 0)
        self.assertIsNone(self.server.tasks.snapshot()['error'])

    async def test_detector_failure_reports_actual_cause(self):
        entered = threading.Event()
        original = self.camera.wait_for_observation
        def wait(*args, **kwargs):
            entered.set()
            return original(*args, **kwargs)
        with patch.object(self.camera, 'wait_for_observation', side_effect=wait):
            operation = asyncio.create_task(self.service._dispatch(self.command(), 1, 0))
            await self.wait_entered(entered)
            with self.camera._condition:
                self.camera._detection_error = 'Detector failed'
                self.camera._condition.notify_all()
            await asyncio.wait_for(operation, 1.)
        self.server.executor.activate.assert_not_called()
        self.assertIn('Detector failed', self.server.tasks.snapshot()['error'])

    async def test_unexpected_preparation_error_cannot_fall_through_to_activation(self):
        with patch.object(self.camera, 'wait_for_observation', side_effect=TypeError('Unexpected camera failure')):
            with self.assertRaisesRegex(TypeError, 'Unexpected camera failure'):
                await self.service._dispatch(self.command(), 1, 0)
        self.server.executor.activate.assert_not_called()
        self.assertIn('Camera preparation did not complete', self.server.tasks.snapshot()['error'])
        self.assertFalse(self.server.interrupting)


if __name__ == '__main__':
    unittest.main()
