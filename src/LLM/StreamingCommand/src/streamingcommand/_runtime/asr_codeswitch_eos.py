"""Single-generation EOS-protected domain-prior/context comparisons."""
import json

from .asr_codeswitch import GLOSSARY
from .asr_codeswitch_bias import MARKER, policy as eligibility
from .asr_codeswitch_prior6 import PHRASES
from .asr_codeswitch_banks import CLASSIFIERS, configure as configure_base
from .asr_codeswitch_verbs import VERBS

PROFILES = ('control', 'eos_glossary40', 'eos_classifiers40', 'eos_phrases60',
            'eos_merged40', 'eos_merged60', 'eos_short_merged40', 'eos_short_merged60')


def policy(profile, **kwargs):
    """Serialize a fixed bilingual context and bounded bias for the EOS-protected worker."""
    if profile not in PROFILES:
        raise ValueError(profile)
    if profile == 'control':
        return None
    selected = eligibility('prior20', **kwargs)
    if selected is None:
        return None
    short = profile.startswith('eos_short_') and len(kwargs['pcm']) <= 32000
    context = (GLOSSARY if short or profile == 'eos_glossary40' else
               GLOSSARY + CLASSIFIERS if profile == 'eos_classifiers40' else
               PHRASES if profile == 'eos_phrases60' else
               GLOSSARY + CLASSIFIERS + ' ' + PHRASES)
    weight = 4. if short else float(profile[-2:]) / 10
    selected['context'] = MARKER + json.dumps({'context': context, 'words': VERBS, 'weight': weight}, ensure_ascii=False)
    selected['audit'].update(profile=profile, weight=weight, eos_prior_guard=True,
                             short_context=short, directional_tokens_biased=False)
    return selected


def configure(asr, profile):
    """Install the EOS-protected final request policy for this context or prior comparison."""
    configure_base(asr, 'control')
    if profile not in PROFILES:
        raise ValueError(profile)
    if profile != 'control':
        asr.final_request_policy = lambda **kwargs: policy(profile, **kwargs)
    asr.status['experimental_codeswitch_profile'] = profile
    asr.status['eos_prior_guard'] = True
    return asr
