"""Voice mapping, control revision gates, cancellation and worker ownership."""

import asyncio
from concurrent.futures import Future
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from llm import LLMService
from llm.mapping import task_for
from llm.fast_stop import has_stop_word
from tasks import TaskCoordinator, TaskState


def server():
    return SimpleNamespace(node=SimpleNamespace(control_lock=threading.RLock(),
            cmd_interrupt=Mock(return_value=None), flex_stop_delivered=False, flex=None),
        tasks=TaskState(), recorder=Mock(), executor=Mock(), stopping=False,
        interrupting=False, relay=None, camera=Mock())


class MappingTests(unittest.TestCase):
    def test_stop_word_matches_whole_word_in_streaming_transcripts(self):
        for text in ('stop', 'STOP!', 'please stop now', '請 stop！', 'stop啦'):
            self.assertTrue(has_stop_word(text), text)
        for text in ('stopwatch', 'stopping', 'nonstop', 'stop_1', '', None):
            self.assertFalse(has_stop_word(text), text)

    def test_all_existing_tasks(self):
        pairs = [('stop', 'none', 0), ('move', 'forward', 1), ('move', 'backward', 2),
                 ('zoom in', 'none', 3), ('zoom out', 'none', 4), ('bend', 'up', 5),
                 ('bend', 'down', 6), ('bend', 'left', 7), ('bend', 'right', 8),
                 ('self rotate', 'left', 9), ('self rotate', 'right', 10),
                 ('bird view', 'none', 11), ('reset', 'none', 12), ('clean lens', 'none', 13),
                 ('spray water', 'none', 14), ('spray air', 'none', 15), ('track', 'none', 16)]
        for intent, direction, task in pairs:
            self.assertEqual(task_for({'I': intent, 'T': 'none', 'P': direction}), task)
        for slots in ({'I': 'move', 'T': 'none', 'P': 'up'},
                      {'I': 'bend', 'T': 'liver', 'P': 'up'}):
            with self.assertRaises(ValueError):
                task_for(slots)

    def test_revision_and_cancellation_checked_before_hardware_and_activation(self):
        s = server()
        result, code = TaskCoordinator(s).select(3, expected_revision=999)
        self.assertEqual(code, 409)
        s.node.cmd_interrupt.assert_not_called()
        valid = Mock(side_effect=[True, False])
        result, code = TaskCoordinator(s).select(3, valid=valid, expected_revision=0)
        self.assertFalse(result['ok'])
        s.executor.activate.assert_not_called()
        self.assertEqual(s.tasks.snapshot()['id'], 0)
        result, code = TaskCoordinator(s).select(3, valid=lambda: True,
                                                expected_revision=s.tasks.snapshot()['revision'])
        self.assertTrue(result['ok'])
        s.executor.activate.assert_called_once()


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.server = server()
        self.service = LLMService(self.server)
        self.service._desired = True
        self.service._generation = 1

    async def test_worker_failure_preserves_original_traceback_and_log_path(self):
        import json
        from streamingcommand._runtime.selectable_commands import SelectableCommandParser, CommandWorkerError
        backend = SelectableCommandParser.__new__(SelectableCommandParser)
        backend.status = {'log_path': 'worker.log'}
        backend._process = SimpleNamespace(returncode=None,
            stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()),
            stdout=SimpleNamespace(readline=AsyncMock(return_value=json.dumps({
                'ok': False, 'error': 'Tokenizer failed', 'error_type': 'TypeError',
                'traceback': 'original worker stack'}).encode())))
        with self.assertRaises(CommandWorkerError) as raised:
            await backend._rpc('parse', utterance='你好')
        self.assertEqual(raised.exception.worker_traceback, 'original worker stack')
        self.assertEqual(raised.exception.worker_log_path, 'worker.log')
        self.assertTrue(backend._process.stdin.write.call_args.args[0].isascii())

    async def test_fast_stop_preempts_pending_task_entry_before_llm_result(self):
        entered = threading.Event()
        completion = Future()
        self.server.relay = Mock()
        self.server.relay.freedrive_busy.return_value = False
        def interrupt(_):
            entered.set()
            return completion
        self.server.node.cmd_interrupt.side_effect = interrupt
        stopped_before_ack = []
        async def listen(**kwargs):
            yield self.command()  # Start a non-Stop task whose device acknowledgement waits.
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            yield {'type': 'transcript.partial', 'segment_id': 2, 'revision': 1, 'text': 'STOP!'}
            for _ in range(100):
                if self.server.tasks.snapshot()['id'] == 0:
                    break
                await asyncio.sleep(.005)
            stopped_before_ack.append(self.server.tasks.snapshot()['id'] == 0 and not completion.done())
            completion.set_result(None)
            yield {'type': 'transcript.final', 'segment_id': 2, 'revision': 2, 'text': 'stop'}
            yield dict(self.command(), segment_id=2)  # Late LLM prediction must not restart motion.
        try:
            await self.service._listen(SimpleNamespace(listen=listen))
        finally:
            if not completion.done():
                completion.set_result(None)
        self.assertEqual(stopped_before_ack, [True])
        self.server.executor.activate.assert_not_called()
        self.assertEqual(self.server.tasks.snapshot()['id'], 0)
        events = self.service.snapshot()['events']
        self.assertEqual(sum(e['type'] == 'command.fast_stop' for e in events), 1)
        self.assertTrue(any(e['type'] == 'command.dispatch' and e['task_id'] == 0 for e in events))

    async def test_fast_stop_respects_disabled_listen_gate(self):
        self.service._desired = False
        async def listen(**kwargs):
            yield {'type': 'transcript.partial', 'segment_id': 1, 'text': 'stop'}
            yield self.command('stop')
        with patch.object(self.service, '_dispatch') as dispatch:
            await self.service._listen(SimpleNamespace(listen=listen))
        dispatch.assert_not_awaited()
        self.assertFalse(any(e['type'] == 'command.fast_stop' for e in self.service.snapshot()['events']))

    async def test_audio_failure_invalidates_task_waiting_for_device_ack(self):
        entered = threading.Event()
        completion = Future()
        self.server.relay = Mock()
        self.server.relay.freedrive_busy.return_value = False
        def interrupt(_):
            entered.set()
            return completion
        self.server.node.cmd_interrupt.side_effect = interrupt
        async def listen(**kwargs):
            yield self.command()
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            yield {'type': 'asr.error', 'message': 'Microphone disconnected'}
        operation = asyncio.create_task(self.service._listen(SimpleNamespace(listen=listen)))
        try:
            for _ in range(100):
                if self.service._generation != 1:
                    break
                await asyncio.sleep(.005)
            self.assertNotEqual(self.service._generation, 1)
            completion.set_result(None)
            with self.assertRaisesRegex(RuntimeError, 'Microphone disconnected'):
                await operation
        finally:
            if not completion.done():
                completion.set_result(None)
            await asyncio.gather(operation, return_exceptions=True)
        self.server.executor.activate.assert_not_called()

    def command(self, intent='zoom in', target='none', direction='none'):
        return {'type': 'command.final', 'segment_id': 1, 'revision': 2,
                'status': 'EXECUTE', 'resolved': True, 'execute': True,
                'slots': {'I': intent, 'T': target, 'P': direction}}

    async def test_dispatch_rejects_unknown_target_and_stale_result(self):
        await self.service._dispatch(self.command('track', 'liver'), 1, 0)
        self.server.camera.select_category.assert_not_called()
        self.server.executor.activate.assert_not_called()
        self.assertEqual(self.service.snapshot()['events'][-1]['type'], 'command.rejected')
        self.server.tasks.select(4)
        await self.service._dispatch(self.command(), 1, 0)
        self.server.executor.activate.assert_not_called()
        await self.service._dispatch(self.command('stop'), 1, 0)
        self.assertEqual(self.server.tasks.snapshot()['id'], 0)

    async def test_disable_during_ack_never_activates_and_waits_for_result(self):
        entered, release = threading.Event(), threading.Event()
        completion = Future()
        self.server.relay = Mock()
        self.server.relay.freedrive_busy.return_value = False
        def interrupt(_):
            entered.set()
            return completion
        self.server.node.cmd_interrupt.side_effect = interrupt
        operation = asyncio.create_task(self.service._dispatch(self.command(), 1, 0))
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        self.service._desired = False
        self.service._generation += 1
        operation.cancel()
        completion.set_result(None)
        with self.assertRaises(asyncio.CancelledError):
            await operation
        self.server.executor.activate.assert_not_called()
        self.assertEqual(self.server.tasks.snapshot()['id'], 0)

    async def test_partials_and_duplicate_finals_never_duplicate_execution(self):
        events = [dict(self.command(), type='command.partial'), self.command(), self.command(),
                  dict(self.command(), segment_id=2, execute=False)]
        async def listen(**kwargs):
            for event in events:
                yield event
        sdk = SimpleNamespace(listen=listen)
        with patch.object(self.service, '_dispatch') as dispatch:
            await self.service._listen(sdk)
        dispatch.assert_awaited_once()

    async def test_command_error_logs_full_context_and_keeps_recognition_alive(self):
        events = [dict(self.command(), type='command.error', message='Tokenizer failed',
                       input_text_repr="'bad text'", traceback='worker traceback line 19'),
                  dict(self.command(), segment_id=2)]
        recovered = []
        async def listen(**kwargs):
            for event in events:
                yield event
                if event['type'] == 'command.error':
                    recovered.append(self.service.snapshot()['phase'])
        with self.assertLogs('LLM', level='ERROR') as logs, patch.object(self.service, '_dispatch') as dispatch:
            await self.service._listen(SimpleNamespace(listen=listen))
        self.assertEqual(recovered, ['error'])
        self.assertIn('worker traceback line 19', logs.output[0])
        self.assertIn("Input: 'bad text'", logs.output[0])
        self.assertIsNone(self.service.snapshot()['error'])
        dispatch.assert_awaited_once()

    async def test_stop_owned_never_cancels_newer_manual_task(self):
        self.service._owned_revision = 1
        self.server.tasks.select(3)
        self.server.tasks.select(4)
        await self.service._stop_owned()
        self.server.node.cmd_interrupt.assert_not_called()


class WorkerTests(unittest.TestCase):
    def test_start_preloads_and_toggles_only_gate_commands_without_resetting_session(self):
        opened, released = threading.Event(), threading.Event()
        class SDK:
            starts = 0
            listens = 0
            closes = 0
            async def start(self):
                self.starts += 1
                self.loop = asyncio.get_running_loop()
                self.queue = asyncio.Queue()
            def info(self):
                return {'ready': True}
            async def listen(self, **kwargs):
                self.listens += 1
                try:
                    opened.set()
                    while True:
                        yield await self.queue.get()
                finally:
                    released.set()
            def feed(self, event):
                self.loop.call_soon_threadsafe(self.queue.put_nowait, event)
            async def close(self):
                self.closes += 1

        sdk = SDK()
        s = server()
        service = LLMService(s, factory=lambda: sdk)
        def wait_for(predicate):
            deadline = time.monotonic() + 2.
            while not predicate() and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertTrue(predicate())
        def command(segment):
            return {'type': 'command.final', 'segment_id': segment, 'revision': 1,
                    'status': 'EXECUTE', 'resolved': True, 'execute': True,
                    'slots': {'I': 'zoom in', 'T': 'none', 'P': 'none'}}
        try:
            service.start()
            service.start()  # Idempotent startup does not create another owner.
            self.assertTrue(opened.wait(2))
            self.assertFalse(service.snapshot()['enabled'])
            self.assertEqual(service.snapshot()['phase'], 'ready')
            with patch.object(service, '_dispatch') as dispatch:
                sdk.feed({'type': 'speech.started', 'segment_id': 1})
                wait_for(lambda: any(e['type'] == 'speech.started' for e in service.snapshot()['events']))
                service.set_enabled(True)
                sdk.feed(command(1))  # Began while disabled: cannot become a task.
                wait_for(lambda: any(e['type'] == 'command.suppressed' for e in service.snapshot()['events']))
                dispatch.assert_not_awaited()
                sdk.feed(command(2))
                wait_for(lambda: dispatch.await_count == 1)
                s.tasks.select(3)
                service._owned_revision = s.tasks.snapshot()['revision']
                service.set_enabled(False)
                sdk.feed(command(3))
                wait_for(lambda: sum(e['type'] == 'command.suppressed' for e in service.snapshot()['events']) == 2)
                service.set_enabled(True)
                sdk.feed(command(4))
                wait_for(lambda: dispatch.await_count == 2)
                self.assertFalse(released.is_set())
                self.assertEqual(sdk.closes, 0)
                self.assertEqual(sdk.starts, 1)
                self.assertEqual(sdk.listens, 1)
                self.assertEqual(s.tasks.snapshot()['id'], 3)
                s.node.cmd_interrupt.assert_not_called()
                self.assertEqual({e['stream_id'] for e in service.snapshot()['events']
                                  if e['type'] == 'command.final'}, {1})
        finally:
            service.close()
        self.assertTrue(released.is_set())
        self.assertEqual(sdk.closes, 1)
        self.assertFalse(service._thread.is_alive())

    def test_close_during_preload_releases_models_without_opening_microphone(self):
        loading = threading.Event()
        class SDK:
            closes = 0
            async def start(self):
                loading.set()
                await asyncio.Event().wait()
            async def close(self):
                self.closes += 1
        sdk = SDK()
        service = LLMService(server(), factory=lambda: sdk)
        service.start()
        self.assertTrue(loading.wait(2))
        service.close()
        self.assertEqual(sdk.closes, 1)
        self.assertFalse(service._thread.is_alive())


if __name__ == '__main__':
    unittest.main()
