"""Optional acoustic language tags; the helper's words never select ASR text.

The cached SenseVoice CTC model supplies only a full-input language tag. Qwen
still produces the sole final transcript. Loading and inference are off the
audio receive loop, on the same explicitly selected physical ASR GPU.
"""
from __future__ import annotations
from .paths import model_path, worker_path

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import datetime, timezone
import gc
import json
import math
import os
from pathlib import Path
import re
import sys
import time

import numpy as np

from .asr_backend import WHISPER


class AcousticLanguageHint:
    def __init__(self, device, model_dir=None, *, condition_english=False, int8=False,
                 minimum_language_probability=0.95,feature_dither=None):
        """Configure a SenseVoice language-only helper and initialize its process, cache, and locking
        state.
        """
        self.device = device
        self.model_dir = Path(model_dir or model_path("language"))
        self._model = None
        self._torch = None
        self._lock = asyncio.Lock()
        self.condition_english = condition_english
        self.int8 = int8
        self.minimum_language_probability = minimum_language_probability
        if feature_dither is not None and feature_dither<0:
            raise ValueError('Feature dither must be nonnegative')
        self.feature_dither=feature_dither
        self._effective_feature_dither=None
        if int8 and device != 'cpu':
            raise ValueError('Dynamic int8 language helper requires CPU')
        self._executor = None
        self._cpu_process = None
        self._cpu_ready = False
        self._cpu_stderr = None
        self.logs_dir = __import__("streamingcommand._runtime.paths",fromlist=["LOGS"]).LOGS
        self.log_path = None

    @property
    def is_loaded(self):
        """Report readiness from the CPU worker handshake or the in-process model handle."""
        return self._model is not None or (self._cpu_ready and self._cpu_process is not None
                                          and self._cpu_process.returncode is None)

    async def _cpu_read(self, timeout):
        """Read one timeout-bounded JSON line from the CPU helper and surface protocol errors."""
        line = await asyncio.wait_for(self._cpu_process.stdout.readline(), timeout)
        if not line:
            raise RuntimeError(f'CPU language worker exited; see {self.log_path}')
        result = json.loads(line)
        if not result.get('ok'):
            raise RuntimeError(f"CPU language worker: {result.get('error')}; see {self.log_path}")
        return result

    async def _start_cpu(self):
        """Launch an isolated CPU language worker and wait for its ready message."""
        if self.is_loaded:
            return
        await self._close_cpu()
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.logs_dir / ('acoustic-lid-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ') + '.log')
        self._cpu_stderr = self.log_path.open('wb')
        arguments = [sys.executable, str(Path(__file__).with_name('language_query_worker.py')),
                     '--model-dir', str(self.model_dir)]
        if self.int8:arguments.append('--int8')
        if self.condition_english:arguments.append('--condition-english')
        arguments.extend(['--minimum-language-probability',str(self.minimum_language_probability)])
        if self.feature_dither is not None:
            arguments.extend(['--feature-dither',str(self.feature_dither)])
        environment = dict(os.environ, CUDA_VISIBLE_DEVICES='', OMP_NUM_THREADS='2',
                           MKL_NUM_THREADS='2', OPENBLAS_NUM_THREADS='2')
        self._cpu_process = await asyncio.create_subprocess_exec(*arguments,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=self._cpu_stderr, env=environment, start_new_session=True)
        ready = await self._cpu_read(120)
        if ready.get('event') != 'ready':
            raise RuntimeError('CPU language worker did not become ready')
        self._cpu_ready = True

    async def _close_cpu(self):
        """Close the owned CPU worker's stdin and escalate only if it does not exit in time."""
        process, self._cpu_process = self._cpu_process, None
        self._cpu_ready = False
        if process is not None and process.returncode is None:
            if process.stdin:process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                with suppress(ProcessLookupError):process.terminate()
                try:await asyncio.wait_for(process.wait(), 3)
                except asyncio.TimeoutError:
                    with suppress(ProcessLookupError):process.kill()
                    await process.wait()
        if self._cpu_stderr is not None:
            self._cpu_stderr.close();self._cpu_stderr = None

    async def _cpu_predict(self, pcm):
        """Encode PCM16 for a single language query and return the worker's hint."""
        self._cpu_process.stdin.write((json.dumps({'op':'classify',
            'pcm16_b64':base64.b64encode(pcm).decode('ascii')})+'\n').encode())
        await self._cpu_process.stdin.drain()
        return (await self._cpu_read(15))['hint']

    def _load(self):
        """Load, validate, and warm the SenseVoice helper with no auxiliary transcription models."""
        from funasr import AutoModel
        import torch
        self._torch = torch
        self._model = AutoModel(
            model=str(self.model_dir), device=self.device, trust_remote_code=False,
            disable_update=True, disable_pbar=True, disable_log=True,
            log_level='ERROR', ncpu=2, batch_size=1, fp16=False, bf16=False,
            **self._frontend_options(),
        )
        self._configure_frontend()
        if str(next(self._model.model.parameters()).device) != self.device:
            raise RuntimeError('Language helper loaded on the wrong physical GPU')
        if self._model.vad_model or self._model.punc_model or self._model.spk_model:
            raise RuntimeError('Unexpected language helper auxiliary model')
        if self.int8:
            self._model.model = torch.ao.quantization.quantize_dynamic(
                self._model.model, {torch.nn.Linear}, dtype=torch.qint8, inplace=False)
        # SenseVoice places its utterance-language query at encoder position 0.
        # Project just that position: no word-frame CTC probabilities, greedy
        # transcript or beam search are needed for this helper.
        original_projection = self._model.model.ctc.log_softmax

        def language_projection(encoded):
            """Project only the first encoder position and retain its peak language probability."""
            probabilities = original_projection(encoded[:, :1, :])
            self._language_probability = math.exp(float(probabilities[0, 0].max().item()))
            return probabilities

        self._model.model.ctc.log_softmax = language_projection
        self._predict(bytes(32000))  # Warm outside the measured audio session.

    def _frontend_options(self):
        """Supply feature dither at FunASR construction so it survives frontend resets."""
        # Configure at construction: FunASR snapshots its immutable frontend
        # and restores that snapshot before every generate() call.
        return {} if self.feature_dither is None else {
            'frontend_conf':{'dither':float(self.feature_dither)}}

    def _configure_frontend(self):
        """Verify the effective dither setting and retain it for language audit records."""
        frontend=self._model.kwargs['frontend']
        if self.feature_dither is not None:
            if getattr(frontend,'dither',None)!=float(self.feature_dither):
                raise RuntimeError('Language frontend did not apply the requested inference dither')
        self._effective_feature_dither=getattr(frontend,'dither',None)

    async def start(self):
        """Start the CPU worker or load the in-process helper on its dedicated executor."""
        async with self._lock:
            if self.device == 'cpu':
                await self._start_cpu()
                return
            if self._model is None:
                import torch
                # Initialize OpenMP in the owner thread. Keep CPU load and
                # inference on one dedicated thread rather than moving a live
                # OpenMP model between default-executor threads.
                torch.set_num_threads(2)
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='acoustic-lid')
                loading = asyncio.get_running_loop().run_in_executor(self._executor, self._load)
                try:
                    await asyncio.shield(loading)
                except asyncio.CancelledError:
                    # A Python thread cannot cancel an in-flight CUDA load.
                    # Join it before the owning backend releases the helper.
                    try:
                        await loading
                    finally:
                        self._model = None
                        gc.collect()
                        if self._torch is not None and self.device.startswith('cuda:'):
                            with self._torch.cuda.device(self.device):
                                self._torch.cuda.empty_cache()
                    raise

    def _predict(self, pcm):
        """Compute only acoustic language evidence from PCM and apply the configured confidence threshold."""
        audio = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.0
        started = time.monotonic()
        with self._torch.inference_mode():
            outputs = self._model.generate(
                input=audio, cache={}, language='auto', use_itn=False,
                batch_size=1, ban_emo_unk=False, output_timestamp=False,
            )
            if self.device.startswith('cuda:'):
                self._torch.cuda.synchronize(self.device)
        tags = [re.findall(r'<\|([^|]+)\|>', str(o.get('text', ''))) for o in outputs]
        language = tags[0][0] if tags and tags[0] else None
        return {'source': 'SenseVoiceSmall_acoustic_language_only',
                'native_language_tag': language,
                'language_probability': self._language_probability,
                'requested_language': (('Chinese' if language == 'zh' else
                                       'English' if language == 'en' and self.condition_english else None)
                                       if self._language_probability >= self.minimum_language_probability else None),
                'minimum_language_probability':self.minimum_language_probability,
                'feature_dither':self._effective_feature_dither,
                'input_samples': len(pcm) // 2,
                'inference_ms': (time.monotonic() - started) * 1000,
                'helper_words_used': False, 'device': self.device}

    async def classify(self, pcm):
        """Serialize language requests and finish draining the inference before propagating cancellation."""
        async with self._lock:
            if not self.is_loaded:
                raise RuntimeError('Language helper is not ready')
            inference = (asyncio.create_task(self._cpu_predict(pcm)) if self.device == 'cpu'
                         else asyncio.get_running_loop().run_in_executor(self._executor, self._predict, pcm))
            try:
                return await asyncio.shield(inference)
            except asyncio.CancelledError:
                await inference
                raise

    async def close(self):
        """Release the helper process or model and shut down its owned executor."""
        async with self._lock:
            await self._close_cpu()
            self._model = None
            gc.collect()
            if self._torch is not None and self.device.startswith('cuda:'):
                with self._torch.cuda.device(self.device):
                    self._torch.cuda.empty_cache()
            if self._executor is not None:
                self._executor.shutdown(wait=True)
                self._executor = None
