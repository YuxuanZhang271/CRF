"""Offline windows, live tailing, slice discovery and MJPEG extraction."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from recording_viewer import RecordingData, resolve_recording


class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='viewer-test-', dir=Path(__file__).resolve().parents[1])
        self.root = Path(self.temp.name)
        self.session = self.root / '20261118T000000Z'
        self.segment = self.session / '00000'
        self.segment.mkdir(parents=True)
        self.file = self.segment / 'events.jsonl'

    def tearDown(self):
        self.temp.cleanup()

    def append(self, stamp, kind='state', data=None):
        with self.file.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'type': kind, 'elapsed_s': stamp,
                                     'data': data if data is not None else {'robot': {'joints_deg': [stamp] * 6}}}) + '\n')

    def test_live_tail_partial_line_and_new_slice(self):
        self.append(0., 'segment_start', {'config': {'tip_limit': 10}})
        self.append(1.)
        data = RecordingData(self.root, live=True, duration=5.).refresh()
        self.assertEqual(len(data.states), 1)
        partial = json.dumps({'type': 'state', 'elapsed_s': 2., 'data': {'robot': {}}})
        with self.file.open('a') as stream:
            stream.write(partial[:20])
        data.refresh()
        self.assertEqual(len(data.states), 1)
        with self.file.open('a') as stream:
            stream.write(partial[20:] + '\n')
        data.refresh()
        self.assertEqual(len(data.states), 2)
        self.file = self.session / '00001/events.jsonl'
        self.file.parent.mkdir()
        self.append(10.)
        data.refresh()
        self.assertEqual([stamp for stamp, _ in data.states], [10.])
        self.assertEqual(data.window, (5., 10.))
        data.refresh()
        self.assertEqual(data.counts['state'], 3, 'Re-polling must not duplicate old lines')

    def test_offline_window_config_and_malformed_complete_line(self):
        self.append(0., 'segment_start', {'config': {'tip_limit': 10}})
        for stamp in (1., 5., 10., 15.):
            self.append(stamp)
        self.append(20., 'config', {'tip_limit': 20})
        self.append(21., 'session_stop', {})
        with self.file.open('a') as stream:
            stream.write('bad complete line\n')
        data = RecordingData(self.session, start=5, duration=5).refresh()
        self.assertEqual([stamp for stamp, _ in data.states], [5., 10.])
        self.assertEqual(data.summary()['configuration'], {'tip_limit': 10})
        self.assertEqual(data.invalid_lines, 1)
        self.assertTrue(data.ended)
        self.assertEqual(data.at(data.states, 6.)['robot']['joints_deg'], [5.] * 6)

    def test_mjpeg_byte_ranges_and_path_validation(self):
        self.append(0.)
        (self.segment / 'camera.mjpeg').write_bytes(b'prefixJPEGsuffix')
        data = RecordingData(self.session).refresh()
        frame = {'image': {'path': '00000/camera.mjpeg', 'offset_bytes': 6, 'length_bytes': 4}}
        self.assertEqual(data.image_bytes(frame), b'JPEG')
        self.assertIsNone(data.image_bytes({'image': {**frame['image'], 'length_bytes': 100}}))
        with self.assertRaises(ValueError):
            data.image_bytes({'image': '../outside.jpg'})
        with self.assertRaises(ValueError):
            data.image_bytes({'image': {**frame['image'], 'offset_bytes': -1}})

    def test_latest_session_and_direct_segment_selection(self):
        self.append(1.)
        data = RecordingData(self.root, live=True).refresh()
        newer = self.root / '20261118T010000Z/00000'
        newer.mkdir(parents=True)
        (newer / 'events.jsonl').write_text('{"type":"state","elapsed_s":0,"data":{"robot":{}}}\n')
        data.refresh()
        self.assertEqual(data.session, newer.parent)
        self.assertEqual(data.counts['state'], 1)
        self.assertEqual(resolve_recording(self.segment)[1], [self.segment / 'events.jsonl'])

    def test_direct_later_slice_automatically_selects_its_own_time_window(self):
        self.append(300., 'segment_start', {'config': {}})
        self.append(310.)
        data = RecordingData(self.segment).refresh()
        self.assertEqual(data.start, 300.)
        self.assertEqual(len(data.states), 1)

    def test_performance_retains_preceding_sample_for_offline_seek(self):
        self.append(0., 'session_start', {'snapshot': {'performance': {'sequence': 1}}})
        self.append(4., 'performance', {'sequence': 2, 'cpu': {'percent': 20}})
        self.append(7., 'performance', {'sequence': 3, 'cpu': {'percent': 30}})
        data = RecordingData(self.session, start=5, duration=1).refresh()
        self.assertEqual(data.at(data.performance, 5.)['sequence'], 2)
        self.assertEqual(data.summary()['performance']['cpu']['percent'], 20)


if __name__ == '__main__':
    unittest.main()
