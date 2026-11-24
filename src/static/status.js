// Shared display-only status labels; no control requests or browser ownership.
function systemStatus(s, {sessionStopped = false, stopping = false, interrupting = false, currentTask = s.task} = {}) {
  const chips = {};
  const setStatusChip = (id, label, tone = '') => { chips[id] = {label, tone}; };
  const flex = s.flex_hardware;
  const armFault = s.arm_enabled && !s.stopping && (s.motion_fault || s.kinematics_ready === false);
  const armConnected = s.relay_connected && s.joints_received;
  const flexFault = flex?.fault || flex?.setup_encoder_mismatch;
  if (s.arm_enabled === false) setStatusChip('arm-status', 'Disabled');
  else if (armFault) setStatusChip('arm-status', 'Error', 'error');
  else if (armConnected) setStatusChip('arm-status', 'Connected', 'ok');
  else setStatusChip('arm-status', 'Disconnected', 'warning');

  if (flexFault) setStatusChip('flex-connection', 'Error', 'error');
  else if (flex?.connected) setStatusChip('flex-connection', 'Connected', 'ok');
  else if (flex?.driver_online) setStatusChip('flex-connection', 'Connecting', 'warning');
  else if (!flex && s.arm_enabled !== undefined) {
    setStatusChip('flex-connection', s.arm_enabled ? 'Modeled' : 'Disabled');
  } else setStatusChip('flex-connection', 'Disconnected', 'warning');

  const armStill = s.arm_enabled === false || (armConnected && !armFault && s.arm_moving === false);
  const flexStill = !flex || (flex.connected && !flexFault && flex.moving === false);
  if (sessionStopped) setStatusChip('motion-status', 'Stopped', 'ok');
  else if (stopping || s.stopping || interrupting || s.interrupting) setStatusChip('motion-status', 'Stopping', 'warning');
  else if (s.freedrive_transition) setStatusChip('motion-status', 'Changing Freedrive', 'warning');
  else if (s.freedrive_active) setStatusChip('motion-status', 'Freedrive', 'active');
  else if ([5, 6, 7, 8].includes(currentTask?.id) && flex?.tracking) setStatusChip('motion-status', 'Looking', 'active');
  else if (flex?.tracking || s.tracking) setStatusChip('motion-status', 'Tracking', 'tracking');
  else if (s.arm_moving === true || flex?.moving === true) setStatusChip('motion-status', 'Moving', 'active');
  else if ((s.arm_enabled || flex) && armStill && flexStill) setStatusChip('motion-status', 'Standstill', 'ok');
  else setStatusChip('motion-status', 'Unavailable');
  return chips;
}

function taskStatus(task) {
  if (!task || !Number.isInteger(task.id) || !Number.isInteger(task.revision)) {
    return {label: 'Unavailable', tone: 'warning', title: 'Task status unavailable.'};
  }
  const phase = task.error ? 'Error' : ({idle: 'Ready', starting: 'Starting', running: 'Running', limited: 'Limited', error: 'Error'}[task.phase] || (task.id ? 'Running' : 'Ready'));
  return {phase, label: task.id ? `${task.id} · ${phase}` : phase,
    tone: task.error ? 'error' : task.phase === 'limited' ? 'warning' : 'ok',
    title: `${task.id} · ${task.name} · ${phase}`};
}

function speechStatus(state) {
  const label = state.error ? 'Error' : ({idle: 'Loading', loading: 'Loading', ready: 'Ready',
    listening: 'Listening', closed: 'Closed', unavailable: 'Unavailable'}[state.phase] || 'Unavailable');
  return {label, tone: state.error ? 'error' : label === 'Loading' ? 'warning' : 'ok',
    title: `${label} · Task triggering ${state.enabled ? 'enabled' : 'disabled'}`};
}
