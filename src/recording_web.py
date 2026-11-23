#!/usr/bin/env python3
"""Read-only browser replay. python src/recording_web.py [recordings/session] --port 8081"""

import argparse
from bisect import bisect_right
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit

from recording_viewer import RecordingData, event_label
from status import RECORDINGS_DIR

STATIC = Path(__file__).with_name('static')


class RecordingArchive:
    """Cache JSON indexes, never the multi-gigabyte MJPEG files."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self._cache = {}
        self._lock = threading.Lock()

    def sessions(self):
        paths = [self.root] if list(self.root.glob('[0-9]*/events.jsonl')) else sorted(
            {p.parent.parent for p in self.root.glob('*/[0-9]*/events.jsonl')}, reverse=True)
        return [{'id': p.name, 'label': p.name} for p in paths]

    def _session(self, name):
        if not isinstance(name, str) or name not in {s['id'] for s in self.sessions()}:
            raise ValueError('Choose a recording from the session list.')
        path = self.root if name == self.root.name else (self.root / name).resolve()
        if path != self.root and path.parent != self.root:
            raise ValueError('Recording path is outside the configured directory.')
        return path

    def load(self, name):
        path = self._session(name)
        with self._lock:
            files = sorted(path.glob('[0-9]*/events.jsonl'))
            signature = tuple((str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in files)
            old = self._cache.get(name)
            if old and old['signature'] == signature:
                return old
            data = {'path': path, 'signature': signature, 'states': [], 'frames': [],
                    'events': [], 'configs': [], 'yolo': [], 'invalid': 0,
                    'counts': Counter(), 'duration_s': 0., 'ended': False, 'dropped': {}}
            for file in files:
                with file.open(encoding='utf-8') as stream:
                    for line in stream:
                        if not line.endswith('\n'):
                            continue
                        try:
                            row = json.loads(line)
                            stamp, kind, payload = row['elapsed_s'], row['type'], row['data']
                            if (type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0
                                    or not isinstance(payload, dict) or not isinstance(kind, str)):
                                raise ValueError('Invalid record')
                            data['duration_s'] = max(data['duration_s'], stamp)
                            data['counts'][kind] += 1
                            if kind == 'state':
                                data['states'].append((stamp, payload))
                                data['dropped'] = payload.get('recording', {}).get('dropped', {})
                            elif kind == 'session_start' and payload.get('snapshot'):
                                data['states'].append((stamp, payload['snapshot']))
                            elif kind == 'camera_frame':
                                data['frames'].append((stamp, payload))
                            elif kind == 'yolo':
                                data['yolo'].append((stamp, payload))
                            elif kind in ('segment_start', 'config'):
                                data['configs'].append((stamp, payload.get('config', {}) if kind == 'segment_start' else payload))
                            elif kind == 'session_stop':
                                data['ended'] = True
                                data['dropped'] = payload.get('dropped', {})
                            if kind in ('task', 'llm', 'console', 'command', 'command_result', 'state_error', 'config', 'session_stop'):
                                if payload.get('path') != '/api/key/heartbeat':
                                    data['events'].append((stamp, kind, payload))
                        except (ValueError, TypeError, KeyError):
                            data['invalid'] += 1
            for key in ('states', 'frames', 'events', 'configs', 'yolo'):
                data[key].sort(key=lambda item: item[0])
                data[key + '_times'] = [r[0] for r in data[key]]
            if len(self._cache) >= 2:
                self._cache.pop(next(iter(self._cache)))
            self._cache[name] = data
            return data

    @staticmethod
    def _window(data, key, start, end, maximum):
        rows, times = data[key], data[key + '_times']
        lo, hi = max(0, bisect_right(times, start) - 1), bisect_right(times, end)
        selected = rows[lo:hi]
        step = max(1, math.ceil(len(selected) / maximum))
        result = selected[::step]
        if selected and result[-1] != selected[-1]:
            result.append(selected[-1])
        return result

    def window(self, name, start, duration):
        if not math.isfinite(start) or start < 0 or not math.isfinite(duration) or not 1 <= duration <= 300:
            raise ValueError('Start must be nonnegative and the window must be 1–300 seconds.')
        data = self.load(name)
        frames = data['frames']
        lo = max(0, bisect_right(data['frames_times'], start) - 1)
        hi = bisect_right(data['frames_times'], start + duration)
        return {'session': name, 'duration_s': data['duration_s'], 'ended': data['ended'],
                'counts': data['counts'], 'invalid': data['invalid'], 'dropped': data['dropped'],
                'states': self._window(data, 'states', start, start + duration, 1500),
                'yolo': self._window(data, 'yolo', start, start + duration, 1500),
                'configs': self._window(data, 'configs', start, start + duration, 200),
                'events': [{'time': t, 'kind': kind, 'data': payload, 'label': event_label(kind, payload)}
                           for t, kind, payload in self._window(data, 'events', start, start + duration, 2500)],
                'frames': [{'time': frames[i][0], 'index': i} for i in range(lo, hi)]}

    def image(self, name, index):
        data = self.load(name)
        if type(index) is not int or not 0 <= index < len(data['frames']):
            raise ValueError('Frame index is outside this recording.')
        # Reuse the viewer's validated path and byte-range reader without loading its timeline.
        reader = RecordingData(data['path'])
        reader.session = data['path']
        return reader.image_bytes(data['frames'][index][1])


def serve_recording(handler, url, archive):
    """Shared read-only routes for the dashboard and standalone replay server."""
    assets = {'/viewer': ('recording.html', 'text/html'),
              '/static/recording.js': ('recording.js', 'text/javascript'),
              '/static/recording.css': ('recording.css', 'text/css')}
    if url.path in assets:
        filename, mime = assets[url.path]
        handler._send(200, mime + '; charset=utf-8', (STATIC / filename).read_bytes())
        return True
    if url.path not in ('/api/viewer/sessions', '/api/viewer/window', '/api/viewer/frame'):
        return False
    try:
        args = parse_qs(url.query)
        if url.path == '/api/viewer/sessions':
            result = {'sessions': archive.sessions()}
        elif url.path == '/api/viewer/window':
            result = archive.window(args.get('session', [''])[0], float(args.get('start', ['0'])[0]),
                                    float(args.get('duration', ['60'])[0]))
        else:
            image = archive.image(args.get('session', [''])[0], int(args.get('index', ['-1'])[0]))
            if not image:
                raise ValueError('Recorded frame is not available yet.')
            handler._send(200, 'image/jpeg', image)
            return True
        handler._json(result)
    except (ValueError, OSError, KeyError, TypeError) as error:
        handler._json({'error': str(error)}, 400)
    return True


class ViewerHandler(BaseHTTPRequestHandler):
    def _send(self, code, mime, body):
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value, code=200):
        self._send(code, 'application/json', json.dumps(value, ensure_ascii=False).encode('utf-8'))

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == '/':
            url = urlsplit('/viewer')
        if url.path in ('/static/dashboard.css', '/static/theme.js'):
            filename = Path(url.path).name
            self._send(200, 'text/css' if filename.endswith('.css') else 'text/javascript', (STATIC / filename).read_bytes())
        elif not serve_recording(self, url, self.server.archive):
            self._send(404, 'text/plain', b'Not found')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', nargs='?', type=Path, default=RECORDINGS_DIR)
    parser.add_argument('--port', type=int, default=8081)
    args = parser.parse_args()
    with ThreadingHTTPServer(('127.0.0.1', args.port), ViewerHandler) as server:
        server.archive = RecordingArchive(args.path)
        print(f'Recording viewer: http://127.0.0.1:{server.server_port}/viewer', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
