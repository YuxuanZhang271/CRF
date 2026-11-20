import asyncio
import sys
from pathlib import Path
import pytest
from streamingcommand import StreamingASR, StreamingCommand, StreamingConfig
from streamingcommand._runtime.asr_backend import FakeASR


class VAD:
    def is_speech(self, frame, rate):
        """Treat nonzero PCM in a test frame as a positive VAD decision."""
        return frame[0:2] != b"\0\0"


def config():
    """Create a short-endpoint test config with command inference disabled."""
    value = StreamingConfig(interval_ms=800, endpoint_ms=120)
    value.command.enabled = False
    return value


@pytest.mark.asyncio
async def test_final_drain_and_one_active_session():
    """Check that one recognizer rejects concurrent sessions and delivers final events before completion."""
    asr = FakeASR()
    async with StreamingASR(config(), asr_backend=asr) as runtime:
        async with runtime.session(vad=VAD()) as session:
            collected = []
            async def consume():
                """Drain package events into the test's ordered event trace."""
                async for event in session.events():
                    collected.append(event)
            consumer = asyncio.create_task(consume())
            with pytest.raises(RuntimeError, match="Only one"):
                await runtime.session(vad=VAD()).start()
            await session.feed(b"\x01\x01" * 16000)
            await session.feed(bytes(16000))
            await session.finish()
            await consumer
            finals = [e for e in collected if e["type"] == "transcript.final"]
            assert len(finals) == 1
            assert finals[0]["complete_audio_coverage"] is True
            assert collected[-1]["type"] == "session.completed"
        assert not runtime._lock.locked()
    assert asr.status["state"] == "closed"


@pytest.mark.asyncio
async def test_source_error_and_early_stop_cleanup():
    """Check that errors in an async PCM producer propagate and release the recognizer's session claim."""
    runtime = StreamingASR(config(), asr_backend=FakeASR())
    async def broken_source():
        """Yield audio and then raise a deterministic source failure."""
        yield bytes(640)
        raise ValueError("input failed")
    with pytest.raises(ValueError, match="input failed"):
        async for event in runtime.stream(broken_source()):
            pass
    assert not runtime._lock.locked()
    await runtime.close()


@pytest.mark.asyncio
async def test_bounded_queue_cancellation_releases():
    """Verify that cancelling a session releases it even when pending events reach the queue limit."""
    value = config()
    value.event_queue_size = 1
    runtime = StreamingASR(value, asr_backend=FakeASR())
    await runtime.start()
    session = runtime.session(vad=VAD())
    await session.start()
    await session.feed(b"\x01\x01" * 16000)
    await asyncio.sleep(.02)
    await asyncio.wait_for(session.close(), 2)
    assert not runtime._lock.locked()
    await runtime.close()


@pytest.mark.asyncio
async def test_invalid_pcm_and_repeated_sessions():
    """Reject odd PCM byte lengths and prove the same recognizer can start a new session after cleanup."""
    async with StreamingASR(config(), asr_backend=FakeASR()) as runtime:
        for _ in range(2):
            async with runtime.session(vad=VAD()) as session:
                with pytest.raises(ValueError, match="PCM16"):
                    await session.feed(b"x")
                drain = asyncio.create_task(session.finish())
                assert [e["type"] async for e in session.events()][-1] == "session.completed"
                await drain


@pytest.mark.asyncio
async def test_history_and_llm_are_integrated():
    """Verify that resolved final commands establish H1 for the next turn."""
    class Parser:
        default_candidates = {"I": ["bend"], "T": ["none"], "P": ["none", "left"]}
        status = {"ready": True, "max_history_depth": 1}
        histories = []
        # Provide the no-op startup hook expected by the package's injected parser.
        async def start(self): pass
        # Provide the no-op shutdown hook expected by the package's injected parser.
        async def close(self): pass
        async def parse(self, text, candidates, history):
            """Record each supplied history and return a fixed resolved action."""
            self.histories.append(dict(history))
            return {"command": "Y", "resolved": True, "slots": {"I": "bend", "T": "none", "P": "left"}}
    parser = Parser()
    runtime = StreamingCommand(config(), asr_backend=FakeASR(), command_backend=parser)
    async with runtime:
        async with runtime.session(vad=VAD()) as session:
            events = []
            async def consume():
                """Collect the combined transcript and command event stream until completion."""
                async for e in session.events(): events.append(e)
            consumer = asyncio.create_task(consume())
            for _ in range(2):
                await session.feed(b"\x01\x01" * 16000 + bytes(16000))
                await session.audio._idle.wait()
                await session.audio._outgoing.join()
                await session.commands.finish()
            await session.finish()
            await consumer
            finals = [e for e in events if e["type"] == "command.final"]
            assert len(finals) == 2
            assert finals[-1]["history_after"] == {"H1": "I=bend;T=none;P=left"}
            assert any(h.get("H1") for h in parser.histories)


def test_import_is_lightweight_in_fresh_process():
    """Check in a clean interpreter that importing the package does not import Torch or Transformers."""
    import subprocess
    result = subprocess.run([sys.executable, "-c", "import streamingcommand,sys; assert 'torch' not in sys.modules; assert 'transformers' not in sys.modules"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_owner_close_stops_source_even_when_iterator_is_retained():
    """Check that closing the SDK also closes its PCM producer even while the event iterator remains
    referenced.
    """
    closed = asyncio.Event()
    async def source():
        """Yield continuous silence and signal when cancellation closes the producer."""
        try:
            while True:
                yield bytes(640)
                await asyncio.sleep(.01)
        finally:
            closed.set()
    runtime = StreamingASR(config(), asr_backend=FakeASR())
    stream = runtime.stream(source())
    assert (await anext(stream))["type"] == "session.started"
    await asyncio.wait_for(runtime.close(), 2)
    assert closed.is_set()
    await stream.aclose()


@pytest.mark.asyncio
async def test_asr_only_accepts_a_command_enabled_config():
    """Verify that the ASR-only API ignores semantic-pair settings when starting without a command model."""
    value = config()
    value.command.recognition_logic = "semantic-pair"
    async with StreamingASR(value, asr_backend=FakeASR()) as runtime:
        async with runtime.session(vad=VAD()) as session:
            assert session.commands is None


@pytest.mark.asyncio
async def test_close_during_backend_construction_releases_the_started_backend(monkeypatch):
    """Reproduce close during resource resolution and verify that the eventually started backend is closed.
    """
    from streamingcommand import StreamingCommand

    started = asyncio.Event()
    resume = asyncio.Event()
    backend = FakeASR()

    async def build(_):
        """Block fake backend construction until the test has started recognizer shutdown."""
        started.set()
        await resume.wait()
        return backend, None

    monkeypatch.setattr("streamingcommand.backends.build_backends", build)
    recognizer = StreamingCommand()
    startup = asyncio.create_task(recognizer.start())
    await started.wait()
    shutdown = asyncio.create_task(recognizer.close())
    await asyncio.sleep(0)
    assert not shutdown.done()
    resume.set()
    await asyncio.gather(startup, shutdown)
    assert not recognizer.info()["ready"]
    assert backend.status["state"] == "closed"


def test_worker_selects_only_from_inherited_cuda_devices(monkeypatch):
    """Exercise CUDA ordinal remapping and reject devices outside the parent mask without touching a GPU."""
    from streamingcommand._runtime import device_policy

    env = {"CUDA_VISIBLE_DEVICES": "2,0"}
    assert device_policy.worker_visibility("cuda:0", env) == "2"
    assert device_policy.worker_visibility("cuda:1", env) == "0"
    assert device_policy.worker_visibility("cpu", env) == ""
    with pytest.raises(ValueError, match="outside"):
        device_policy.worker_visibility("cuda:2", env)
    with pytest.raises(ValueError, match="outside"):
        device_policy.worker_visibility("cuda:0", {"CUDA_VISIBLE_DEVICES": ""})
    monkeypatch.setattr(device_policy, "DEVICE", "cuda:0")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "2")
    device_policy.pin_device()
    assert device_policy.os.environ["CUDA_VISIBLE_DEVICES"] == "2"


def test_asr_respects_existing_deployment_lock_and_preserves_metadata(tmp_path):
    """Verify mutual exclusion with the existing deployment lock and rollback of the newly acquired lease."""
    fcntl = pytest.importorskip("fcntl")
    from streamingcommand._runtime.asr_instance import ASRInstanceLease

    previous = tmp_path / "old-asr.lock"
    current = tmp_path / "package.lock"
    previous.write_text("existing owner")
    lease = ASRInstanceLease("cpu", lock_path=current, compatibility_lock_file=previous)
    with previous.open("r+") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="existing deployment"):
            lease.acquire()
        assert previous.read_text() == "existing owner"
        independent = ASRInstanceLease("cpu", lock_path=current)
        independent.acquire()
        independent.release()
    lease.acquire()
    lease.release()
