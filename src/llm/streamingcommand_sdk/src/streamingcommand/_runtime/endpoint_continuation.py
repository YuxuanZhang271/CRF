"""Opt-in causal continuation while an endpoint decode is still unpublished.

There is no extra timer and no text, intent or equality matcher. If speech
resumes within the bounded acoustic gap while the old final is still in flight,
keep its result provisional and decode the contiguous full utterance at its
next endpoint. A final already published is never revoked or deduplicated.
"""
from __future__ import annotations

import copy

from .streaming import FRAME_SAMPLES, SAMPLE_RATE, StreamingSession


class EndpointContinuationSession(StreamingSession):
    def __init__(self, *args, continuation_gap_ms=440, **kwargs):
        """Initialize the unpublished-endpoint tracking state and bound how long an observed resumption can
        link.
        """
        super().__init__(*args, **kwargs)
        if not self.endpoint_ms <= continuation_gap_ms <= 1000:
            raise ValueError('Continuation gap must cover the endpoint and be <=1000ms')
        self.continuation_samples = round(continuation_gap_ms * SAMPLE_RATE / 1000)
        self._audio_tail = bytearray()
        self._tail_start = 0
        self._tentative = None
        self._continued = {}
        self._decoding_job = None
        self.stats.update(endpoint_continuations=0, endpoint_provisional_decodes=0,
                          endpoint_wait_added_ms=0)

    def _frame(self, frame, valid_samples):
        """Retain observed acoustic resumption while processing the ordinary frame and endpoint logic."""
        # A small raw PCM tail fills any silence between two non-overlapping
        # original segments. Never reconstruct or omit samples in that gap.
        self._audio_tail.extend(frame[:valid_samples * 2])
        capacity = self.continuation_samples + 12 * FRAME_SAMPLES
        excess = max(0, len(self._audio_tail) // 2 - capacity)
        if excess:
            del self._audio_tail[:excess * 2]
            self._tail_start += excess
        super()._frame(frame, valid_samples)

    def _submit_final(self, reason):
        """Keep the original silence-ending job provisional for an eligible in-flight continuation."""
        super()._submit_final(reason)
        if reason == 'vad_silence':
            self._tentative = self._finals[-1]
        else:
            self._tentative = None

    def _resume(self, event):
        """Join an observed successor to an unpublished decode only while the acoustic gap is within
        bounds.
        """
        job = self._tentative
        active = self._active
        if job is None or active is None or job.segment_id in self._finalized:
            return
        if not 0 < active['last_voice_end'] - job.last_voice_end_sample <= self.continuation_samples:
            return
        if active['start'] < job.end_sample or active['last_voice_end'] - job.start_sample >= self.max_segment_samples:
            return
        if job.end_sample < self._tail_start:
            return
        gap = bytes(self._audio_tail[(job.end_sample-self._tail_start)*2:
                                    (active['start']-self._tail_start)*2])
        if len(gap)//2 != active['start'] - job.end_sample:
            raise AssertionError('Continuation gap is not available as original PCM')
        joined = job.pcm + gap + bytes(active['pcm'])
        assert len(joined)//2 == self._cursor - job.start_sample
        provisional_id = active['id']
        active.update(id=job.segment_id, start=job.start_sample, pcm=bytearray(joined))
        duration = self._cursor - active['start']
        active['next_partial'] = max(active['next_partial'],
                                     ((duration//self.interval_samples)+1)*self.interval_samples)
        event.update(segment_id=job.segment_id, audio_start_ms=job.start_sample/16,
                     endpoint_continuation=True, continuation_from_temporary_id=provisional_id,
                     continuation_gap_ms=(active['last_voice_end']-job.last_voice_end_sample)/16,
                     extra_audio_wait_ms=0)
        self._continued[id(job)] = dict(resumed_at_sample=self._cursor,
                                        acoustic_gap_ms=(active['last_voice_end']-job.last_voice_end_sample)/16)
        self.stats['endpoint_continuations'] += 1
        self._tentative = None

    def _emit(self, event):
        """Publish an overlapped physical final as provisional when the continuation has already started."""
        if event['type'] == 'speech.started':
            self._resume(event)
        if event['type'] == 'transcript.final':
            job = self._decoding_job
            continuation = self._continued.get(id(job))
            if continuation is not None:
                self._finalized.discard(event['segment_id'])
                event = {**event, 'type': 'transcript.partial', 'is_final': False,
                         'endpoint_provisional': True, 'endpoint_continuation': continuation,
                         'extra_audio_wait_ms': 0}
                event.pop('_last_voice_received', None)
                # The base class increments completed_finals immediately after
                # this call. Account for this decode as a provisional result.
                self.stats['completed_finals'] -= 1
                self.stats['completed_partials'] += 1
                self.stats['endpoint_provisional_decodes'] += 1
            elif self._tentative is job:
                self._tentative = None
        super()._emit(event)

    async def _decode(self, job):
        """Restore prior acoustic history if the current physical decode was reclassified as provisional."""
        helper = getattr(self.asr, '_continuity', None)
        previous_history = copy.deepcopy(helper.history) if helper is not None else None
        self._decoding_job = job
        try:
            await super()._decode(job)
        finally:
            self._decoding_job = None
            if self._continued.pop(id(job), None) is not None and helper is not None:
                # A provisional endpoint cannot seed future acoustic prefixes.
                helper.history[:] = previous_history
