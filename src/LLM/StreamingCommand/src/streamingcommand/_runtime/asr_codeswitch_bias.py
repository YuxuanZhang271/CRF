"""Experimental fixed domain priors for one mixed-language ASR generation."""
import json

from .asr_codeswitch import GLOSSARY

MARKER = '__PHONO_DOMAIN_TOKEN_PRIOR__'
WORDS = tuple(sorted(set(
    'advance push pull move bend rotate roll spin track follow stop reset clean wash '
    'scope tip camera endoscope lens grasper left right up down forward backward '
    'clockwise anticlockwise liver pancreas stomach instrument organ scissor knife '
    'ligasure harmonic scalpel gallbladder curved straight duodenal stump'.split())))
ENGLISH_GLOSSARY = ', '.join(WORDS)
PROFILES = ('control', 'domain_auto', 'prior05', 'prior10', 'prior20', 'prior30',
            'legacy_prior20', 'cantonese_prior20', 'english_glossary_prior20')


def policy(profile, *, pcm, acoustic_hint, language, context):
    """Build a fixed bilingual vocabulary prior only when current or rolling evidence supports Cantonese."""
    if profile not in PROFILES:
        raise ValueError(profile)
    if profile == 'control' or language == 'English' or not 0 < len(pcm) <= 128000:
        return None
    hint = acoustic_hint or {}
    pair = hint.get('temporal_language_queries') or {}
    current = pair.get('current') or hint
    rolling = pair.get('rolling') or {}
    evidence = hint.get('acoustic_speech_evidence')
    if evidence is not None and not evidence['speech_confirmed']:
        return None
    ct, cp = current.get('native_language_tag'), current.get('language_probability', 0)
    rt, rp = rolling.get('native_language_tag'), rolling.get('language_probability', 0)
    if rt == 'en' and rp >= .5:
        return None
    if not ((ct == 'yue' and cp >= .5) or (rt == 'yue' and rp >= .8 and cp < .95)):
        return None
    domain = context if profile == 'legacy_prior20' else (
        ENGLISH_GLOSSARY if profile == 'english_glossary_prior20' else GLOSSARY)
    weight = {'prior05': .5, 'prior10': 1., 'prior20': 2., 'prior30': 3.}.get(profile, 2.)
    if profile != 'domain_auto':
        domain = MARKER + json.dumps({'context': domain, 'words': WORDS, 'weight': weight}, ensure_ascii=False)
    return {'language': 'Cantonese' if profile == 'cantonese_prior20' else language,
            'context': domain, 'reuse_stream_prefix': False,
            'audit': {'profile': profile, 'source': 'current_or_recent_causal_yue_acoustic_evidence',
                      'weight': weight if profile != 'domain_auto' else 0,
                      'extra_audio_wait_ms': 0, 'additional_decoding_passes': 0,
                      'reference_or_hypothesis_used': False}}


def configure(asr, profile):
    """Install the vocabulary prior policy and its ASR worker for the selected experiment."""
    if asr.status.get('asr_engine_count') != 1:
        raise ValueError('A single shared ASR engine is required')
    if profile not in PROFILES:
        raise ValueError(profile)
    if profile == 'control':
        if hasattr(asr, 'final_request_policy'):
            del asr.final_request_policy
    else:
        asr.final_request_policy = lambda **kwargs: policy(profile, **kwargs)
    asr.status['experimental_codeswitch_profile'] = profile
    asr.status['codeswitch_acceptance'] = 'unvalidated_do_not_promote'
    return asr
