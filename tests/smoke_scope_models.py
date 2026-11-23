"""Explicit hardware-free model check; never opens cameras, microphones or robots.

Run with the crf environment: python tests/smoke_scope_models.py [--video FILE]
"""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from scope.catalog import MODEL_DIR, SURGICAL_MODEL_PATH, TEMPORAL_MODEL_PATH
from scope.surgical import SurgicalDetector, load_phase


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--video', type=Path, help='Read one frame from this saved video')
    args = parser.parse_args()
    for entry in json.loads((Path(__file__).resolve().parents[1] / 'src/scope/models.json').read_text())['files']:
        data = (MODEL_DIR / entry['name']).read_bytes()
        assert len(data) == entry['bytes'] and hashlib.sha256(data).hexdigest() == entry['sha256'], entry['name']
    frame = np.zeros((512, 512, 3), np.uint8)
    if args.video:
        if not args.video.is_file():
            parser.error('Video must be an existing file; device indices are not accepted.')
        capture = cv2.VideoCapture(str(args.video))
        try:
            ok, frame = capture.read()
            if not ok:
                raise RuntimeError('Cannot read the saved video')
        finally:
            capture.release()
    detector = SurgicalDetector(SURGICAL_MODEL_PATH)
    detector.detect(frame, None)  # Warm up lazy predictor initialization.
    started = time.perf_counter()
    detections = detector.detect(frame, None)
    segmentation_ms = (time.perf_counter() - started) * 1000
    output = {'segmentation_ms': round(segmentation_ms, 1), 'detections': len(detections),
              'masks': sum(bool(item.polygon) for item in detections), 'stages': {}}
    for name in (TEMPORAL_MODEL_PATH.name, 'YOLO26m_TeCNO_fold0.pt'):
        phase = load_phase(SURGICAL_MODEL_PATH, MODEL_DIR / name)
        first = phase.step(frame_bgr=frame)
        started = time.perf_counter()
        second = phase.step(frame_bgr=frame)
        assert phase.samples == 2 and len(first) == len(second) == 3
        assert all(1 <= item['id'] <= 7 and 0 <= item['prob'] <= 1 for item in second)
        output['stages'][name] = {'sample_ms': round((time.perf_counter() - started) * 1000, 1),
                                  'receptive_field': phase.head.receptive_field, 'top3': second}
        phase.reset()
        assert phase.history is None and phase.samples == 0
        del phase
    print(json.dumps(output, indent=2))


if __name__ == '__main__':
    main()
