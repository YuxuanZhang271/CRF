"""Mode revisions, worker isolation and stage sampling without devices/models."""

from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from scope.camera import CameraFeed
from scope.catalog import SURGICAL_CATEGORIES, SURGICAL_COLORS
from scope.yolo import Detection, Observation, draw_detections


class PerceptionTests(unittest.TestCase):
    def publish(self, feed, frame_id, stamp=None):
        with feed._condition:
            feed._raw = (frame_id, time.monotonic() if stamp is None else stamp,
                         feed._revision, feed._category, np.zeros((40, 40, 3), np.uint8))
            feed._condition.notify_all()

    def wait_for(self, feed, predicate):
        with feed._condition:
            self.assertTrue(feed._condition.wait_for(predicate, 2.))

    def close(self, feed):
        with feed._condition:
            feed._closed = True
            feed._condition.notify_all()
        for worker in (feed._worker, feed._stage_worker):
            if worker.ident is not None:
                worker.join(2.)
                self.assertFalse(worker.is_alive())

    def test_mode_resets_selection_and_never_exposes_background(self):
        feed = CameraFeed()
        feed.select_category(0)  # Person in the test detector.
        feed._observation = Observation(1, time.monotonic(), 1, 0, 40, 40, True, False, 1, 1)
        state = feed.select_mode('full')
        self.assertEqual(len(state['categories']), 27)
        self.assertEqual([(phase['id'], phase['duration_s']) for phase in state['stage']['phases']],
                         list(enumerate((40.8, 568.8, 101.4, 583.8, 490.2, 830.4, 216.6), 1)))
        self.assertNotIn(0, [item['id'] for item in state['categories']])
        self.assertIsNone(state['category'])
        self.assertIsNone(feed.observation())
        self.assertEqual(state['revision'], 2)
        with self.assertRaises(ValueError):
            feed.select_category(0)
        feed.select_category(10)
        self.assertEqual(feed.categories()[10], 'Liver')
        state = feed.select_mode('test')
        self.assertEqual(state['stage']['status'], 'inactive')
        self.assertEqual(state['stage']['top3'], [])
        self.assertEqual(state['stage']['tip'], '')
        with self.assertRaises(ValueError):
            feed.select_mode('invalid')

    def test_blocked_stage_does_not_block_segmentation_and_cannot_publish_after_switch(self):
        feed = CameraFeed()
        feed.select_mode('full')
        entered, release = threading.Event(), threading.Event()
        def step(**kwargs):
            entered.set()
            release.wait(2.)
            return [{'id': 1, 'name': 'Duodenum transection', 'prob': .8}]
        phase = SimpleNamespace(step=step, samples=1)
        detector = SimpleNamespace(detect=lambda *args: [Detection(1, .9, (2, 2, 8, 8))])
        with patch('scope.camera.SurgicalDetector', return_value=detector), \
                patch('scope.camera.Detector', return_value=SimpleNamespace(detect=lambda *args: [])), \
                patch('scope.camera.load_phase', return_value=phase):
            feed._worker.start()
            feed._stage_worker.start()
            try:
                self.publish(feed, 1)
                self.assertTrue(entered.wait(2.))
                self.publish(feed, 2)
                self.wait_for(feed, lambda: feed._observation is not None and feed._observation.frame == 2)
                self.assertEqual(feed.status()['stage']['top3'], [])
                feed.select_mode('test')
                release.set()
                self.publish(feed, 3)
                self.wait_for(feed, lambda: feed._observation is not None and feed._observation.frame == 3)
                self.assertEqual(feed.status()['stage']['status'], 'inactive')
                self.assertEqual(feed.status()['stage']['history'], [])
            finally:
                release.set()
                self.close(feed)

    def test_stage_samples_at_one_hz_and_category_does_not_reset_history(self):
        feed = CameraFeed()
        feed.select_mode('full')
        resets = []
        phase = SimpleNamespace(samples=0)
        def step(**kwargs):
            phase.samples += 1
            return [{'id': 2, 'name': 'Hepatic artery exposure', 'prob': .7}]
        def reset():
            resets.append(True)
            phase.samples = 0
        phase.step, phase.reset = step, reset
        with patch('scope.camera.load_phase', return_value=phase):
            feed._stage_worker.start()
            try:
                stamp = time.monotonic()
                self.publish(feed, 1, stamp)
                self.wait_for(feed, lambda: feed._stage['samples'] == 1)
                self.assertEqual(feed._stage['phase_started_at'], stamp)
                self.publish(feed, 2, stamp + .5)
                with feed._condition:
                    self.assertFalse(feed._condition.wait_for(lambda: phase.samples > 1, .05))
                feed.select_category(10)
                self.publish(feed, 3, stamp + 1.01)
                self.wait_for(feed, lambda: feed._stage['samples'] == 2)
                self.assertEqual(len(feed.status()['stage']['history']), 2)
                self.assertEqual(feed._stage['phase_started_at'], stamp)
                self.assertEqual(resets, [])
                self.publish(feed, 4, stamp + 5)
                self.wait_for(feed, lambda: len(resets) == 1 and feed._stage['samples'] == 1)
                self.assertEqual(len(feed.status()['stage']['history']), 1)
                self.assertEqual(feed._stage['phase_started_at'], stamp + 5)
            finally:
                self.close(feed)

    def test_failed_full_model_can_return_to_test_mode(self):
        feed = CameraFeed()
        feed.select_mode('full')
        with patch('scope.camera.SurgicalDetector', side_effect=ValueError('Wrong weights')), \
                patch('scope.camera.Detector', return_value=SimpleNamespace(detect=lambda *args: [])):
            feed._worker.start()
            try:
                self.wait_for(feed, lambda: feed._vision_state == 'error')
                self.assertIn('Wrong weights', feed.status()['detection_error'])
                feed.select_mode('test')
                self.publish(feed, 1)
                self.wait_for(feed, lambda: feed._observation is not None)
                self.assertIsNone(feed.status()['detection_error'])
            finally:
                self.close(feed)

    def test_masks_are_drawn_only_for_selected_category(self):
        frame = np.zeros((80, 80, 3), np.uint8)
        detections = [Detection(1, .9, (1, 30, 15, 50), ((1, 30), (15, 30), (15, 50), (1, 50))),
                      Detection(10, .8, (50, 40, 70, 65), ((50, 40), (70, 40), (70, 65), (50, 65)))]
        draw_detections(frame, detections, 10, categories=SURGICAL_CATEGORIES,
                        colors=SURGICAL_COLORS, segmentation=True)
        self.assertTrue(frame[60, 60].any())
        self.assertFalse(frame[45, 7].any())

    def test_stale_stage_clears_current_prediction_and_tip(self):
        feed = CameraFeed()
        feed.select_mode('full')
        feed._tips = {'2': 'Configured tip'}
        feed._stage = {'status': 'ready', 'top3': [{'id': 2, 'name': 'Stage 2', 'prob': .8}],
                       'samples': 1, 'captured_at': time.monotonic(), 'history': [], 'error': None,
                       'phase_started_at': time.monotonic() - 420}
        self.assertEqual(feed.status()['stage']['tip'], 'Configured tip')
        self.assertGreaterEqual(feed.status()['stage']['phase_elapsed_s'], 420)
        feed._stage['captured_at'] -= 4
        stage = feed.status()['stage']
        self.assertEqual(stage['status'], 'waiting')
        self.assertEqual(stage['top3'], [])
        self.assertEqual(stage['tip'], '')
        self.assertIsNone(stage['phase_elapsed_s'])

    def test_stage_visit_timer_restarts_when_prediction_changes(self):
        feed = CameraFeed()
        feed.select_mode('full')
        phase = SimpleNamespace(samples=0)
        def step(**kwargs):
            phase.samples += 1
            stage_id = 2 if phase.samples < 3 else 5
            return [{'id': stage_id, 'name': f'Stage {stage_id}', 'prob': .8}]
        phase.step = step
        with patch('scope.camera.load_phase', return_value=phase):
            feed._stage_worker.start()
            try:
                stamp = time.monotonic()
                for frame in range(1, 4):
                    self.publish(feed, frame, stamp + (frame - 1) * 1.01)
                    self.wait_for(feed, lambda: feed._stage['samples'] == frame)
                    self.assertEqual(feed._stage['phase_started_at'], stamp if frame < 3 else stamp + 2.02)
                feed.select_mode('test')
                self.assertIsNone(feed.status()['stage']['phase_elapsed_s'])
            finally:
                self.close(feed)


if __name__ == '__main__':
    unittest.main()
