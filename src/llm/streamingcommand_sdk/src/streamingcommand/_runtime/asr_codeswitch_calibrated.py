"""Fixed vocabulary calibration, no utterance-specific rules or text repair."""
import json
from .asr_codeswitch import GLOSSARY
from .asr_codeswitch_bias import MARKER, policy as eligibility
from .asr_codeswitch_banks import CLASSIFIERS, configure as configure_metadata
from .asr_codeswitch_prior6 import PHRASES
from .asr_codeswitch_verbs import VERBS

PROFILES = ('control', 'calibrated_classifier', 'calibrated_phrases_classifier',
            'calibrated_classifier_phrases', 'calibrated_compact')


def policy(profile, **kwargs):
    """Substitute the calibrated fixed vocabulary for an acoustically eligible mixed-language request."""
    if profile not in PROFILES:
        raise ValueError(profile)
    if profile == 'control':
        return None
    selected = eligibility('prior20', **kwargs)
    if selected is None:
        return None
    short = len(kwargs['pcm']) <= 32000
    context = (GLOSSARY if short or profile == 'calibrated_compact' else
               PHRASES + CLASSIFIERS if profile == 'calibrated_phrases_classifier' else
               CLASSIFIERS + PHRASES if profile == 'calibrated_classifier_phrases' else
               GLOSSARY + CLASSIFIERS)
    selected['context'] = MARKER + json.dumps({'context': context, 'words': VERBS, 'weight': 4.}, ensure_ascii=False)
    selected['audit'].update(profile=profile, weight=4., calibrated_weights={'rotate': 6., 'track': 6.},
                             boundary_prior_guard=True, short_context=short)
    return selected


def configure(asr, profile):
    """Apply the calibrated prior policy on the boundary-protected worker path."""
    configure_metadata(asr, 'control')
    if profile not in PROFILES:
        raise ValueError(profile)
    if profile != 'control':
        asr.final_request_policy = lambda **kwargs: policy(profile, **kwargs)
    asr.status['experimental_codeswitch_profile'] = profile
    asr.status['boundary_prior_guard'] = True
    return asr
