from .paths import model_path, worker_path
"""Default GPU1 short-speech model with retained V15 long-speech decoding."""
import asyncio
from types import SimpleNamespace

from .acoustic_continuity import CausalContinuity
from .asr_backend import NativeASR, WHISPER
from .decoding import decode_final
from .asr_profiles import ASR_PROFILE_VERSIONS


def continuity_asr_class(base_class):
    """Wrap a streaming backend with causal waveform history and short-English request routing."""
    class ContinuityASR(base_class):
        def __init__(self,*args,**kwargs):
            """Configure short and long decode paths and attach a recent-audio continuity helper."""
            kwargs.setdefault('gpu_memory_utilization',.24)
            super().__init__(*args,**kwargs)
            self._continuity_start_lock=asyncio.Lock()
            self.final_prefix_tokens=5
            self.long_final_prefix_tokens=22
            self.refresh_acoustic_language_on_new_voice=True
            self._continuity_asr=NativeASR(device=self.device,
                model_dir=model_path("asr-base"),context=self.context,
                worker=worker_path("qwen_native_continuity_stream_worker.py"),
                cantonese_final_language=None,gpu_memory_utilization=.24)
            self._continuity=CausalContinuity(self,count=2,minimum_active_span_ms=400)
            primary_request=self._continuity.original_request

            async def route(op,**request):
                """Route short English or explicit prefix requests to the continuity path, preserving the
                primary path otherwise.
                """
                helper=self._continuity
                hint=helper.hint or {}
                short_english=(helper.job is not None and helper.current_eligible(helper.job.pcm)
                    and (request.get('language')=='English' or
                         (getattr(self,'allow_weak_english_route',True) and
                          hint.get('native_language_tag')=='en' and hint.get('language_probability',0)>=.5)))
                if op=='transcribe' and (short_english or request.get('context','').startswith('__PHONO_ACOUSTIC_PREFIX__')):
                    request['reuse_stream_prefix']=False
                    result=await self._continuity_asr.request(op,**request)
                    result['selected_model_dir']=str(self._continuity_asr.model_dir)
                    return result
                result=await primary_request(op,**request)
                if op=='transcribe':result['selected_model_dir']=str(self.model_dir)
                return result

            self._continuity.original_request=route
            self.final_strategy='single_pass_acoustic_duration_model_with_bounded_real_audio_prefix'
            self.status.update(asr_profile='v15-continuity',
                profile_version=ASR_PROFILE_VERSIONS['v15-continuity'],
                final_strategy=self.final_strategy,
                continuity_model_dir=str(self._continuity_asr.model_dir),
                continuity_device=self.device,minimum_active_span_ms=400,
                maximum_current_audio_ms=2000,maximum_context_audio_ms=6000,
                maximum_previous_utterances=2,decoder_passes_per_final=1,
                gpu_memory_utilization_per_worker=.24,
                long_final_prefix_tokens=self.long_final_prefix_tokens,
                refresh_acoustic_language_on_new_voice=True,
                model_selection='acoustic_span_and_language_only_no_keyword_matching')

        async def start(self):
            """Start the active backend and its configured continuity view before marking the combined
            profile ready.
            """
            async with self._continuity_start_lock:
                if self.status.get('ready') and self._continuity_asr.status.get('ready'):
                    return self.status.get('worker_ready',dict(self.status))
                try:
                    ready=await super().start()
                    self._phase('warming')
                    self._continuity_asr.logs_dir=self.logs_dir
                    await self._continuity_asr.start()
                    self._phase('ready')
                    return ready
                except BaseException:
                    await self.close()
                    raise

        def begin_audio_session(self):
            """Reset recent audio and hypotheses before accepting a new stream."""
            self._continuity.reset()

        def observe_audio(self,raw):
            """Pass raw received PCM to the continuity helper for sample-aligned prefix selection."""
            self._continuity.observe(raw)

        async def decode_final(self,segment_id,pcm,*,start_sample,end_sample,
                               prefetched_acoustic_hint=None,**kwargs):
            """Bind evidence and timing to one final decode and restore request-local prefix settings
            afterward.
            """
            helper=self._continuity
            original_count=helper.count
            original_preserve=self.preserve_acoustic_native_prefix
            original_rollback=self.final_prefix_tokens
            if len(pcm)>64000:self.final_prefix_tokens=self.long_final_prefix_tokens
            selected=helper.history[-original_count:]
            while selected and end_sample-selected[0]['start']>6*16000:selected.pop(0)
            helper.count=max(1,len(selected))
            helper.job=SimpleNamespace(segment_id=segment_id,pcm=pcm,
                                       start_sample=start_sample,end_sample=end_sample)
            try:
                hint=prefetched_acoustic_hint
                if hint is None:hint=await helper.original_classifier(pcm)
                helper.hint=hint
                if helper.current_eligible(pcm):
                    # The short final may use another model: its decoder
                    # cannot retain a body prefix produced by the V15 SDK.
                    self.preserve_acoustic_native_prefix=False
                    previous=[]
                    for record in reversed(helper.history[-helper.count:]):
                        if not record['text'] or record['language']!='English' or record['samples']>32000:break
                        previous.insert(0,record)
                    if (getattr(self,'allow_recent_english_hint',True)
                            and hint.get('requested_language') is None and hint.get('native_language_tag')=='en'
                            and hint.get('language_probability',0)>=.5 and previous
                            and end_sample-previous[0]['start']<=6*16000
                            and previous[0]['start']>=helper.timeline_start):
                        hint={**hint,'requested_language':'English','original_requested_language':None,
                              'conditioning_policy':'recent_english_asr_and_current_acoustic_english_majority'}
                return await decode_final(self,segment_id,pcm,
                    prefetched_acoustic_hint=hint,**kwargs)
            finally:
                helper.job=helper.hint=None
                helper.count=original_count
                self.preserve_acoustic_native_prefix=original_preserve
                self.final_prefix_tokens=original_rollback

        async def close(self):
            """Release both backend views and discard all retained continuity audio."""
            try:await self._continuity_asr.close()
            finally:
                await super().close()
                self._continuity.reset()

    return ContinuityASR
