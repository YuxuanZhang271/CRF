"""Compare uniform action-word priors without directional-token imbalance."""
import json

from .asr_codeswitch import GLOSSARY
from .asr_codeswitch_bias import MARKER, WORDS, policy as eligibility
from .asr_codeswitch_prior6 import PHRASES
from .asr_codeswitch_banks import CLASSIFIERS, configure as configure_base

VERBS = tuple(sorted(set('advance push pull withdraw move bend rotate roll spin turn track follow stop reset clean wash look'.split())))
BALANCED = tuple(sorted(set(WORDS) | {'in', 'out', 'inward', 'outward', 'withdraw'}))
PROFILES = ('control', 'verbs_glossary40', 'verbs_glossary60', 'verbs_legacy40',
            'verbs_legacy60', 'verbs_phrases40', 'verbs_phrases60',
            'verbs_classifiers40', 'balanced_phrases40')


def policy(profile, **kwargs):
    """Apply balanced action-word priors without separately biasing direction tokens."""
    if profile not in PROFILES:
        raise ValueError(profile)
    if profile == 'control':
        return None
    selected = eligibility('prior20', **kwargs)
    if selected is None:
        return None
    context = (kwargs['context'] if 'legacy' in profile else
               PHRASES if 'phrases' in profile else
               GLOSSARY + CLASSIFIERS if 'classifiers' in profile else GLOSSARY)
    words = BALANCED if profile.startswith('balanced') else VERBS
    weight = float(profile[-2:]) / 10
    selected['context'] = MARKER + json.dumps({'context': context, 'words': words, 'weight': weight}, ensure_ascii=False)
    selected['audit'].update(profile=profile, weight=weight,
                             prior_vocabulary='balanced' if profile.startswith('balanced') else 'verbs_only',
                             directional_tokens_biased=profile.startswith('balanced'))
    return selected


def configure(asr, profile):
    """Install the uniform verb-prior policy on the shared ASR worker."""
    # Install truthful weight-bank metadata with the existing control adapter.
    configure_base(asr, 'control')
    if profile not in PROFILES:
        raise ValueError(profile)
    if profile != 'control':
        asr.final_request_policy = lambda **kwargs: policy(profile, **kwargs)
    asr.status['experimental_codeswitch_profile'] = profile
    return asr
