"""Exercise worker isolation using frames and IPC mappings held behind events."""

import queue
import sys
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flex_endo_robo.flex_endo import flex_ipc as ipc
from scope.camera import CameraFeed
from scope.yolo import Detection


class CameraThreadTests(unittest.TestCase):
    def test_preview_keeps_advancing_and_inference_skips_queued_frames(self):
        frames = queue.Queue()
        entered, release = threading.Event(), threading.Event()
        seen = []

        def detect(frame, category):
            seen.append(int(frame[0, 0, 0]))
            if len(seen) == 1:
                entered.set()
                if not release.wait(2.):
                    raise TimeoutError("Test did not release inference")
            return [Detection(39, 1., (2, 2, 12, 12))]

        capture = SimpleNamespace(read=lambda: (True, frames.get(timeout=2.)), release=lambda: None)
        detector = SimpleNamespace(detect=detect)
        with patch("scope.camera.open_camera", return_value=capture), \
                patch("scope.camera.Detector", return_value=detector):
            feed = CameraFeed()
            recorded = []
            feed.recorder = SimpleNamespace(event=lambda kind, data, **kwargs:
                                           recorded.append((kind, data, kwargs)))
            feed.select_category(39)
            feed.__enter__()
            try:
                frames.put(np.full((32, 32, 3), 10, dtype=np.uint8))
                self.assertTrue(entered.wait(1.))
                with feed._condition:
                    self.assertTrue(feed._condition.wait_for(lambda: feed._latest is not None, 1.))
                    first_jpeg = feed._latest
                for value in (20, 30):
                    frames.put(np.full((32, 32, 3), value, dtype=np.uint8))
                with feed._condition:
                    self.assertTrue(feed._condition.wait_for(lambda: feed._frame_id == 3, 1.))
                    self.assertNotEqual(first_jpeg, feed._latest)
                    newest_stamp = feed._raw[1]
                self.assertEqual(seen, [10])  # Preview advanced while inference was blocked.
                release.set()
                with feed._condition:
                    self.assertTrue(feed._condition.wait_for(
                        lambda: feed._observation is not None and feed._observation.frame == 3, 1.))
                self.assertEqual(seen, [10, 30])
                self.assertEqual(feed.observation().captured_at, newest_stamp)
                camera_records = [item for item in recorded if item[0] == 'camera_frame']
                self.assertTrue(camera_records)
                self.assertTrue(camera_records[-1][2]['jpeg'].startswith(b'\xff\xd8'))
                # Inference publishes its completed record just after waking observers.
                deadline = time.monotonic() + 1.
                while time.monotonic() < deadline and not any(
                        item[0] == 'yolo' and item[1]['observation'].frame == 3 for item in recorded):
                    time.sleep(.001)
                yolo = next(item[1] for item in recorded
                            if item[0] == 'yolo' and item[1]['observation'].frame == 3)
                self.assertEqual(yolo['observation'].captured_at, newest_stamp)
                self.assertEqual(yolo['detections'][0].class_id, 39)
                self.assertEqual(yolo['target'].class_id, 39)
                feed.select_category(41)
                self.assertIsNone(feed.observation())
            finally:
                release.set()
                with feed._condition:
                    feed._closed = True
                    feed._condition.notify_all()
                frames.put(np.zeros((32, 32, 3), dtype=np.uint8))
                feed.__exit__()
        self.assertFalse(feed._capture_worker.is_alive())
        self.assertFalse(feed._worker.is_alive())

    def test_old_category_result_is_discarded(self):
        feed = CameraFeed()
        feed.select_category(39)
        revision = feed.status()["revision"]
        entered, release = threading.Event(), threading.Event()
        second_entered, second_release = threading.Event(), threading.Event()
        calls = 0

        def detect(frame, category):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                release.wait(1.)
            else:
                second_entered.set()
                second_release.wait(1.)
            return [Detection(category, 1., (2, 2, 12, 12))]

        feed._raw = (1, time.monotonic(), revision, 39, np.zeros((32, 32, 3), dtype=np.uint8))
        with patch("scope.camera.Detector", return_value=SimpleNamespace(detect=detect)):
            feed._worker.start()
            try:
                self.assertTrue(entered.wait(1.))
                feed.select_category(41)
                with feed._condition:
                    feed._raw = (2, time.monotonic(), feed._revision, 41,
                                 np.zeros((32, 32, 3), dtype=np.uint8))
                    feed._condition.notify_all()
                release.set()
                self.assertTrue(second_entered.wait(1.))
                self.assertIsNone(feed.observation())
                self.assertIsNone(feed._detections)
            finally:
                with feed._condition:
                    feed._closed = True
                    feed._condition.notify_all()
                release.set()
                second_release.set()
                feed._worker.join(2.)
        self.assertFalse(feed._worker.is_alive())
        self.assertIsNone(feed.observation())
        self.assertIsNone(feed._detections)

    def test_capture_failure_discards_in_flight_detection(self):
        feed = CameraFeed()
        entered, release = threading.Event(), threading.Event()

        def detect(frame, category):
            entered.set()
            release.wait(1.)
            return [Detection(39, 1., (2, 2, 12, 12))]

        feed._raw = (1, time.monotonic(), 0, 39, np.zeros((32, 32, 3), dtype=np.uint8))
        with patch("scope.camera.Detector", return_value=SimpleNamespace(detect=detect)):
            feed._worker.start()
            try:
                self.assertTrue(entered.wait(1.))
                with feed._condition:
                    feed._error = "Camera disconnected"
                    feed._condition.notify_all()
            finally:
                release.set()
                feed._worker.join(2.)
        self.assertFalse(feed._worker.is_alive())
        self.assertIsNone(feed.observation())


class FlexThreadTests(unittest.TestCase):
    def setUp(self):
        self.api = SimpleNamespace(create_mutex=lambda *args: 1, close=lambda handle: None,
                                   now=lambda: round(time.monotonic() * 1000))
        with patch.object(ipc, "_Windows", return_value=self.api), \
                patch.object(ipc.ct, "get_last_error", return_value=0):
            self.client = ipc.FlexClient()
        self.addCleanup(self.client.close)
        self.status = ipc.FlexStatus(7, self.api.now(), self.client.app, 0, 0,
                                     ipc.IDLE, ipc.CALIBRATED, ipc.NO_RESULT, 0,
                                     0., 0., 0., 60., (0.,) * 4)

    def test_io_retries_contention_and_sends_stop_without_coordinator_tick(self):
        writes, condition = [], threading.Condition()
        busy = True

        def write(data):
            nonlocal busy
            if busy:
                busy = False
                raise ipc._MappingBusy("Simulated contended mapping")
            with condition:
                writes.append(ipc.COMMAND.unpack(data))
                condition.notify_all()

        self.client._maps["Command"] = SimpleNamespace(copy=write, close=lambda: None)

        def read_status():
            return replace(self.status, stamp=self.api.now())

        with patch.object(self.client, "_read_status", side_effect=read_status):
            self.client.start()
            deadline = time.monotonic() + 1.
            while self.client.poll() is None and time.monotonic() < deadline:
                threading.Event().wait(0.005)
            self.assertTrue(self.client.ready)
            self.client.command(ipc.JOG, x=1.)
            self.assertTrue(self.client.send())
            with condition:
                self.assertTrue(condition.wait_for(lambda: len(writes) >= 2, 1.))
            self.client.request(ipc.STOP)
            self.client.send()
            with condition:
                self.assertTrue(condition.wait_for(lambda: writes[-1][10] == ipc.STOP, 1.))
            self.assertIsNotNone(self.client.poll())
            self.client.close()
            self.assertFalse(self.client._worker.is_alive())

    def test_worker_does_not_renew_stale_motion_or_vision_timestamps(self):
        commands, vision = [], []
        self.client._maps = {
            "Command": SimpleNamespace(copy=commands.append, close=lambda: None),
            "Vision": SimpleNamespace(copy=vision.append, close=lambda: None),
        }
        self.client.command(ipc.TRACK, frame=1, x=1.)
        self.client._feedback = self.status
        observation = SimpleNamespace(frame=1, captured_at=9.8, valid=True, width=32,
                                      height=32, centered=False, dx=10., dy=0., category=39, revision=0)
        with patch.object(ipc.time, "monotonic", return_value=10.):
            self.client.send(observation)
            self.api.now = lambda: 10000
            self.client._write(self.status)
        with patch.object(ipc.time, "monotonic", return_value=10.6):
            self.api.now = lambda: 10600
            self.client._write(self.status)
        self.assertEqual([ipc.COMMAND.unpack(command)[7] for command in commands], [10000, 10000])
        self.assertEqual(len(vision), 1)
        self.assertEqual(ipc.VISION.unpack(vision[0])[7], 9800)


if __name__ == "__main__":
    unittest.main()
