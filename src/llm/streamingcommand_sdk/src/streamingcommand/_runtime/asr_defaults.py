from .paths import model_path, worker_path
"""Selectable demo profiles; V15 matches the retained long-TTS experiment."""
import asyncio

from .asr_backend import NativeASR, WHISPER
from .asr_profiles import ASR_PROFILES, ASR_PROFILE_VERSIONS, DEFAULT_ASR_PROFILE
from .device_policy import DEFAULT_DEVICE


class V15ASR(NativeASR):
    def __init__(self, *args, **kwargs):
        """Set the V15 worker defaults and expose its supported-margin finalization policy."""
        defaults = dict(
            model_dir=model_path("asr"),
            worker=worker_path("qwen_native_supported_margins_v3_worker.py"),
            context='', language=None, cantonese_final_language=None,
            retry_out_of_domain_language=False, reuse_streaming_language=False,
            word_bias_weight=0, minimum_streaming_hint_ms=1600,
        )
        super().__init__(*args, **{**defaults, **kwargs})
        self.status.update(asr_profile='v15', profile_version=ASR_PROFILE_VERSIONS['v15'],
            worker=str(self.worker), final_audio_policy={
                'name':'supported-margins-v3', 'minimum_existing_outer_quiet_ms':150,
                'maximum_extension_ms':170, 'hop_tolerance_ms':10,
                'canonical_leading_ms':160, 'canonical_trailing_ms':160,
                'threshold_dbfs':-75, 'unsupported_margins':'preserve_raw_pcm'})


def build_asr(profile=DEFAULT_ASR_PROFILE, *, device=DEFAULT_DEVICE):
    """Compose the requested ASR profile without loading models or starting a worker."""
    if profile == 'v17-yue-precision':
        from .asr_yue_precision import configure
        asr = build_asr('v15-canto-commit', device=device)
        asr.worker = worker_path("qwen_native_canto_precision_worker.py")
        configure(asr)
        asr.status.update(asr_profile=profile, profile_version=ASR_PROFILE_VERSIONS[profile],
                          worker=str(asr.worker))
        return asr
    if profile == 'v15-canto-commit':
        from .asr_canto_fusion import configure
        asr = build_asr('v15-speech-aware', device=device)
        asr.worker = worker_path("qwen_native_canto_fusion_worker.py")
        asr.worker_environment['PHONO_ASR_BLEND_FRACTIONS'] = '[0.75]'
        configure(asr, 'fusion75')
        asr.status.update(asr_profile=profile, profile_version=ASR_PROFILE_VERSIONS[profile],
            worker=str(asr.worker),
            memory_policy='one_engine_original_banks_and_one_explicit_mix75_bank',
            codeswitch_acceptance='exposed_regression_validated_v12_deployed_20261115',
            endpoint_commit_policy='yue_observed_continuation_380ms',
            endpoint_continuation_gap_ms=380)
        return asr
    if profile == 'v15':
        return V15ASR(device=device)
    if profile == 'legacy':
        raise ValueError('The historical dual-ASR fallback is available only in original Demo')
    if profile == 'v15-realtime':
        from .realtime_asr import realtime_asr_class
        return realtime_asr_class(V15ASR)(device=device)
    if profile == 'v15-continuity':
        from .realtime_asr import realtime_asr_class
        from .continuity_asr import continuity_asr_class
        return continuity_asr_class(realtime_asr_class(V15ASR))(device=device)
    if profile == 'v15-temporal':
        from .realtime_asr import realtime_asr_class
        from .continuity_asr import continuity_asr_class
        from .temporal_asr import temporal_asr_class
        return temporal_asr_class(continuity_asr_class(realtime_asr_class(V15ASR)))(device=device)
    if profile in ('v15-temporal-stream','v15-speech-aware','v15-speech-aware-dual'):
        from .realtime_asr import realtime_asr_class
        from .continuity_asr import continuity_asr_class
        from .temporal_asr import temporal_asr_class
        from .temporal_stream_asr import temporal_stream_asr_class
        asr=temporal_stream_asr_class(temporal_asr_class(
            continuity_asr_class(realtime_asr_class(V15ASR))))(device=device,
                enable_speech_evidence=profile in ('v15-speech-aware','v15-speech-aware-dual'))
        if profile=='v15-speech-aware':
            from .shared_asr import configure_shared_engine
            configure_shared_engine(asr)
        elif profile=='v15-speech-aware-dual':
            asr.status.update(asr_profile=profile,profile_version=ASR_PROFILE_VERSIONS[profile])
        return asr
    raise ValueError(f'Unknown ASR profile: {profile}')


def segment_limit_ms(profile):
    """Return the supported maximum segment length for the requested ASR profile."""
    return 16000 if profile in ('v17-yue-precision', 'v15-canto-commit', 'v15', 'v15-realtime', 'v15-continuity', 'v15-temporal', 'v15-temporal-stream','v15-speech-aware','v15-speech-aware-dual') else 8000

