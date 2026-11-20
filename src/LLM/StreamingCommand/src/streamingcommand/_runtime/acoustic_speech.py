"""CPU speech evidence for language conditioning; every input still reaches ASR."""
from __future__ import annotations
from .paths import model_path, worker_path

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import time

import numpy as np

MODEL_DIR = model_path("speech")


class AcousticSpeechEvidence:
    def __init__(self, model_dir=None, threshold=.5):
        """Record the Silero model directory and threshold without opening an ONNX session."""
        self.model_dir = Path(model_dir or model_path("speech"))
        self.threshold = threshold
        self._model = None
        self._executor = None
        self._lock = asyncio.Lock()

    @property
    def is_loaded(self):
        """Report whether a ready ONNX speech-evidence session is resident."""
        return self._model is not None

    def _load(self):
        """Verify the model against its provenance and warm a single-threaded CPU ONNX session."""
        import json
        import onnxruntime as ort
        model_path = self.model_dir / 'silero_vad.onnx'
        provenance = json.loads((self.model_dir / 'PROVENANCE.json').read_text())
        expected = next(r['sha256'] for r in provenance['files'] if Path(r['file']).name == model_path.name)
        if hashlib.sha256(model_path.read_bytes()).hexdigest() != expected:
            raise RuntimeError('Speech-evidence model differs from its recorded provenance')
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = opts.inter_op_num_threads = 1
        self._model = ort.InferenceSession(str(model_path), sess_options=opts,
                                           providers=['CPUExecutionProvider'])
        self._predict(bytes(32000))

    async def start(self):
        """Load the CPU session on a dedicated executor and release it if initialization fails."""
        async with self._lock:
            if self.is_loaded:
                return
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='acoustic-speech')
            loading = asyncio.get_running_loop().run_in_executor(self._executor, self._load)
            try:
                await asyncio.shield(loading)
            except BaseException:
                try:
                    await loading
                finally:
                    self._model = None
                    self._executor.shutdown(wait=True)
                    self._executor = None
                raise

    def _predict(self, pcm):
        """Run independent 512-sample VAD windows with reset recurrent state and report the peak speech
        score.
        """
        started = time.monotonic()
        audio = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.
        state = np.zeros((2, 1, 128), np.float32)
        context = np.zeros((1, 64), np.float32)
        maximum = 0.
        speech_frames = 0
        frame_count = 0
        for start in range(0, len(audio), 512):
            chunk = audio[start:start+512]
            if len(chunk) < 512:
                chunk = np.pad(chunk, (0, 512-len(chunk)))
            waveform = np.concatenate([context, chunk[None]], axis=1)
            prediction, state = self._model.run(None, {'input': waveform, 'state': state,
                                                      'sr': np.array(16000, dtype=np.int64)})
            context = waveform[:, -64:]
            probability = float(prediction[0, 0])
            maximum = max(maximum, probability)
            speech_frames += probability >= self.threshold
            frame_count += 1
        return {'source': 'SileroVAD_ONNX_acoustic_speech_only', 'device': 'cpu',
                'maximum_speech_probability': maximum, 'threshold': self.threshold,
                'speech_confirmed': maximum >= self.threshold, 'speech_frames': speech_frames,
                'frames': frame_count, 'input_samples': len(pcm)//2,
                'pcm_sha256': hashlib.sha256(pcm).hexdigest(),
                'inference_ms': (time.monotonic()-started)*1000,
                'audio_rejected': False, 'waveform_changed': False,
                'language_or_transcript_used': False, 'extra_audio_wait_ms': 0}

    async def classify(self, pcm):
        """Serialize speech-evidence inference and drain work before propagating cancellation."""
        async with self._lock:
            if not self.is_loaded:
                raise RuntimeError('Acoustic speech helper is not ready')
            task = asyncio.get_running_loop().run_in_executor(self._executor, self._predict, bytes(pcm))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    async def close(self):
        """Release the ONNX session and shut down the dedicated inference executor."""
        async with self._lock:
            self._model = None
            if self._executor is not None:
                self._executor.shutdown(wait=True)
                self._executor = None
