"""Passive telemetry deltas and degradation, without cameras, robots or models."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import performance
from performance import PerformanceMonitor, process_group, thread_group


class PerformanceTests(unittest.TestCase):
    def test_thread_and_worker_attribution(self):
        for name, expected in [('scope-capture', 'camera'), ('scope-detection', 'vision'),
                               ('scope-stage', 'stage'), ('rtde-motion', 'control'),
                               ('flex-ipc', 'control'), ('llm-interaction', 'voice'),
                               ('session-recorder', 'recording'), ('', 'other')]:
            self.assertEqual(thread_group(name), expected)
        self.assertEqual(process_group(['python', '-u', r'C:\sdk\command_worker.py']), 'llm')
        self.assertEqual(process_group(['python', '/sdk/portable_worker.py']), 'asr')
        self.assertEqual(process_group(['python', 'unrelated.py']), 'children')

    def test_cpu_normalization_shared_ram_and_process_restarts(self):
        root, child = Mock(), Mock()
        root.threads.side_effect = [
            [NS(id=10, user_time=1., system_time=0.), NS(id=20, user_time=2., system_time=0.)],
            [NS(id=10, user_time=1.5, system_time=0.), NS(id=20, user_time=2.25, system_time=0.)],
            [NS(id=10, user_time=1.5, system_time=0.)],
        ]
        root.memory_info.return_value = NS(rss=200)
        root.children.return_value = [child]
        child.pid = 99
        child.create_time.side_effect = [1., 1., 2.]
        child.cmdline.return_value = ['python', 'command_worker.py']
        child.cpu_times.side_effect = [NS(user=10., system=0.), NS(user=11., system=0.), NS(user=90., system=0.)]
        child.memory_info.return_value = NS(rss=100)
        fake = NS(Process=lambda: root, cpu_count=lambda: 4, cpu_percent=lambda **kw: 40.,
                  virtual_memory=lambda: NS(total=1000, available=400, percent=60.), Error=OSError)
        monitor = PerformanceMonitor()
        with patch.object(performance, 'psutil', fake), \
                patch('performance.threading.enumerate', return_value=[NS(native_id=10, name='scope-detection')]), \
                patch('performance.time.monotonic', side_effect=[1., 2., 3.]):
            first = monitor._sample()
            self.assertIsNone(first['cpu']['percent'])
            second = monitor._sample()
            parts = {row['id']: row for row in second['components']}
            self.assertEqual(parts['vision']['cpu_percent'], 12.5)
            self.assertEqual(parts['other']['cpu_percent'], 6.25)
            self.assertEqual(parts['llm']['cpu_percent'], 25.)
            self.assertIsNone(parts['vision']['rss_bytes'])
            self.assertEqual(parts['main']['rss_bytes'], 200)
            self.assertEqual(parts['llm']['rss_bytes'], 100)
            self.assertEqual(second['memory']['used_bytes'], 600)
            restarted = monitor._sample()
            llm = next(row for row in restarted['components'] if row['id'] == 'llm')
            self.assertIsNone(llm['cpu_percent'], 'Reused PIDs must start a fresh baseline')
            self.assertNotIn(('thread', 20), monitor._previous)

    def test_partial_gpu_support_keeps_vram_and_clears_old_utilization(self):
        monitor = PerformanceMonitor()
        nv = Mock()
        nv.nvmlDeviceGetCount.return_value = 1
        nv.nvmlDeviceGetName.return_value = b'Test GPU'
        nv.nvmlDeviceGetUtilizationRates.side_effect = [NS(gpu=42), RuntimeError('Not supported')]
        nv.nvmlDeviceGetMemoryInfo.return_value = NS(used=20, total=100)
        monitor._nvml = nv
        self.assertEqual(monitor._gpus()[0]['percent'], 42)
        row = monitor._gpus()[0]
        self.assertIsNone(row['percent'])
        self.assertEqual(row['memory_used_bytes'], 20)
        nv.nvmlDeviceGetCount.side_effect = RuntimeError('Device lost')
        self.assertEqual(monitor._gpus(), [])
        self.assertIn('unavailable', monitor._gpu_error)

    def test_unavailable_dependency_and_snapshot_isolation(self):
        monitor = PerformanceMonitor()
        with patch.object(performance, 'psutil', None):
            monitor._run()
        state = monitor.snapshot()
        self.assertEqual(state['status'], 'unavailable')
        state['status'] = 'changed'
        self.assertEqual(monitor.snapshot()['status'], 'unavailable')
        monitor._state = {'sampled_at': 10.}
        with patch('performance.time.time', return_value=16.):
            self.assertEqual(monitor.snapshot()['sample_age_s'], 6.)


if __name__ == '__main__':
    unittest.main()
