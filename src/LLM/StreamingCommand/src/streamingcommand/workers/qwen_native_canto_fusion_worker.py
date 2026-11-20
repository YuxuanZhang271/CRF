"""One engine, unchanged short-utterance prior, long acoustic-bank experiment."""
import hashlib
import json
import sys

import qwen_native_codeswitch_boundary_worker
import qwen_native_codeswitch_bias_worker as original
import asr_shared_weight_bank as banks
from asr_blended_weight_bank import BlendedWeightBankExtension, BlendedEngineController
from asr_fusion_priors import make_bias
from qwen_asr import Qwen3ASRModel
import qwen_native_prefix_final_stream_server as server

banks.WeightBankExtension = BlendedWeightBankExtension
banks.SharedEngineController = BlendedEngineController
whole_word_bias = original.whole_word_bias  # Prior6 wrapper, not the original <=3 helper.
cache = {}


def transcribe(self, audio, context='', language=None, **kwargs):
    """Decode one waveform with temporary mixed-language vocabulary priors and restore sampling options."""
    if not context.startswith(original.MARKER):
        return original.original_transcribe(self, audio=audio, context=context, language=language, **kwargs)
    spec = json.loads(context[len(original.MARKER):])
    if language not in (None, 'English', 'Cantonese', 'Chinese'):
        raise ValueError('Unsupported language for the fixed ASR prior')
    # Include every override in the cache key; short and long requests must
    # never share a cached prior with different track/rotate strengths.
    key = (id(self.processor.tokenizer), json.dumps(spec, sort_keys=True, ensure_ascii=False))
    if key not in cache:
        cache[key] = make_bias(self.processor.tokenizer, spec, whole_word_bias)
    sampling = self.sampling_params
    try:
        self.sampling_params = sampling.clone()
        self.sampling_params.logit_bias = cache[key]
        print(json.dumps({'fixed_domain_token_prior': True, 'weight': spec['weight'],
                          'word_weights': spec['word_weights'], 'words': spec['words'],
                          'context_sha256': hashlib.sha256(spec['context'].encode()).hexdigest(),
                          'requested_language': language, 'transcript_rewrite': False}, ensure_ascii=False),
              file=sys.stderr, flush=True)
        return original.original_transcribe(self, audio=audio, context=spec['context'], language=language, **kwargs)
    finally:
        self.sampling_params = sampling


Qwen3ASRModel.transcribe = transcribe

if __name__ == '__main__':
    raise SystemExit(server.main())
