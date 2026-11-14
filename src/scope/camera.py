"""Capture and encode one camera feed for all web viewers."""

import logging
import threading
import time
from contextlib import ExitStack

import cv2

from .yolo import CATEGORIES, MODEL_PATH, Detector, Observation, draw_detections, target_error


CAMERA_INDEX = 0
TARGET_WIDTH = 1504
TARGET_HEIGHT = 1504
TARGET_FPS = 30
FOURCC = "MJPG"

log = logging.getLogger("ScopeCamera")


def open_camera(index):
    capture = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    if not capture.isOpened():
        capture.release()
        raise RuntimeError(f"Could not open camera {index} (DirectShow)")
    capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*FOURCC))
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, TARGET_WIDTH)
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, TARGET_HEIGHT)
    capture.set(cv2.CAP_PROP_FPS, TARGET_FPS)
    log.info("Camera %d opened at %.0fx%.0f, %.1f FPS", index,
             capture.get(cv2.CAP_PROP_FRAME_WIDTH), capture.get(cv2.CAP_PROP_FRAME_HEIGHT),
             capture.get(cv2.CAP_PROP_FPS))
    return capture


class CameraFeed:
    def __init__(self, index=CAMERA_INDEX, model_path=MODEL_PATH):
        self.index = index
        self.model_path = model_path
        self._condition = threading.Condition()
        self._worker = threading.Thread(target=self._run, name="scope-camera")
        self._closed = False
        self._error = None
        self._category = None
        self._revision = 0
        self._frame_id = 0
        self._latest = None
        self._observation = None

    def __enter__(self):
        self._worker.start()
        return self

    def __exit__(self, *args):
        with self._condition:
            self._closed = True
            self._observation = None
            self._condition.notify_all()
        self._worker.join()

    def select_category(self, category):
        if category is not None and (type(category) is not int or category not in CATEGORIES):
            raise ValueError("Select one of the available categories or send null for all categories.")
        with self._condition:
            if category != self._category:
                self._category = category
                self._revision += 1
                self._latest = None
                self._observation = None
                self._condition.notify_all()
            return self._status()

    def status(self):
        with self._condition:
            return self._status()

    def observation(self):
        with self._condition:
            return self._observation

    def _status(self):
        return {"category": self._category, "revision": self._revision,
                "ready": self._latest is not None, "error": self._error}

    def frames(self, revision):
        previous_id = 0
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._error
                                         or self._revision != revision
                                         or (self._latest is not None and self._frame_id != previous_id))
                if self._closed or self._error or self._revision != revision:
                    return
                previous_id = self._frame_id
                jpeg = self._latest
            yield jpeg

    def _run(self):
        try:
            with ExitStack() as resources:
                detector = Detector(self.model_path)
                capture = open_camera(self.index)
                resources.callback(capture.release)
                self._capture(capture, detector)
        except Exception as error:
            log.exception("SCOPE camera stopped")
            with self._condition:
                self._error = str(error) or type(error).__name__
                self._latest = None
                self._observation = None
                self._condition.notify_all()

    def _capture(self, capture, detector):
        while True:
            with self._condition:
                if self._closed:
                    return
                category, revision = self._category, self._revision
            ok, frame = capture.read()
            captured_at = time.monotonic()
            if not ok:
                raise RuntimeError(f"Camera {self.index} stopped returning frames")
            detections = detector.detect(frame, category)
            target = draw_detections(frame, detections, category)
            height, width = frame.shape[:2]
            dx, dy, centered = target_error(target, frame.shape)
            ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if not ok:
                raise RuntimeError("Could not encode the camera frame as JPEG")
            with self._condition:
                if self._closed or revision != self._revision:
                    continue
                self._frame_id += 1
                self._observation = Observation(
                    self._frame_id, captured_at, revision, category, width, height,
                    target is not None, centered, dx, dy)
                self._latest = encoded.tobytes()
                self._condition.notify_all()
