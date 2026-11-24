"""Recording durability, source timing, overload and failure recovery without hardware."""

import json
import logging
import sys
import tempfile
import threading
import time
import unittest
import wave
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from status import Recorder, Status


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='recording-test-',
                                                 dir=Path(__file__).resolve().parents[1])
        self.config = {'tip_limit': 10}
        self.source = SimpleNamespace(snapshot=lambda: {
            'config': dict(self.config), 'robot': {'joints_deg': [1., 2., 3.]},
            'camera': {'ready': True},
        })
        self.recorder = Recorder(self.source, self.temp.name, segment_seconds=.08)

    def tearDown(self):
        self.recorder.close()
        self.temp.cleanup()

    def rows(self):
        return [json.loads(line) for path in sorted(Path(self.recorder.status()['session']).glob('*/events.jsonl'))
                for line in path.read_text(encoding='utf-8').splitlines()]

    def test_performance_samples_recorded_once_and_restart_resets_baseline(self):
        performance = {'status': 'ready', 'sequence': 4, 'sampled_at': 123.,
                       'sample_age_s': .1, 'cpu': {'percent': 12.},
                       'memory': {'percent': 45.}, 'gpus': [], 'components': []}
        source_snapshot = self.source.snapshot
        self.source.snapshot = lambda: {**source_snapshot(), 'performance': dict(performance)}
        for _ in range(2):
            self.recorder.start()
            time.sleep(.12)
            self.recorder.stop()
            rows = self.rows()
            self.assertEqual(next(r for r in rows if r['type'] == 'session_start')['data']['snapshot']['performance']['sequence'], 4)
            samples = [r['data'] for r in rows if r['type'] == 'performance']
            self.assertEqual(len(samples), 1, 'Repeated 20 Hz snapshots must not duplicate 1 Hz readings')
            self.assertEqual(samples[0]['cpu']['percent'], 12.)

    def test_status_reads_cached_performance_without_sampling_hardware(self):
        source = Status.__new__(Status)
        source.launch_config = source.defaults = {}
        source.server = SimpleNamespace(
            node=SimpleNamespace(get_state_snapshot=lambda: {}),
            tasks=SimpleNamespace(snapshot=lambda: {}), relay=None,
            stopping=False, interrupting=False, camera=SimpleNamespace(status=lambda: {}),
            performance=SimpleNamespace(snapshot=lambda: {'status': 'ready', 'sequence': 12}))
        self.assertEqual(source.snapshot()['performance']['sequence'], 12)
        del source.server.performance
        self.assertIsNone(source.snapshot()['performance'])

    def test_mixed_sources_rotation_configuration_and_images(self):
        @dataclass(frozen=True)
        class Target:
            confidence: float
            bounds: tuple

        started = self.recorder.start()
        self.recorder.event('camera_frame', {'frame': 71, 'captured_at': 123.4}, jpeg=b'jpeg-data')
        self.recorder.event('yolo', {'target': Target(.99, (1, 2, 3, 4)), 'captured_at': 123.4})
        self.recorder.event('command', {'command': 'TRACK'})
        logging.getLogger('recording-test').warning('Recorded Python console message')
        self.recorder.event('console', {'message': 'Dashboard output', 'origin': 'dashboard'})
        self.config['tip_limit'] = 20
        time.sleep(.18)
        stopped = self.recorder.stop()
        self.assertFalse(stopped['active'])
        self.assertIsNone(stopped['error'])
        rows = self.rows()
        self.assertGreaterEqual(sum(row['type'] == 'segment_start' for row in rows), 2)
        self.assertEqual(rows[-1]['type'], 'session_stop')
        self.assertEqual([row['sequence'] for row in rows], list(range(1, len(rows) + 1)))
        frame = next(row for row in rows if row['type'] == 'camera_frame')
        image = frame['data']['image']
        with (Path(started['session']) / image['path']).open('rb') as stream:
            stream.seek(image['offset_bytes'])
            self.assertEqual(stream.read(image['length_bytes']), b'jpeg-data')
        self.assertEqual(frame['data']['captured_at'], 123.4)
        yolo = next(row for row in rows if row['type'] == 'yolo')
        self.assertEqual(yolo['data']['target']['bounds'], [1, 2, 3, 4])
        self.assertTrue(any(row['type'] == 'config' and row['data']['tip_limit'] == 20 for row in rows))
        console = [row['data']['message'] for row in rows if row['type'] == 'console']
        self.assertIn('Recorded Python console message', console)
        self.assertIn('Dashboard output', console)
        self.assertTrue(all('timestamp' in row and 'monotonic_s' in row for row in rows))
        for path in sorted(Path(started['session']).glob('*/events.jsonl'))[1:]:
            self.assertEqual(json.loads(path.read_text().splitlines()[0])['data']['config']['tip_limit'], 20)

    def test_slow_disk_never_blocks_producers_and_counts_drops(self):
        entered, release = threading.Event(), threading.Event()
        write = self.recorder._write

        def slow_write(kind, data, stamp):
            if kind == 'block':
                entered.set()
                release.wait(2.)
            write(kind, data, stamp)

        self.recorder.queue_bytes = 8192
        self.recorder.start()
        try:
            with patch.object(self.recorder, '_write', side_effect=slow_write):
                self.recorder.event('block', {})
                self.assertTrue(entered.wait(1.))
                begin = time.monotonic()
                for frame in range(1000):
                    self.recorder.event('camera_frame', {'frame': frame}, jpeg=b'x' * 4096)
                self.assertLess(time.monotonic() - begin, .25)
                self.assertGreater(self.recorder.status()['dropped'].get('camera_frame', 0), 0)
                self.assertLessEqual(self.recorder._pending_bytes, 8192)
                release.set()
                self.recorder.stop()
        finally:
            release.set()
        self.assertGreater(self.rows()[-1]['data']['dropped']['camera_frame'], 0)

    def test_disk_failure_visible_and_new_session_can_start(self):
        failed = threading.Event()
        write = self.recorder._write

        def fail(kind, data, stamp):
            if kind == 'fail':
                failed.set()
                raise OSError('disk full')
            return write(kind, data, stamp)

        first = self.recorder.start()['session']
        with patch.object(self.recorder, '_write', side_effect=fail):
            self.recorder.event('fail', {})
            self.assertTrue(failed.wait(1.))
            self.recorder._worker.join(1.)
        stopped = self.recorder.stop()
        self.assertFalse(stopped['active'])
        self.assertIn('disk full', stopped['error'])
        self.assertEqual(self.recorder._pending_bytes, 0)
        second = self.recorder.start()['session']
        self.assertNotEqual(first, second)
        self.assertIsNone(self.recorder.stop()['error'])

    def test_stop_drains_and_late_events_are_ignored(self):
        self.recorder.start()
        for number in range(50):
            self.recorder.event('command', {'number': number})
        self.recorder.stop()
        self.recorder.event('command', {'number': 999})
        commands = [row['data']['number'] for row in self.rows() if row['type'] == 'command']
        self.assertEqual(commands, list(range(50)))
        self.assertFalse(self.recorder._worker.is_alive())
        self.assertFalse(self.recorder._sampler.is_alive())
        self.assertFalse(self.recorder.stop()['active'])

    def test_audio_and_clean_images_survive_rotation(self):
        self.recorder.start()
        for index in range(2):
            self.recorder.event('camera_frame', {'frame': index}, jpeg=b'preview', raw_jpeg=b'clean')
            self.recorder.event('microphone_audio', {'stream_id': 1}, pcm=b'\x12\x34' * 320)
            time.sleep(.1)
        self.recorder.stop()
        rows = self.rows()
        audio = [row for row in rows if row['type'] == 'microphone_audio']
        self.assertEqual(len(audio), 2)
        self.assertNotEqual(audio[0]['data']['audio']['path'], audio[1]['data']['audio']['path'])
        for row in audio:
            ref = row['data']['audio']
            with wave.open(str(Path(self.recorder.status()['session']) / ref['path']), 'rb') as stream:
                self.assertEqual((stream.getnchannels(), stream.getsampwidth(), stream.getframerate()), (1, 2, 16000))
                stream.setpos(ref['offset_samples'])
                self.assertEqual(stream.readframes(ref['sample_count']), b'\x12\x34' * 320)
        for row in (r for r in rows if r['type'] == 'camera_frame'):
            ref = row['data']['raw_image']
            self.assertEqual((Path(self.recorder.status()['session']) / ref['path']).read_bytes(), b'clean')

    def test_live_wav_header_is_flushed_before_index(self):
        self.recorder.segment_seconds = 300
        self.recorder.start()
        self.recorder.event('microphone_audio', {}, pcm=b'\x12\x34' * 320)
        deadline = time.monotonic() + 2
        path = Path(self.recorder.status()['session']) / '00000' / 'events.jsonl'
        while time.monotonic() < deadline and 'microphone_audio' not in path.read_text(encoding='utf-8'):
            time.sleep(.02)
        self.assertIn('microphone_audio', path.read_text(encoding='utf-8'))
        with wave.open(str(path.with_name('microphone.wav')), 'rb') as stream:
            self.assertEqual(stream.readframes(320), b'\x12\x34' * 320)
        self.assertTrue(self.recorder.status()['active'])

    def test_audio_overload_is_counted_without_blocking_and_inactive_audio_is_ignored(self):
        self.recorder.event('microphone_audio', {}, pcm=b'\0' * 640)
        self.recorder.start()
        self.recorder.event('microphone_audio', {}, pcm=b'\0' * self.recorder.queue_bytes)
        self.recorder.stop()
        self.assertEqual(self.recorder.status()['dropped']['microphone_audio'], 1)
        self.assertFalse(any(row['type'] == 'microphone_audio' for row in self.rows()))

    def test_output_directory_failure_is_visible(self):
        destination = Path(self.temp.name) / 'ordinary-file'
        destination.write_text('occupied')
        self.recorder.directory = destination
        with self.assertRaises(OSError):
            self.recorder.start()
        self.assertFalse(self.recorder.status()['active'])
        self.assertTrue(self.recorder.status()['error'])

    def test_recording_clock_and_slice_index_freeze_on_stop(self):
        self.recorder.start()
        time.sleep(.11)
        status = self.recorder.status()
        self.assertGreaterEqual(status['slice_index'], 1)
        self.assertLess(status['slice_elapsed_s'], .08)
        stopped = self.recorder.stop()
        time.sleep(.02)
        self.assertEqual(self.recorder.status()['elapsed_s'], stopped['elapsed_s'])

    def test_stalled_status_source_is_recorded_without_losing_other_events(self):
        self.recorder.start()
        with patch.object(self.source, 'snapshot', side_effect=TimeoutError('stale state')):
            time.sleep(.08)
            self.recorder.event('command', {'command': 'STOP'})
            self.recorder.stop()
        self.assertTrue(any(row['type'] == 'state_error' for row in self.rows()))
        self.assertEqual(self.rows()[-1]['type'], 'session_stop')
        self.assertIsNone(self.recorder.status()['error'])


if __name__ == '__main__':
    unittest.main()
