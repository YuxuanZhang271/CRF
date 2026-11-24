#!/usr/bin/env python3
"""Inspect JSONL/MJPEG recordings offline or monitor a running recording.

python src/recording_viewer.py recordings/<session> --start 300
python src/recording_viewer.py recordings --live
python src/recording_viewer.py recordings/<session> --output activity.png

Only recording files are read. No camera, motor, RTDE or control app is opened.
"""

import argparse
import json
import math
from collections import Counter, deque
from pathlib import Path

from status import RECORDINGS_DIR


VIEWER_COLORS = {
    'background': '#17151b', 'panel': '#211e27', 'edge': '#3b3542',
    'text': '#f3f0f5', 'muted': '#b9b2c1', 'purple': '#c995d1',
    'gold': '#dda300', 'camera': '#101014',
}
TRACE_COLORS = ('#c995d1', '#e9bc62', '#5bc5a4', '#79b2f2', '#edaa78', '#d8d2de')


def resolve_recording(path):
    """Accept a session, a segment, a JSONL file, or a root containing sessions."""
    path = Path(path).resolve()
    if path.is_file():
        if path.name != 'events.jsonl':
            raise ValueError('Choose events.jsonl, a segment, a session, or the recordings root.')
        return path.parent.parent, [path]
    if (path / 'events.jsonl').is_file():
        return path.parent, [path / 'events.jsonl']
    files = sorted(path.glob('[0-9]*/events.jsonl'))
    if files:
        return path, files
    candidates = sorted(p.parent.parent for p in path.glob('*/[0-9]*/events.jsonl'))
    if candidates:
        session = candidates[-1]
        return session, sorted(session.glob('[0-9]*/events.jsonl'))
    return path, []


class RecordingData:
    """Incrementally read complete lines, retaining one bounded time window.

    Offline start/duration select any part of a session. Live mode follows the
    newest window and automatically follows new slices and sessions under a root.
    """

    def __init__(self, path, *, start=None, duration=300., live=False):
        self.automatic_start = start is None
        start = 0. if start is None else start
        if start < 0 or not math.isfinite(start) or not 0 < duration <= 3600:
            raise ValueError('Start must be finite and nonnegative; duration must be 0–3600 seconds.')
        self.path = Path(path)
        self.start, self.duration, self.live = start, duration, live
        self.session = None
        self._reset()

    def _reset(self):
        self.offsets = {}
        # Hard caps supplement the time window when source rates or timestamps are bad.
        self.states = deque(maxlen=80000)
        self.frames = deque(maxlen=120000)
        self.yolo = deque(maxlen=20000)
        self.events = deque(maxlen=2000)
        self.configs = deque(maxlen=512)
        self.counts = Counter()
        self.invalid_lines = 0
        self.latest = 0.
        self.ended = False
        self.origin_monotonic = None

    def refresh(self):
        session, files = resolve_recording(self.path)
        if (files and self.automatic_start and not self.live
                and (self.path.is_file() or (self.path / 'events.jsonl').is_file())):
            with files[0].open('rb') as stream:
                try:
                    first = json.loads(stream.readline())
                    stamp = float(first['elapsed_s'])
                    if math.isfinite(stamp) and stamp >= 0:
                        self.start = stamp
                except (ValueError, TypeError, KeyError):
                    pass
        if session != self.session or any(p.exists() and p.stat().st_size < offset
                                          for p, offset in self.offsets.items()):
            self.session = session
            self._reset()
        for path in files:
            with path.open('rb') as stream:
                stream.seek(self.offsets.get(path, 0))
                while True:
                    offset = stream.tell()
                    line = stream.readline()
                    if not line or not line.endswith(b'\n'):
                        self.offsets[path] = offset  # Retry an incomplete live line later.
                        break
                    try:
                        row = json.loads(line)
                        if (not isinstance(row, dict) or not isinstance(row.get('data'), dict)
                                or not isinstance(row.get('elapsed_s'), (int, float))
                                or not math.isfinite(row['elapsed_s']) or row['elapsed_s'] < 0):
                            raise ValueError('Invalid event schema')
                        self._accept(row)
                    except (ValueError, TypeError, KeyError, UnicodeError):
                        self.invalid_lines += 1
        self._trim()
        return self

    def _accept(self, row):
        kind, stamp, data = row['type'], row['elapsed_s'], row['data']
        self.counts[kind] += 1
        self.latest = max(self.latest, stamp)
        if kind == 'session_stop':
            self.ended = True
        keep_config = self.live or stamp <= self.start + self.duration
        if kind == 'segment_start':
            self.origin_monotonic = data.get('session_origin_monotonic_s')
            if keep_config:
                self.configs.append((stamp, data.get('config', {})))
        elif kind == 'config' and keep_config:
            self.configs.append((stamp, data))
        if not self.live and not self.start <= stamp <= self.start + self.duration:
            return
        if kind == 'state':
            self.states.append((stamp, data))
        elif kind == 'session_start' and data.get('snapshot'):
            self.states.append((stamp, data['snapshot']))
        elif kind == 'camera_frame':
            self.frames.append((stamp, data))
        elif kind == 'yolo':
            self.yolo.append((stamp, data))
        elif (kind in ('console', 'state_error', 'camera_recording_error', 'session_stop', 'task', 'llm', 'perception_mode', 'surgical_stage')
              or kind == 'config' or
              (kind in ('command', 'command_result') and data.get('path') != '/api/key/heartbeat')):
            self.events.append((stamp, kind, data))
        if self.live:
            self._trim()

    def _trim(self):
        low, _ = self.window
        for records in (self.states, self.frames, self.yolo, self.events):
            while records and records[0][0] < low:
                records.popleft()
        while len(self.configs) > 1 and self.configs[1][0] < low:
            self.configs.popleft()

    @property
    def window(self):
        low = max(0., self.latest - self.duration) if self.live else self.start
        return low, low + self.duration

    @staticmethod
    def at(records, stamp):
        return next((data for at, data in reversed(records) if at <= stamp), None)

    def image_bytes(self, frame):
        reference = frame.get('image')
        if not reference:
            return None
        relative = reference if isinstance(reference, str) else reference['path']
        path = (self.session / relative).resolve()
        if not path.is_relative_to(self.session) or path.suffix.lower() not in ('.jpg', '.jpeg', '.mjpeg'):
            raise ValueError('Image reference is outside the recording session.')
        if isinstance(reference, str):
            if path.stat().st_size > 16 * 1024 * 1024:
                raise ValueError('Recorded image is too large.')
            return path.read_bytes()
        offset, length = reference['offset_bytes'], reference['length_bytes']
        if type(offset) is not int or type(length) is not int or offset < 0 or not 0 < length <= 16 * 1024 * 1024:
            raise ValueError('Invalid JPEG offset or size.')
        with path.open('rb') as stream:
            stream.seek(offset)
            jpeg = stream.read(length)
        return jpeg if len(jpeg) == length else None  # MJPEG may still be buffered by the writer.

    def summary(self):
        return {'session': str(self.session), 'duration_s': self.latest, 'ended': self.ended,
                'event_counts': dict(self.counts), 'invalid_complete_lines': self.invalid_lines,
                'window_s': self.window, 'visible_states': len(self.states),
                'configuration': self.at(self.configs, self.window[1])}


def event_label(kind, data):
    if kind == 'task':
        return f"TASK {data.get('id')} · {data.get('name')} · {data.get('reason', '')}"
    if kind == 'llm':
        if data.get('type') == 'command.dispatch':
            return f"LLM → TASK {data.get('task_id')} · {data.get('result', {}).get('error') or 'accepted'}"
        return f"LLM {data.get('type', '')} · {data.get('text') or data.get('message') or data.get('status', '')}"
    if kind == 'console':
        return f"{data.get('source', '')}: {data.get('message', '')}"
    if kind == 'command_result':
        return f"{data.get('command')} → HTTP {data.get('http_status')} {data.get('response', {}).get('error', '')}"
    if kind == 'command':
        return f"{data.get('command')} {json.dumps(data.get('body', {}), ensure_ascii=False)}"
    if kind == 'surgical_stage':
        top = (data.get('top3') or [{}])[0]
        return f"Stage {top.get('id', '—')} · {top.get('name', '')}"
    if kind == 'perception_mode':
        return f"Perception mode · {data.get('mode', '')}"
    if kind in ('state_error', 'camera_recording_error'):
        return str(data.get('error', 'State unavailable'))
    return kind.replace('_', ' ')


class ActivityViewer:
    def __init__(self, data):
        import matplotlib.pyplot as plt
        from matplotlib.widgets import Slider

        self.data, self.plt = data, plt
        self.figure = plt.figure(figsize=(15, 9), facecolor=VIEWER_COLORS['background'])
        grid = self.figure.add_gridspec(3, 3, left=.06, right=.97, bottom=.14, top=.89,
                                       hspace=.48, wspace=.26, width_ratios=(1.1, 1.1, 1.25))
        self.joints = self.figure.add_subplot(grid[0, :2])
        self.flex = self.figure.add_subplot(grid[1, :2])
        self.tendons = self.figure.add_subplot(grid[2, :2])
        self.camera = self.figure.add_subplot(grid[:2, 2])
        self.console = self.figure.add_subplot(grid[2, 2])
        self.title = self.figure.text(.06, .955, '', fontsize=15, weight='bold', color=VIEWER_COLORS['purple'])
        self.status = self.figure.text(.06, .912, '', fontsize=10, color=VIEWER_COLORS['muted'])
        self.notice = self.figure.text(.06, .025, '', fontsize=9, color=VIEWER_COLORS['muted'])
        low, high = data.window
        self.slider = Slider(self.figure.add_axes((.12, .067, .75, .025)), 'Time (s)', low, high,
                             valinit=min(high, max(low, data.latest)),
                             color=VIEWER_COLORS['purple'], track_color=VIEWER_COLORS['edge'],
                             initcolor=VIEWER_COLORS['muted'],
                             handle_style={'facecolor': VIEWER_COLORS['purple'],
                                           'edgecolor': VIEWER_COLORS['background']})
        self.slider.ax.set_facecolor(VIEWER_COLORS['background'])
        self.slider.label.set_color(VIEWER_COLORS['text'])
        self.slider.valtext.set_color(VIEWER_COLORS['text'])
        self.slider.on_changed(self.select)
        self._frame_key = None
        self.timer = None
        self.draw()

    @staticmethod
    def _style_axis(axis, *, camera=False):
        # Reapply after clear() so live redraws keep the same local theme.
        axis.set_facecolor(VIEWER_COLORS['camera' if camera else 'panel'])
        axis.set_prop_cycle(color=TRACE_COLORS)
        axis.tick_params(colors=VIEWER_COLORS['muted'])
        axis.xaxis.label.set_color(VIEWER_COLORS['muted'])
        axis.yaxis.label.set_color(VIEWER_COLORS['muted'])
        axis.xaxis.get_offset_text().set_color(VIEWER_COLORS['muted'])
        axis.yaxis.get_offset_text().set_color(VIEWER_COLORS['muted'])
        axis.title.set_color(VIEWER_COLORS['text'])
        for spine in axis.spines.values():
            spine.set_color(VIEWER_COLORS['edge'])

    def draw(self):
        import numpy as np

        data = self.data
        low, high = data.window
        if data.states:
            high = max(low + .1, min(high, data.latest))
        # Keep redraws bounded even for a one-hour analysis window.
        states = list(data.states)
        step = max(1, len(states) // 2000)
        states = states[::step]
        t = [stamp for stamp, _ in states]

        def value(snapshot, key, index=None):
            robot = snapshot.get('robot', {})
            hardware = robot.get('flex_hardware')
            if key == 'bend':
                result = hardware.get('bend') if hardware else robot.get('flex_theta_total_deg')
            elif key == 'roll':
                result = hardware.get('roll') if hardware else robot.get('flex_base_yaw')
            elif key == 'tendon':
                tendons = hardware.get('tendons') if hardware else robot.get('tendons')
                result = (tendons[index] if isinstance(tendons, list) and len(tendons) > index else
                          tendons.get(('R', 'G', 'B', 'Y')[index], [None])[0] if isinstance(tendons, dict) else None)
            else:
                result = robot.get(key)
                if index is not None:
                    result = result[index] if isinstance(result, list) and len(result) > index else None
            return result if isinstance(result, (int, float)) else np.nan

        for axis, title, unit in ((self.joints, 'UR5 joints', 'degrees'),
                                   (self.flex, 'FlexEndo bend / roll', 'degrees'),
                                   (self.tendons, 'FlexEndo tendons', 'mm')):
            axis.clear()
            self._style_axis(axis)
            axis.set(title=title, ylabel=unit, xlim=(low, high))
            axis.grid(color=VIEWER_COLORS['edge'], alpha=1, lw=.7)
            axis.tick_params(labelsize=8)
        self.tendons.set_xlabel('Session elapsed time (s)')
        for index in range(6):
            self.joints.plot(t, [value(s, 'joints_deg', index) for _, s in states], lw=1, label=f'J{index + 1}')
        for key in ('bend', 'roll'):
            self.flex.plot(t, [value(s, key) for _, s in states], lw=1.3, label=key)
        for index in range(4):
            self.tendons.plot(t, [value(s, 'tendon', index) for _, s in states], lw=1, label=f'T{index + 1}')
        for axis in (self.joints, self.flex, self.tendons):
            axis.legend(loc='upper left', ncol=6, fontsize=8, framealpha=.9,
                        facecolor=VIEWER_COLORS['panel'], edgecolor=VIEWER_COLORS['edge'],
                        labelcolor=VIEWER_COLORS['text'])
        for stamp, kind, event in data.events:
            if kind == 'command' and event.get('path', '').startswith(('/api/cmd/', '/api/flex/tracking')):
                self.joints.axvline(stamp, color=VIEWER_COLORS['gold'], alpha=.65, lw=.7)
                self.joints.text(stamp, .05, event.get('command', ''), rotation=90,
                                 transform=self.joints.get_xaxis_transform(), fontsize=6, color=VIEWER_COLORS['gold'])
        self.cursors = [axis.axvline(self.slider.val, color=VIEWER_COLORS['purple'], lw=1, ls='--') for axis in
                        (self.joints, self.flex, self.tendons)]
        self.title.set_text(f'CRF recording activities  ·  {data.session.name if data.session else "Waiting for recordings"}')
        self.slider.valmin, self.slider.valmax = low, high
        self.slider.ax.set_xlim(low, high)
        stamp = min(high, data.latest) if data.live else min(high, max(low, self.slider.val))
        self.slider.set_val(max(low, stamp))
        self.notice.set_text(f'{"LIVE — follows the newest session" if data.live else "Drag the time slider to inspect state and camera"}'
                             f'   |   window {low:.0f}–{high:.0f} s   |   {data.invalid_lines} invalid complete lines'
                             f'{"   |   session ended" if data.ended else ""}'
                             '   |   tendon values follow the recorded feedback mode')
        self.figure.canvas.draw_idle()

    def select(self, stamp):
        import io
        import textwrap

        data = self.data
        snapshot = data.at(data.states, stamp) or {}
        robot = snapshot.get('robot', {})
        flex = robot.get('flex_hardware') or {}
        drops = snapshot.get('recording', {}).get('dropped', {})
        tracking = flex.get('tracking_state', 'Off')
        arm = 'disabled' if robot.get('arm_enabled') is False else 'connected' if robot.get('joints_received') else 'unavailable'
        connection = ('connected' if flex.get('connected') else
                      'modeled' if not flex and robot.get('arm_enabled') is True else
                      'disabled' if not flex and robot.get('arm_enabled') is False else 'unavailable')
        camera_status = snapshot.get('camera', {})
        camera_message = (camera_status.get('error') or camera_status.get('detection_error')
                          or ('ready' if camera_status.get('ready') else 'waiting'))
        self.status.set_text(f't={stamp:.2f} s  |  ARM {arm}  |  FLEX {connection}  |  TRACK {tracking}'
                             f'  |  Pull Out {robot.get("pull_out_state", "—")}  |  drops {sum(drops.values())}'
                             f'  |  Camera: {camera_message[:70]}\n'
                             f'Tip limit {robot.get("rcm_cone_deg", "—")}° · arm speed {robot.get("arm_linear_speed_mm_s", "—")} mm/s'
                             f' · bend limit {flex.get("bend_limit", robot.get("flex_model_bend_limit", "—"))}°'
                             f' · Flex speed {flex.get("speed_limit_deg_s", robot.get("flex_model_speed_deg_s", "—"))}°/s'
                             f'{" · " + str(robot["error"])[:75] if robot.get("error") else ""}')
        for cursor in getattr(self, 'cursors', []):
            cursor.set_xdata([stamp, stamp])
        frame = data.at(data.frames, stamp)
        key = str(data.session) + json.dumps(frame.get('image')) if frame else None
        if key != self._frame_key or self._frame_key is None:
            self.camera.clear()
            self._style_axis(self.camera, camera=True)
            self.camera.axis('off')
            self.camera.set_title('Recorded camera preview', fontsize=11)
            shown = False
            if frame:
                try:
                    image = data.image_bytes(frame)
                    if image:
                        self.camera.imshow(self.plt.imread(io.BytesIO(image), format='jpeg'))
                        self._frame_key = key
                        shown = True
                except (OSError, ValueError, KeyError, TypeError) as error:
                    self.camera.text(.05, .5, str(error), transform=self.camera.transAxes,
                                     wrap=True, fontsize=9, color=VIEWER_COLORS['text'])
            if not shown:
                self.camera.text(.5, .3, 'No readable frame at this time', ha='center',
                                 transform=self.camera.transAxes, fontsize=10, color=VIEWER_COLORS['muted'])
        result = data.at(data.yolo, stamp) or {}
        obs = result.get('observation', {})
        self.camera.set_title(f'Camera · YOLO frame {obs.get("frame", "—")} · '
                              f'{len(result.get("detections", []))} detections · '
                              f'{"target centered" if obs.get("centered") else "target visible" if obs.get("valid") else "no target"}', fontsize=9)
        self.console.clear()
        self._style_axis(self.console)
        self.console.axis('off')
        self.console.set_title('Commands / console', loc='left', fontsize=11, color=VIEWER_COLORS['text'])
        lines = []
        for at, kind, event in reversed(data.events):
            if at <= stamp:
                wrapped = textwrap.wrap(f'{at:7.2f}  {event_label(kind, event)}', 61)
                lines[0:0] = wrapped[:2]
                if len(lines) >= 10:
                    break
        self.console.text(0, .98, '\n'.join(lines[-10:]) or 'No events in this window.',
                          va='top', fontsize=8, family='monospace', transform=self.console.transAxes,
                          color=VIEWER_COLORS['text'])
        self.figure.canvas.draw_idle()

    def follow(self):
        def update():
            try:
                self.data.refresh()
                self.draw()
            except (OSError, ValueError) as error:
                self.notice.set_text(f'Waiting for recording files: {error}')
                self.figure.canvas.draw_idle()
        self.timer = self.figure.canvas.new_timer(interval=1000)
        self.timer.add_callback(update)
        self.timer.start()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('path', nargs='?', type=Path, default=RECORDINGS_DIR)
    parser.add_argument('--live', action='store_true', help='Follow new lines, slices, and sessions')
    parser.add_argument('--start', type=float, help='Offline window start; default: session/slice beginning')
    parser.add_argument('--duration', type=float, default=300., help='Visible window in seconds (maximum 3600)')
    parser.add_argument('--output', type=Path, help='Save a PNG/PDF plot instead of opening a GUI')
    parser.add_argument('--summary', action='store_true', help='Print JSON summary without GUI dependencies')
    args = parser.parse_args()
    try:
        data = RecordingData(args.path, start=args.start, duration=args.duration, live=args.live).refresh()
        if args.summary:
            print(json.dumps(data.summary(), indent=2, ensure_ascii=False))
            return 0
        if not data.offsets and not args.live:
            parser.error('No events.jsonl files found. Choose a recorded session or use --live to wait.')
        if args.output:
            import matplotlib
            matplotlib.use('Agg')
        viewer = ActivityViewer(data)
        if args.output:
            viewer.figure.savefig(args.output, dpi=150, facecolor=viewer.figure.get_facecolor())
            print(f'Saved {args.output.resolve()}')
        else:
            if args.live:
                viewer.follow()
            viewer.plt.show()
        return 0
    except (OSError, ValueError) as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
