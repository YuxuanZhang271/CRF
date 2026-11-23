"""Single shared R17 engine with an optional fixed mixed-language token prior.

The marker carries only a domain vocabulary and a scalar weight. It is removed
before building the SDK system prompt. This changes sampling logits before the
one generation; it never changes the generated transcript or invokes a scorer.
"""
import hashlib
import json
import sys

import qwen_native_complete_update_worker
import qwen_native_acoustic_prefix_worker
import qwen_native_prefix_final_stream_server as server
from qwen_asr import Qwen3ASRModel
from asr_word_bias import whole_word_bias

MARKER = '__PHONO_DOMAIN_TOKEN_PRIOR__'
original_transcribe = Qwen3ASRModel.transcribe
cache = {}


def transcribe(self, audio, context='', language=None, **kwargs):
    """Apply a validated fixed domain prior for one transcribe call and restore the original sampler."""
    if not context.startswith(MARKER):
        return original_transcribe(self, audio=audio, context=context, language=language, **kwargs)
    spec = json.loads(context[len(MARKER):])
    if set(spec) != {'context', 'words', 'weight'}:
        raise ValueError('Invalid fixed domain-token prior')
    if language not in (None, 'English', 'Cantonese', 'Chinese'):
        raise ValueError('Domain-token prior supports the application languages only')
    key = (id(self.processor.tokenizer), tuple(spec['words']), float(spec['weight']))
    if key not in cache:
        cache[key] = whole_word_bias(self.processor.tokenizer, spec['words'], spec['weight'])
    original_sampling = self.sampling_params
    try:
        sampling = original_sampling.clone()
        sampling.logit_bias = cache[key]
        self.sampling_params = sampling
        print(json.dumps({'fixed_domain_token_prior': True, 'weight': spec['weight'],
                          'words': spec['words'], 'tokens': len(cache[key]),
                          'context_sha256': hashlib.sha256(spec['context'].encode()).hexdigest(),
                          'requested_language': language, 'transcript_rewrite': False}, ensure_ascii=False),
              file=sys.stderr, flush=True)
        return original_transcribe(self, audio=audio, context=spec['context'], language=language, **kwargs)
    finally:
        self.sampling_params = original_sampling


Qwen3ASRModel.transcribe = transcribe

if __name__ == '__main__':
    raise SystemExit(server.main())
