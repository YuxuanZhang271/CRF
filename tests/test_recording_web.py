"""Real recording indexes, random seek, malformed input and frame confinement."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from recording_web import RecordingArchive


class ArchiveTests(unittest.TestCase):
    def test_seek_retains_prior_state_and_reads_only_indexed_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segment = root / 'session' / '00000'
            segment.mkdir(parents=True)
            rows = []
            def row(t, kind, payload):
                rows.append(json.dumps({'elapsed_s': t, 'type': kind, 'data': payload})+'\n')
            row(0, 'segment_start', {'config': {'speed': 10}})
            row(0, 'state', {'robot': {'task': {'id': 0}}})
            row(5, 'task', {'id': 3, 'name': 'Zoom in'})
            row(8, 'camera_frame', {'image': {'path': '00000/camera.mjpeg', 'offset_bytes': 6, 'length_bytes': 4}})
            row(9, 'llm', {'type': 'transcript.final', 'text': 'zoom in'})
            row(20, 'session_stop', {'dropped': {}})
            (segment/'events.jsonl').write_text(''.join(rows)+'bad\n{"partial":', encoding='utf-8')
            (segment/'camera.mjpeg').write_bytes(b'prefixJPEGsuffix')
            archive = RecordingArchive(root)
            data = archive.window('session', 10, 10)
            self.assertEqual(data['duration_s'], 20)
            self.assertEqual(data['states'][0][0], 0)
            self.assertEqual(data['configs'][0][1], {'speed': 10})
            self.assertEqual(data['frames'], [{'time': 8, 'index': 0}])
            self.assertEqual(data['invalid'], 1)
            self.assertEqual(archive.image('session', 0), b'JPEG')
            self.assertTrue(archive.window('session', 0, 10)['events'])
            for name in ('../session', '../../outside', ''):
                with self.assertRaises(ValueError):
                    archive.load(name)
            with self.assertRaises(ValueError):
                archive.image('session', -1)
            (segment/'camera.mjpeg').write_bytes(b'prefix')
            self.assertIsNone(archive.image('session', 0))
            self.assertEqual(RecordingArchive(root/'session').sessions()[0]['id'], 'session')


if __name__ == '__main__':
    unittest.main()
