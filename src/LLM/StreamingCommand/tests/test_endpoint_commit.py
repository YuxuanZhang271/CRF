import asyncio
import unittest
from streamingcommand._runtime.endpoint_commit import YueEndpointCommitSession
from .test_endpoint_continuation import ControlledASR, FrameVAD


class CommitTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self,language):
        """Overlap a controlled final decode with resumed speech and inject an already completed language
        hint.
        """
        asr=ControlledASR()
        events=[]
        # Collect transcript events so raw physical decodes can be compared with final commits.
        async def emit(event):events.append(event)
        session=YueEndpointCommitSession(asr,emit,interval_ms=800,endpoint_ms=240,
                                          max_segment_ms=16000,vad=FrameVAD())
        await session.start()
        self.addAsyncCleanup(session.close)
        await session.feed(b'\1\0'*(320*5))
        await session.feed(b'\0\0'*(320*12))
        await asyncio.wait_for(asr.started.wait(),1)
        future=asyncio.get_running_loop().create_future()
        future.set_result(dict(native_language_tag=language,language_probability=.99))
        session._tentative.language_prefetch=future
        await session.feed(b'\1\0'*(320*3))
        asr.release.set()
        await session.finish()
        return asr,session,events

    async def test_raw_decodes_and_history_are_unchanged(self):
        """Verify that assembled Yue commits preserve the two raw decodes and their history with zero added
        wait.
        """
        asr,session,events=await self.exercise('yue')
        self.assertEqual(asr.calls,[320*17,320*3])
        self.assertEqual(len(asr._continuity.history),2)
        finals=[e for e in events if e['type']=='transcript.final']
        self.assertEqual(len(finals),1)
        self.assertEqual(finals[0]['text'],'same command same command')
        self.assertEqual([c['text'] for c in finals[0]['endpoint_components']],['same command','same command'])
        self.assertEqual(finals[0]['generation_count'],2)
        self.assertTrue(all(c['generation_count']==1 for c in finals[0]['endpoint_components']))
        self.assertEqual(session.stats['endpoint_wait_added_ms'],0)

    async def test_english_repeat_keeps_two_independent_commits(self):
        """Keep both physical commits for the same overlap when the hint indicates English."""
        asr,session,events=await self.exercise('en')
        self.assertEqual(asr.calls,[320*17,320*3])
        self.assertEqual(len([e for e in events if e['type']=='transcript.final']),2)
        self.assertEqual(session.stats['endpoint_continuations'],0)


if __name__=='__main__':unittest.main()
