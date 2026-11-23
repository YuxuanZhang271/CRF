"""Async JSON-line adapter for the existing isolated Qwen native worker.

Importing this module never loads a model or touches CUDA. NativeASR is the
actual default backend; FakeASR is explicitly labelled and intended for tests.
Only the worker process group created here is terminated during cleanup.
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import time
from datetime import datetime, timezone
import wave

import numpy as np

if __package__:
    from .device_policy import resolve_device, worker_visibility
    from .asr_instance import ASRInstanceLease
else:
    from device_policy import resolve_device, worker_visibility
    from asr_instance import ASRInstanceLease

from .paths import ASSETS, WORKERS, CACHE, LOGS, model_path, worker_path
import sys
PROJECT = Path(__file__).resolve().parent
WORKSPACE = CACHE
WHISPER = CACHE

def default_context():
    """Read the bundled ASR context text using its declared UTF-8 encoding."""
    return (ASSETS / "context.txt").read_text(encoding="utf-8")


def encode_pcm16(pcm: bytes) -> str:
    """Convert whole little-endian PCM16 samples into base64-encoded float32 worker input."""
    if len(pcm) % 2:
        raise ValueError("PCM16 must contain a whole number of 16-bit samples")
    audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    return base64.b64encode(audio.astype("<f4", copy=False).tobytes()).decode("ascii")


class NativeASR:
    backend = "qwen-native"
    is_fake = False

    def __init__(self, device="auto", *, model_dir=None, python=None, worker=None,
                 context=None, language=None, cantonese_final_language="English", logs_dir=None,
                 startup_timeout=240.0, request_timeout=120.0,
                 gpu_memory_utilization=0.35, max_model_len=4096,
                 kv_cache_memory_bytes=None,
                 alternate_model_dir=None,
                 weight_bank_mode='copy', worker_environment=None,
                 max_new_tokens=96, warmup=True,
                 retry_out_of_domain_language=False, reuse_streaming_language=False,
                 word_bias_weight=0.0, word_bias_words=(), minimum_streaming_hint_ms=1600):
        """Configure interpreter, model, generation options, and ownership state without starting a worker.
        """
        if device != "auto" and device != "cpu" and not re.fullmatch(r"cuda:\d+", device):
            raise ValueError("Native Qwen requires a device such as cuda:1")
        self.device, self.gpu_uuid = resolve_device(device)
        self.model_dir = Path(model_dir or model_path("asr-base")).resolve()
        # The venv executable is a symlink. Resolving it selects the base
        # interpreter and loses pyvenv.cfg / the private Qwen installation.
        self.python = Path(python or sys.executable).absolute()
        self.worker = Path(worker or worker_path("qwen_native_safe_stream_server.py")).resolve()
        self.context = default_context() if context is None else context
        self.language = language
        self.cantonese_final_language = cantonese_final_language
        self.retry_out_of_domain_language = retry_out_of_domain_language
        self.reuse_streaming_language = reuse_streaming_language
        self.minimum_streaming_hint_samples = round(minimum_streaming_hint_ms * 16)
        self.minimum_streaming_hint_ms = minimum_streaming_hint_ms
        if not 0 <= float(word_bias_weight) <= 3:
            raise ValueError('Word bias must be between 0 and 3')
        self.word_bias_weight = float(word_bias_weight)
        self.word_bias_words = tuple(word_bias_words)
        self.logs_dir = Path(logs_dir or LOGS).resolve()
        self.startup_timeout, self.request_timeout = startup_timeout, request_timeout
        self.gpu_memory_utilization = gpu_memory_utilization
        if kv_cache_memory_bytes is not None and int(kv_cache_memory_bytes) <= 0:
            raise ValueError('KV cache memory must be positive')
        self.kv_cache_memory_bytes = kv_cache_memory_bytes
        self.alternate_model_dir = Path(alternate_model_dir).resolve() if alternate_model_dir else None
        if weight_bank_mode not in ('copy','swap'):raise ValueError('Invalid ASR weight bank mode')
        self.weight_bank_mode=weight_bank_mode
        self.worker_environment = dict(worker_environment or {})
        self.max_model_len, self.max_new_tokens = max_model_len, max_new_tokens
        self.warmup = warmup
        self.status = {"backend": self.backend, "is_fake": False, "phase": "idle", "state": "idle",
                       "loading": False, "ready": False, "error": None,
                       "device": device, "language": language,
                       "language_mode": "forced" if language else "auto",
                       "cantonese_final_language": cantonese_final_language,
                       "retry_out_of_domain_language": retry_out_of_domain_language,
                       "reuse_streaming_language": reuse_streaming_language,
                       "minimum_streaming_hint_ms": minimum_streaming_hint_ms,
                       "word_bias_weight": self.word_bias_weight,
                       "word_bias_words": list(self.word_bias_words),
                       "final_language_policy": "auto_then_cantonese_english_rescore" if language is None and cantonese_final_language else "single_pass",
                       "model_dir": str(self.model_dir), "log_path": None}
        if language is None and cantonese_final_language:
            if reuse_streaming_language:
                self.status['final_language_policy'] = 'native_streaming_hint_or_auto_then_english_rescore'
            elif retry_out_of_domain_language:
                self.status['final_language_policy'] = 'auto_then_cantonese_or_out_of_domain_english_rescore'
        self._process = None
        self._stderr = None
        self._request_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closing = False
        self._protocol_broken = False
        self._instance_lease = ASRInstanceLease(self.device)

    def _phase(self, phase, error=None):
        """Update the worker's phase, loading flag, readiness, and diagnostic error together."""
        self.status.update(phase=phase, state=phase, loading=phase in {"loading", "warming"},
                           ready=phase == "ready", error=error)

    async def _read(self, timeout):
        """Read a bounded JSON reply and surface invalid output, worker exit, or a reported failure."""
        assert self._process is not None
        line = await asyncio.wait_for(self._process.stdout.readline(), timeout)
        if not line:
            raise RuntimeError(f"Qwen worker exited ({self._process.returncode}); "
                               f"see {self.status['log_path']}")
        try:
            response = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("Invalid JSON from Qwen worker; see its log") from error
        if not response.get("ok"):
            raise RuntimeError(response.get("error", "Qwen worker reported an error"))
        return response

    async def start(self):
        """Acquire the ASR lease, start the isolated worker, and warm it before advertising readiness."""
        async with self._start_lock:
            if self.status["ready"] and self._process is not None and self._process.returncode is None:
                return self.status.get("worker_ready", dict(self.status))
            if self._process is not None:
                await self.close()
            self._closing = False
            self._protocol_broken = False
            self._phase("loading")
            try:
                if not self.python.is_file() or not self.worker.is_file() or not self.model_dir.is_dir():
                    raise RuntimeError("Existing Qwen runtime, worker or model directory is missing")
                self._instance_lease.acquire()
                self.logs_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
                log_path = self.logs_dir / f"qwen-{stamp}-{os.getpid()}.log"
                self.status["log_path"] = str(log_path)
                self._stderr = log_path.open("wb")
                environment = {**os.environ, **self.worker_environment}
                environment.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
                visible = worker_visibility(self.device, os.environ)
                if self.status.get("backend") == "vllm" and not visible.isdecimal():
                    raise ValueError("Legacy vLLM requires a numeric CUDA_VISIBLE_DEVICES selector")
                environment.update(CUDA_VISIBLE_DEVICES=visible,
                                   PHONO_ASR_GPU_UUID=self.gpu_uuid,
                                   STREAMINGCOMMAND_DEVICE=self.device)
                environment.pop("PYTHONPATH", None)
                memory_arguments = ([] if self.kv_cache_memory_bytes is None else
                                    ['--kv-cache-memory-bytes', str(self.kv_cache_memory_bytes)])
                if self.alternate_model_dir is not None:
                    memory_arguments += ['--alternate-model-dir', str(self.alternate_model_dir)]
                    memory_arguments += ['--weight-bank-mode', self.weight_bank_mode]
                self._process = await asyncio.create_subprocess_exec(
                    str(self.python), str(self.worker), "--model-dir", str(self.model_dir),
                    "--gpu-memory-utilization", str(self.gpu_memory_utilization),
                    "--max-model-len", str(self.max_model_len),
                    "--max-new-tokens", str(self.max_new_tokens),
                    *memory_arguments,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=self._stderr, env=environment, start_new_session=True)
                ready = await self._read(self.startup_timeout)
                if ready.get("event") != "ready":
                    raise RuntimeError("Qwen worker did not send its ready event")
                self.status["worker_ready"] = ready
                self.status["device_uuid"] = ready.get("device_uuid")
                if self.warmup:
                    self._phase("warming")
                    audio_path = ASSETS / "warmup.wav"
                    with wave.open(str(audio_path), "rb") as audio:
                        if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (16000, 1, 2):
                            raise RuntimeError("Warmup recording must be mono 16k PCM16")
                        pcm = audio.readframes(16000)
                    started = time.monotonic()
                    await self.request("transcribe", segment_id="warmup", context=self.context,
                                       language=self.language, audio_float32_b64=encode_pcm16(pcm))
                    self.status["warmup_ms"] = (time.monotonic() - started) * 1000
                self._phase("ready")
                return ready
            except BaseException as error:
                self._phase("error", str(error) or type(error).__name__)
                self._protocol_broken = True
                await self._terminate_owned_process()
                raise

    async def _exchange(self, payload):
        """Send one request and verify that the reply matches its operation and segment."""
        assert self._process is not None
        self._process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
        await self._process.stdin.drain()
        result = await self._read(self.request_timeout)
        if result.get("op") != payload["op"]:
            raise RuntimeError("Out-of-order response from Qwen worker")
        if "segment_id" in payload and result.get("segment_id") != payload["segment_id"]:
            raise RuntimeError("Qwen response has the wrong segment ID")
        return result

    async def request(self, op, **kwargs):
        """Serialize worker requests and finish consuming an in-flight reply when its caller is cancelled."""
        if op == "transcribe" and kwargs.get("language") == "English" and self.word_bias_weight:
            kwargs['word_bias'] = {'weight': self.word_bias_weight, 'words': list(self.word_bias_words)}
        async with self._request_lock:
            if self._process is None or self._process.returncode is not None or self._protocol_broken:
                raise RuntimeError("Qwen worker is unavailable; start it first")
            if self._closing and op != "close":
                raise RuntimeError("Qwen worker is closing")
            # A cancelled caller must consume its reply before another request
            # uses stdout; otherwise the protocol could associate the wrong IDs.
            exchange = asyncio.create_task(self._exchange({"op": op, **kwargs}))
            try:
                return await asyncio.shield(exchange)
            except asyncio.CancelledError:
                try:
                    await exchange
                except Exception as error:
                    self._protocol_broken = True
                    self._phase("error", str(error))
                raise
            except Exception as error:
                self._protocol_broken = True
                self._phase("error", str(error))
                raise

    async def _terminate_owned_process(self):
        """Stop the worker or its owned process group, close the log, and release the ASR lease."""
        process = self._process
        if process is not None:
            # start_new_session makes this group exclusive to our worker and
            # its vLLM children. Never enumerate or stop another GPU task.
            try:
                process.terminate() if os.name == "nt" else os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                try:
                    process.kill() if os.name == "nt" else os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
            self._process = None
        if self._stderr is not None:
            self._stderr.close()
            self._stderr = None
        self._instance_lease.release()

    async def close(self):
        """Attempt graceful protocol shutdown before enforcing cleanup of the owned worker."""
        async with self._close_lock:
            self._closing = True
            try:
                if self._process is not None and not self._protocol_broken and self._process.returncode is None:
                    try:
                        await self.request("close")
                        await asyncio.wait_for(self._process.wait(), 10)
                    except Exception:
                        pass
            finally:
                await self._terminate_owned_process()
                self._phase("closed")


class FakeASR:
    """Explicit simulated protocol backend; its text is not speech recognition."""
    backend, is_fake = "fake", True

    def __init__(self, *, latency_ms=20, text="[FAKE] 模拟转写，未运行 Qwen"):
        """Set the transcript and delay used by the lightweight ASR protocol double."""
        self.context, self.language = "", "English"
        self.latency_ms, self.text = latency_ms, text
        self.status = {"backend": "fake", "is_fake": True, "phase": "idle", "state": "idle",
                       "ready": False, "loading": False, "error": None}
        self._samples = 0

    async def start(self):
        """Mark the ASR protocol double ready without loading weights."""
        self.status.update(phase="ready", state="ready", ready=True)
        return {"event": "ready", "ok": True, "backend": "fake", "is_fake": True}

    async def request(self, op, **kwargs):
        """Simulate the streaming protocol, including received sample counts and the configured text."""
        await asyncio.sleep(self.latency_ms / 1000)
        if op == "start":
            self._samples = 0
        elif op in {"update", "transcribe"}:
            samples = len(base64.b64decode(kwargs["audio_float32_b64"])) // 4
            self._samples = samples if op == "transcribe" else self._samples + samples
        return {"ok": True, "op": op, "segment_id": kwargs.get("segment_id"),
                "text": self.text if op in {"update", "transcribe"} else "",
                "raw_decoded": self.text if op in {"update", "transcribe"} else "",
                "raw_decoded_chunks": [self.text] if op == "transcribe" else [],
                "detected_language": "English", "audio_processed_samples": self._samples,
                "generations": [], "decode_ms": self.latency_ms}

    async def close(self):
        """Mark the ASR protocol double closed without managing any external process."""
        self.status.update(phase="closed", state="closed", ready=False)
