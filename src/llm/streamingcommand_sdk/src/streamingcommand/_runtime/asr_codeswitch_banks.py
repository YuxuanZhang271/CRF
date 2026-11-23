"""Uniform bank/context diagnostics after the mixed-word prior screen."""
import json

from .asr_codeswitch import GLOSSARY
from .asr_codeswitch_bias import MARKER, WORDS, policy as eligibility

CLASSIFIERS = '粵語用語：個鏡頭、支鏡、支內窺鏡、把鉗、幫我、麻煩、高啲、低啲、上面、下面、左邊、右邊。'
DICTIONARY = ('操作詞彙：bend（彎曲），rotate（旋轉），roll（滾轉），spin（自轉），'
              'advance（推進），pull（拉出），track（追蹤），reset（重設），stop（停止），clean（沖洗）。')
PROFILES = ('control', 'base_prior40', 'base_cantonese40', 'base_classifiers40',
            'v15_classifiers40', 'v15_dictionary40', 'english_long40')


def policy(profile, **kwargs):
    """Select a bank or compact context for eligible diagnostic requests while keeping the same raw audio."""
    if profile not in PROFILES:
        raise ValueError(profile)
    if profile == 'control':
        return None
    result = eligibility('prior20', **kwargs)
    if result is None:
        return None
    context = GLOSSARY + (CLASSIFIERS if 'classifiers' in profile else DICTIONARY if 'dictionary' in profile else '')
    result['context'] = MARKER + json.dumps({'context': context, 'words': WORDS, 'weight': 4.}, ensure_ascii=False)
    if profile.startswith('base_'):
        result['model_variant'] = 'base'
    if profile == 'base_cantonese40':
        result['language'] = 'Cantonese'
    if profile == 'english_long40' and len(kwargs['pcm']) > 64000:
        result['language'] = 'English'
    result['audit'].update(profile=profile, weight=4.)
    return result


def configure(asr, profile):
    """Require one shared ASR engine and install the selected bank comparison policy."""
    if asr.status.get('asr_engine_count') != 1 or profile not in PROFILES:
        raise ValueError('Invalid single-engine candidate')
    if not getattr(asr, '_actual_variant_metadata_installed', False):
        original = asr._continuity.original_request

        async def request(op, **kwargs):
            """Record the resolved primary or alternate model directory for the bank the worker actually
            used.
            """
            response = await original(op, **kwargs)
            variant = (response.get('shared_engine') or {}).get('variant')
            if op == 'transcribe' and variant in ('base', 'v15'):
                response['selected_model_dir'] = str(asr.alternate_model_dir if variant == 'base' else asr.model_dir)
            return response

        asr._continuity.original_request = request
        asr._actual_variant_metadata_installed = True
    if profile == 'control':
        if hasattr(asr, 'final_request_policy'):
            del asr.final_request_policy
    else:
        asr.final_request_policy = lambda **kwargs: policy(profile, **kwargs)
    asr.status['experimental_codeswitch_profile'] = profile
    asr.status['codeswitch_acceptance'] = 'unvalidated_do_not_promote'
    return asr
