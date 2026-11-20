"""Length/acoustic gating for the final isolated single-engine ASR experiment."""
import json

from .asr_codeswitch_bias import MARKER
from .asr_codeswitch_boundary import policy as baseline_policy
from .asr_canto_blend import configure as configure_metadata

PROFILES = ('control', 'fusion75')


def policy(profile, **kwargs):
    """Use the mix75 bank on long audio with strong current Cantonese evidence and preserve the fallback
    policy otherwise.
    """
    if profile not in PROFILES:
        raise ValueError(profile)
    if profile == 'control':
        return None
    selected = baseline_policy('boundary_classifier40', **kwargs)
    if selected is None:
        return None
    long = len(kwargs['pcm']) > 32000  # PCM16 mono 16 kHz, >1 second.
    hint = kwargs['acoustic_hint'] or {}
    current = (hint.get('temporal_language_queries') or {}).get('current') or hint
    mixed = long and current.get('native_language_tag') == 'yue' and current.get('language_probability', 0) >= .9
    spec = json.loads(selected['context'][len(MARKER):])
    spec['word_weights'] = {'rotate': 6., 'track': 8.} if long else {}
    selected['context'] = MARKER + json.dumps(spec, ensure_ascii=False)
    if mixed:
        selected['model_variant'] = 'mix75'
    selected['audit'].update(profile=profile, longer_than_one_second=long,
                             experimental_bank='mix75' if mixed else None,
                             word_weights=spec['word_weights'],
                             original_checkpoint_files_modified=False)
    return selected


def configure(asr, profile):
    """Install the selected fusion policy and record the worker's boundary prior guard."""
    if profile not in PROFILES:
        raise ValueError(profile)
    configure_metadata(asr, 'control')
    if profile != 'control':
        asr.final_request_policy = lambda **kwargs: policy(profile, **kwargs)
    asr.status.update(experimental_codeswitch_profile=profile, boundary_prior_guard=True)
    return asr
