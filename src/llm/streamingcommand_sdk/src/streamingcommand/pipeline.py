"""Persistent engines, single-owner streaming sessions and asynchronous events."""
import asyncio
from contextlib import suppress
import copy
from pathlib import Path
from .config import CLARIFICATION_MODELS, StreamingConfig


class StreamingCommand:
    def __init__(self, config=None, *, config_path=None, asr_backend=None, command_backend=None):
        """Accept a config object or JSON path and establish ownership of any injected backends."""
        if config_path is not None:
            if config is not None:
                raise ValueError("Choose either config or config_path")
            config = Path(config_path)
        self.config = StreamingConfig.load(config) if isinstance(config, (str, Path)) else config or StreamingConfig()
        self.asr = asr_backend
        self.commands = command_backend
        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._session = None
        self._closed = False
        self._ready = False

    @classmethod
    async def initialize(cls, config_path=None, *, config=None):
        """Start a recognizer with defaults or a JSON file and return the ready instance."""
        return await cls(config, config_path=config_path).start()

    async def start(self):
        """Resolve and start the owned backends once, unwinding both if initialization fails."""
        async with self._start_lock:
            if self._closed:
                raise RuntimeError("Runtime is closed; create a new instance")
            if self._ready:
                return self
            try:
                if self.asr is None:
                    from .backends import build_backends
                    self.asr, self.commands = await build_backends(self.config)
                await self.asr.start()
                if self.commands is not None:
                    await self.commands.start()
                self._ready = True
            except BaseException:
                await self._close()
                raise
        return self

    async def __aenter__(self):
        """Start the recognizer and return the ready instance for an async context."""
        return await self.start()

    async def __aexit__(self, *exc):
        """Close the recognizer when the caller leaves the async context."""
        await self.close()

    async def close(self):
        """Wait for any in-progress startup before releasing the recognizer's resources."""
        async with self._start_lock:
            await self._close()

    async def _close(self):
        """Release the active session and both backends while the lifecycle lock is held."""
        if self._closed:
            return
        self._closed = True
        self._ready = False
        try:
            if self._session is not None:
                await self._session.close()
        finally:
            try:
                if self.asr is not None:
                    await self.asr.close()
            finally:
                if self.commands is not None:
                    await self.commands.close()

    def info(self):
        """Return a detached snapshot of readiness and backend status without starting models."""
        return {"implementation": "streamingcommand", "ready": self._ready,
                "asr": copy.deepcopy(self.asr.status) if self.asr else None,
                "command": copy.deepcopy(self.commands.status) if self.commands else None}

    def session(self, *, candidates=None, history=None, vad=None, interval_ms=None,
                endpoint_ms=None, max_segment_ms=None, recognition_logic=None):
        """Create a PCM session whose optional timing and recognition settings override the recognizer
        defaults.
        """
        return CommandStream(self, candidates=candidates, history=history, vad=vad,
                             interval_ms=interval_ms, endpoint_ms=endpoint_ms,
                             max_segment_ms=max_segment_ms, recognition_logic=recognition_logic)

    async def switch_model(self, model):
        """Select a supported command model while idle and resolve its configured checkpoint before loading
        it.
        """
        await self.start()
        if self.commands is None:
            raise RuntimeError("Command parsing is disabled")
        if self._lock.locked():
            raise RuntimeError("Close the active audio session before switching models")
        async with self._lock:
            if self.config.command.recognition_logic == "semantic-pair":
                if model not in CLARIFICATION_MODELS:
                    raise ValueError("Semantic-pair recognition requires a clarification model")
            if model in self.commands.specs and model != self.commands.status["model_id"]:
                from .resources import ModelStore
                store = ModelStore.from_config(self.config)
                checkpoint = await asyncio.to_thread(store.ensure, model)
                self.commands.specs[model].update(
                    checkpoint=str(checkpoint), checkpoint_source=store.source(model))
            return await self.commands.switch(model)

    async def stream(self, source, *, candidates=None, history=None):
        """Consume PCM16 mono16k source and yield independent transcript/command events."""
        async with self.session(candidates=candidates, history=history) as session:
            async def receive():
                """Feed PCM from the source, signal EOF or failure, and close an owned async iterator."""
                try:
                    async for pcm in source:
                        await session.feed(pcm)
                    await session.finish()
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    await session.fail(exc)
                finally:
                    if hasattr(source, "aclose"):
                        await source.aclose()
            receiver = asyncio.create_task(receive())
            session._producer = receiver
            try:
                async for event in session.events():
                    yield event
            finally:
                receiver.cancel()
                with suppress(asyncio.CancelledError):
                    await receiver

    async def listen(self, *, device=None, candidates=None, history=None):
        """Convert microphone input into PCM and yield transcript and command events."""
        from .audio import microphone
        async for event in self.stream(microphone(device), candidates=candidates, history=history):
            yield event

    async def transcribe_file(self, path, *, realtime=True, candidates=None, history=None):
        """Decode and resample a file to the streaming format, optionally feeding it in real time."""
        from .audio import audio_file
        async for event in self.stream(audio_file(path, realtime=realtime), candidates=candidates, history=history):
            yield event

    async def parse(self, text, *, candidates=None, history=None):
        """Parse one text turn outside an audio session, using the supplied candidates and history."""
        await self.start()
        if self.commands is None:
            raise RuntimeError("Command parsing is disabled")
        if self._lock.locked():
            raise RuntimeError("A streaming session is active; use its feedback API")
        async with self._lock:
            return await self.commands.parse(text, candidates or self.commands.default_candidates, history)


class StreamingASR(StreamingCommand):
    """ASR-only convenience API, with the identical audio and lifecycle interfaces."""
    def __init__(self, config=None, **kwargs):
        """Use the same configuration interface as StreamingCommand with command inference disabled."""
        super().__init__(copy.deepcopy(config), **kwargs)
        self.config.command.enabled = False


class CommandStream:
    def __init__(self, owner, *, candidates=None, history=None, vad=None, interval_ms=None,
                 endpoint_ms=None, max_segment_ms=None, recognition_logic=None):
        """Validate one session's timing overrides and allocate its bounded output queue."""
        self.owner = owner
        self.candidates, self.history, self.vad = candidates, history, vad
        config = owner.config
        self.interval_ms = config.interval_ms if interval_ms is None else interval_ms
        self.endpoint_ms = config.endpoint_ms if endpoint_ms is None else endpoint_ms
        self.max_segment_ms = config.max_segment_ms if max_segment_ms is None else max_segment_ms
        self.recognition_logic = config.command.recognition_logic if recognition_logic is None else recognition_logic
        if (any(type(value) is not int for value in (self.interval_ms, self.endpoint_ms, self.max_segment_ms))
                or min(self.interval_ms, self.endpoint_ms) < 20
                or not self.interval_ms <= self.max_segment_ms <= config.max_pending_audio_ms):
            raise ValueError("Invalid streaming intervals or audio buffer capacity")
        self.queue = asyncio.Queue(maxsize=owner.config.event_queue_size)
        self.audio = self.commands = None
        self._started = self._closed = self._finished = False
        self._owns_lock = False
        self._consumer = False
        self._failure = None
        self._producer = None

    async def __aenter__(self):
        """Start this audio session before accepting PCM from its caller."""
        await self.start()
        return self

    async def __aexit__(self, *exc):
        """Release this session's queues and ownership when leaving the async context."""
        await self.close()

    async def start(self):
        """Claim the recognizer, select its endpoint and command policies, and emit the effective session
        settings.
        """
        if self._started:
            return
        await self.owner.start()
        if self.owner._lock.locked():
            raise RuntimeError("Only one active audio session is supported")
        await self.owner._lock.acquire()
        self._owns_lock = True
        self.owner._session = self
        try:
            from ._runtime.streaming import StreamingSession
            from ._runtime.endpoint_commit_bounded import BoundedYueEndpointCommitSession
            from ._runtime.session_profiles import endpoint_policy
            from ._runtime.command_stream import CommandSession
            from ._runtime.semantic_pair import PairedCommandSession, validate_logic
            logic = validate_logic(self.recognition_logic if self.owner.commands is not None else "original", self.owner.commands)
            if self.owner.commands is not None:
                kind = PairedCommandSession if logic == "semantic-pair" else CommandSession
                self.commands = kind(self.owner.commands, self._emit, self.candidates or self.owner.commands.default_candidates, self.history)
            commit_policy = endpoint_policy(self.owner.asr, self.endpoint_ms)
            kind = BoundedYueEndpointCommitSession if commit_policy["continuation_enabled"] else StreamingSession
            self.audio = kind(self.owner.asr, self._transcript, interval_ms=self.interval_ms,
                endpoint_ms=self.endpoint_ms, max_segment_ms=self.max_segment_ms, vad=self.vad)
            await self.audio.start()
            self._started = True
            await self._emit({"type": "session.started", "sample_rate": 16000, "implementation": "streamingcommand",
                              "interval_ms": self.interval_ms, "endpoint_ms": self.endpoint_ms,
                              "max_segment_ms": self.max_segment_ms, "endpoint_commit": commit_policy,
                              "recognition_logic": logic})
        except BaseException:
            await self.close()
            raise

    async def _emit(self, event):
        """Queue an event with backpressure while the audio session is still open."""
        if not self._closed:
            await self.queue.put(event)

    async def _transcript(self, event):
        """Publish an ASR update and pass eligible transcript events to the command scheduler."""
        await self._emit(event)
        if self.commands and event.get("type") in {"transcript.partial", "transcript.final"}:
            self.commands.submit(event)

    async def feed(self, pcm):
        """Accept whole PCM16 samples or raise when queued audio or events exceed their limits."""
        if not self._started or self._closed or self._finished:
            raise RuntimeError("Audio session is not accepting input")
        if not isinstance(pcm, bytes) or len(pcm) % 2:
            raise ValueError("Expected little-endian PCM16 mono16k bytes")
        if len(pcm) > self.owner.config.max_pending_audio_ms * 32:
            raise BufferError("Audio packet exceeds the configured buffer capacity")
        # Backpressure is explicit. Audio is never silently dropped.
        pending = sum(len(job.pcm) for job in self.audio._finals)
        if self.audio._outgoing.qsize() > self.owner.config.event_queue_size:
            raise BufferError("Event consumer cannot keep up; stop or consume events faster")
        if pending + len(pcm) > self.owner.config.max_pending_audio_ms * 32:
            raise BufferError("ASR cannot keep up with incoming audio; stop or pace the source")
        await self.audio.feed(pcm)

    async def feedback(self, text, clarification_id):
        """Send a text answer to the current clarification without running ASR."""
        if self.commands is None:
            raise RuntimeError("Command parsing is disabled")
        await self.commands.feedback(text, clarification_id)

    async def clear_memory(self):
        """Clear pending clarification state through the command scheduler when command parsing is enabled.
        """
        if self.commands is None:
            raise RuntimeError("Command parsing is disabled")
        await self.commands.clear_memory()

    def configure_candidates(self, candidates):
        """Replace the current candidate pool and return its new revision."""
        if self.commands is None:
            raise RuntimeError("Command parsing is disabled")
        return self.commands.configure(candidates)

    async def finish(self):
        """Drain final ASR and command results before enqueueing completion and end-of-stream."""
        if self._finished:
            return
        self._finished = True
        await self.audio.finish()
        if self.commands:
            await self.commands.finish()
        await self._emit({"type": "session.completed"})
        await self.queue.put(None)

    async def fail(self, exc):
        """Store an input failure so the event consumer receives it after queued events."""
        self._failure = exc
        await self.queue.put(None)

    async def events(self):
        """Yield queued events to one consumer and surface any source failure at end-of-stream."""
        if self._consumer:
            raise RuntimeError("Only one event consumer is supported")
        self._consumer = True
        try:
            while True:
                event = await self.queue.get()
                if event is None:
                    if self._failure:
                        raise self._failure
                    return
                yield event
        finally:
            self._consumer = False

    async def close(self):
        """Cancel the input producer, unblock publishers, and release only this session's recognizer claim.
        """
        if self._closed:
            return
        self._closed = True
        if self._producer is not None and self._producer is not asyncio.current_task():
            self._producer.cancel()
            with suppress(asyncio.CancelledError):
                await self._producer
        # Draining the queue releases publishers when the consumer stops early.
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put_nowait(None)
        try:
            if self.audio:
                await self.audio.close()
        finally:
            try:
                if self.commands:
                    await self.commands.close()
            finally:
                if self._owns_lock:
                    self._owns_lock = False
                    self.owner._session = None
                    self.owner._lock.release()
