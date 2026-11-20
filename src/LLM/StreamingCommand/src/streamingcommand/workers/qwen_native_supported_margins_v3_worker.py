"""Native V15 with supported final margins and one frontend-hop tolerance."""
import qwen_native_gpu1_benchmark_worker
import qwen_native_graph_stream_server as server
import hashlib
import json
import sys

import numpy as np
from qwen_asr import Qwen3ASRModel
from asr_supported_margins_v3 import MAX_EXTENSION_SAMPLES, supported_margin_pcm

original_transcribe = Qwen3ASRModel.transcribe


def transcribe(self, audio, **kwargs):
    """Apply supported silence-margin normalization before the native one-pass transcribe call."""
    if not (isinstance(audio, tuple) and audio[1] == 16000 and isinstance(audio[0], np.ndarray)):
        raise ValueError('The margin policy requires mono16k tuple input')
    pcm = np.rint(audio[0] * 32768).clip(-32768, 32767).astype(np.int16)
    normalized, transform = supported_margin_pcm(pcm)
    assert len(normalized) <= len(pcm) + MAX_EXTENSION_SAMPLES
    print(json.dumps({'final_audio_transform': transform,
        'source_pcm_sha256': hashlib.sha256(pcm.tobytes()).hexdigest(),
        'model_pcm_sha256': hashlib.sha256(normalized.tobytes()).hexdigest()},
        ensure_ascii=False), file=sys.stderr, flush=True)
    return original_transcribe(self, audio=(normalized.astype(np.float32) / 32768, 16000), **kwargs)


Qwen3ASRModel.transcribe = transcribe

if __name__ == '__main__':
    raise SystemExit(server.main())
