"""Race, PCM continuity and independent-repeat checks for opt-in endpoints."""
import asyncio
import base64
from types import SimpleNamespace
import unittest

from streamingcommand._runtime.endpoint_continuation import EndpointContinuationSession


class FrameVAD:
    def is_speech(self, frame, rate):
        """Treat any nonzero frame as speech for deterministic endpoint tests."""
        return frame[0] == 1


class ControlledASR:
    language = 'English'
    context = ''
    cantonese_final_language = None
    status = {'is_fake': True}

    def __init__(self):
        """Hold the first final behind an event and record sample counts for all ASR requests."""
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []
        self._continuity = SimpleNamespace(history=[])

    async def start(self):
        """Mark the controlled ASR fixture ready without loading a model."""
        pass

    async def request(self, op, **kwargs):
        """Record the decoded sample count and optionally block the first final until resumption is
        observed.
        """
        assert op == 'transcribe', op
        samples = len(base64.b64decode(kwargs['audio_float32_b64']))//4
        self.calls.append(samples)
        if len(self.calls) == 1:
            self.started.set()
            await self.release.wait()
        self._continuity.history.append({'samples': samples})
        return dict(text='same command', audio_processed_samples=samples,
                    detected_language='English', generations=[{'decode_ms': 1}])


class ContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def make(self, **kwargs):
        """Start a continuation session with the controlled ASR and collect its outgoing events."""
        asr = ControlledASR()
        events = []
        async def emit(event):
            """Append one segmentation or transcript event to the fixture's trace."""
            events.append(event)
        session = EndpointContinuationSession(asr, emit, interval_ms=800, endpoint_ms=240,
                                               max_segment_ms=16000, vad=FrameVAD(), **kwargs)
        await session.start()
        self.addAsyncCleanup(session.close)
        return asr, session, events

    async def frames(self, session, count, voice):
        """Feed the requested number of nonzero speech frames."""
        await session.feed((b'\1\0' if voice else b'\0\0') * (320*count))

    async def pause(self, session):
        """Feed the requested number of silent frames to trigger or extend the endpoint."""
        await self.frames(session, 12, False)

    async def test_inflight_resumption_is_provisional_then_one_full_pcm_final(self):
        """Verify that resumption reclassifies the in-flight final and retains all PCM in the assembled
        result.
        """
        asr, session, events = await self.make()
        await self.frames(session, 5, True)
        await self.pause(session)
        await asyncio.wait_for(asr.started.wait(), 1)
        await self.frames(session, 3, True)
        asr.release.set()
        await session.finish()
        finals = [e for e in events if e['type'] == 'transcript.final']
        probes = [e for e in events if e.get('endpoint_provisional')]
        self.assertEqual(len(finals), 1)
        self.assertEqual(len(probes), 1)
        self.assertEqual(finals[0]['audio_input_samples'], session.stats['received_samples'])
        self.assertEqual(asr._continuity.history, [{'samples': session.stats['received_samples']}])
        self.assertEqual(session.stats['completed_finals'], 1)
        self.assertEqual(session.stats['endpoint_wait_added_ms'], 0)

    async def test_completed_identical_commands_are_both_kept(self):
        """Require two independent commits when equal text was already finalized in the first segment."""
        asr, session, events = await self.make()
        asr.release.set()
        await self.frames(session, 5, True)
        await self.pause(session)
        await asyncio.wait_for(session._idle.wait(), 1)
        await session._outgoing.join()
        await self.frames(session, 5, True)
        await session.finish()
        self.assertEqual(len([e for e in events if e['type'] == 'transcript.final']), 2)
        self.assertEqual(session.stats['endpoint_continuations'], 0)

    async def test_slow_decoder_cannot_join_long_gap(self):
        """Check that decode delay cannot extend the allowed acoustic continuation window."""
        asr, session, events = await self.make()
        await self.frames(session, 5, True)
        await self.pause(session)
        await asyncio.wait_for(asr.started.wait(), 1)
        await self.frames(session, 12, False)
        await self.frames(session, 3, True)
        asr.release.set()
        await session.finish()
        self.assertEqual(len([e for e in events if e['type'] == 'transcript.final']), 2)
        self.assertEqual(session.stats['endpoint_continuations'], 0)

    async def test_original_silence_gap_pcm_is_retained(self):
        """Verify that resumed input retains the original inter-segment silence byte for byte."""
        asr, session, events = await self.make(continuation_gap_ms=600)
        await self.frames(session, 5, True)
        await self.pause(session)
        await asyncio.wait_for(asr.started.wait(), 1)
        await self.frames(session, 12, False)
        await self.frames(session, 3, True)
        asr.release.set()
        await session.finish()
        final = [e for e in events if e['type'] == 'transcript.final']
        self.assertEqual(len(final), 1)
        self.assertEqual(final[0]['audio_input_samples'], session.stats['received_samples'])

    async def test_max_segment_cut_is_never_joined(self):
        """Keep forced maximum-duration cuts independent even if speech resumes immediately."""
        asr, session, events = await self.make()
        await self.frames(session, 5, True)
        session._submit_final('max_segment')
        await asyncio.wait_for(asr.started.wait(), 1)
        await self.frames(session, 3, True)
        asr.release.set()
        await session.finish()
        self.assertEqual(len([e for e in events if e['type'] == 'transcript.final']), 2)
        self.assertEqual(session.stats['endpoint_continuations'], 0)


if __name__ == '__main__':
    unittest.main()
