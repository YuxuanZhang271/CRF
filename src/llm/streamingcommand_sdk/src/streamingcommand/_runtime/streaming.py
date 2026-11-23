"""Nonblocking PCM reception, causal VAD and one Qwen inference scheduler."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import importlib
from pathlib import Path
import sys
import time

try:
    from .asr_backend import encode_pcm16
    from .acoustic_vad import DEFAULT_GATE_CONFIG, AcousticGateVAD
    from .decoding import decode_final
except ImportError:
    from asr_backend import encode_pcm16
    from acoustic_vad import DEFAULT_GATE_CONFIG, AcousticGateVAD
    from decoding import decode_final

SAMPLE_RATE = 16000
FRAME_SAMPLES = 320
FRAME_BYTES = FRAME_SAMPLES * 2
DEFAULT_PARTIAL_QUIET_MS = 40


def make_vad(mode=2):
    """Create the WebRTC VAD used for PCM16 speech decisions."""
    if mode not in (0, 1, 2, 3):
        raise ValueError("WebRTC VAD mode must be 0, 1, 2 or 3")
    module = importlib.import_module("webrtcvad")
    return module.Vad(mode)


@dataclass
class _Job:
    kind: str
    segment_id: int
    pcm: bytes
    start_sample: int
    end_sample: int
    enqueued: float
    last_voice_received: float
    last_voice_end_sample: int
    language_prefetch: object = None


class StreamingSession:
    def __init__(self, asr, emit_async, interval_ms=800, endpoint_ms=240, *, vad=None,
                 max_segment_ms=8000, partial_quiet_ms=DEFAULT_PARTIAL_QUIET_MS,
                 first_partial_ms=None):
        """Initialize the streaming scheduler, VAD framing, decode queues, and per-segment revision state."""
        if interval_ms < 20 or endpoint_ms < 20 or max_segment_ms < interval_ms:
            raise ValueError("Intervals must be >=20ms and max segment >= ASR interval")
        self.asr, self.emit_async = asr, emit_async
        self.interval_ms, self.endpoint_ms = interval_ms, endpoint_ms
        self.interval_samples = round(interval_ms * SAMPLE_RATE / 1000)
        self.first_partial_ms = (min(interval_ms, getattr(asr, 'first_partial_ms', interval_ms))
                                 if first_partial_ms is None else first_partial_ms)
        if not 20 <= self.first_partial_ms <= max_segment_ms:
            raise ValueError('First partial must be >=20ms and <=max segment')
        self.first_partial_samples = round(self.first_partial_ms * SAMPLE_RATE / 1000)
        self.endpoint_samples = round(endpoint_ms * SAMPLE_RATE / 1000)
        self.max_segment_samples = round(max_segment_ms * SAMPLE_RATE / 1000)
        if partial_quiet_ms < 0:
            raise ValueError("Partial quiet delay must be nonnegative")
        self.partial_quiet_samples = round(partial_quiet_ms * SAMPLE_RATE / 1000)
        self.vad = (AcousticGateVAD(make_vad(2), getattr(asr, 'vad_gate_config', DEFAULT_GATE_CONFIG))
                    if vad is None else vad)
        self._ring = deque(maxlen=10)
        self._flags = deque(maxlen=3)
        self._pending = bytearray()
        self._cursor = 0
        self._active = None
        self._next_id = 0
        self._finals = deque()
        self._partial = None
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._outgoing = asyncio.Queue()
        self._worker = self._publisher = None
        self._started = self._finishing = self._closed = False
        self._closing = False
        self._error = self._emit_error = None
        self._sdk_states = {}
        self._language_tasks = set()
        self._sdk_active = None
        self._inference_busy = False
        self._partial_ready_at = 0.0
        self._texts, self._revisions, self._finalized = {}, {}, set()
        self.stats = {"received_samples": 0, "frames": 0, "partial_requests": 0,
                      "final_requests": 0, "coalesced_partials": 0,
                      "completed_partials": 0, "completed_finals": 0}
        self.stats["partials_deferred_for_quiet_tail"] = 0
        self.stats.update(language_prefetch_started=0, language_prefetch_reused=0)
        self.stats['partials_deferred_for_compute'] = 0

    def _identity(self):
        """Read the ASR backend and fake status for outgoing event metadata."""
        status = getattr(self.asr, "status", {})
        return {"backend": status.get("backend", getattr(self.asr, "backend", "unknown")),
                "is_fake": status.get("is_fake", getattr(self.asr, "is_fake", False))}

    def _emit(self, event):
        """Enqueue an event unless the session is closed or its publisher has failed."""
        if not self._closed and self._emit_error is None:
            self._outgoing.put_nowait({**self._identity(), **event})

    async def _publish(self):
        """Deliver events in order, attach final latency, and record downstream publishing failures."""
        try:
            while True:
                event = await self._outgoing.get()
                try:
                    estimated_end = event.pop("_last_voice_received", None)
                    if estimated_end is not None:
                        event["server_end_to_final_ms"] = (time.monotonic() - estimated_end) * 1000
                    await self.emit_async(event)
                finally:
                    self._outgoing.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._emit_error = error
        finally:
            while not self._outgoing.empty():
                self._outgoing.get_nowait()
                self._outgoing.task_done()

    async def start(self):
        """Ensure the ASR backend is ready, reset audio-session state, and start decoding and publishing
        tasks.
        """
        if self._started:
            return
        if self._closed:
            raise RuntimeError("Create a new session after close")
        await self.asr.start()
        begin_audio_session=getattr(self.asr,'begin_audio_session',None)
        if begin_audio_session is not None:begin_audio_session()
        self._started = True
        self._publisher = asyncio.create_task(self._publish())
        self._worker = asyncio.create_task(self._infer())

    async def feed(self, pcm16_bytes):
        """Buffer received PCM, frame it for VAD, and propagate earlier decode or publish errors."""
        if not self._started or self._finishing or self._closed:
            raise RuntimeError("Session is not accepting audio")
        if self._error is not None:
            raise RuntimeError("ASR session failed") from self._error
        if not isinstance(pcm16_bytes, (bytes, bytearray, memoryview)):
            raise TypeError("feed expects mono 16k PCM16 bytes")
        if len(pcm16_bytes) % 2:
            raise ValueError("PCM16 byte count must be even")
        observe_audio=getattr(self.asr,'observe_audio',None)
        if observe_audio is not None:observe_audio(pcm16_bytes)
        self.stats["received_samples"] += len(pcm16_bytes) // 2
        self._pending.extend(pcm16_bytes)
        while len(self._pending) >= FRAME_BYTES:
            frame = bytes(self._pending[:FRAME_BYTES])
            del self._pending[:FRAME_BYTES]
            self._frame(frame, FRAME_SAMPLES)
        # No model or network await: the audio receive loop remains independent.

    def _frame(self, frame, valid_samples):
        """Update pre-roll and speech state, triggering coalesced partials or a final at configured
        boundaries.
        """
        start = self._cursor
        self._cursor += valid_samples
        end = self._cursor
        now = time.monotonic()
        # Optional acoustic wrappers need the segment state for hysteresis.
        # Active speech uses the original VAD; the default wrapper gates only
        # onset after enough idle history has been observed in the background.
        if hasattr(self.vad, "set_active"):
            self.vad.set_active(self._active is not None)
        speech = self.vad.is_speech(frame, SAMPLE_RATE)
        actual_pcm = frame[:valid_samples * 2]
        self.stats["frames"] += 1
        if self._active is None:
            self._ring.append((start, end, actual_pcm, speech, now))
            self._flags.append(speech)
            if len(self._flags) == 3 and sum(self._flags) >= 2:
                voiced = [item for item in self._ring if item[3]]
                self._active = {"id": self._next_id, "start": self._ring[0][0],
                    "pcm": bytearray(b"".join(item[2] for item in self._ring)),
                    "last_voice_end": voiced[-1][1], "last_voice_received": voiced[-1][4],
                    "next_partial": self.first_partial_samples}
                self._next_id += 1
                self._ring.clear()
                self._flags.clear()
                self._emit({"type": "speech.started", "segment_id": self._active["id"],
                    "audio_start_ms": self._active["start"] * 1000 / SAMPLE_RATE,
                    "audio_end_ms": end * 1000 / SAMPLE_RATE, "interrupt_requested": True})
            else:
                return
        else:
            self._active["pcm"].extend(actual_pcm)
            if speech:
                self._active["last_voice_end"] = end
                self._active["last_voice_received"] = now
        active = self._active
        duration = end - active["start"]
        refresh_language=getattr(self.asr,'refresh_acoustic_language_on_new_voice',False)
        if (refresh_language and active.get('language_prefetch_attempted')
                and active.get('language_prefetch_voice_end')!=active['last_voice_end']):
            # An intra-utterance pause is not the final tail. Keep draining the
            # old CPU request, then allow one fresh query at the next pause.
            active['language_prefetch_attempted']=False
        previous_language_task=active.get('language_prefetch')
        if (getattr(self.asr, 'prefetch_acoustic_language', False)
                and end - active['last_voice_end'] >= 80 * 16
                and not active.get('language_prefetch_attempted')
                and (not refresh_language or previous_language_task is None
                     or previous_language_task.done())):
            # Use waiting time that already belongs to the acoustic endpoint.
            # A later voiced frame invalidates this hint; no extra audio wait.
            active['language_prefetch_attempted'] = True
            active['language_prefetch_voice_end'] = active['last_voice_end']
            window_classifier=getattr(self.asr,'acoustic_language_for_window',None)
            if window_classifier is not None:
                query=window_classifier(bytes(active['pcm']),start_sample=active['start'],end_sample=end)
            else:
                query=self.asr.acoustic_language_for_final(bytes(active['pcm']))
            task = asyncio.create_task(query)
            active['language_prefetch'] = task
            self._language_tasks.add(task)
            self.stats['language_prefetch_started'] += 1
        if end - active["last_voice_end"] >= self.endpoint_samples:
            self._submit_final("vad_silence")
        elif duration >= self.max_segment_samples:
            self._submit_final("max_segment")
        elif duration >= active["next_partial"]:
            quiet = end - active["last_voice_end"]
            if self.partial_quiet_samples and quiet >= self.partial_quiet_samples:
                # Keep receiving every frame. A speech resumption can send
                # the pending update immediately; a quiet endpoint goes
                # straight to fresh final instead of starting another decode.
                self.stats["partials_deferred_for_quiet_tail"] += 1
            else:
                pacing = getattr(self.asr, 'partial_compute_rest_fraction', 0)
                if pacing and (self._inference_busy or now < self._partial_ready_at):
                    self.stats['partials_deferred_for_compute'] += 1
                else:
                    self._submit("partial")
                    while active["next_partial"] <= duration:
                        active["next_partial"] += self.interval_samples

    def _submit(self, kind):
        """Freeze active PCM and timing as a decode job and coalesce superseded partials."""
        active = self._active
        pcm = bytes(active["pcm"])
        job = _Job(kind, active["id"], pcm, active["start"], self._cursor,
                   time.monotonic(), active["last_voice_received"], active["last_voice_end"])
        if (kind == 'final' and active.get('language_prefetch_voice_end') == active['last_voice_end']):
            job.language_prefetch = active.get('language_prefetch')
        if len(pcm) // 2 != job.end_sample - job.start_sample:
            raise AssertionError("PCM segment has a sample gap")
        if kind == "final":
            self._finals.append(job)
            self.stats["final_requests"] += 1
            if self._partial is not None and self._partial.segment_id == job.segment_id:
                self._partial = None
        else:
            if self._partial is not None:
                self.stats["coalesced_partials"] += 1
            self._partial = job
            self.stats["partial_requests"] += 1
        self._idle.clear()
        self._wake.set()

    def _submit_final(self, reason):
        """Queue the active segment's final and release its segmentation state."""
        active = self._active
        self._emit({"type": "speech.stopped", "segment_id": active["id"],
                    "audio_start_ms": active["start"] * 1000 / SAMPLE_RATE,
                    "audio_end_ms": self._cursor * 1000 / SAMPLE_RATE,
                    "vad_estimated_speech_end_ms": active["last_voice_end"] * 1000 / SAMPLE_RATE,
                    "reason": reason})
        self._submit("final")
        self._active = None
        self._ring.clear()
        self._flags.clear()

    async def _decode(self, job):
        """Send one audio job to the backend and emit a revision with coverage and generation metadata."""
        started = time.monotonic()
        queued_ms = (started - job.enqueued) * 1000
        final = job.kind == "final"
        requested_language = getattr(self.asr, "language", "English")
        initial_language = requested_language
        language_probe, decoding_passes = None, None
        initial_detected_language = None
        short_update=None
        if not final and getattr(self.asr,'decode_partial',None) is not None:
            short_update=await self.asr.decode_partial(job.segment_id,job.pcm,
                start_sample=job.start_sample,end_sample=job.end_sample)
        if final:
            # A fresh full-audio final does not use the SDK's partial state.
            # Updating its remaining buffer first would decode the same audio
            # twice and could delay an otherwise short final by seconds.
            async def publish_probe(response):
                """Emit diagnostic information for a backend probe without treating it as a transcript
                revision.
                """
                self._emit({"type": "asr.language_probe", "segment_id": job.segment_id,
                            "requested_language": initial_language,
                            "audio_start_ms": job.start_sample * 1000 / SAMPLE_RATE,
                            "audio_end_ms": job.end_sample * 1000 / SAMPLE_RATE,
                            "response": response})
            state = self._sdk_states.get(job.segment_id, {})
            acoustic_hint = None
            if job.language_prefetch is not None:
                acoustic_hint = {**await job.language_prefetch, 'prefetched': True,
                    'same_vad_speech_end_sample': job.last_voice_end_sample,
                    'final_input_samples': len(job.pcm) // 2}
                self.stats['language_prefetch_reused'] += 1
            custom_decoder=getattr(self.asr,'decode_final',None)
            async def final_decoder(*args,**kwargs):
                """Run the backend's profile-specific final decoder with the current segment's sample
                range.
                """
                if custom_decoder is not None:
                    return await custom_decoder(*args,start_sample=job.start_sample,
                                                end_sample=job.end_sample,**kwargs)
                return await decode_final(self.asr,*args,**kwargs)
            decoded = await final_decoder(job.segment_id, job.pcm,
                                         on_probe=publish_probe,
                                         should_retry=lambda: not self._closing,
                                         streaming_language_hint=state.get("language_hint"),
                                         prefetched_acoustic_hint=acoustic_hint)
            result = decoded["result"]
            generations, request_ops = decoded["generations"], decoded["request_ops"]
            requested_language = decoded["requested_language"]
            initial_language = decoded["initial_language"]
            initial_detected_language = decoded["initial_detected_language"]
            language_probe, decoding_passes = decoded["language_probe"], decoded["decoding_passes"]
            # The worker has one streaming state. The next partial always
            # starts a fresh state, including when a newer segment was queued.
            self._sdk_states.clear()
            self._sdk_active = None
        elif short_update is not None:
            result=short_update['result']
            requested_language=initial_language=short_update['requested_language']
            request_ops=['transcribe']
            generations=[{**g,'source_op':'transcribe','causal_partial_snapshot':True}
                         for g in result.get('generations',[])]
        else:
            request_ops = []
            state = self._sdk_states.get(job.segment_id)
            if state is None or self._sdk_active != job.segment_id or len(job.pcm) < state["fed_bytes"]:
                await self.asr.request("start", segment_id=job.segment_id,
                    context=getattr(self.asr, "context", ""), language=requested_language,
                    chunk_size_sec=min(self.first_partial_ms, self.interval_ms) / 1000,
                    unfixed_chunk_num=2, unfixed_token_num=5)
                request_ops.append("start")
                state = {"fed_bytes": 0}
                self._sdk_states[job.segment_id] = state
                self._sdk_active = job.segment_id
                if self._closing:
                    return
            delta = job.pcm[state["fed_bytes"]:]
            result = await self.asr.request("update", segment_id=job.segment_id,
                                           audio_float32_b64=encode_pcm16(delta))
            request_ops.append("update")
            state["fed_bytes"] = len(job.pcm)
            if requested_language is None:
                state["language_hint"] = {"segment_id": job.segment_id,
                    "native_language": result.get("detected_language"),
                    "text_nonempty": bool(result.get("text")),
                    "audio_processed_samples": result.get("audio_processed_samples", 0),
                    "reusable_prefix_tokens": result.get("next_prefix_tokens", 0)}
            generations = [{**g, "source_op": "update"}
                           for g in result.get("generations", [])]
        if self._closing:
            return
        samples = int(result.get("audio_processed_samples", len(job.pcm) // 2))
        if not 0 <= samples <= len(job.pcm) // 2:
            raise AssertionError("ASR used audio outside the received waveform")
        if final and samples != len(job.pcm) // 2:
            raise AssertionError("Fresh final did not cover the complete received segment")
        text = result.get("text", "")
        previous = self._texts.get(job.segment_id, "")
        offset = 0
        while offset < min(len(text), len(previous)) and text[offset] == previous[offset]:
            offset += 1
        revision = self._revisions.get(job.segment_id, 0) + 1
        self._texts[job.segment_id], self._revisions[job.segment_id] = text, revision
        if final:
            self._finalized.add(job.segment_id)
        inference_ms = (time.monotonic() - started) * 1000
        event = {"type": "transcript.final" if final else "transcript.partial",
            "segment_id": job.segment_id, "text": text,
            "raw_decoded": result.get("raw_decoded", text),
            "raw_decoded_chunks": result.get("raw_decoded_chunks", []),
            "generations": generations,
            "request_ops": request_ops,
            "generation_count": len(generations),
            "audio_input_samples": len(job.pcm) // 2,
            "audio_processed_samples": samples,
            "complete_audio_coverage": samples == len(job.pcm) // 2,
            "update_before_final_raw": None,
            "revision": revision, "is_final": final, "text_changed": text != previous,
            "replace_from": offset, "replacement": text[offset:],
            "retracted_characters": len(previous) - offset,
            "audio_start_ms": job.start_sample * 1000 / SAMPLE_RATE,
            "audio_end_ms": (job.start_sample + samples) * 1000 / SAMPLE_RATE,
            "inference_ms": inference_ms, "queue_ms": queued_ms,
            "server_end_to_final_ms": None,
            "vad_estimated_speech_end_ms": job.last_voice_end_sample * 1000 / SAMPLE_RATE,
            "latency_basis": "server_receive_time_of_last_vad_speech_frame_not_ground_truth",
            "requested_language": requested_language,
            "initial_language": initial_language,
            "initial_detected_language": initial_detected_language,
            "language_probe": language_probe,
            "decoding_passes": decoding_passes,
            "language_policy_source": (decoded["language_policy_source"] if final else
                short_update['language_policy_source'] if short_update is not None else
                "streaming_auto" if requested_language is None else "configured"),
            "streaming_language_hint": decoded["streaming_language_hint"] if final else None,
            "acoustic_language_hint": (decoded.get('acoustic_language_hint') if final else
                short_update.get('acoustic_language_hint') if short_update is not None else None),
            "language_mode": "forced" if requested_language is not None else "auto",
            "detected_language": result.get("detected_language"),
            "selected_asr_model":result.get('selected_model_dir'),
            "acoustic_continuity":result.get('acoustic_continuity'),
            "language_label_is_forced": requested_language is not None}
        if final:
            event["_last_voice_received"] = job.last_voice_received
        self._emit(event)
        self.stats["completed_finals" if final else "completed_partials"] += 1
        self._emit({"type": "metrics", **self.stats,
                    "audio_received_ms": self.stats["received_samples"] * 1000 / SAMPLE_RATE,
                    "pending_final_requests": len(self._finals),
                    "pending_partial_requests": int(self._partial is not None)})

    async def _infer(self):
        """Drain decode jobs with priority for finals and notify waiters when work is idle."""
        try:
            while not self._closing:
                if self._finals:
                    job = self._finals.popleft()
                elif self._partial is not None:
                    job, self._partial = self._partial, None
                else:
                    self._idle.set()
                    self._wake.clear()
                    await self._wake.wait()
                    continue
                if job.segment_id in self._finalized:
                    continue
                self._inference_busy = True
                started = time.monotonic()
                try:
                    await self._decode(job)
                finally:
                    self._inference_busy = False
                    if job.kind == 'partial':
                        self._partial_ready_at = time.monotonic() + min(0.8,
                            (time.monotonic() - started) * getattr(self.asr, 'partial_compute_rest_fraction', 0))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._error = error
            self._emit({"type": "error", "error": str(error)})
            self._finals.clear()
            self._partial = None
        finally:
            self._idle.set()

    async def finish(self):
        """Flush the remaining PCM, wait for queued finals, and drain the event publisher."""
        if self._closed:
            return
        if not self._started:
            return
        self._finishing = True
        if self._pending:
            tail = bytes(self._pending)
            self._pending.clear()
            self._frame(tail + bytes(FRAME_BYTES - len(tail)), len(tail) // 2)
        if self._active is not None:
            self._submit_final("end_of_stream")
        await self._idle.wait()
        await self._outgoing.join()
        if self._error is not None:
            raise RuntimeError("ASR session failed") from self._error
        if self._emit_error is not None:
            raise RuntimeError("Transcript delivery failed") from self._emit_error

    async def close(self):
        """Stop the session tasks and language prefetches while leaving the shared model available."""
        if self._closed:
            return
        self._closed = self._closing = True
        self._active = None
        self._pending.clear()
        self._finals.clear()
        self._partial = None
        self._wake.set()
        if self._publisher is not None:
            self._publisher.cancel()
            try:
                await self._publisher
            except asyncio.CancelledError:
                pass
        try:
            if self._worker is not None:
                # Finish only the already-running exchange and discard queued
                # work before the server releases its single-session lock.
                # The global model stays resident until server lifespan ends.
                await self._worker
        finally:
            self._sdk_states.clear()
            self._sdk_active = None
            if self._language_tasks:
                await asyncio.gather(*self._language_tasks, return_exceptions=True)
                self._language_tasks.clear()
