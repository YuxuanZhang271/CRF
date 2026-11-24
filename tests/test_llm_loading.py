"""Speech startup concurrency, cancellation, loading controls and PCM tap without models."""
import asyncio
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import llm  # Select this checkout's vendored SDK, as the app does.
from streamingcommand import StreamingCommand, StreamingConfig
from streamingcommand.config import ASRConfig, CommandConfig
from streamingcommand._runtime.model_loading import configure_loading
from streamingcommand._runtime.startup import start_together
from streamingcommand._runtime.acoustic_language import AcousticLanguageHint
from streamingcommand._runtime.pooled_acoustic_language import PooledAcousticLanguageHint
from streamingcommand._runtime.realtime_asr import realtime_asr_class
from streamingcommand._runtime.asr_backend import NativeASR
from streamingcommand.resources import ModelStore


class LoadingTests(unittest.IsolatedAsyncioTestCase):
    def test_parallel_resource_verification_still_rejects_same_size_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ModelStore(directory, offline=True)
            store.manifest = {}
            for name in ('asr', 'asr-base'):
                data = name.encode('ascii')
                path = store.root / name
                path.mkdir(parents=True)
                (path / 'weights').write_bytes(data)
                store.manifest[name] = dict(repo_id='test/' + name, revision='0' * 40,
                    files={'weights': dict(size=len(data), sha256=hashlib.sha256(data).hexdigest())})
            with patch('huggingface_hub.try_to_load_from_cache', return_value=None):
                resolved = store.ensure_many(['asr', 'asr-base'], parallel=True)
                self.assertEqual(set(resolved), {'asr', 'asr-base'})
                (store.root / 'asr-base' / 'weights').write_bytes(b'X' * len(b'asr-base'))
                with self.assertRaisesRegex(FileNotFoundError, 'missing or corrupt'):
                    store.ensure_many(['asr', 'asr-base'], parallel=True)

    def test_resource_verification_runs_independently_with_bounded_threads(self):
        store = ModelStore()
        entered = threading.Barrier(4, timeout=2)
        def ensure(name):
            entered.wait()
            return Path(name)
        with patch.object(store, 'ensure', side_effect=ensure):
            result = store.ensure_many(['a', 'b', 'c', 'd'], parallel=True)
        self.assertEqual(list(result), ['a', 'b', 'c', 'd'])

    async def test_concurrent_group_joins_cancelled_sibling_before_returning(self):
        entered, stopped = asyncio.Event(), asyncio.Event()
        async def slow():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                stopped.set()
        async def fail():
            await entered.wait()
            raise ValueError('load failed')
        with self.assertRaisesRegex(ValueError, 'load failed'):
            await start_together(slow(), fail())
        self.assertTrue(stopped.is_set())

    async def test_language_peers_start_together_and_publish_only_complete_pool(self):
        pool = PooledAcousticLanguageHint('cpu', int8=True, feature_dither=0.0)
        pool.parallel_start = True
        entered = []
        both = asyncio.Event()
        async def start(worker):
            self.assertIsNone(pool._available)
            entered.append(worker)
            if len(entered) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 1)
        with patch.object(AcousticLanguageHint, 'start', start), \
                patch.object(AcousticLanguageHint, 'close', AsyncMock()):
            await pool.start()
            self.assertEqual(pool._available.qsize(), 2)
            await pool.start()
            self.assertEqual(len(entered), 2)
            await pool.close()
            self.assertIsNone(pool._available)

    async def test_failed_language_peer_joins_other_peer_before_cleanup(self):
        pool = PooledAcousticLanguageHint('cpu', int8=True, feature_dither=0.0)
        pool.parallel_start = True
        entered, stopped = asyncio.Event(), asyncio.Event()
        async def start(worker):
            if worker is pool:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
            else:
                await entered.wait()
                raise ValueError('peer failed')
        async def close():
            self.assertTrue(stopped.is_set())
        with patch.object(AcousticLanguageHint, 'start', start), \
                patch.object(pool, '_close_workers', AsyncMock(side_effect=close)):
            with self.assertRaisesRegex(ValueError, 'peer failed'):
                await pool.start()
        self.assertIsNone(pool._available)

    async def test_cancelling_language_pool_drains_all_starts_before_close(self):
        pool = PooledAcousticLanguageHint('cpu', int8=True, feature_dither=0.0)
        pool.parallel_start = True
        entered, stopped = [], []
        both = asyncio.Event()
        async def start(worker):
            entered.append(worker)
            if len(entered) == 2:
                both.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(worker)
        async def close():
            self.assertCountEqual(stopped, entered)
        with patch.object(AcousticLanguageHint, 'start', start), \
                patch.object(pool, '_close_workers', AsyncMock(side_effect=close)):
            task = asyncio.create_task(pool.start())
            await asyncio.wait_for(both.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNone(pool._available)

    async def test_language_and_asr_engine_overlap_before_readiness(self):
        asr = realtime_asr_class(NativeASR)(device='cpu')
        asr.parallel_start = True
        entered = [asyncio.Event(), asyncio.Event()]
        async def load(index):
            entered[index].set()
            await asyncio.wait_for(entered[1-index].wait(), 1)
            return {'event': 'ready'}
        asr._acoustic_language = SimpleNamespace(start=lambda: load(1))
        with patch.object(NativeASR, 'start', lambda _: load(0)):
            ready = await asr.start()
        self.assertEqual(ready, {'event': 'ready'})
        self.assertTrue(asr.status['ready'])

    async def test_models_start_concurrently_and_report_elapsed_time(self):
        entered = [asyncio.Event(), asyncio.Event()]
        def backend(index):
            async def start():
                entered[index].set()
                await asyncio.wait_for(entered[1-index].wait(), 1)
            return SimpleNamespace(start=start, close=AsyncMock(), status={})
        asr, command = backend(0), backend(1)
        sdk = StreamingCommand(StreamingConfig(parallel_start=True), asr_backend=asr, command_backend=command)
        await sdk.start()
        self.assertTrue(sdk.info()['ready'])
        self.assertGreaterEqual(sdk.info()['asr']['startup_seconds'], 0)
        await sdk.close()
        asr.close.assert_awaited_once()
        command.close.assert_awaited_once()

    async def test_failed_start_cancels_and_joins_sibling_before_close(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def slow():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        async def fail():
            await entered.wait()
            raise ValueError('model failed')
        async def close():
            self.assertTrue(cancelled.is_set())
        asr = SimpleNamespace(start=slow, close=AsyncMock(side_effect=close), status={})
        command = SimpleNamespace(start=fail, close=AsyncMock(side_effect=close), status={})
        sdk = StreamingCommand(StreamingConfig(parallel_start=True), asr_backend=asr, command_backend=command)
        with self.assertRaisesRegex(ValueError, 'model failed'):
            await sdk.start()
        self.assertFalse(sdk.info()['ready'])
        asr.close.assert_awaited_once()
        command.close.assert_awaited_once()

    async def test_microphone_tap_gets_exact_pcm_including_silence_before_feed(self):
        blocks = [b'\0'*640, b'\x12\x34'*320]
        tapped, fed, closed = [], [], []
        async def microphone(device):
            try:
                for pcm in blocks:
                    yield pcm
            finally:
                closed.append(True)
        async def stream(source, **kwargs):
            async for pcm in source:
                self.assertEqual(tapped[-1], pcm)
                fed.append(pcm)
                yield {'type': 'test'}
        sdk = StreamingCommand(asr_backend=SimpleNamespace())
        sdk.stream = stream
        with patch('streamingcommand.audio.microphone', microphone):
            events = [e async for e in sdk.listen(audio_callback=tapped.append)]
        self.assertEqual(tapped, blocks)
        self.assertEqual(fed, blocks)
        self.assertEqual(len(events), 2)
        self.assertEqual(closed, [True])

    async def test_cancelling_start_joins_both_backends_before_releasing(self):
        entered = [asyncio.Event(), asyncio.Event()]
        stopped = [False, False]
        def backend(index):
            async def start():
                entered[index].set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped[index] = True
            async def close():
                self.assertTrue(all(stopped))
            return SimpleNamespace(start=start, close=AsyncMock(side_effect=close), status={})
        asr, command = backend(0), backend(1)
        sdk = StreamingCommand(StreamingConfig(parallel_start=True), asr_backend=asr, command_backend=command)
        task = asyncio.create_task(sdk.start())
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in entered)), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        asr.close.assert_awaited_once()
        command.close.assert_awaited_once()

    def test_worker_count_validated_and_applied_to_pinned_transformers(self):
        for cls in (ASRConfig, CommandConfig):
            for value in (0, -1, True, 33, 2.5):
                with self.assertRaises(ValueError):
                    cls(num_workers=value)
        from transformers import core_model_loading
        original = core_model_loading.GLOBAL_WORKERS
        try:
            with patch.dict(os.environ, STREAMINGCOMMAND_LOADING_WORKERS='8'):
                self.assertEqual(configure_loading(), 8)
                self.assertEqual(core_model_loading.GLOBAL_WORKERS, 8)
        finally:
            core_model_loading.GLOBAL_WORKERS = original

