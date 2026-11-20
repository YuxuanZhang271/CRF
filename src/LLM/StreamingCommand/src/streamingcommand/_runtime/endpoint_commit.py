"""Opt-in endpoint assembly; raw ASR segment decodes and histories are unchanged.

Only already-observed acoustic resumption can defer an unpublished endpoint.
Each physical segment is decoded once as usual. Its original text and decoder
receipt remain in endpoint_components; one assembled transcript is committed.
"""
from __future__ import annotations

from .streaming import StreamingSession


class YueEndpointCommitSession(StreamingSession):
    def __init__(self,*args,continuation_gap_ms=440,**kwargs):
        """Validate the continuation window and track unpublished decode groups without adding a wait."""
        super().__init__(*args,**kwargs)
        if not self.endpoint_ms<=continuation_gap_ms<=1000:
            raise ValueError('Continuation gap must cover endpoint and be <=1000ms')
        self.continuation_samples=round(continuation_gap_ms*16)
        self._tentative=None
        self._decoding_job=None
        self._links={}
        self._components={}
        self._group_start={}
        self.stats.update(endpoint_continuations=0,endpoint_provisional_decodes=0,endpoint_wait_added_ms=0)

    def _submit_final(self,reason):
        """Mark silence-triggered finals as eligible for a later acoustic continuation."""
        super()._submit_final(reason)
        self._tentative=self._finals[-1] if reason=='vad_silence' else None

    def _resume(self,event):
        """Link unpublished segments only when the gap, span, and already-completed Yue evidence qualify."""
        job,active=self._tentative,self._active
        if job is None or active is None or job.segment_id in self._finalized:
            return
        gap=active['last_voice_end']-job.last_voice_end_sample
        group_start=self._group_start.get(job.segment_id,job.start_sample)
        if not 0<gap<=self.continuation_samples:
            return
        if active['last_voice_end']-group_start>=self.max_segment_samples:
            return
        task=job.language_prefetch
        if task is None or not task.done() or task.cancelled():return
        try:hint=task.result()
        except Exception:return
        pair=hint.get('temporal_language_queries') or {}
        current=pair.get('current') or hint
        rolling=pair.get('rolling') or {}
        yue=(current.get('native_language_tag')=='yue' and current.get('language_probability',0)>=.7)
        recent=(rolling.get('native_language_tag')=='yue' and rolling.get('language_probability',0)>=.9)
        if not (yue or recent):return
        self._links[job.segment_id]=active['id']
        self._group_start[active['id']]=group_start
        self._tentative=None
        self.stats['endpoint_continuations']+=1
        event.update(endpoint_continuation=True,continuation_from_segment_id=job.segment_id,
                     continuation_gap_ms=gap/16,extra_audio_wait_ms=0,
                     continuation_language_evidence=dict(source='existing_completed_acoustic_query',
                         current_yue=yue,rolling_yue=recent,extra_queries=0))

    def _emit(self,event):
        """Publish provisional components or assemble ordered original ASR texts into one final transcript.
        """
        if event['type']=='speech.started':self._resume(event)
        if event['type']=='transcript.final':
            segment=event['segment_id']
            # Save each raw per-segment ASR result before any display assembly.
            raw={k:v for k,v in event.items() if not k.startswith('_')}
            components=self._components.pop(segment,[])+[raw]
            if len(components)>1:
                text=' '.join(c['text'] for c in components if c['text'].strip())
                previous_length=event['replace_from']+event['retracted_characters']
                event={**event,'text':text,'raw_decoded':None,
                    'raw_decoded_chunks':[g for c in components for g in c['raw_decoded_chunks']],
                    'audio_start_ms':components[0]['audio_start_ms'],
                    'audio_input_samples':sum(c['audio_input_samples'] for c in components),
                    'audio_processed_samples':sum(c['audio_processed_samples'] for c in components),
                    'complete_audio_coverage':all(c['complete_audio_coverage'] for c in components),
                    'generation_count':sum(c['generation_count'] for c in components),
                    'generations':[g for c in components for g in c['generations']],
                    'decoding_passes':[g for c in components for g in c['decoding_passes']],
                    'request_ops':[g for c in components for g in c['request_ops']],
                    'inference_ms':sum(c['inference_ms'] for c in components),
                    'queue_ms':max(c['queue_ms'] for c in components),
                    'replace_from':0,'replacement':text,'retracted_characters':previous_length,
                    'text_changed':True,'endpoint_components':components,
                    'endpoint_assembly':'ordered_original_ASR_texts_no_redecode_no_keyword_rewrite',
                    'coverage_basis':'each_original_physical_ASR_segment',
                    'extra_audio_wait_ms':0}
                self._texts[segment]=text
            target=self._links.pop(segment,None)
            if target is not None:
                self._components[target]=components
                event={**event,'type':'transcript.partial','is_final':False,
                       'endpoint_provisional':True,'endpoint_continues_in_segment_id':target,'extra_audio_wait_ms':0}
                event.pop('_last_voice_received',None)
                self.stats['completed_finals']-=1
                self.stats['completed_partials']+=1
                self.stats['endpoint_provisional_decodes']+=1
            elif self._tentative is self._decoding_job:
                self._tentative=None
            self._group_start.pop(segment,None)
        super()._emit(event)

    async def _decode(self,job):
        """Track the active physical decode while delegating generation to the normal scheduler."""
        self._decoding_job=job
        try:await super()._decode(job)
        finally:self._decoding_job=None
