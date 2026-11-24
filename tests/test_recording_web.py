"""Real recording indexes, random seek, malformed input and frame confinement."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
import io
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from recording_web import RecordingArchive


class ArchiveTests(unittest.TestCase):
    def test_audio_clip_preserves_gaps_and_rejects_escaping_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segment = root / 'session' / '00000'
            segment.mkdir(parents=True)
            with wave.open(str(segment / 'microphone.wav'), 'wb') as stream:
                stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                stream.writeframes(b'\x12\x34' * 640)
            def write(path='00000/microphone.wav'):
                rows = [{'elapsed_s': 0, 'type': 'segment_start', 'data': {'session_origin_monotonic_s': 100}},
                        *[{'elapsed_s': t, 'type': 'microphone_audio', 'data': {
                            'audio_start_monotonic_s': 100+t, 'audio': {'path': path,
                                'offset_samples': i*320, 'sample_count': 320}}} for i, t in enumerate((1., 1.04))]]
                (segment / 'events.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
            write()
            archive = RecordingArchive(root)
            with wave.open(io.BytesIO(archive.audio('session', 1, .06)), 'rb') as stream:
                pcm = stream.readframes(960)
            self.assertEqual(pcm, b'\x12\x34'*320 + b'\0'*640 + b'\x12\x34'*320)
            with self.assertRaises(ValueError):
                archive.audio('session', 0, 31)
            with self.assertRaises(ValueError):
                archive.audio('session', 5, 1)
            write('../../outside.wav')
            with self.assertRaisesRegex(ValueError, 'outside'):
                archive.audio('session', 1, 1)

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
            row(8, 'camera_frame', {'image': {'path': '00000/camera.mjpeg', 'offset_bytes': 6, 'length_bytes': 4},
                'raw_image': {'path': '00000/camera_raw.mjpeg', 'offset_bytes': 0, 'length_bytes': 5}})
            row(9, 'llm', {'type': 'transcript.final', 'text': 'zoom in'})
            row(20, 'session_stop', {'dropped': {}})
            (segment/'events.jsonl').write_text(''.join(rows)+'bad\n{"partial":', encoding='utf-8')
            (segment/'camera.mjpeg').write_bytes(b'prefixJPEGsuffix')
            (segment/'camera_raw.mjpeg').write_bytes(b'CLEAN')
            archive = RecordingArchive(root)
            data = archive.window('session', 10, 10)
            self.assertEqual(data['duration_s'], 20)
            self.assertEqual(data['states'][0][0], 0)
            self.assertEqual(data['configs'][0][1], {'speed': 10})
            self.assertEqual(data['frames'], [{'time': 8, 'index': 0}])
            self.assertEqual(data['invalid'], 1)
            self.assertEqual(archive.image('session', 0), b'JPEG')
            self.assertEqual(archive.image('session', 0, raw=True), b'CLEAN')
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
