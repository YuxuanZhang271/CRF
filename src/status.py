"""Recording schema, status collection, and bounded asynchronous session storage.

Robot snapshots, camera frames, inference, commands and console messages share
one timestamped JSONL timeline, using a bounded background writer.
"""

import importlib
import json
import logging
import math
import queue
import threading
import time
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path


RECORDINGS_DIR = Path(__file__).resolve().parents[1] / 'recordings'
SEGMENT_SECONDS = 300
STATE_HZ = 20
QUEUE_ITEMS = 4096
QUEUE_BYTES = 32 * 1024 * 1024
SCHEMA_VERSION = 1

# Runtime settings retained with every segment and whenever they change.
CONFIG_FIELDS = (
    'rcm_cone_deg', 'arm_linear_speed_mm_s', 'jog_speed_scale',
    'pull_out_tilt_speed_deg_s', 'rcm_point', 'rcm_ref_axis', 'rcm_ref_depth_m',
    'flex_model_bend_limit', 'flex_model_speed_deg_s',
)
FLEX_CONFIG_FIELDS = (
    'bend_limit', 'speed_limit_deg_s', 'calibrated', 'planned_zero',
    'planned_feedback', 'simulated', 'manual_bench',
)
CONFIG_MODULES = (
    'flex_endo_robo.control_node', 'flex_endo_robo.ur5_rtde_server',
    'flex_endo_robo.kinematics', 'flex_endo_robo.flex_endo.flex_control',
    'flex_endo_robo.flex_endo.flex_ipc', 'scope.camera', 'scope.yolo',
)
COMMAND_NAMES = {
    '/api/cmd/bird_eye': 'BIRD-EYE', '/api/cmd/pull_out': 'PULL OUT',
    '/api/flex/tracking': 'TRACK', '/api/cmd/interrupt': 'INTERRUPT',
    '/api/cmd/stop': 'STOP', '/api/flex/calibrate': 'CALIBRATE',
    '/api/cmd/clean_lens': 'CLEAN LENS',
    '/api/task': 'TASK', '/api/task/complete': 'TASK COMPLETE',
}


def json_value(value):
    """Produce strict JSON, including dataclasses and numpy scalar/array values."""
    if is_dataclass(value):
        return json_value(asdict(value))
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, 'tolist'):
        return json_value(value.tolist())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def app_state(server):
    """Use cached control feedback; never acquire the motion lock."""
    state = server.node.get_state_snapshot()
    state.update(stopping=server.stopping, interrupting=server.interrupting,
                 arm_enabled=server.relay is not None)
    state['task'] = server.tasks.snapshot()
    if server.relay is not None:
        state['motion_fault'] = server.relay.motion_fault()
    return state


class Status:
    """All recording configuration and sampled data are assembled here."""

    def __init__(self, server, launch_config):
        self.server = server
        self.launch_config = json_value(launch_config)
        self.defaults = {}
        for name in CONFIG_MODULES:
            constants = {}
            for key, value in vars(importlib.import_module(name)).items():
                if key.isupper() and not key.startswith('_'):
                    try:
                        value = json_value(value)
                        json.dumps(value, allow_nan=False)
                    except (TypeError, ValueError):
                        continue
                    constants[key] = value
            self.defaults[name] = constants

    def snapshot(self):
        try:
            state = app_state(self.server)
        except TimeoutError as error:
            state = {'error': str(error), 'stale': True,
                     'stopping': self.server.stopping,
                     'interrupting': self.server.interrupting,
                     'task': self.server.tasks.snapshot()}
        camera = self.server.camera.status()
        config = {'launch': self.launch_config, 'module_defaults': self.defaults,
                  'control': {key: state.get(key) for key in CONFIG_FIELDS},
                  'flex': {key: state.get('flex_hardware', {}).get(key)
                           for key in FLEX_CONFIG_FIELDS},
                  'camera': {'category': camera['category'], 'revision': camera['revision']}}
        return {'config': config, 'robot': state, 'camera': camera}


class _LogHandler(logging.Handler):
    def __init__(self, recorder):
        super().__init__()
        self.recorder = recorder

    def emit(self, record):
        self.recorder.event('console', {'source': record.name, 'level': record.levelname,
                                       'message': self.format(record),
                                       'origin': 'python', 'created_unix_s': record.created})


class Recorder:
    """Bounded producers and one disk writer, independent of dashboard polling.

    Events retain their own source times. Overload drops incoming records and
    exposes per-kind counts rather than blocking camera/control producers.
    """

    def __init__(self, source, directory=RECORDINGS_DIR, *, segment_seconds=SEGMENT_SECONDS,
                 state_hz=STATE_HZ, queue_items=QUEUE_ITEMS, queue_bytes=QUEUE_BYTES):
        self.source = source
        self.directory = Path(directory)
        self.segment_seconds = segment_seconds
        self.state_period = 1 / state_hz
        self.queue_items, self.queue_bytes = queue_items, queue_bytes
        self._lock = threading.Lock()
        self._lifecycle = threading.Lock()
        self._active = False
        self._worker = None
        self._error = None
        self._session = None
        self._dropped = {}
        self._closing = threading.Event()
        self._sampler = None
        self._origin = None
        self._stopped_elapsed = 0.
        self._log_handler = _LogHandler(self)
        logging.getLogger().addHandler(self._log_handler)

    def status(self):
        with self._lock:
            elapsed = (time.monotonic() - self._origin if self._active and self._origin is not None
                       else self._stopped_elapsed)
            return {'active': self._active, 'session': str(self._session) if self._session else None,
                    'error': self._error, 'dropped': dict(self._dropped),
                    'elapsed_s': elapsed, 'segment_seconds': self.segment_seconds,
                    'slice_index': int(elapsed // self.segment_seconds),
                    'slice_elapsed_s': elapsed % self.segment_seconds}

    def start(self):
        with self._lifecycle:
            if ((self._worker is not None and self._worker.is_alive()) or
                    (self._sampler is not None and self._sampler.is_alive())):
                if self.status()['active']:
                    return self.status()
                raise RuntimeError('The previous recording is still closing.')
            snapshot = json_value(self.source.snapshot())
            now = datetime.now(timezone.utc)
            session = self.directory / (now.strftime('%Y%m%dT%H%M%S_%fZ_') + uuid.uuid4().hex[:8])
            try:
                session.mkdir(parents=True)
            except OSError as error:
                with self._lock:
                    self._error = str(error)
                raise
            self._origin = time.monotonic()
            self._stopped_elapsed = 0.
            self._origin_unix = time.time()
            self._config = snapshot['config']
            self._segment = -1
            self._file = None
            self._frames = None
            self._sequence = 0
            self._queue = queue.Queue(self.queue_items)
            self._pending_bytes = 0
            self._closing.clear()
            with self._lock:
                self._session, self._error, self._dropped = session, None, {}
            try:
                self._open_segment(0)
                self._write('session_start', {'snapshot': snapshot}, self._origin)
                self._file.flush()
            except Exception as error:
                with self._lock:
                    self._error = str(error)
                if self._file:
                    try:
                        self._file.close()
                    except OSError:
                        pass
                raise
            with self._lock:
                self._active = True
            self._worker = threading.Thread(target=self._run, name='session-recorder')
            self._sampler = threading.Thread(target=self._sample_loop, name='recording-status')
            self._worker.start()
            self._sampler.start()
            return self.status()

    def stop(self):
        with self._lifecycle:
            with self._lock:
                if self._active:
                    self._stopped_elapsed = time.monotonic() - self._origin
                self._active = False
            self._closing.set()
            if self._sampler is not None:
                self._sampler.join(timeout=1.)
                if self._sampler.is_alive():
                    raise RuntimeError('Recording status collection is still closing.')
            if self._worker is not None:
                self._worker.join(timeout=5.)
                if self._worker.is_alive():
                    raise RuntimeError('Recording is still flushing to disk. Check recording status.')
            return self.status()

    def close(self):
        logging.getLogger().removeHandler(self._log_handler)
        self.stop()

    def event(self, kind, data, *, jpeg=None):
        # Payloads are immutable source snapshots. JPEG bytes are shared, not copied.
        size = len(jpeg) if jpeg is not None else 4096
        with self._lock:
            if not self._active:
                return
            # Reserve room for commands/logs when camera throughput saturates disk.
            byte_limit = self.queue_bytes * 3 // 4 if jpeg is not None else self.queue_bytes
            item_limit = (self.queue_items - min(128, self.queue_items // 4)
                          if jpeg is not None else self.queue_items)
            if self._pending_bytes + size > byte_limit or self._queue.qsize() >= item_limit:
                self._dropped[kind] = self._dropped.get(kind, 0) + 1
                return
            self._pending_bytes += size
            self._queue.put_nowait((time.monotonic(), kind, data, jpeg, size))

    def _open_segment(self, index):
        if self._frames is not None:
            self._frames.close()
            self._frames = None
        if self._file is not None:
            self._file.close()
        self._segment = index
        self._segment_dir = self._session / f'{index:05d}'
        self._segment_dir.mkdir()
        self._file = (self._segment_dir / 'events.jsonl').open('w', encoding='utf-8')
        self._write('segment_start', {
            'schema_version': SCHEMA_VERSION, 'segment': index, 'config': self._config,
            'session_origin_unix_s': self._origin_unix,
            'session_origin_monotonic_s': self._origin,
            'segment_seconds': self.segment_seconds, 'state_hz': 1 / self.state_period,
        }, self._origin + index * self.segment_seconds)

    def _write(self, kind, data, stamp):
        self._sequence += 1
        elapsed = stamp - self._origin
        row = {'sequence': self._sequence, 'type': kind,
               'elapsed_s': elapsed, 'monotonic_s': stamp,
               'timestamp': datetime.fromtimestamp(self._origin_unix + elapsed, timezone.utc).isoformat(),
               'data': json_value(data)}
        self._file.write(json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n')

    def _rotate(self, stamp):
        index = int((stamp - self._origin) // self.segment_seconds)
        if index > self._segment:
            self._open_segment(index)

    def _write_state(self, snapshot, stamp):
        config = json_value(snapshot['config'])
        if config != self._config:
            self._config = config
            self._write('config', config, stamp)
        self._write('state', {'robot': snapshot['robot'], 'camera': snapshot['camera'],
                              'recording': {'dropped': self.status()['dropped']}}, stamp)

    def _flush(self):
        # Make image bytes available before publishing their JSON index.
        if self._frames is not None:
            self._frames.flush()
        self._file.flush()

    def _sample_loop(self):
        while not self._closing.is_set():
            started = time.monotonic()
            try:
                self.event('state', self.source.snapshot())
            except Exception as error:
                self.event('state_error', {'error': str(error)})
            self._closing.wait(max(0., self.state_period - (time.monotonic() - started)))

    def _run(self):
        last_flush = time.monotonic()
        try:
            while not self._closing.is_set() or not self._queue.empty():
                try:
                    stamp, kind, data, jpeg, size = self._queue.get(timeout=.1)
                except queue.Empty:
                    if time.monotonic() - last_flush >= 1.:
                        self._flush()
                        last_flush = time.monotonic()
                    continue
                with self._lock:
                    self._pending_bytes -= size
                self._rotate(stamp)
                if jpeg is not None:
                    if self._frames is None:
                        self._frames = (self._segment_dir / 'camera.mjpeg').open('wb')
                    offset = self._frames.tell()
                    self._frames.write(jpeg)
                    data = {**data, 'image': {'path': f'{self._segment:05d}/camera.mjpeg',
                                            'offset_bytes': offset, 'length_bytes': len(jpeg)}}
                if kind == 'state':
                    self._write_state(data, stamp)
                else:
                    self._write(kind, data, stamp)
                if time.monotonic() - last_flush >= 1.:
                    self._flush()
                    last_flush = time.monotonic()
            stamp = time.monotonic()
            self._rotate(stamp)
            try:
                snapshot = self.source.snapshot()
            except Exception as error:
                self._write('state_error', {'error': str(error)}, stamp)
            else:
                self._write_state(snapshot, stamp)
            self._write('session_stop', {'dropped': self.status()['dropped']}, stamp)
        except Exception as error:
            with self._lock:
                self._error = f'{type(error).__name__}: {error}'
                self._stopped_elapsed = time.monotonic() - self._origin
                self._active = False
            self._closing.set()
            # Do not log through the recorder from its failure path.
        finally:
            for file in (self._frames, self._file):
                try:
                    if file is not None:
                        file.close()
                except OSError as error:
                    with self._lock:
                        self._error = str(error)
            with self._lock:
                self._active = False
            # Release queued image bytes after a disk/serialization failure.
            with self._lock:
                while not self._queue.empty():
                    self._queue.get_nowait()
                self._pending_bytes = 0
