"""Dashboard QA with saved video; no camera, microphone or robot connections.

python tests/preview_perception.py --video path/to/video.mkv --port 8099
"""

import argparse
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import time
from unittest.mock import patch

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from app import _Handler
from flex_endo_robo.control_node import ControlNode
from scope.camera import CameraFeed
from status import Recorder, Status
from tasks import TaskState, TaskExecutor
from diagnostics import DiagnosticLog
from keyboard_listener import KeyboardListener


class SavedVideo:
    def __init__(self, path):
        self.capture = cv2.VideoCapture(str(path))
        if not self.capture.isOpened():
            raise RuntimeError('Cannot open saved video')

    def read(self):
        time.sleep(1 / 30)
        ok, frame = self.capture.read()
        if not ok:
            self.capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self.capture.read()
        return ok, frame

    def release(self):
        self.capture.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--video', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8099)
    args = parser.parse_args()
    if not args.video.is_file():
        parser.error('An existing video file is required; camera indices are not accepted.')
    root = Path(__file__).resolve().parents[1]
    with ExitStack() as resources:
        server = resources.enter_context(ThreadingHTTPServer(('127.0.0.1', args.port), _Handler))
        server.daemon_threads = True
        server.relay, server.llm = None, None
        server.stopping = server.interrupting = False
        server.node = ControlNode('127.0.0.1', 0, root / 'src/flex_endo_robo/ur5_endo_flex.urdf')
        server.tasks = TaskState()
        server.executor = TaskExecutor(server)
        server.node.task_executor = server.executor
        server.keyboard = KeyboardListener()  # Not started: no native keyboard hook.
        server.diagnostics = DiagnosticLog()
        resources.callback(server.diagnostics.close)
        video = SavedVideo(args.video)
        resources.callback(video.release)
        resources.enter_context(patch('scope.camera.open_camera', return_value=video))
        server.camera = resources.enter_context(CameraFeed())
        server.recorder = Recorder(Status(server, {'preview_saved_video': str(args.video)}), root / 'recordings')
        resources.callback(server.recorder.close)
        server.camera.recorder = server.recorder
        print(f'Saved-video dashboard: http://127.0.0.1:{args.port}', flush=True)
        server.serve_forever()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
