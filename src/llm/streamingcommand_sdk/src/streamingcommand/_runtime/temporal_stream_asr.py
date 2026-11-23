"""Bounded causal short-window updates using the same acoustic final policy."""
import copy
from types import SimpleNamespace
from .asr_backend import encode_pcm16
from .asr_profiles import ASR_PROFILE_VERSIONS
from .temporal_language import history_agrees
from .pooled_acoustic_language import PooledAcousticLanguageHint
from .acoustic_speech import AcousticSpeechEvidence


def temporal_stream_asr_class(base_class):
    """Compose a temporal backend with pooled language evidence and an optional speech-evidence helper."""
    class TemporalStreamASR(base_class):
        def __init__(self,*args,enable_speech_evidence=False,**kwargs):
            """Configure bounded rolling language queries and the optional Silero speech evidence path."""
            super().__init__(*args,**kwargs)
            self._acoustic_language=PooledAcousticLanguageHint('cpu',condition_english=True,
                int8=True,feature_dither=0.0,workers=2,cache_size=64)
            self._continuity.original_classifier=self._acoustic_language.classify
            self.parallel_language_queries=True
            self._speech_evidence=AcousticSpeechEvidence() if enable_speech_evidence else None
            self._continuity.reject_short_repetitions=True
            self._continuity.maximum_previous_samples=6*16000
            self._continuity.require_long_history_repetition=True
            self._continuity.fresh_short_transcribe=True
            self.language_rolling_samples=80000
            self.status.update(asr_profile='v15-temporal-stream',
                profile_version=ASR_PROFILE_VERSIONS['v15-temporal-stream'],
                language_rolling_window_ms=5000,
                short_partial_strategy='one_causal_snapshot_decode_without_committing_history',
                short_repeated_history_allowed=False,
                maximum_previous_audio_ms=6000,
                long_previous_must_contain_whole_phrase_repetition=True,
                short_final_prefix_policy='fresh_single_pass_no_current_sdk_body_prefix',
                short_partial_requires_agreed_english_history=True,
                other_partial_strategy='retained_native_incremental_decode')
            if self._speech_evidence is not None:
                self.status.update(asr_profile='v15-speech-aware',
                    profile_version=ASR_PROFILE_VERSIONS['v15-speech-aware'])
                self.status['acoustic_speech_helper']={'model':'SileroVAD_ONNX','device':'cpu',
                    'model_dir':str(self._speech_evidence.model_dir),'threshold':.5,
                    'threads':1,'purpose':'language_conditioning_only_not_audio_rejection',
                    'short_audio_max_ms':2000,'extra_audio_wait_ms':0}
            self.status['acoustic_language_helper']={**self.status['acoustic_language_helper'],
                'feature_dither':0.0,'frontend_policy':'deterministic_inference_no_random_dither',
                'cpu_workers':2,'threads_per_worker':2,'exact_pcm_cache_entries':64,
                'pair_queries':'parallel_same_audio_and_same_model'}

        def begin_audio_session(self):
            """Reset parent streaming state and discard language hints cached for the preceding recording."""
            super().begin_audio_session()
            self._acoustic_language.clear_cache()

        async def start(self):
            """Start the base ASR profile and warm its optional speech-evidence helper before reporting
            readiness.
            """
            try:
                ready=await super().start()
                if self._speech_evidence is not None:
                    self._phase('warming')
                    await self._speech_evidence.start()
                    self._phase('ready')
                return ready
            except BaseException:
                await self.close()
                raise

        async def close(self):
            """Close the optional speech helper and release the underlying ASR profile."""
            try:
                if self._speech_evidence is not None:await self._speech_evidence.close()
            finally:await super().close()

        async def decode_partial(self,segment_id,pcm,*,start_sample,end_sample):
            """Allow a short causal preview only when recent English hypotheses and audio evidence agree."""
            if len(pcm)>64000:return None
            helper=self._continuity
            previous=helper.history[-2:]
            if (not history_agrees(previous,2,reject_short_repetitions=True) or
                any(r['language']!='English' for r in previous) or
                end_sample-previous[0]['start']>6*16000):
                return None
            hint=await self.acoustic_language_for_window(pcm,start_sample=start_sample,end_sample=end_sample)
            pair=hint['temporal_language_queries']
            # A partial is not an independent utterance or a final language
            # observation. It cannot seed or validate its own later prefix.
            policy=copy.copy(self._temporal_language).choose(
                pcm,pair['current'],pair['rolling'],start_sample/16,end_sample/16,
                speech_evidence=hint.get('acoustic_speech_evidence'))
            hint={**hint,'requested_language':policy['requested_language'],
                  'temporal_language_policy':policy}
            if policy['requested_language']!='English':return None
            assert helper.job is None and helper.hint is None and helper.record_history
            helper.job=SimpleNamespace(segment_id=segment_id,pcm=pcm,
                start_sample=start_sample,end_sample=end_sample)
            helper.hint=hint;helper.record_history=False
            try:
                result=await self.request('transcribe',segment_id=segment_id,
                    audio_float32_b64=encode_pcm16(pcm),context=self.context,
                    language=policy['requested_language'],reuse_stream_prefix=False)
                assert result['audio_processed_samples']==len(pcm)//2
                return {'result':result,'requested_language':policy['requested_language'],
                        'acoustic_language_hint':hint,'language_policy_source':'short_causal_acoustic_snapshot'}
            finally:
                helper.job=helper.hint=None;helper.record_history=True

    return TemporalStreamASR
