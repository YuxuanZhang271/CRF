"""Speech startup concurrency, cancellation, loading controls and PCM tap without models."""
import asyncio
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import llm  # Select this checkout's vendored SDK, as the app does.
from streamingcommand import StreamingCommand, StreamingConfig
from streamingcommand.config import ASRConfig, CommandConfig
from streamingcommand._runtime.model_loading import configure_loading


class LoadingTests(unittest.IsolatedAsyncioTestCase):
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

