"""Bounded server log feed for the dashboard, independent of recording state."""

from collections import deque
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import threading
from zoneinfo import ZoneInfo


class RunLog:
    """One UTF-8 warnings/errors file per app run, using the client's local time."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.handler = None
        self.path = None

    def __enter__(self):
        zone = ZoneInfo('Asia/Hong_Kong')
        started = datetime.now(zone)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / f"crf_{started:%Y%m%d_%H%M%S_%f}_{os.getpid()}.log"
        self.handler = logging.FileHandler(self.path, mode='x', encoding='utf-8', errors='backslashreplace')
        self.handler.setLevel(logging.WARNING)
        class LocalFormatter(logging.Formatter):
            def formatTime(self, record, datefmt=None):
                return datetime.fromtimestamp(record.created, zone).isoformat(timespec='milliseconds')
        self.handler.setFormatter(LocalFormatter('%(asctime)s %(levelname)s [%(name)s] %(message)s'))
        self.handler.stream.write(f'Run started: {started.isoformat()}\nPID: {os.getpid()}\nWarnings and errors:\n')
        self.handler.flush()
        logging.getLogger().addHandler(self.handler)
        logging.captureWarnings(True)
        self._previous_thread_hook = threading.excepthook
        def thread_error(args):
            if args.exc_type is SystemExit:
                return self._previous_thread_hook(args)
            logging.getLogger('UncaughtThread').critical('Unhandled error in %s', args.thread.name,
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        self._thread_hook = thread_error
        threading.excepthook = thread_error
        return self

    def __exit__(self, kind, error, tb):
        if error is not None and kind is not KeyboardInterrupt and not (
                kind is SystemExit and error.code in (None, 0)):
            logging.getLogger('WindowsApp').error('App exited with an unhandled error', exc_info=(kind, error, tb))
        if threading.excepthook is self._thread_hook:
            threading.excepthook = self._previous_thread_hook
        logging.captureWarnings(False)
        logging.getLogger().removeHandler(self.handler)
        self.handler.stream.write(f'Run ended: {datetime.now(ZoneInfo("Asia/Hong_Kong")).isoformat()}\n')
        self.handler.close()


class DiagnosticLog(logging.Handler):
    def __init__(self, capacity=512):
        super().__init__(logging.INFO)
        self._rows = deque(maxlen=capacity)
        self._sequence = 0
        self._rows_lock = threading.Lock()

    def start(self):
        logging.getLogger().addHandler(self)
        return self

    def emit(self, record):
        # This browser has already displayed its own forwarded message. Do not
        # send it back around the HTTP/log loop; terminal handlers still see it.
        if getattr(record, 'from_dashboard', False):
            return
        source = 'TASK' if record.name == 'Tasks' else record.name
        with self._rows_lock:
            self._sequence += 1
            self._rows.append({'sequence': self._sequence, 'source': source,
                               'level': record.levelname.lower(), 'message': self.format(record),
                               'timestamp': datetime.fromtimestamp(record.created, timezone.utc).isoformat()})

    def snapshot(self, after=0):
        with self._rows_lock:
            return {'sequence': self._sequence,
                    'messages': [dict(row) for row in self._rows if row['sequence'] > after]}

    def close(self):
        logging.getLogger().removeHandler(self)
        super().close()
