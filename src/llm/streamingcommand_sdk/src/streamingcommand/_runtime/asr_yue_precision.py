"""Validated optional classifier20 native ASR policy (2026-11-17).

Same payload as the frozen V5 causal experiment. Selection uses current/recent
acoustic language evidence and PCM duration; it never reads transcript text.
"""
import json

from .asr_canto_fusion import policy as deployed_policy
from .asr_codeswitch import GLOSSARY
from .asr_codeswitch_bias import MARKER

VOCABULARY = (
    '粵語及英語內窺鏡操作詞彙，保留實際語言及用詞：'
    '順時針、逆時針、向左、向右、向上、向下、左邊、右邊、上面、下面、'
    '高啲、低啲、大啲、細啲、多啲、少啲、少少、推入、拉出、褪後、向前、'
    '停、停晒、停止、唔好、唔好再、繼續、返原位、重設、沖洗、追蹤、'
    '個鏡頭、支鏡、內窺鏡、把鉗、手術位、全景；'
    'bend、rotate、roll、spin、clockwise、anticlockwise、stop、reset、track、'
    'follow、clean、scope、tip、camera、grasper。'
)


def policy(**kwargs):
    """Use acoustic evidence to choose native short Yue decoding or bounded classifier priors for longer
    audio.
    """
    result = deployed_policy('fusion75', **kwargs)
    if result is None:
        return result
    hint = kwargs.get('acoustic_hint') or {}
    pair = hint.get('temporal_language_queries') or {}
    current, rolling = pair.get('current') or hint, pair.get('rolling') or {}
    if ((current.get('native_language_tag') == 'en' and current.get('language_probability', 0) >= .5)
            or (rolling.get('native_language_tag') == 'en' and rolling.get('language_probability', 0) >= .5)):
        return result
    short = len(kwargs['pcm']) <= 38400  # PCM16/16kHz, <=1.2 seconds.
    strong = (current.get('native_language_tag') == 'yue'
              and current.get('language_probability', 0) >= .9)
    recent = (current.get('native_language_tag') == 'yue'
              and current.get('language_probability', 0) >= .5
              and rolling.get('native_language_tag') == 'yue'
              and rolling.get('language_probability', 0) >= .95)
    if not (strong or (short and recent)):
        return result
    spec = json.loads(result['context'][len(MARKER):])
    if short:
        spec['context'] += ' ' + VOCABULARY
        spec['weight'] = 0.
        spec['word_weights'] = {}
        result['language'] = 'Cantonese'
    else:
        spec['context'] = VOCABULARY + ' ' + GLOSSARY
    weight = 0. if short else 2.
    spec.update(cjk_words=['支', '個', '把'], cjk_weight=weight)
    result['context'] = MARKER + json.dumps(spec, ensure_ascii=False)
    result['audit'].update(profile='classifier20', short_Yue_unbiased=short,
        extra_ASR_generations=0, no_reference_or_hypothesis_used=True,
        transcript_rewrite=False, cjk_classifier_weight=weight,
        cjk_classifier_words=spec['cjk_words'])
    return result


def configure(asr):
    """Attach the Yue precision policy only to a single shared ASR engine."""
    if asr.status.get('asr_engine_count') != 1:
        raise ValueError('Yue precision requires one shared ASR engine')
    asr.final_request_policy = policy
    asr.status.update(precision_profile='classifier20',
        codeswitch_acceptance='exposed_regression_validated_precision_20261117')
    return asr
