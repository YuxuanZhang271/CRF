"""One persistent recognizer on an owned asyncio thread, with bounded event history."""

import asyncio
from collections import deque
import copy
import json
import logging
from pathlib import Path
import threading
import time
import traceback

from .mapping import task_for
from .fast_stop import has_stop_word

DEFAULT_CONFIG = Path(__file__).with_name('settings.json')
log = logging.getLogger('LLM')


def describe_event(event):
    """Use the same full diagnostic text in the terminal and browser event feed."""
    kind = event['type']
    level = 'info'
    text = None
    if kind == 'models.loading':
        text = 'Loading cached speech and command models.'
    elif kind == 'models.ready':
        info = event.get('info', {})
        asr, command = info.get('asr') or {}, info.get('command') or {}
        text = f"Models ready. ASR: {asr.get('device', 'ready')}; LLM: {command.get('device', 'ready')}."
        for name, backend in (('ASR', asr), ('LLM', command)):
            if backend.get('log_path'):
                text += f"\n{name} worker log: {backend['log_path']}"
    elif kind == 'listen.changed':
        text = 'Task triggering enabled.' if event['enabled'] else 'Task triggering disabled; recognition remains active.'
    elif kind == 'session.started':
        text = 'Microphone recognition active.'
    elif kind == 'transcript.final':
        text = f"Segment {event.get('segment_id')}: {ascii(event.get('text', ''))}"
    elif kind in ('command.final', 'command.feedback'):
        text = f"Segment {event.get('segment_id')}: {event.get('status', 'Result')} {json.dumps(event.get('slots', {}), ensure_ascii=True)}"
        if event.get('question'):
            text += '\n' + event['question']
    elif kind == 'command.dispatch':
        result = event.get('result', {})
        text = f"Task {event.get('task_id')}: {result.get('error') or 'accepted'}"
        if not result.get('ok'):
            level = 'warning'
    elif kind == 'command.fast_stop':
        text = f"Segment {event.get('segment_id')}: STOP word detected in {event.get('source')}; triggering Task 0 without LLM parsing."
    elif kind == 'command.waiting':
        text = event['message']
    elif kind == 'command.suppressed':
        text = f"Segment {event.get('segment_id')}: {event.get('message', 'task triggering disabled for this utterance.')}"
    elif kind == 'command.rejected' or kind.endswith('.error'):
        level = 'warning' if kind == 'command.rejected' else 'error'
        text = event.get('message', kind)
        for key, label in (('input_text_repr', 'Input'), ('worker_log_path', 'Worker log'),
                           ('traceback', 'Traceback')):
            if event.get(key):
                text += f"\n{label}: {event[key]}"
    return text, level


class LLMService:
    def __init__(self, server, config=DEFAULT_CONFIG, device=None, factory=None):
        self.server, self.config, self.device = server, Path(config), device
        self.factory = factory
        self._lock = threading.RLock()
        self._events = deque(maxlen=512)
        self._sequence = 0
        self._generation = 0
        self._desired = False
        self._closed = False
        self._phase = 'idle'
        self._error = None
        self._command_error = None
        self._thread = None
        self._loop = None
        self._wake = None
        self._listen_task = None
        self._runner_task = None
        self._stream_id = 0
        self._owned_revision = None

    def snapshot(self, after=0):
        with self._lock:
            error = self._error or self._command_error
            return {'enabled': self._desired, 'phase': 'error' if error else self._phase, 'error': error,
                    'sequence': self._sequence, 'generation': self._generation,
                    'events': copy.deepcopy([e for e in self._events if e['sequence'] > after])}

    def _publish(self, event):
        diagnostic, level = describe_event(event)
        with self._lock:
            if event['type'] == 'command.error':
                self._command_error = event.get('message', 'Command parsing failed.')
            elif event['type'] in ('command.final', 'command.feedback', 'models.loading', 'models.ready'):
                self._command_error = None
            self._sequence += 1
            row = {**copy.deepcopy(event), 'sequence': self._sequence,
                   'received_monotonic_s': time.monotonic(),
                   'generation': event.get('generation', self._generation),
                   'stream_id': self._stream_id}
            if diagnostic:
                row.update(diagnostic=diagnostic, level=level)
            self._events.append(row)
        self.server.recorder.event('llm', row)
        if diagnostic:
            log.log({'error': logging.ERROR, 'warning': logging.WARNING}.get(level, logging.INFO),
                    '[%s] %s', event['type'], diagnostic)

    def start(self):
        """Preload models and start continuous recognition without enabling task triggers."""
        with self.server.node.control_lock, self._lock:
            if self._closed:
                raise ValueError('The control session is stopping.')
            if self._thread is None:
                self._phase = 'loading'
                self._thread = threading.Thread(target=self._run, name='llm-interaction')
                self._thread.start()
        return self

    def set_enabled(self, enabled):
        """Change only the task-trigger gate; keep models, audio and session memory alive."""
        if type(enabled) is not bool:
            raise ValueError('Expected a boolean enabled value.')
        with self.server.node.control_lock:
            if self.server.stopping or self._closed:
                raise ValueError('The control session is stopping.')
            self.start()
            with self._lock:
                if enabled == self._desired:
                    return self.snapshot()
                self._desired = enabled
                self._generation += 1
                if self._phase in ('ready', 'listening'):
                    self._phase = 'listening' if enabled else 'ready'
                loop = self._loop
            if loop and not loop.is_closed():
                loop.call_soon_threadsafe(self._signal)
        self._publish({'type': 'listen.changed', 'enabled': enabled})
        return self.snapshot()

    def _signal(self):
        if self._closed and self._runner_task is not None:
            self._runner_task.cancel()
        self._wake.set()

    def _run(self):
        asyncio.run(self._main())

    async def _main(self):
        self._loop = asyncio.get_running_loop()
        self._runner_task = asyncio.current_task()
        self._wake = asyncio.Event()
        sdk = None
        try:
            while not self._closed:
                try:
                    if sdk is None:
                        self._phase = 'loading'
                        self._error = None
                        self._publish({'type': 'models.loading'})
                        if self.factory is None:
                            from streamingcommand import StreamingCommand, StreamingConfig
                            config = StreamingConfig.load(self.config)
                            # Weights are provisioned outside the control session.
                            config.offline = True
                            sdk = StreamingCommand(config=config)
                        else:
                            sdk = self.factory()
                        await sdk.start()
                        self._publish({'type': 'models.ready', 'info': sdk.info()})
                    if self._closed:
                        break
                    self._phase = 'listening' if self._desired else 'ready'
                    self._listen_task = asyncio.create_task(self._listen(sdk))
                    try:
                        await self._listen_task
                    finally:
                        self._listen_task = None
                    if not self._closed:
                        raise RuntimeError('Microphone stream ended unexpectedly.')
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    message = str(error)
                    if isinstance(error, FileNotFoundError):
                        message += ' Download models first: python -m streamingcommand.cli --config src/llm/settings.json download'
                    with self._lock:
                        self._desired = False
                        self._generation += 1
                        self._error, self._phase = message, 'error'
                    self._publish({'type': 'llm.error', 'message': message, 'traceback': traceback.format_exc()})
                    await self._stop_owned()
                    if sdk is not None:
                        await sdk.close()
                        sdk = None
                    self._wake.clear()
                    if not self._closed:
                        await self._wake.wait()
        except asyncio.CancelledError:
            if not self._closed:
                raise
        finally:
            await self._stop_owned()
            if sdk is not None:
                await sdk.close()
            self._phase = 'closed'

    async def _listen(self, sdk):
        seen = set()
        segments = {}
        stopped_segments = set()
        dispatches = set()
        failed = False
        def dispatch(event, generation, revision=None):
            job = asyncio.create_task(self._dispatch(event, generation, revision))
            dispatches.add(job)
            def completed(task):
                dispatches.discard(task)
                if not task.cancelled() and task.exception() is not None:
                    error = task.exception()
                    self._publish({'type': 'command.error', 'message': str(error),
                        'traceback': ''.join(traceback.format_exception(type(error), error, error.__traceback__))})
            job.add_done_callback(completed)
        audio_origin = time.monotonic()
        with self._lock:
            self._stream_id += 1
        events = sdk.listen(device=self.device)
        try:
            async for event in events:
                if self._closed:
                    break
                segment = event.get('segment_id')
                if segment is not None and segment not in segments:
                    with self.server.node.control_lock, self._lock:
                        # An utterance must begin after the gate is enabled. Never
                        # execute speech from the disabled period on a later toggle.
                        segments[segment] = (self._generation if self._desired else None,
                                             self.server.tasks.snapshot()['revision'])
                    if len(segments) > 512:
                        oldest = next(iter(segments))
                        segments.pop(oldest)
                        stopped_segments.discard(oldest)
                self._publish({**event, 'session_audio_origin_monotonic_s': audio_origin})
                if event['type'] in ('transcript.partial', 'transcript.final') and segment not in stopped_segments and has_stop_word(event.get('text')):
                    with self._lock:
                        generation, _ = segments.get(segment, (None, None))
                        allowed = self._desired and generation is not None and generation == self._generation
                        if allowed:
                            # Invalidate every earlier in-flight command before asking
                            # the shared controller to stop; late LLM results cannot restart it.
                            self._generation += 1
                            stop_generation = self._generation
                    if allowed:
                        stopped_segments.add(segment)
                        segments[segment] = (None, None)
                        stop = {'type': 'command.fast_stop', 'segment_id': segment,
                                'revision': event.get('revision'), 'source': event['type'],
                                'status': 'EXECUTE', 'resolved': True,
                                'slots': {'I': 'stop', 'T': 'none', 'P': 'none'}}
                        self._publish(stop)
                        dispatch(stop, stop_generation)
                        await asyncio.sleep(0)
                if event['type'] == 'command.error':
                    continue  # One failed parse does not close the resident ASR/LLM workers.
                if event['type'].endswith('.error'):
                    raise RuntimeError(event.get('message', 'Recognition failed.'))
                if event['type'] not in ('command.final', 'command.feedback'):
                    continue
                if segment in stopped_segments:
                    self._publish({'type': 'command.suppressed', 'segment_id': segment,
                                   'message': 'Stop already handled by the direct word trigger.'})
                    continue
                if not (event.get('resolved') and event.get('execute') and
                        event.get('status') == 'EXECUTE' and not event.get('clarification_required')
                        and not event.get('is_fake') and not event.get('skipped_reason')):
                    continue
                identity = (event.get('segment_id'), event.get('revision'), event.get('candidate_version'))
                if identity in seen:
                    continue
                seen.add(identity)
                generation, revision = segments.get(segment, (None, None))
                if generation is not None and self._desired and generation == self._generation:
                    dispatch(event, generation, revision)
                    await asyncio.sleep(0)  # Keep receiving Stop while device acknowledgements wait.
                else:
                    self._publish({'type': 'command.suppressed', 'segment_id': segment,
                                   'message': 'Task triggering disabled for this utterance.'})
        except BaseException:
            failed = True
            with self._lock:
                self._generation += 1
            raise
        finally:
            try:
                await events.aclose()
            finally:
                jobs = list(dispatches)
                if failed or self._closed or asyncio.current_task().cancelling():
                    for job in jobs:
                        job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)

    async def _dispatch(self, event, generation, expected_revision=None):
        from tasks import TaskCoordinator
        from scope.yolo import CATEGORIES
        try:
            task_id = task_for(event['slots'])
            camera_selection = None
            def valid():
                if not (self._desired and not self._closed and generation == self._generation):
                    return False
                if camera_selection is not None:
                    state = self.server.camera.status()
                    return (state['revision'], state['category']) == camera_selection
                return True

            def prepare_tracking(revision):
                camera_revision, category = camera_selection
                self._publish({'type': 'command.waiting', 'segment_id': event.get('segment_id'),
                    'generation': generation,
                    'message': f'Waiting for a fresh YOLO observation for {CATEGORIES.get(category, "the selected category")}.'})
                observation = self.server.camera.wait_for_observation(camera_revision, category,
                    cancelled=lambda: not valid() or self.server.stopping or
                        self.server.tasks.snapshot()['revision'] != revision)
                return observation is not None

            def dispatch():
                nonlocal camera_selection
                with self.server.node.control_lock:
                    if not valid():
                        return {'ok': False, 'error': 'Voice command expired after a control change.'}, 409
                    if task_id and expected_revision is not None and self.server.tasks.snapshot()['revision'] != expected_revision:
                        return {'ok': False, 'error': 'Task changed after the voice command started.'}, 409
                    target = event['slots']['T']
                    if task_id == 16 and target != 'none':
                        category = next((k for k, name in CATEGORIES.items() if name == target), None)
                        if category is None:
                            raise ValueError(f'The current detector cannot track {target!r}.')
                        if self.server.tasks.snapshot()['id'] != 0:
                            raise ValueError('Interrupt the current task before changing the tracking target.')
                        state = self.server.camera.select_category(category)
                        camera_selection = (state['revision'], state['category'])
                    elif task_id == 16:
                        state = self.server.camera.status()
                        camera_selection = (state['revision'], state['category'])
                return TaskCoordinator(self.server).select(task_id, valid=valid,
                    expected_revision=expected_revision if task_id else None,
                    prepare=prepare_tracking if task_id == 16 else None)

            operation = asyncio.create_task(asyncio.to_thread(dispatch))
            cancelled = False
            try:
                result, code = await asyncio.shield(operation)
            except asyncio.CancelledError:
                # A hardware acknowledgement may already be in flight. Own its result
                # before closing the session, so disabling listening cannot orphan motion.
                cancelled = True
                result, code = await operation
            if result.get('ok'):
                revision = result['task']['revision']
                self._owned_revision = revision if task_id and result['task']['id'] == task_id else None
            self._publish({'type': 'command.dispatch', 'segment_id': event.get('segment_id'),
                           'generation': generation, 'task_id': task_id,
                           'http_status': code, 'result': result})
            if cancelled:
                raise asyncio.CancelledError()
        except (ValueError, RuntimeError, OSError) as error:
            self._publish({'type': 'command.rejected', 'segment_id': event.get('segment_id'),
                           'generation': generation, 'message': str(error)})

    async def _stop_owned(self):
        revision, self._owned_revision = self._owned_revision, None
        if revision is None:
            return
        from tasks import TaskCoordinator
        await asyncio.to_thread(TaskCoordinator(self.server).select, 0, expected_revision=revision)

    def close(self):
        with self.server.node.control_lock:
            with self._lock:
                self._desired = False
                self._closed = True
                self._generation += 1
                loop = self._loop
            if loop and not loop.is_closed():
                loop.call_soon_threadsafe(self._signal)
        if self._thread is not None:
            self._thread.join(timeout=30.)
            if self._thread.is_alive():
                raise RuntimeError('LLM workers did not finish closing within 30 seconds.')
