"""Shared task state and execution on the existing control coordinator."""

import logging
from concurrent.futures import TimeoutError as FutureTimeoutError
import math
import threading
import time

import numpy as np

from flex_endo_robo.flex_endo import flex_ipc as ipc


TASK_NAMES = (
    'Stop', 'Go forward', 'Go backward', 'Zoom in', 'Zoom out',
    'Look up', 'Look down', 'Look left', 'Look right', 'Rotate left',
    'Rotate right', 'Bird-eye view', 'Reset', 'Clean lens', 'Spray water',
    'Spray air', 'Tracking',
)
LOOK_DIRECTIONS = {5: (0., -1.), 6: (0., 1.), 7: (-1., 0.), 8: (1., 0.)}
log = logging.getLogger('Tasks')
ARM_INTERRUPT_CONFIRM_TIMEOUT_S = 2.0
FLEX_STOP_CONFIRM_TIMEOUT_S = 1.0


class TaskState:
    """Short, independent lock so status readers never wait for robot communication.

    Mutations are also serialized by the server's control lock. Revisions prevent
    a delayed request from affecting a newer invocation.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._state = {'id': 0, 'name': TASK_NAMES[0], 'revision': 0,
                       'simulated': False, 'error': None, 'phase': 'idle',
                       'message': 'Manual controls available when devices are ready.', 'zoom': 1.}

    def snapshot(self):
        with self._lock:
            return dict(self._state)

    def select(self, task_id):
        if type(task_id) is not int or not 0 <= task_id <= 16:
            raise ValueError('Expected a task ID from 0 to 16.')
        with self._lock:
            self._state = {'id': task_id, 'name': TASK_NAMES[task_id],
                           'revision': self._state['revision'] + 1,
                           'simulated': False, 'error': None,
                           'phase': 'starting' if task_id else 'idle',
                           'message': 'Waiting for motion to stop.' if task_id else 'Manual controls available when devices are ready.',
                           'zoom': self._state['zoom']}
            return dict(self._state)

    def fail(self, message):
        with self._lock:
            self._state = {**self._state, 'error': message,
                           'phase': 'error', 'message': message,
                           'revision': self._state['revision'] + 1}
            return dict(self._state)

    def update(self, **fields):
        with self._lock:
            self._state = {**self._state, **fields}
            return dict(self._state)


class TaskExecutor:
    """Nonblocking task steps, serialized with manual control and INTERRUPT.

    The coordinator owns execution and its heartbeats. HTTP handlers only start
    a task after the previous motion has confirmed stopping. No browser timers
    or completion claims can unlock robot controls.
    """

    def __init__(self, server):
        self.server = server
        self.node = server.node
        self.active = None

    def cancel(self):
        self.active = None

    def _event(self, reason):
        state = self.server.tasks.snapshot()
        self.server.recorder.event('task', {**state, 'reason': reason})
        return state

    def _note(self, message, phase='running'):
        state = self.server.tasks.snapshot()
        if state['message'] != message or state['phase'] != phase:
            self.server.tasks.update(message=message, phase=phase)
            self._event(phase)
            (log.warning if phase == 'limited' else log.info)('TASK %s · %s', state['id'], message)

    def fail(self, message):
        self.active = None
        try:
            self.node.cmd_stop()
        finally:
            self.server.tasks.fail(message)
            self._event('execution failed')
            log.error('Task stopped: %s', message)

    def activate(self, task_id, revision):
        n, f = self.node, self.node.flex
        arm = self.server.relay is not None
        if task_id not in (3, 4) and arm and not n._arm_ready():
            raise ValueError('Task requires fresh UR5 feedback and a ready arm.')
        if task_id in (1, 2, 11, 16) and not arm:
            raise ValueError('This task requires the UR5 connection.')
        if task_id not in (3, 4) and not arm and f is None:
            raise ValueError('Robot hardware is disabled in camera preview.')
        if task_id in (13, 14, 15, 16) and f is None:
            raise ValueError('This task requires the Flex hardware interface.')
        if task_id in LOOK_DIRECTIONS and arm and (n.arm_mode != 'rcm' or not n.rcm_enabled):
            raise ValueError('Enable UR5 RCM before starting a look task.')
        if task_id == 12 and arm:
            if (n.arm_mode != 'rcm' or not n.rcm_enabled or n.rcm_ref_axis is None
                    or n.rcm_world_point is None):
                raise ValueError('Task 12 requires active UR5 RCM; enable RCM before resetting the tip.')
            axis, _, tip = n.kin.tube_axis_in_base(n.joint_positions)
            depth = float(np.dot(tip - n.rcm_world_point, axis))
            if depth <= 1e-6 or not n._rcm_pose_is_valid(n.joint_positions, depth):
                raise ValueError('Task 12 requires the UR5 tip inside the captured RCM envelope.')
        if f is not None and task_id not in (3, 4):
            issue = f.connection_issue()
            if issue:
                raise ValueError(issue)
        now = time.monotonic()
        a = self.active = {'id': task_id, 'revision': revision, 'started': now,
                           'last_at': now, 'deadline': now + 120., 'arm': arm,
                           'key': None, 'request': None, 'limited': False}
        accepted = True
        if task_id in (1, 2):
            a['key'] = 'arm_fwd' if task_id == 1 else 'arm_bwd'
            accepted = n.set_key(a['key'], True)
        elif task_id in LOOK_DIRECTIONS:
            if f is not None:
                accepted = f.start_look(LOOK_DIRECTIONS[task_id], n)
            else:
                # Model x/y correspond to tendon down/left, respectively.
                dx, dy = LOOK_DIRECTIONS[task_id]
                direction = np.array((dy, -dx))
                bend = n._model_bend.copy()
                along = float(bend @ direction)
                perpendicular = bend - along * direction
                target = perpendicular + direction * math.sqrt(max(0., n.flex_max_bend**2 - float(perpendicular @ perpendicular)))
                a['target'] = target
                n._set_flex_slew_target(curvature=float(np.linalg.norm(target)) / (n.flex_arc_len - .008),
                                        steer_yaw=math.atan2(target[1], target[0]))
        elif task_id in (9, 10):
            if f is not None:
                a['key'] = 'flex_roll_minus' if task_id == 9 else 'flex_roll_plus'
                accepted = n.set_key(a['key'], True)
            else:
                n._model_horizon_paused = True
        elif task_id == 11:
            accepted = n.cmd_bird_eye_view()
            a['request'] = f.pending[0] if f is not None and f.pending else None
        elif task_id == 12:
            if arm:
                a['rcm_depth'] = depth
                a['rcm_axis'] = n.rcm_ref_axis.copy()
                a['rcm_pivot'] = n.rcm_world_point.copy()
            if f is not None:
                accepted = f.return_to_zero()
                a['request'] = f.pending[0] if f.pending else None
                if f.pending:
                    a['deadline'] = max(a['deadline'], f.pending[2] + 2.)
            if f is None and accepted:
                n._set_flex_slew_target(curvature=0., steer_yaw=0.)
                n._model_horizon_paused = True
        elif task_id in (13, 14, 15):
            action = {13: ipc.CLEAN_LENS, 14: ipc.WATER_SPRAY, 15: ipc.GAS_SPRAY}[task_id]
            accepted = f.start_spray(action, n, timed=task_id != 13)
            a['request'] = f.spray[0]
            a['deadline'] = now + 20.
        elif task_id == 16:
            accepted = f.set_tracking(True, n)
        if not accepted:
            raise ValueError('Task command was refused. Check device readiness and diagnostics.')
        self._note(f'{TASK_NAMES[task_id]} active.')

    def _native_done(self, a):
        f = self.node.flex
        s = f.status
        if s.app != f.client.app or s.request != a['request']:
            return False
        if s.result in (ipc.REFUSED, ipc.CANCELED, ipc.LIMIT):
            raise ValueError(f'{TASK_NAMES[a["id"]]} {ipc.RESULTS[s.result]}.')
        return s.result == ipc.DONE and not s.moving and not s.spray_phase

    def _finish(self):
        task_id = self.active['id']
        self.active = None
        self.node._flex_slew_target = None
        self.server.tasks.select(0)
        self._event('completed')
        log.info('TASK %s · %s completed; ready.', task_id, TASK_NAMES[task_id])

    def _rcm_reset_aligned(self, a):
        n = self.node
        if (n.arm_mode != 'rcm' or not n.rcm_enabled or n.rcm_ref_axis is None
                or n.rcm_world_point is None
                or not np.allclose(n.rcm_ref_axis, a['rcm_axis'], atol=1e-10, rtol=0.)
                or not np.allclose(n.rcm_world_point, a['rcm_pivot'], atol=1e-10, rtol=0.)):
            raise ValueError('The captured RCM changed during tip reset.')
        q = n.joint_positions
        if not n._rcm_pose_is_valid(q, a['rcm_depth']):
            raise ValueError('RCM pivot or insertion depth changed during tip reset.')
        axis = n.kin.tube_axis_in_base(q)[0]
        return float(np.dot(axis, a['rcm_axis'])) >= math.cos(math.radians(.35))

    def step_motion(self):
        """Run inside the coordinator's servo accounting, after its tick flag resets."""
        a = self.active
        if (a is None or a['id'] != 12 or not a['arm']
                or self.server.interrupting or self.server.stopping):
            return
        n = self.node
        try:
            if not n._arm_ready():
                raise ValueError('UR5 feedback lost during RCM tip reset.')
            if self._rcm_reset_aligned(a):
                if n._teleop_servo_active:
                    n._send_servo_stop()
                return
            q = n.joint_positions.copy()
            seed = n._q_cmd
            if (seed is None or np.max(np.abs(seed - q)) > n.TELEOP_RESYNC_RAD
                    or not n._rcm_pose_is_valid(seed, a['rcm_depth'])):
                seed = q
            axis = n.kin.tube_axis_in_base(seed)[0]
            reference = a['rcm_axis']
            angle = math.acos(float(np.clip(np.dot(axis, reference), -1., 1.)))
            step = min(math.radians(n.ARM_TILT_VEL_DEG_S),
                       n.arm_linear_speed_m_s / a['rcm_depth']) * n.dt
            if angle <= step:
                target_axis = reference
            else:
                cross = np.cross(axis, reference)
                target_axis = n.kin.rotate_about(axis, cross / np.linalg.norm(cross), step)
            target = n.kin.ik_along_axis(a['rcm_pivot'] + a['rcm_depth'] * target_axis,
                                       target_axis, seed)
            if target is None or not n._send_servo(target, q, n.dt,
                    validate=lambda joints: n._rcm_pose_is_valid(joints, a['rcm_depth'])):
                raise ValueError('No valid constrained RCM path for the tip reset.')
        except (ValueError, RuntimeError, OSError) as error:
            self.fail(str(error))

    def tick(self):
        a = self.active
        if a is None or self.server.interrupting or self.server.stopping:
            return
        state = self.server.tasks.snapshot()
        if state['id'] != a['id'] or state['revision'] != a['revision']:
            self.cancel()
            return
        try:
            self._step(a)
        except (ValueError, RuntimeError, OSError) as error:
            self.fail(str(error))

    def _step(self, a):
        n, f = self.node, self.node.flex
        task_id = a['id']
        now = time.monotonic()
        dt = max(0., min(n.dt * 2, now - a['last_at']))
        a['last_at'] = now
        if a['arm'] and task_id not in (3, 4) and not n._arm_ready():
            raise ValueError('UR5 feedback lost during task.')
        if f is not None and task_id not in (3, 4):
            issue = f.connection_issue()
            if issue:
                raise ValueError(issue)
            if f.status.spray_error:
                raise ValueError(ipc.SPRAY_ERRORS.get(f.status.spray_error, 'Pump fault.'))
            if (task_id in (5, 6, 7, 8, 9, 10) and f.status.sequence == f.client.sequence
                    and f.status.result == ipc.REFUSED):
                raise ValueError('The Flex driver refused the task movement.')
        if task_id in (11, 12, 13, 14, 15) and now >= a['deadline']:
            raise ValueError('Task completion timed out. Motion stopped; use INTERRUPT.')
        if task_id in (3, 4):
            zoom = self.server.tasks.snapshot()['zoom']
            zoom = float(np.clip(zoom + (1 if task_id == 3 else -1) * .2 * dt, 1., 2.))
            self.server.tasks.update(zoom=zoom)
            if zoom == (2. if task_id == 3 else 1.):
                self._note('Zoom limit reached; holding.', 'limited')
        elif task_id in LOOK_DIRECTIONS:
            if f is not None:
                if f.task_direction is None or f.tracking is None:
                    raise ValueError(f.tracking_state or 'Directional look stopped.')
                message = f.tracking_state.replace('Tracking limited', 'Look limited')
                self._note(message, 'limited' if f.tracking_state.startswith('Tracking limited') else 'running')
            else:
                self._model_look(a, now)
        elif task_id in (9, 10):
            if f is not None:
                s = f.status
                sign = -1 if task_id == 9 else 1
                if (sign * s.roll >= f._roll_limit() - .05 or
                        a['key'] not in f.keys or
                        (s.result == ipc.LIMIT and s.sequence == f.client.sequence)):
                    n.set_key(a['key'], False)
                    a['limited'] = True
                    self._note('Roll edge reached; holding.', 'limited')
            else:
                value = n.flex_base_yaw + (-1 if task_id == 9 else 1) * math.radians(n.flex_model_speed_deg_s) * dt
                n.flex_base_yaw = float(np.clip(value, -math.pi, math.pi))
                if abs(n.flex_base_yaw) >= math.pi:
                    self._note('Roll edge reached; holding.', 'limited')
        elif task_id == 11:
            done = self._native_done(a) if f is not None else n._flex_slew_target is None
            if done:
                self._finish()
        elif task_id == 12:
            if f is not None:
                # Keep the saved bend/roll zero while the arm aligns. Resume
                # ordinary horizon behavior only after subsequent manual motion.
                f._horizon_paused = True
                f._zero_hold_joints = None
            flex_done = self._native_done(a) if f is not None else float(np.linalg.norm(n._model_bend)) < 1e-5
            if f is None:
                step = math.radians(n.flex_model_speed_deg_s) * dt
                n.flex_base_yaw += float(np.clip(-n.flex_base_yaw, -step, step))
                n.flex_ext_yaw = 0.
                flex_done = flex_done and abs(n.flex_base_yaw) < 1e-5
            arm_done = not a['arm'] or (self._rcm_reset_aligned(a) and
                n._arm_moving is False and not n._teleop_servo_active)
            if arm_done and flex_done:
                if f is not None and a['arm']:
                    f._zero_hold_joints = n.joint_positions.copy()
                self._finish()
        elif task_id in (13, 14, 15):
            if self._native_done(a):
                self._finish()
        elif task_id == 16:
            if f.tracking is None:
                raise ValueError(f.tracking_state or 'Tracking stopped.')
            self._note(f.tracking_state, 'limited' if f.tracking_state.startswith('Tracking limited') else 'running')
        if self.active is not None and a['key'] and not a['limited']:
            n.refresh_keys([a['key']])

    def _model_look(self, a, now):
        n = self.node
        if a['limited'] or float(np.linalg.norm(n._model_bend - a['target'])) > math.radians(.05):
            return
        n._flex_slew_target = None
        if a['key'] is None:
            a['key'] = {5: 'arm_up', 6: 'arm_down', 7: 'arm_left', 8: 'arm_right'}[a['id']]
            if not n.set_key(a['key'], True):
                raise ValueError('UR5 look assistance unavailable.')
            a['last_joints'], a['progress_at'] = n.joint_positions.copy(), now
            self._note('Flex edge reached; looking with UR5.')
        elif float(np.max(np.abs(n.joint_positions - a['last_joints']))) > 1e-4:
            a['last_joints'], a['progress_at'] = n.joint_positions.copy(), now
        elif now - a['progress_at'] > 1.:
            n.set_key(a['key'], False)
            n._send_servo_stop()
            a['limited'] = True
            self._note('Flex and UR5 cannot advance farther; holding.', 'limited')


class TaskCoordinator:
    """Shared task entry for HTTP and voice; hardware acknowledgements precede activation."""

    def __init__(self, server):
        self.server = server

    @staticmethod
    def _result(body, code=200):
        return body, code

    def _task_event(self, task, reason):
        self.server.recorder.event('task', {**task, 'reason': reason})
        log.info('TASK %s · %s (%s)', task['id'], task['name'], reason)

    def select(self, task_id=0, zoom=None, *, valid=None, expected_revision=None, prepare=None):
        node = self.server.node
        started = False
        error = None
        arm_failure = None
        preparation_error = None
        try:
            with node.control_lock:
                if valid is not None and not valid():
                    return self._result({'error': 'Voice command expired.'}, 409)
                if expected_revision is not None and self.server.tasks.snapshot()['revision'] != expected_revision:
                    return self._result({'error': 'Task changed after the voice command started.'}, 409)
                if self.server.interrupting and not task_id and not self.server.stopping:
                    # Task 0 takes precedence over a task whose entry stop is in flight.
                    task = self.server.tasks.select(0)
                    self.server.executor.cancel()
                    self._task_event(task, 'interrupt requested')
                    return self._result({'ok': True, 'pending': True, 'task': task}, 202)
                if self.server.stopping or self.server.interrupting:
                    return self._result({'error': 'A stop request is already in progress.'}, 409)
                if task_id and self.server.relay is not None and self.server.relay.freedrive_busy() is True:
                    return self._result({'error': 'Exit Freedrive before starting a task.'}, 409)
                self.server.interrupting = started = True
                self.server.executor.cancel()
                # Lock out manual inputs before clearing keys or talking to devices.
                if task_id:
                    task = self.server.tasks.select(task_id)
                    revision = task['revision']
                    if zoom is not None:
                        self.server.tasks.update(zoom=float(zoom))
                    self._task_event(self.server.tasks.snapshot(), 'selected')
                arm_result = node.cmd_interrupt(self.server.relay is not None)
                flex_request = node.flex.stop_request if node.flex_stop_delivered else None
            flex = self._wait_for_flex_stop(flex_request, idle=bool(task_id))
            if arm_result is not None:
                try:
                    arm_failure = arm_result.result(timeout=ARM_INTERRUPT_CONFIRM_TIMEOUT_S)
                except FutureTimeoutError:
                    arm_failure = 'The UR5 did not confirm that motion stopped.'
            error = ' '.join(filter(None, [arm_failure,
                'Flex did not confirm that motion stopped.' if flex == 'unconfirmed' else None]))
            if not error and task_id and prepare is not None:
                with node.control_lock:
                    current = (self.server.tasks.snapshot()['revision'] == revision
                               and (valid is None or valid()))
                    if current:
                        self.server.tasks.update(message='Waiting for a fresh camera observation.')
                        self._task_event(self.server.tasks.snapshot(), 'waiting for camera')
                if current:
                    preparation_error = 'Camera preparation did not complete.'
                    try:
                        if prepare(revision):
                            preparation_error = None
                        else:
                            preparation_error = 'Task canceled while waiting for the camera.'
                    except (ValueError, RuntimeError, OSError) as failure:
                        preparation_error = str(failure)
                else:
                    preparation_error = 'Task canceled before camera preparation.'
        except (OSError, RuntimeError) as failure:
            error = str(failure)
            flex, arm_result = 'unconfirmed', None
        finally:
            if started:
                with node.control_lock:
                    if error:
                        self._task_event(self.server.tasks.fail(error), 'stop failed')
                    elif not task_id:
                        self._task_event(self.server.tasks.select(0), 'interrupted')
                    elif self.server.tasks.snapshot()['revision'] == revision:
                        if valid is not None and not valid():
                            self._task_event(self.server.tasks.select(0), 'voice command expired')
                            error = 'Voice command expired.'
                        elif preparation_error:
                            error = preparation_error
                            self._task_event(self.server.tasks.fail(error), 'camera preparation failed')
                            log.warning('Task was not started: %s', error)
                        else:
                            try:
                                self.server.executor.activate(task_id, revision)
                            except (ValueError, RuntimeError, OSError) as failure:
                                error = str(failure)
                                self.server.executor.fail(error)
                    if preparation_error and not error:
                        error = preparation_error
                    self.server.interrupting = False
        arm = 'disabled' if arm_result is None else 'unconfirmed' if arm_failure else 'confirmed'
        return self._result({'ok': not bool(error), 'error': error, 'arm': arm, 'flex': flex,
                    'task': self.server.tasks.snapshot()}, 503 if error else 200)

    def _wait_for_flex_stop(self, flex_request, *, idle=False):
        flex = self.server.node.flex
        if flex is None:
            return 'disabled'
        if flex_request is not None:
            deadline = time.monotonic() + FLEX_STOP_CONFIRM_TIMEOUT_S
            while time.monotonic() < deadline:
                with self.server.node.control_lock:
                    if (flex.stop_acknowledged(flex_request) and
                            (not idle or (flex.status.action == 0 and flex.stop_request is None))):
                        return 'confirmed'
                time.sleep(0.01)
        return 'unconfirmed'
