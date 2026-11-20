"""Opt-in acoustic ASR ablations; not selected by the application default.

All policies are fixed before decoding. They receive acoustic evidence and PCM
length only, never table rows, expected words, parser output or ASR hypotheses.
"""

PROFILES = ('control', 'yue_language', 'yue_glossary_auto',
            'yue_glossary_language', 'yue_base_glossary_auto', 'yue_base_glossary_language')

# A domain glossary, not a reference sentence or a per-recording prompt.
GLOSSARY = (
    '內窺鏡操作用語（粵語及英語）：'
    'endoscope, camera, scope, tip, lens, grasper, advance, push, pull, bend, '
    'rotate, roll, spin, clockwise, anticlockwise, track, follow, stop, reset, clean; '
    '內窺鏡, 鏡頭, 鏡, 鉗, 左, 右, 上, 下, 向前, 褪後, 推入, 拉出, '
    '停, 停晒, 高啲, 低啲, 少少, 返原位, 沖洗。'
)


def request_policy(profile, *, pcm, acoustic_hint, language, context):
    """Select a short mixed-language request recipe from acoustic hints without inspecting generated text."""
    if profile not in PROFILES:
        raise ValueError(f'Unknown ASR ablation: {profile}')
    if profile == 'control' or language == 'English' or not 0 < len(pcm) <= 128000:
        return None
    hint = acoustic_hint or {}
    pair = hint.get('temporal_language_queries') or {}
    current = pair.get('current') or hint
    rolling = pair.get('rolling') or {}
    evidence = hint.get('acoustic_speech_evidence')
    if evidence is not None and not evidence['speech_confirmed']:
        return None
    if (current.get('native_language_tag') != 'yue'
            or current.get('language_probability', 0) < .5):
        return None
    # Short English can be misclassified as Yue. Do not override an acoustic
    # English majority in the recent causal window, even if the current
    # classifier prefers Yue. No recognized words enter this protection.
    if (rolling.get('native_language_tag') == 'en'
            and rolling.get('language_probability', 0) >= .5):
        return None
    return {
        'language': 'Cantonese' if profile.endswith('language') else language,
        'context': GLOSSARY if 'glossary' in profile else context,
        'reuse_stream_prefix': False,
        **({'model_variant': 'base'} if '_base_' in profile else {}),
        'audit': {
            'profile': profile,
            'source': 'current_acoustic_yue_majority',
            'current_language_probability': current['language_probability'],
            'maximum_audio_ms': 4000,
            'reference_or_hypothesis_used': False,
            'extra_audio_wait_ms': 0,
            'additional_decoding_passes': 0,
        },
    }


def configure_candidate(asr, profile):
    """Keep R17's sole engine, scheduler, English route and endpoint policy."""
    if asr.status.get('asr_engine_count') != 1:
        raise ValueError('A codeswitch candidate requires the single shared R17 engine')
    if profile not in PROFILES:
        raise ValueError(f'Unknown ASR ablation: {profile}')
    if profile != 'control':
        asr.final_request_policy = lambda **kwargs: request_policy(profile, **kwargs)
    elif hasattr(asr, 'final_request_policy'):
        del asr.final_request_policy
    asr.status['experimental_codeswitch_profile'] = profile
    asr.status['codeswitch_acceptance'] = 'unvalidated_do_not_promote'
    return asr
