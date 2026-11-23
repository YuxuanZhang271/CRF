"""Capture and encode one camera feed for all web viewers."""

import logging
import threading
import time
from contextlib import ExitStack

import cv2

from .yolo import CATEGORIES, MODEL_PATH, Detector, Observation, draw_detections, select_target, target_error


CAMERA_INDEX = 0
TARGET_WIDTH = 1504
TARGET_HEIGHT = 1504
TARGET_FPS = 30
FOURCC = "MJPG"
DETECTION_MAX_AGE_S = 0.5

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
        self._capture_worker = threading.Thread(target=self._run_capture, name="scope-capture")
        self._worker = threading.Thread(target=self._run_detection, name="scope-detection")
        self._closed = False
        self._error = None
        self._detection_error = None
        self._category = None
        self._revision = 0
        self._frame_id = 0
        self._latest = None
        self._observation = None
        self._raw = None  # One latest frame; inference never accumulates a backlog.
        self._detections = None
        self.recorder = None

    def __enter__(self):
        self._capture_worker.start()
        self._worker.start()
        return self

    def __exit__(self, *args):
        with self._condition:
            self._closed = True
            self._observation = None
            self._condition.notify_all()
        self._capture_worker.join()
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
                self._raw = self._detections = None
                self._condition.notify_all()
            return self._status()

    def status(self):
        with self._condition:
            return self._status()

    def observation(self):
        with self._condition:
            return self._observation

    def wait_for_observation(self, revision, category, *, timeout=2., cancelled=None):
        """Wait for inference for one exact selection, without holding any motion lock.

        Target visibility is not required: tracking can remain in Waiting for target.
        Cancellation is checked outside the camera lock to preserve lock ordering.
        """
        deadline = time.monotonic() + timeout
        while True:
            if cancelled is not None and cancelled():
                return None
            with self._condition:
                if self._closed:
                    raise ValueError('Camera closed while preparing tracking.')
                if self._error or self._detection_error:
                    raise ValueError(f'Camera unavailable for tracking: {self._error or self._detection_error}')
                if category is None:
                    raise ValueError('Select one YOLO category before tracking.')
                if (revision, category) != (self._revision, self._category):
                    raise ValueError('YOLO category changed while preparing tracking.')
                obs = self._observation
                if (obs is not None and (obs.revision, obs.category) == (revision, category)
                        and 0 <= time.monotonic() - obs.captured_at <= DETECTION_MAX_AGE_S):
                    return obs
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError(f'Timed out waiting for a fresh YOLO observation for {CATEGORIES[category]}.')
                self._condition.wait(min(.05, remaining))

    def _status(self):
        return {"category": self._category, "revision": self._revision,
                "ready": self._latest is not None, "error": self._error,
                "detection_error": self._detection_error}

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

    def _run_capture(self):
        try:
            with ExitStack() as resources:
                capture = open_camera(self.index)
                resources.callback(capture.release)
                self._capture(capture)
        except Exception as error:
            log.exception("SCOPE capture stopped")
            with self._condition:
                self._error = str(error) or type(error).__name__
                self._latest = self._raw = self._observation = self._detections = None
                self._condition.notify_all()

    def _capture(self, capture):
        frame_id = 0
        while True:
            with self._condition:
                if self._closed:
                    return
                category, revision = self._category, self._revision
            ok, frame = capture.read()
            captured_at = time.monotonic()
            if not ok:
                raise RuntimeError(f"Camera {self.index} stopped returning frames")
            with self._condition:
                if self._closed or revision != self._revision:
                    continue
                frame_id += 1
                self._raw = (frame_id, captured_at, revision, category, frame)
                cached = self._detections
                self._condition.notify_all()
            detections = (cached[2] if cached and cached[0] == revision
                          and captured_at - cached[1] <= DETECTION_MAX_AGE_S else [])
            preview = frame.copy()  # Inference retains the unmodified capture frame.
            draw_detections(preview, detections, category)
            ok, encoded = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if not ok:
                raise RuntimeError("Could not encode the camera frame as JPEG")
            with self._condition:
                if self._closed or revision != self._revision:
                    continue
                self._frame_id = frame_id
                self._latest = encoded.tobytes()
                jpeg = self._latest
                self._condition.notify_all()
            if self.recorder is not None:
                self.recorder.event('camera_frame', {
                    'frame': frame_id, 'captured_at': captured_at,
                    'revision': revision, 'category': category,
                    'width': frame.shape[1], 'height': frame.shape[0],
                    'overlay_captured_at': cached[1] if detections else None,
                }, jpeg=jpeg)

    def _run_detection(self):
        try:
            detector = Detector(self.model_path)
            previous_id = 0
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._closed or self._error or
                                             (self._raw is not None and self._raw[0] != previous_id))
                    if self._closed or self._error:
                        return
                    frame_id, captured_at, revision, category, frame = self._raw
                previous_id = frame_id
                detections = detector.detect(frame, category)
                target = select_target(detections, category, frame.shape)
                dx, dy, centered = target_error(target, frame.shape)
                height, width = frame.shape[:2]
                with self._condition:
                    if self._closed or self._error or revision != self._revision:
                        continue
                    # Keep the capture timestamp; a late result must remain stale.
                    self._observation = Observation(
                        frame_id, captured_at, revision, category, width, height,
                        target is not None, centered, dx, dy)
                    observation = self._observation
                    self._detections = (revision, captured_at, detections)
                    self._condition.notify_all()
                if self.recorder is not None:
                    self.recorder.event('yolo', {'observation': observation,
                                                 'detections': detections, 'target': target})
        except Exception as error:
            log.exception("SCOPE detection stopped; camera preview remains available")
            with self._condition:
                self._detection_error = str(error) or type(error).__name__
                self._observation = self._detections = None
                self._condition.notify_all()
