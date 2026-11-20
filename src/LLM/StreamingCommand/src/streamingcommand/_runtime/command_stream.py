"""Revision-aware semantic requests, independent of the ASR publisher."""
from __future__ import annotations

import asyncio
import copy
from collections import OrderedDict, deque
import time
import uuid

from .command_backend import validate_candidates, validate_history


class CommandSession:
    def __init__(self, backend, emit, candidates, history=None):
        """Create the revision-aware command queue and initialize candidate, history, and clarification
        state.
        """
        self.backend, self.emit = backend, emit
        self.candidates = getattr(backend, "prepare_candidates", validate_candidates)(candidates)
        self.history = getattr(backend, "prepare_history", validate_history)(history)
        self.candidate_version = 0
        self._finals, self._partials = deque(), OrderedDict()
        self._latest, self._finalized = {}, set()
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._closed = False
        self.session_id = uuid.uuid4().hex
        self._pending_clarification = None
        self._reply_bindings = {}
        self._feedback_in_progress = False
        self._operation_lock = asyncio.Lock()
        self._task = asyncio.create_task(self._worker())

    def configure(self, candidates):
        """Validate a new candidate pool and invalidate outstanding clarifications tied to the previous
        revision.
        """
        self.candidates = getattr(self.backend, "prepare_candidates", validate_candidates)(candidates)
        self.candidate_version += 1
        self._pending_clarification = None
        return {"candidates": copy.deepcopy(self.candidates), "candidate_version": self.candidate_version}

    def submit(self, event):
        """Queue an immutable candidate snapshot for a transcript or transfer a provisional reply binding."""
        if self._closed:
            return
        segment = event["segment_id"]
        if event.get("endpoint_provisional"):
            # The physical decode is retained in ASR logs, but its semantic
            # commit belongs to the continuation's final segment. Carry any
            # pending spoken-answer binding with it and suppress stale previews.
            target = event["endpoint_continues_in_segment_id"]
            reply = self._reply_bindings.pop(segment, None)
            if reply is not None:
                self._reply_bindings.setdefault(target, reply)
            else:
                self._bind_reply(target, event["text"])
            self._partials.pop(segment, None)
            self._finalized.add(segment)
            return
        final = event["type"] == "transcript.final"
        if segment in self._finalized:
            return
        revision = event["revision"]
        self._latest[segment] = revision
        job = {"event": copy.deepcopy(event), "candidates": copy.deepcopy(self.candidates),
               "candidate_version": self.candidate_version, "queued": time.perf_counter()}
        self._bind_reply(segment, event["text"])
        if final:
            self._finalized.add(segment)
            self._partials.pop(segment, None)
            self._finals.append(job)
        else:
            self._partials[segment] = job
        self._idle.clear()
        self._wake.set()

    def _bind_reply(self, segment, text):
        """Bind at most one nonempty transcript segment to the pending clarification ID."""
        pending = self._pending_clarification
        if pending is not None and text.strip() and not self._feedback_in_progress:
            token = pending["clarification_id"]
            if token not in self._reply_bindings.values():
                self._reply_bindings.setdefault(segment, token)

    async def _parse_command(self, event, job, history, final):
        """Dispatch a turn using a session-aware backend when available and commit only final transcripts."""
        if hasattr(self.backend, "parse_turn"):
            return await self.backend.parse_turn(event["text"], job["candidates"], history,
                session_id=self.session_id, commit=final)
        return await self.backend.parse(event["text"], job["candidates"], history)

    def _prediction_ready(self, event, result, final):
        """Provide an extension point for policies that track each accepted prediction."""
        pass

    async def _worker(self):
        """Prioritize finals, discard stale previews, and publish predictions with history and
        clarification updates.
        """
        try:
            while not self._closed:
                if self._finals:
                    job = self._finals.popleft()
                elif self._partials:
                    _, job = self._partials.popitem(last=False)
                else:
                    self._idle.set()
                    self._wake.clear()
                    await self._wake.wait()
                    continue
                async with self._operation_lock:
                    event = job["event"]
                    segment, revision = event["segment_id"], event["revision"]
                    final = event["type"] == "transcript.final"
                    if not final and (segment in self._finalized or self._latest[segment] != revision):
                        continue
                    history = copy.deepcopy(self.history)
                    queued_ms = (time.perf_counter()-job["queued"])*1000
                    started = time.perf_counter()
                    result = {"type": "command.final" if final else "command.partial", "segment_id": segment,
                              "revision": revision, "is_final": final, "text": event["text"],
                              "history": history, "candidates": job["candidates"],
                              "candidate_version": job["candidate_version"], "queue_ms": round(queued_ms, 3)}
                    pending = self._pending_clarification
                    self._bind_reply(segment, event["text"])
                    reply_id = self._reply_bindings.get(segment)
                    valid_reply = (pending is not None and reply_id == pending["clarification_id"]
                                   and pending["candidate_version"] == job["candidate_version"] == self.candidate_version)
                    try:
                        if not event["text"].strip():
                            prediction = {"command": None, "slots": {s: None for s in ("I", "T", "P")},
                                          "raw_output": "", "resolved": False, "scores": {},
                                          "skipped_reason": "empty_transcript", "backbone_calls": 0}
                        elif reply_id is not None:
                            result.update(clarification_reply_source="audio", answered_clarification_id=reply_id)
                            if not valid_reply:
                                prediction = {"command": None, "slots": dict.fromkeys(("I", "T", "P")),
                                              "resolved": False, "scores": {}, "backbone_calls": 0,
                                              "skipped_reason": "stale_clarification_reply"}
                            elif not final:
                                await self.emit({**result, "type": "command.feedback_partial", "backbone_calls": 0,
                                                 "clarification_id": reply_id, "history_after": history})
                                continue
                            else:
                                result["clarification_for_segment_id"] = pending["segment_id"]
                                self._feedback_in_progress = True
                                try:
                                    await self.emit({**result, "type": "command.feedback_processing"})
                                    prediction = await self.backend.feedback(self.session_id, event["text"], reply_id)
                                finally:
                                    self._feedback_in_progress = False
                        else:
                            prediction = await self._parse_command(event, job, history, final)
                        result.update(prediction, transcript_to_result_ms=round((time.perf_counter()-job["queued"])*1000, 3))
                    except Exception as exc:
                        result.update(type="command.error", message=str(exc), parse_ms=round((time.perf_counter()-started)*1000, 3))
                    if self._closed:
                        continue
                    if not final and (segment in self._finalized or self._latest[segment] != revision):
                        continue
                    self._prediction_ready(event, result, final)
                    if final and not result.get("skipped_reason") and result["type"] != "command.error":
                        self._pending_clarification = result if result.get("clarification_id") else None
                        self._update_history(result)
                    if final:
                        self._reply_bindings.pop(segment, None)
                    result["history_after"] = copy.deepcopy(self.history)
                    await self.emit(result)
        finally:
            self._idle.set()

    def _update_history(self, result):
        """Serialize resolved slots into H1 and retain H2 only when the backend permits it."""
        if result.get("resolved"):
            if self.backend.status.get("max_history_depth", 2) == 1:
                self.history.pop("H2", None)
            elif self.history.get("H1"):
                self.history["H2"] = self.history["H1"]
            self.history["H1"] = ";".join(f"{s}={result['slots'][s] if result['slots'][s] is not None else ''}" for s in ("I", "T", "P"))

    async def feedback(self, reply, clarification_id):
        """Drain queued turns before validating and applying a text reply to the current clarification."""
        await self.finish()
        async with self._operation_lock:
            pending = self._pending_clarification
            if (pending is None or pending.get("clarification_id") != clarification_id or
                    pending["candidate_version"] != self.candidate_version):
                raise ValueError("澄清已过期或候选已改变；请再次说 back 后回答")
            if not hasattr(self.backend, "feedback"):
                raise ValueError("当前模型不支持澄清记忆")
            self._feedback_in_progress = True
            try:
                result = await self.backend.feedback(self.session_id, reply, clarification_id)
            finally:
                self._feedback_in_progress = False
            self._pending_clarification = ({**pending, **result}
                if result.get("feedback_allowed") and result.get("clarification_id") else None)
            previous = copy.deepcopy(self.history)
            self._update_history(result)
            await self.emit({**result, "type": "command.feedback", "text": reply, "is_final": True,
                             "clarification_reply_source": "text", "answered_clarification_id": clarification_id,
                             "segment_id": pending["segment_id"], "revision": pending["revision"],
                             "history": previous, "history_after": copy.deepcopy(self.history),
                             "candidates": copy.deepcopy(self.candidates), "candidate_version": self.candidate_version,
                             "transcript_to_result_ms": result.get("parse_ms", 0)})

    async def clear_memory(self):
        """Drain queued turns, clear backend clarification state, and notify the event consumer."""
        await self.finish()
        async with self._operation_lock:
            if hasattr(self.backend, "clear_memory"):
                await self.backend.clear_memory(self.session_id)
            self._pending_clarification = None
            await self.emit({"type": "command.memory_cleared", "memory_active": False})

    async def finish(self):
        """Wait for queued command work to drain and propagate an ended worker's error."""
        await self._idle.wait()
        if self._task.done():
            await self._task

    async def close(self):
        """Discard queued turns, drain any active request, and release the backend's state for this
        session.
        """
        self._closed = True
        self._finals.clear()
        self._partials.clear()
        self._wake.set()
        # Let the owned backend finish an already running request. Discard its
        # result before the recording/session lock is released.
        await self._task
        if hasattr(self.backend, "end_session"):
            await self.backend.end_session(self.session_id)
