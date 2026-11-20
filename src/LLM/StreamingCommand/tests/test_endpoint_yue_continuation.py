import asyncio
import unittest
from streamingcommand._runtime.endpoint_yue_continuation import YueEndpointContinuationSession
from .test_endpoint_continuation import ControlledASR, FrameVAD


class YueContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, hint):
        """Send overlapping speech and silence through the Yue continuation policy with an optional
        precomputed hint.
        """
        asr = ControlledASR()
        events = []
        # Collect the fixture's segmentation and transcript events.
        async def emit(event): events.append(event)
        session = YueEndpointContinuationSession(asr,emit,interval_ms=800,endpoint_ms=240,
                                                  max_segment_ms=16000,vad=FrameVAD())
        await session.start()
        self.addAsyncCleanup(session.close)
        await session.feed(b'\1\0' * (320*5))
        await session.feed(b'\0\0' * (320*12))
        await asyncio.wait_for(asr.started.wait(),1)
        if hint is not None:
            future=asyncio.get_running_loop().create_future()
            future.set_result(hint)
            session._tentative.language_prefetch=future
        await session.feed(b'\1\0' * (320*3))
        asr.release.set()
        await session.finish()
        return session, [e for e in events if e['type']=='transcript.final']

    async def test_pure_english_inflight_repeat_is_preserved(self):
        """Check that overlapping English utterances still produce two final transcripts."""
        session,finals=await self.exercise({'native_language_tag':'en','language_probability':.99})
        self.assertEqual(len(finals),2)
        self.assertEqual(session.stats['endpoint_continuations'],0)

    async def test_current_cantonese_allows_continuation(self):
        """Verify that strong current Cantonese evidence permits one continuation."""
        session,finals=await self.exercise({'native_language_tag':'yue','language_probability':.8})
        self.assertEqual(len(finals),1)
        self.assertEqual(session.stats['endpoint_continuations'],1)

    async def test_codeswitch_english_with_causal_yue_context(self):
        """Verify that a completed rolling Yue hint allows an English-current continuation without adding
        wait.
        """
        session,finals=await self.exercise({'temporal_language_queries':{
            'current':{'native_language_tag':'en','language_probability':.99},
            'rolling':{'native_language_tag':'yue','language_probability':.99}}})
        self.assertEqual(len(finals),1)
        self.assertEqual(session.stats['endpoint_wait_added_ms'],0)

    async def test_missing_or_weak_evidence_does_not_wait_or_merge(self):
        """Keep independent final events when the Yue hint is absent or under threshold."""
        for hint in (None,{'native_language_tag':'yue','language_probability':.6}):
            session,finals=await self.exercise(hint)
            self.assertEqual(len(finals),2)
            self.assertEqual(session.stats['endpoint_continuations'],0)


if __name__=='__main__':unittest.main()
