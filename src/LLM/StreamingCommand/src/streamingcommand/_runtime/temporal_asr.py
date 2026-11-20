"""Experimental short-speech ASR with causal acoustic language and agreed history."""
import asyncio
import hashlib
from .asr_profiles import ASR_PROFILE_VERSIONS
from .temporal_language import TemporalLanguage


def temporal_asr_class(base_class):
    """Extend the continuity backend with current and rolling acoustic language evidence."""
    class TemporalASR(base_class):
        def __init__(self,*args,**kwargs):
            """Set up the temporal language policy and configure the predecessor audio constraints."""
            super().__init__(*args,**kwargs)
            self._temporal_language=TemporalLanguage()
            self.language_rolling_samples=40000
            self.allow_weak_english_route=False
            self.allow_recent_english_hint=False
            self._continuity.minimum_active_span_ms=200
            self._continuity.maximum_previous_samples=6*16000
            self._continuity.minimum_history_agreement=2
            self._continuity.allow_weak_english=False
            self.status.update(asr_profile='v15-temporal',
                profile_version=ASR_PROFILE_VERSIONS['v15-temporal'],
                minimum_active_span_ms=200,maximum_previous_audio_ms=6000,
                minimum_previous_agreement=2,language_rolling_window_ms=2500,
                maximum_recent_english_gap_ms=1500,
                language_policy='current_and_causal_recent_acoustic_evidence',
                history_policy='two_actual_matching_hypotheses_before_prefix_reuse')

        def begin_audio_session(self):
            """Reset temporal language state alongside the base profile's waveform history."""
            super().begin_audio_session()
            self._temporal_language=TemporalLanguage()

        async def acoustic_language_for_window(self,raw,*,start_sample,end_sample):
            """Evaluate current and rolling audio windows and return their causal language hints."""
            # Snapshot at submission: only the specified received samples enter
            # the query, even when a large feed packet contains newer audio.
            helper=self._continuity
            start=max(helper.timeline_start,end_sample-self.language_rolling_samples)
            a,b=(start-helper.timeline_start)*2,(end_sample-helper.timeline_start)*2
            assert 0<=a<=b<=len(helper.timeline)
            assert end_sample-start_sample==len(raw)//2
            rolling=bytes(helper.timeline[a:b])
            speech_helper=getattr(self,'_speech_evidence',None)
            speech_evidence=None
            if len(raw)<=64000 and getattr(self,'parallel_language_queries',False):
                queries=[helper.original_classifier(raw),helper.original_classifier(rolling)]
                if speech_helper is not None:queries.append(speech_helper.classify(raw))
                answers=await asyncio.gather(*queries)
                current,previous=answers[:2]
                if speech_helper is not None:speech_evidence=answers[2]
            else:
                current=await helper.original_classifier(raw)
                previous=None
            pair={'current_start_sample':start_sample,'current_end_sample':end_sample,
                  'rolling_start_sample':start,'rolling_end_sample':end_sample,
                  'rolling_pcm_sha256':hashlib.sha256(rolling).hexdigest(),
                  'current':current,'rolling':None,'future_audio_used':False}
            if len(raw)<=64000:
                pair['rolling']=(previous if previous is not None else
                                 await helper.original_classifier(rolling))
            return {**current,'temporal_language_queries':pair,
                    'acoustic_speech_evidence':speech_evidence}

        async def decode_final(self,segment_id,pcm,*,start_sample,end_sample,
                               prefetched_acoustic_hint=None,**kwargs):
            """Select a language condition from acoustic evidence and carry its audit record into final
            decoding.
            """
            hint=prefetched_acoustic_hint
            if hint is None or 'temporal_language_queries' not in hint:
                hint=await self.acoustic_language_for_window(pcm,start_sample=start_sample,end_sample=end_sample)
            pair=hint['temporal_language_queries']
            assert pair['current_start_sample']==start_sample and pair['current_end_sample']<=end_sample
            if len(pcm)<=64000:
                policy=self._temporal_language.choose(pcm,pair['current'],pair['rolling'],
                    start_sample/16,end_sample/16,
                    speech_evidence=hint.get('acoustic_speech_evidence'))
                hint={**hint,'requested_language':policy['requested_language'],
                      'temporal_language_policy':policy}
            return await super().decode_final(segment_id,pcm,start_sample=start_sample,end_sample=end_sample,
                                               prefetched_acoustic_hint=hint,**kwargs)

    return TemporalASR
