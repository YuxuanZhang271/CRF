"""Boundary-protected compact/classifier/phrase ASR context comparisons."""
import json
from .asr_codeswitch import GLOSSARY
from .asr_codeswitch_bias import MARKER, policy as eligibility
from .asr_codeswitch_banks import CLASSIFIERS, configure as configure_metadata
from .asr_codeswitch_prior6 import PHRASES
from .asr_codeswitch_verbs import VERBS

PROFILES = ('control', 'boundary_classifier40', 'boundary_classifier60', 'boundary_phrases60')


def policy(profile, **kwargs):
    """Turn the inherited mixed-language recipe into a boundary-protected context and prior specification."""
    if profile not in PROFILES:
        raise ValueError(profile)
    if profile == 'control':
        return None
    selected = eligibility('prior20', **kwargs)
    if selected is None:
        return None
    short = len(kwargs['pcm']) <= 32000
    context = GLOSSARY if short else (PHRASES if profile == 'boundary_phrases60' else GLOSSARY + CLASSIFIERS)
    weight = 4. if short else float(profile[-2:]) / 10
    selected['context'] = MARKER + json.dumps({'context': context, 'words': VERBS, 'weight': weight}, ensure_ascii=False)
    selected['audit'].update(profile=profile, weight=weight, boundary_prior_guard=True,
                             short_context=short, reference_or_hypothesis_used=False)
    return selected


def configure(asr, profile):
    """Install the boundary guard and tag the chosen diagnostic variant in worker status."""
    configure_metadata(asr, 'control')
    if profile not in PROFILES:
        raise ValueError(profile)
    if profile != 'control':
        asr.final_request_policy = lambda **kwargs: policy(profile, **kwargs)
    asr.status['experimental_codeswitch_profile'] = profile
    asr.status['boundary_prior_guard'] = True
    return asr
