"""Bounded CPU language workers and exact-PCM reuse for deterministic inference."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import hashlib

from .acoustic_language import AcousticLanguageHint
from .startup import start_together


class PooledAcousticLanguageHint(AcousticLanguageHint):
    def __init__(self, *args, workers=2, cache_size=64, **kwargs):
        """Create a bounded CPU worker pool and PCM cache for deterministic zero-dither language queries."""
        super().__init__(*args, **kwargs)
        if self.device != 'cpu' or self.feature_dither != 0.0:
            raise ValueError('Exact language reuse requires CPU inference with zero dither')
        if workers < 1 or cache_size < 0:
            raise ValueError('Invalid language pool bounds')
        self.worker_count = workers
        self.parallel_start = False
        self.cache_size = cache_size
        self._peers = []
        self._available = None
        self._pool_lock = asyncio.Lock()
        self._cache = OrderedDict()
        self._inflight = {}
        self.query_stats = dict(requests=0, computed=0, cache_hits=0, inflight_hits=0)

    def clear_cache(self):
        """Clear PCM-derived hint records without resetting ASR history."""
        # Predictions contain only language evidence; ASR history is separate.
        self._cache.clear()

    async def start(self):
        """Start each CPU peer and release the whole pool if any peer fails to initialize."""
        async with self._pool_lock:
            if self._available is not None:
                return
            try:
                for _ in range(self.worker_count - 1):
                    peer = AcousticLanguageHint('cpu', self.model_dir, int8=self.int8,
                        condition_english=self.condition_english,
                        minimum_language_probability=self.minimum_language_probability,
                        feature_dither=self.feature_dither)
                    peer.logs_dir = self.logs_dir
                    self._peers.append(peer)
                if self.parallel_start:
                    await start_together(super().start(), *(peer.start() for peer in self._peers))
                else:
                    await super().start()
                    for peer in self._peers:
                        await peer.start()
                self._available = asyncio.Queue()
                for worker in [self, *self._peers]:
                    self._available.put_nowait(worker)
            except BaseException:
                await self._close_workers()
                raise

    async def _compute(self, key, pcm):
        """Borrow one worker, save a hint in the bounded cache, and return the worker even after failure."""
        worker = await self._available.get()
        try:
            if worker is self:
                result = await super().classify(pcm)
            else:
                result = await worker.classify(pcm)
            self.query_stats['computed'] += 1
            if self.cache_size:
                self._cache[key] = dict(result)
                self._cache.move_to_end(key)
                while len(self._cache) > self.cache_size:
                    self._cache.popitem(last=False)
            return result
        finally:
            self._available.put_nowait(worker)
            self._inflight.pop(key, None)

    async def classify(self, pcm):
        """Reuse identical PCM requests and shield shared inference from an individual caller's
        cancellation.
        """
        if self._available is None:
            raise RuntimeError('Language pool is not ready')
        self.query_stats['requests'] += 1
        key = (len(pcm), hashlib.sha256(pcm).digest())
        if key in self._cache:
            self.query_stats['cache_hits'] += 1
            self._cache.move_to_end(key)
            value = self._cache[key]
            return {**value, 'source_inference_ms': value['inference_ms'],
                    'inference_ms': 0.0, 'exact_pcm_cache_hit': True}
        task = self._inflight.get(key)
        reused = task is not None
        if task is None:
            task = asyncio.create_task(self._compute(key, bytes(pcm)))
            self._inflight[key] = task
        else:
            self.query_stats['inflight_hits'] += 1
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            # Drain protocol replies before returning a CPU worker to the pool.
            await task
            raise
        return {**result, 'exact_pcm_cache_hit': False, 'shared_inflight_query': reused}

    async def _close_workers(self):
        """Stop each owned language worker and reset pool availability and cache state."""
        for peer in self._peers:
            await peer.close()
        self._peers.clear()
        await super().close()
        self._available = None
        self._cache.clear()

    async def close(self):
        """Drain existing queries before shutting down the worker pool."""
        async with self._pool_lock:
            if self._inflight:
                await asyncio.gather(*list(self._inflight.values()), return_exceptions=True)
            await self._close_workers()
