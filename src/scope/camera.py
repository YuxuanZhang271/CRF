"""Capture and encode one camera feed for all web viewers."""

import logging
import json
import threading
import time
from contextlib import ExitStack

import cv2

from .yolo import MODEL_PATH, Detector, Observation, draw_detections, select_target, target_error
from .catalog import (SURGICAL_MODEL_PATH, TEMPORAL_MODEL_PATH, TIPS_PATH,
                      PHASE_NAMES, PHASE_DURATIONS_S, categories_for, colors_for, category_metadata)
from .surgical import SurgicalDetector, load_phase


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
    def __init__(self, index=CAMERA_INDEX, model_path=MODEL_PATH, *,
                 surgical_model=SURGICAL_MODEL_PATH, temporal_model=TEMPORAL_MODEL_PATH,
                 stage_tips=TIPS_PATH):
        self.index = index
        self.model_path = model_path
        self.surgical_model = surgical_model
        self.temporal_model = temporal_model
        self._mode = "test"
        self._mode_revision = 0
        self._vision_state = "loading"
        self._stage = {"status": "inactive", "top3": [], "samples": 0,
                       "captured_at": None, "history": [], "error": None}
        self._tips = {}
        self._tips_error = None
        try:
            with open(stage_tips, encoding="utf-8") as stream:
                tips = json.load(stream)
            if not isinstance(tips, dict) or any(
                    key not in {str(i) for i in range(1, 8)} or not isinstance(value, str)
                    for key, value in tips.items()):
                raise ValueError("Stage tips must map stage IDs 1–7 to text.")
            self._tips = tips
        except (OSError, ValueError) as error:
            self._tips_error = str(error)
            log.warning("Stage tips unavailable: %s", error)
        self._condition = threading.Condition()
        self._capture_worker = threading.Thread(target=self._run_capture, name="scope-capture")
        self._worker = threading.Thread(target=self._run_detection, name="scope-detection")
        self._stage_worker = threading.Thread(target=self._run_stage, name="scope-stage")
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
        self._stage_worker.start()
        return self

    def __exit__(self, *args):
        with self._condition:
            self._closed = True
            self._observation = None
            self._condition.notify_all()
        self._capture_worker.join()
        self._worker.join()
        self._stage_worker.join()

    def categories(self):
        with self._condition:
            return dict(categories_for(self._mode))

    def select_mode(self, mode):
        if mode not in ("test", "full"):
            raise ValueError("Perception mode must be test or full.")
        with self._condition:
            if mode != self._mode:
                self._mode = mode
                self._mode_revision += 1
                self._revision += 1
                self._category = None
                self._latest = self._raw = self._observation = self._detections = None
                self._detection_error = None
                self._vision_state = "loading"
                self._stage = {"status": "loading" if mode == "full" else "inactive",
                               "top3": [], "samples": 0, "captured_at": None,
                               "history": [], "error": None}
                self._condition.notify_all()
            state = self._status()
        if self.recorder is not None:
            self.recorder.event("perception_mode", state)
        return state

    def select_category(self, category):
        with self._condition:
            if category is not None and (type(category) is not int or category not in categories_for(self._mode)):
                raise ValueError("Select one of the available categories or send null for all categories.")
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
                    raise ValueError(f'Timed out waiting for a fresh YOLO observation for {categories_for(self._mode)[category]}.')
                self._condition.wait(min(.05, remaining))

    def _status(self):
        stage = {**self._stage, "history": list(self._stage["history"])}
        if stage['status'] == 'ready' and time.monotonic() - stage['captured_at'] > 3:
            stage.update(status='waiting', top3=[])
        started = stage.get('phase_started_at')
        stage['phase_elapsed_s'] = (max(0., time.monotonic() - started)
                                    if stage['status'] == 'ready' and started is not None else None)
        top = stage['top3']
        stage.update(phases=[{'id': i, 'name': name, 'duration_s': PHASE_DURATIONS_S[i - 1]}
                             for i, name in enumerate(PHASE_NAMES, 1)],
                     tip=self._tips.get(str(top[0]['id']), '') if top else '',
                     tips_error=self._tips_error)
        return {"category": self._category, "revision": self._revision,
                "mode": self._mode, "mode_revision": self._mode_revision,
                "categories": category_metadata(self._mode), "vision_status": self._vision_state,
                "stage": stage,
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
                self._stage = {**self._stage, 'status': 'error' if self._mode == 'full' else 'inactive',
                               'top3': [], 'history': [], 'captured_at': None,
                               'error': self._error if self._mode == 'full' else None}
                self._condition.notify_all()

    def _capture(self, capture):
        frame_id = 0
        while True:
            with self._condition:
                if self._closed:
                    return
                category, revision, mode = self._category, self._revision, self._mode
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
            draw_detections(preview, detections, category, categories=categories_for(mode),
                            colors=colors_for(mode), segmentation=mode == "full")
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
                raw_jpeg = None
                if self.recorder.status()['active']:
                    ok, raw_encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
                    if ok:
                        raw_jpeg = raw_encoded.tobytes()
                    else:
                        self.recorder.event('camera_recording_error', {'frame': frame_id,
                            'captured_at': captured_at, 'error': 'Could not encode clean camera image'})
                self.recorder.event('camera_frame', {
                    'frame': frame_id, 'captured_at': captured_at,
                    'revision': revision, 'category': category, 'mode': mode,
                    'width': frame.shape[1], 'height': frame.shape[0],
                    'overlay_captured_at': cached[1] if detections else None,
                }, jpeg=jpeg, raw_jpeg=raw_jpeg)

    def _run_detection(self):
        detector, epoch, previous_id = None, -1, 0
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._error or
                    epoch != self._mode_revision or (detector is not None and
                    self._raw is not None and self._raw[0] != previous_id))
                if self._closed or self._error:
                    return
                changed = epoch != self._mode_revision
                mode, epoch = self._mode, self._mode_revision
                raw = self._raw
            try:
                if changed:
                    detector = None  # Release the previous model on its owning worker.
                    detector = (SurgicalDetector(self.surgical_model) if mode == "full"
                                else Detector(self.model_path))
                    with self._condition:
                        if epoch == self._mode_revision:
                            self._vision_state = "ready"
                            self._condition.notify_all()
                    continue
                frame_id, captured_at, revision, category, frame = raw
                previous_id = frame_id
                detections = detector.detect(frame, category)
                target = select_target(detections, category, frame.shape)
                dx, dy, centered = target_error(target, frame.shape)
                height, width = frame.shape[:2]
                with self._condition:
                    if self._closed or self._error or revision != self._revision:
                        continue
                    # Capture time determines freshness, even if inference was slow.
                    self._observation = Observation(
                        frame_id, captured_at, revision, category, width, height,
                        target is not None, centered, dx, dy)
                    observation = self._observation
                    self._detections = (revision, captured_at, detections)
                    self._condition.notify_all()
                if self.recorder is not None:
                    self.recorder.event('yolo', {'mode': mode, 'observation': observation,
                                                 'detections': detections, 'target': target})
            except Exception as error:
                log.exception("SCOPE inference failed; camera preview remains available")
                detector = None
                with self._condition:
                    if epoch == self._mode_revision:
                        self._detection_error = str(error) or type(error).__name__
                        self._vision_state = "error"
                        self._observation = self._detections = None
                        self._condition.notify_all()

    def _run_stage(self):
        phase, epoch, last_sample = None, -1, None
        active_stage, phase_started_at = None, None
        history = []
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._error or
                    epoch != self._mode_revision or (phase is not None and
                    self._raw is not None and
                    (last_sample is None or self._raw[1] - last_sample >= 1.0)))
                if self._closed or self._error:
                    return
                changed = epoch != self._mode_revision
                mode, epoch = self._mode, self._mode_revision
                raw = self._raw
            try:
                if changed:
                    phase, last_sample, history = None, None, []
                    active_stage, phase_started_at = None, None
                    if mode == "full":
                        phase = load_phase(self.surgical_model, self.temporal_model)
                        with self._condition:
                            if epoch == self._mode_revision:
                                self._stage = {**self._stage, 'status': 'waiting'}
                                self._condition.notify_all()
                    continue
                frame_id, captured_at, revision, category, frame = raw
                # A discontinuity must not pretend that widely spaced tokens are 1 Hz.
                if last_sample is not None and captured_at - last_sample > 2.5:
                    phase.reset()
                    history = []
                    active_stage, phase_started_at = None, None
                last_sample = captured_at
                top3 = phase.step(frame_bgr=frame)
                if top3[0]['id'] != active_stage:
                    active_stage, phase_started_at = top3[0]['id'], captured_at
                history = (history + [{'captured_at': captured_at, 'id': top3[0]['id']}])[-120:]
                with self._condition:
                    if self._closed or self._error or epoch != self._mode_revision:
                        continue
                    self._stage = {'status': 'ready', 'top3': top3, 'samples': phase.samples,
                                   'phase_started_at': phase_started_at,
                                   'captured_at': captured_at, 'history': history, 'error': None}
                    self._condition.notify_all()
                if self.recorder is not None:
                    self.recorder.event('surgical_stage', {'mode_revision': epoch,
                        'frame': frame_id, 'captured_at': captured_at,
                        'phase_started_at': phase_started_at,
                        'top3': top3, 'samples': phase.samples})
            except Exception as error:
                log.exception("SCOPE stage recognition failed; segmentation remains available")
                phase = None
                with self._condition:
                    if epoch == self._mode_revision:
                        self._stage = {'status': 'error', 'top3': [], 'samples': 0,
                                       'captured_at': None, 'history': [],
                                       'error': str(error) or type(error).__name__}
                        self._condition.notify_all()
