"""Uniform fixed-vocabulary/context ablation; no per-reference decisions."""
import json

from .asr_codeswitch import GLOSSARY
from .asr_codeswitch_bias import MARKER, WORDS, policy as eligibility

PHRASES = (
    'Endoscope operation vocabulary: ' + ', '.join(WORDS) + '. '
    'advance scope; push scope in; pull scope out; withdraw scope; '
    'bend tip left; bend tip right; bend tip up; bend tip down; '
    'rotate scope left; rotate scope right; rotate clockwise; rotate anticlockwise; '
    'start tracking; stop tracking; reset scope; return home; clean lens; bird eye view.'
)
PROFILES = ('control', 'prior40', 'prior50', 'prior60', 'phrases30', 'phrases40',
            'phrases60', 'bilingual_phrases40', 'cantonese_phrases40')


def policy(profile, **kwargs):
    """Select the fixed six-variant vocabulary or context recipe while retaining the acoustic gate."""
    if profile not in PROFILES:
        raise ValueError(profile)
    if profile == 'control':
        return None
    selected = eligibility('prior20', **kwargs)
    if selected is None:
        return None
    weight = float(profile[-2:]) / 10 if profile[-2:].isdigit() else 4.
    context = (GLOSSARY + ' ' + PHRASES if profile == 'bilingual_phrases40' else
               PHRASES if 'phrases' in profile else GLOSSARY)
    selected['context'] = MARKER + json.dumps({'context': context, 'words': WORDS, 'weight': weight}, ensure_ascii=False)
    if profile == 'cantonese_phrases40':
        selected['language'] = 'Cantonese'
    selected['audit'].update(profile=profile, weight=weight)
    return selected


def configure(asr, profile):
    """Install the prior-comparison worker and attach the selected profile to its status."""
    if asr.status.get('asr_engine_count') != 1 or profile not in PROFILES:
        raise ValueError('Invalid single-engine candidate')
    if profile == 'control':
        if hasattr(asr, 'final_request_policy'):
            del asr.final_request_policy
    else:
        asr.final_request_policy = lambda **kwargs: policy(profile, **kwargs)
    asr.status['experimental_codeswitch_profile'] = profile
    asr.status['codeswitch_acceptance'] = 'unvalidated_do_not_promote'
    return asr
