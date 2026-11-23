"""Causal Cantonese audio continuity experiments, without a second decode."""
import hashlib
import json

from .asr_backend import encode_pcm16
from .asr_codeswitch import GLOSSARY
from .asr_codeswitch_bias import MARKER as DOMAIN_MARKER, policy as eligibility
from .asr_codeswitch_prior6 import PHRASES
from .asr_codeswitch_banks import CLASSIFIERS, configure as configure_metadata
from .asr_codeswitch_verbs import VERBS

MARKER = '__PHONO_CANTO_AUDIO_PREFIX__'
PROFILES = ('control', 'compact_no_history', 'compact_history',
            'classifier_no_history', 'classifier_history', 'phrases_history')


def prefix_plan(asr, profile, pcm, hint):
    """Build a bounded previous-audio prefix from aligned recent samples and acoustic evidence."""
    if profile not in ('compact_history', 'classifier_history', 'phrases_history') or not 32000 < len(pcm) <= 80000:
        return None
    current = ((hint or {}).get('temporal_language_queries') or {}).get('current') or hint or {}
    if current.get('native_language_tag') != 'yue' or current.get('language_probability', 0) < .9:
        return None
    helper = asr._continuity
    job = helper.job
    if job is None or not helper.history:
        return None
    previous = helper.history[-1]
    gap = job.start_sample - previous['end']
    if (previous['language'] != 'Cantonese' or not previous['text'] or previous['samples'] > 48000
            or not 0 <= gap <= 24000 or job.end_sample - previous['start'] > 96000
            or previous['start'] < helper.timeline_start):
        return None
    a, b = (previous['start'] - helper.timeline_start) * 2, (job.end_sample - helper.timeline_start) * 2
    source = bytes(helper.timeline[a:b])
    assert len(source) == (job.end_sample - previous['start']) * 2
    assert source[-len(pcm):] == pcm
    return {'pcm': source, 'previous': previous,
            'audit': {'used': True, 'source': 'actual_previous_ASR_and_matching_contiguous_audio',
                      'previous_id': previous['id'], 'previous_asr_text': previous['text'],
                      'model_start_sample': previous['start'], 'model_end_sample': job.end_sample,
                      'current_start_sample': job.start_sample, 'current_end_sample': job.end_sample,
                      'model_pcm_sha256': hashlib.sha256(source).hexdigest(),
                      'current_pcm_sha256': hashlib.sha256(pcm).hexdigest(),
                      'future_audio_used': False, 'reference_used': False, 'extra_audio_wait_ms': 0}}


def policy(asr, profile, **kwargs):
    """Attach a qualifying audio prefix to the request recipe while retaining the selected ASR policy."""
    if profile == 'control':
        return None
    selected = eligibility('prior20', **kwargs)
    if selected is None:
        return None
    short = len(kwargs['pcm']) <= 32000
    context = (GLOSSARY + CLASSIFIERS if profile.startswith('classifier') and not short else
               PHRASES if profile == 'phrases_history' and not short else GLOSSARY)
    weight = 6. if profile == 'phrases_history' and not short else 4.
    plan = prefix_plan(asr, profile, kwargs['pcm'], kwargs['acoustic_hint'])
    if plan is not None:
        context = MARKER + json.dumps({'context': context, 'previous_asr_text': plan['previous']['text']}, ensure_ascii=False)
        selected['language'] = 'Cantonese'
    selected['context'] = DOMAIN_MARKER + json.dumps({'context': context, 'words': VERBS, 'weight': weight}, ensure_ascii=False)
    selected['audit'].update(profile=profile, weight=weight, eos_prior_guard=True,
                             canto_audio_prefix=plan['audit'] if plan else {'used': False})
    return selected


def configure(asr, profile):
    """Install the causal continuity policy and preserve the original request path for single-pass
    decoding.
    """
    if profile not in PROFILES:
        raise ValueError(profile)
    configure_metadata(asr, 'control')
    asr._canto_profile = profile
    if not getattr(asr, '_canto_prefix_request_installed', False):
        original = asr._continuity.original_request

        async def request(op, **kwargs):
            """Submit the prefixed waveform once and report coverage against the current segment."""
            helper = asr._continuity
            plan = None
            if op == 'transcribe' and helper.job is not None:
                context = kwargs.get('context', '')
                if context.startswith(DOMAIN_MARKER):
                    spec = json.loads(context[len(DOMAIN_MARKER):])
                    if spec['context'].startswith(MARKER):
                        plan = prefix_plan(asr, asr._canto_profile, helper.job.pcm, helper.hint)
                        assert plan is not None
                        kwargs['audio_float32_b64'] = encode_pcm16(plan['pcm'])
            response = await original(op, **kwargs)
            if plan is not None:
                assert response['audio_processed_samples'] == len(plan['pcm']) // 2
                assert len(response['generations']) == 1
                response['model_audio_processed_samples'] = response['audio_processed_samples']
                response['audio_processed_samples'] = response['audio_received_samples'] = len(helper.job.pcm) // 2
                response['cantonese_acoustic_prefix'] = plan['audit']
            return response

        asr._continuity.original_request = request
        asr._canto_prefix_request_installed = True
    if profile != 'control':
        asr.final_request_policy = lambda **kwargs: policy(asr, profile, **kwargs)
    asr.status['experimental_codeswitch_profile'] = profile
    asr.status['canto_prefix_maximum_total_ms'] = 6000
    return asr
