"""Read-only host/process telemetry, sampled away from control and HTTP threads."""

import copy
import threading
import time

try:
    import psutil
except ImportError:
    psutil = None


THREAD_GROUPS = {
    'camera': ('Camera', ('scope-capture',)),
    'vision': ('Vision', ('scope-detection',)),
    'stage': ('Stage', ('scope-stage',)),
    'control': ('Control / IPC', ('ctrl-tick', 'tcp-recv', 'rtde-', 'flex-ipc')),
    'voice': ('Voice service', ('llm-interaction',)),
    'recording': ('Recording', ('session-recorder', 'recording-status')),
    'other': ('App / native pools', ()),
}
PROCESS_LABELS = {'main': 'CRF shared RAM', 'asr': 'ASR workers', 'llm': 'LLM workers',
                  'children': 'Other workers'}


def thread_group(name):
    return next((key for key, (_, prefixes) in THREAD_GROUPS.items()
                 if prefixes and name.startswith(prefixes)), 'other')


def process_group(command):
    # Inspect worker script names only; never expose process command lines in the API.
    scripts = [str(arg).replace('\\', '/').rsplit('/', 1)[-1].lower() for arg in command]
    if 'command_worker.py' in scripts:
        return 'llm'
    if any(name in ('portable_worker.py', 'asr_windows_native.py', 'language_query_worker.py')
           or name.startswith('qwen_native_') for name in scripts):
        return 'asr'
    return 'children'


class PerformanceMonitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._worker = None
        self._state = {'status': 'starting', 'sampled_at': None}
        self._previous = {}
        self._last_sample = None
        self._sequence = 0
        self._nvml = None
        self._gpu_error = 'NVIDIA GPU monitoring unavailable.'

    def start(self):
        self._worker = threading.Thread(target=self._run, name='performance-monitor', daemon=True)
        self._worker.start()
        return self

    def close(self):
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=2.)

    def snapshot(self):
        with self._lock:
            state = copy.deepcopy(self._state)
        stamp = state.get('sampled_at')
        state['sample_age_s'] = max(0., time.time() - stamp) if stamp is not None else None
        return state

    def _init_gpu(self):
        try:
            import pynvml
            pynvml.nvmlInit()
            self._nvml = pynvml
            self._gpu_error = None
        except Exception:
            # Missing library, unsupported hardware and unavailable drivers are normal.
            self._gpu_error = 'NVIDIA GPU monitoring unavailable (NVML / driver).'

    def _gpus(self):
        if self._nvml is None:
            return []
        nv = self._nvml
        rows = []
        try:
            count = nv.nvmlDeviceGetCount()
        except Exception:
            self._gpu_error = 'GPU monitoring unavailable; restart CRF after checking the driver.'
            return []
        for index in range(count):
            row = {'id': f'gpu{index}', 'label': f'GPU {index}', 'percent': None,
                   'memory_used_bytes': None, 'memory_total_bytes': None}
            try:
                handle = nv.nvmlDeviceGetHandleByIndex(index)
                name = nv.nvmlDeviceGetName(handle)
                row['name'] = name.decode() if isinstance(name, bytes) else name
                try:
                    row['percent'] = float(nv.nvmlDeviceGetUtilizationRates(handle).gpu)
                except Exception:
                    pass
                try:
                    memory = nv.nvmlDeviceGetMemoryInfo(handle)
                    row.update(memory_used_bytes=int(memory.used), memory_total_bytes=int(memory.total))
                except Exception:
                    pass
            except Exception:
                row['error'] = 'Device unavailable'
            rows.append(row)
        return rows

    def _sample(self):
        now = time.monotonic()
        interval = None if self._last_sample is None else max(.001, now - self._last_sample)
        current = {}
        cores = psutil.cpu_count() or 1

        def usage(key, value):
            current[key] = value
            before = self._previous.get(key)
            if interval is None or before is None:
                return None
            return min(100., max(0., (value - before) / interval / cores * 100.))

        root = psutil.Process()
        components = {key: {'id': key, 'label': label, 'cpu_percent': None, 'rss_bytes': None}
                      for key, (label, _) in THREAD_GROUPS.items()}
        names = {t.native_id: t.name for t in threading.enumerate()}
        try:
            for thread in root.threads():
                group = thread_group(names.get(thread.id, ''))
                value = usage(('thread', thread.id), thread.user_time + thread.system_time)
                if value is not None:
                    row = components[group]
                    row['cpu_percent'] = (row['cpu_percent'] or 0.) + value
        except psutil.Error:
            pass
        # Read process totals separately: RAM belongs to a process, never to its threads.
        processes = {'main': {'id': 'main', 'label': PROCESS_LABELS['main'],
                              'cpu_percent': None, 'rss_bytes': None}}
        try:
            processes['main']['rss_bytes'] = root.memory_info().rss
        except psutil.Error:
            pass
        try:
            children = root.children(recursive=True)
        except psutil.Error:
            children = []
        for child in children:
            try:
                group = process_group(child.cmdline())
                identity = (child.pid, child.create_time())
                times = child.cpu_times()
                value = usage(('process', *identity), times.user + times.system)
                rss = child.memory_info().rss
            except psutil.Error:
                continue
            row = processes.setdefault(group, {'id': group, 'label': PROCESS_LABELS[group],
                                               'cpu_percent': None, 'rss_bytes': 0})
            if value is not None:
                row['cpu_percent'] = (row['cpu_percent'] or 0.) + value
            row['rss_bytes'] += rss
        memory = psutil.virtual_memory()
        self._sequence += 1
        state = {'status': 'ready', 'sequence': self._sequence, 'sampled_at': time.time(),
                 'cpu': {'percent': psutil.cpu_percent(interval=None) if interval else None, 'cores': cores},
                 'memory': {'percent': memory.percent, 'used_bytes': memory.total - memory.available,
                            'total_bytes': memory.total},
                 'components': list(components.values()) + list(processes.values()),
                 'gpus': self._gpus(), 'gpu_error': self._gpu_error}
        self._previous = current
        self._last_sample = now
        return state

    def _run(self):
        try:
            if psutil is None:
                with self._lock:
                    self._state = {'status': 'unavailable', 'error': 'Install psutil to monitor CPU and RAM.',
                                   'sampled_at': None}
                return
            psutil.cpu_percent(interval=None)  # Prime on the same thread as subsequent reads.
            self._init_gpu()
            while not self._stop.is_set():
                try:
                    state = self._sample()
                except Exception:
                    state = {'status': 'unavailable', 'error': 'Performance sampling unavailable.',
                             'sampled_at': None}
                with self._lock:
                    self._state = state
                self._stop.wait(1.)
        finally:
            if self._nvml is not None:
                try:
                    self._nvml.nvmlShutdown()
                except Exception:
                    pass
