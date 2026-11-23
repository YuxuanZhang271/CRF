"""Experiment: generated suffix after a real preceding-audio/text prefix.

The prefix is previous ASR output, accompanied by its real source audio. It is
not a reference answer or an output replacement. Normal requests are unchanged.
"""
import json
from types import SimpleNamespace

import qwen_native_gpu1_benchmark_worker
import qwen_native_graph_stream_server as server
from qwen_asr import Qwen3ASRModel

MARKER = '__PHONO_ACOUSTIC_PREFIX__'
original = Qwen3ASRModel.transcribe


def transcribe(self, audio, context='', language=None, **kwargs):
    """Decode a real waveform prefix with the already observed ASR text and return the current suffix."""
    if not context.startswith(MARKER):
        return original(self, audio=audio, context=context, language=language, **kwargs)
    request = json.loads(context[len(MARKER):])
    assert language == 'English' and request['previous_asr_text']
    prompt = self._build_text_prompt(context=request['context'], force_language=language)
    prompt += request['previous_asr_text'] + ' '
    outputs = self.model.generate([{'prompt':prompt,'multi_modal_data':{'audio':[audio[0]]}}],
                                 sampling_params=self.sampling_params,use_tqdm=False)
    # Exactly the newly generated suffix, not the previous utterance supplied
    # as decoder input. Raw suffix tokens remain available in generations.
    text = outputs[0].outputs[0].text
    return [SimpleNamespace(text=text,language=language)]


Qwen3ASRModel.transcribe = transcribe
if __name__ == '__main__':
    raise SystemExit(server.main())
