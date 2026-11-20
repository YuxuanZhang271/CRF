"""Bounded actual-audio/ASR-prefix continuity; no command decisions or text edits."""
import hashlib
import json
from .asr_backend import encode_pcm16


class CausalContinuity:
    """Use recent actual English ASR text with matching preceding waveform.

    No command recognition, word matching, confidence from reference answers,
    future audio, second ASR pass, or generated-text editing is used.
    """
    def __init__(self, backend, count=2, minimum_active_span_ms=0):
        """Retain the backend's original hooks and attach a bounded recent-audio and hypothesis history."""
        self.backend = backend
        self.count = count
        self.maximum_history = count
        self.minimum_active_span_ms=minimum_active_span_ms
        self.maximum_previous_samples=32000
        self.minimum_history_agreement=0
        self.allow_weak_english=True
        self.reject_short_repetitions=False
        self.record_history=True
        self.fresh_short_transcribe=False
        self.require_long_history_repetition=False
        self.timeline = bytearray()
        self.timeline_start = 0
        self.history = []
        self.job = None
        self.hint = None
        self.original_request = backend.request
        self.original_classifier = backend.acoustic_language_for_final
        backend.request = self.request
        backend.acoustic_language_for_final = self.classify

    def reset(self):
        """Clear buffered PCM, absolute sample offsets, and recent hypotheses at a session boundary."""
        self.timeline.clear()
        self.timeline_start = 0
        self.history.clear()
        self.job = self.hint = None

    def observe(self, raw):
        """Append received PCM and retain at most 32 seconds while keeping absolute sample positions."""
        self.timeline.extend(raw)
        # 32 s covers a 16 s queued final and the bounded 6 s audio context.
        trim = len(self.timeline) - 32*32000
        if trim > 0:
            self.timeline_start += trim//2
            del self.timeline[:trim]

    async def classify(self, raw):
        """Delegate acoustic language classification to the backend's original helper."""
        hint = await self.original_classifier(raw)
        # Non-prefetched requests execute in the final's serialized decode.
        # A prefetched hint is explicitly bound to its job by ReplaySession.
        return hint

    def current_eligible(self, raw):
        """Check whether a short current segment meets the active speech-span threshold."""
        if len(raw)>64000:return False
        if not self.minimum_active_span_ms:return True
        from .acoustic_span import acoustic_span
        return acoustic_span(raw)['active_span_ms']>=self.minimum_active_span_ms

    def previous_eligible(self,record):
        """Allow a recent English hypothesis only when its text and waveform fit the prefix constraints."""
        if not record['text'] or record['language']!='English' or record['samples']>self.maximum_previous_samples:
            return False
        if self.require_long_history_repetition and record['samples']>32000:
            from .temporal_language import repetition_signature
            import re
            # A merged long waveform paired with only one short hypothesis
            # can leave speech unrepresented in its decoder prefix. Preserve
            # genuinely repeated hypotheses on merged audio, but fall back
            # to an independent current decode for an unaligned prefix.
            return len(re.findall(r'\w+',record['text']))>len(repetition_signature(record['text']))
        return True

    async def request(self, op, **kwargs):
        """Supply one aligned audio prefix when eligible, then report coverage for the current segment
        alone.
        """
        job = self.job if op == 'transcribe' else None
        if job is None:
            return await self.original_request(op, **kwargs)
        previous = []
        source = None
        language = kwargs.get('language')
        if self.fresh_short_transcribe and len(job.pcm)<=64000:
            kwargs['reuse_stream_prefix']=False
        hint = self.hint or {}
        weak_english = (self.allow_weak_english and language is None and hint.get('native_language_tag')=='en'
                        and hint.get('language_probability',0)>=0.5)
        agreed=True
        if self.minimum_history_agreement:
            from .temporal_language import history_agrees
            agreed=history_agrees(self.history,self.minimum_history_agreement,
                                 self.reject_short_repetitions)
        if (language=='English' or weak_english) and self.current_eligible(job.pcm) and agreed:
            for record in reversed(self.history[-self.count:]):
                if not self.previous_eligible(record):
                    break
                previous.insert(0,record)
            if previous:
                start, end = previous[0]['start'], job.end_sample
                if end-start<=6*16000 and start>=self.timeline_start:
                    a,b=(start-self.timeline_start)*2,(end-self.timeline_start)*2
                    source=bytes(self.timeline[a:b])
                    assert len(source)==(end-start)*2
                    current_start=(job.start_sample-start)*2
                    assert source[current_start:]==job.pcm
                else:
                    previous=[]
        audit={'used':bool(previous), 'source_policy':'real_previous_audio_and_actual_asr_prefix',
               'current_start_sample':job.start_sample,'current_end_sample':job.end_sample,
               'current_pcm_sha256':hashlib.sha256(job.pcm).hexdigest(),
               'reference_used':False,'extra_audio_wait_ms':0}
        if self.minimum_history_agreement:
            audit.update(minimum_previous_agreement=self.minimum_history_agreement,
                         previous_agreement_met=agreed,
                         short_repeated_history_allowed=not self.reject_short_repetitions,
                         long_history_requires_repetition=self.require_long_history_repetition)
        if self.minimum_active_span_ms:
            from .acoustic_span import acoustic_span
            audit['current_acoustic_span']=acoustic_span(job.pcm) if len(job.pcm)<=64000 else None
            audit['minimum_active_span_ms']=self.minimum_active_span_ms
        if previous:
            prefix=' '.join(r['text'] for r in previous)
            kwargs.update(context='__PHONO_ACOUSTIC_PREFIX__'+json.dumps(
                {'context':self.backend.context,'previous_asr_text':prefix},ensure_ascii=False),
                audio_float32_b64=encode_pcm16(source),language='English',reuse_stream_prefix=False)
            audit.update(previous_ids=[r['id'] for r in previous],previous_asr_text=prefix,
                         model_start_sample=previous[0]['start'],model_end_sample=job.end_sample,
                         model_pcm_sha256=hashlib.sha256(source).hexdigest(),
                         current_language_hint=hint,weak_english_conditioning=weak_english)
        response=await self.original_request(op,**kwargs)
        if previous:
            assert response['audio_processed_samples']==len(source)//2
            audit['model_audio_processed_samples']=response['audio_processed_samples']
            response={**response,'audio_processed_samples':len(job.pcm)//2,
                      'audio_received_samples':len(job.pcm)//2}
        response['acoustic_continuity']=audit
        if self.record_history:
            self.history.append({'id':job.segment_id,'start':job.start_sample,'end':job.end_sample,
                'text':response['text'],'language':response.get('detected_language'),
                'samples':len(job.pcm)//2})
            self.history=self.history[-self.maximum_history:]
        return response
