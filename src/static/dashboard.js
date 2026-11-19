const consoleOutput = document.getElementById('console-output');
const reportedStatus = new Map();
let recordingActive = false;
let recordingPending = false;
const consoleRequests = new Set();
let consoleForwardFailed = false;
let stateFeedReady = false;
let recordingClock = null;

function writeOutput(source, message, level = 'info', record = true) {
  const atEnd = consoleOutput.scrollHeight - consoleOutput.scrollTop - consoleOutput.clientHeight < 12;
  const line = document.createElement('div');
  line.className = 'console-line';
  line.dataset.level = level;
  const stamp = document.createElement('span');
  stamp.className = 'console-source';
  stamp.textContent = `${new Date().toLocaleTimeString()} ${source}`;
  line.append(stamp, document.createTextNode(message));
  consoleOutput.append(line);
  if (consoleOutput.childElementCount > 120) consoleOutput.firstElementChild.remove();
  if (atEnd) consoleOutput.scrollTop = consoleOutput.scrollHeight;
  if (recordingActive && record) {
    const request = fetch('/api/recording/console', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({source, message: message.slice(0, 3000), level,
                            client_timestamp: new Date().toISOString()}),
      signal: AbortSignal.timeout(2000)
    }).then(response => {
      if (!response.ok) throw new Error('Console forwarding failed.');
      consoleForwardFailed = false;
    }).catch(() => {
      if (!consoleForwardFailed) {
        consoleForwardFailed = true;
        writeOutput('RECORD', 'A dashboard console message could not be recorded.', 'error', false);
      }
    });
    consoleRequests.add(request);
    request.finally(() => consoleRequests.delete(request));
  }
}

const recordButton = document.getElementById('record');
function renderRecording(state) {
  recordingActive = state.active;
  recordButton.setAttribute('aria-pressed', String(recordingActive));
  recordButton.textContent = recordingActive ? 'RECORD ●' : 'RECORD';
  recordButton.title = recordingActive ? 'Stop recording' : 'Start recording';
  recordButton.disabled = recordingPending || stopping || sessionStopped;
  const label = document.getElementById('recording-status');
  recordingClock = {at: performance.now(), state};
  const dropped = Object.values(state.dropped || {}).reduce((a, b) => a + b, 0);
  label.textContent = state.error ? `Recording error: ${state.error}` :
    `${recordingActive ? 'Recording' : state.session ? 'Saved' : 'Ready to record'}${dropped ? ` · ${dropped} dropped records` : ''}`;
  label.title = state.session || '';
  updateRecordingClock();
  reportStatus('recording-error', 'RECORD', state.error || '', 'error');
  reportStatus('recording-drops', 'RECORD', dropped ? `${dropped} records dropped; disk writer could not keep up.` : '', 'error');
}

function updateRecordingClock() {
  if (!recordingClock) return;
  const {state, at} = recordingClock;
  if (state.error || !state.session) return;
  const elapsed = (state.elapsed_s || 0) + (state.active ? (performance.now() - at) / 1000 : 0);
  const duration = state.segment_seconds || 300;
  const slice = Math.floor(elapsed / duration);
  const seconds = Math.floor(elapsed % duration);
  const timer = `${Math.floor(seconds / 60).toString().padStart(2, '0')}:${(seconds % 60).toString().padStart(2, '0')}`;
  const drops = Object.values(state.dropped || {}).reduce((a, b) => a + b, 0);
  document.getElementById('recording-status').textContent =
    `${state.active ? 'Recording' : 'Saved'} · Slice ${String(slice).padStart(5, '0')} · ${timer}${drops ? ` · ${drops} dropped records` : ''}`;
}
setInterval(updateRecordingClock, 200);

recordButton.addEventListener('click', async () => {
  if (recordingPending || stopping || sessionStopped) return;
  recordingPending = true;
  recordButton.disabled = true;
  try {
    const current = await fetch('/api/recording', {signal: AbortSignal.timeout(3000)});
    if (!current.ok) throw new Error('Recording status unavailable.');
    const state = await current.json();
    renderRecording(state);
    if (state.active) {
      releaseKeys();
      if (!window.confirm('Stop recording and save the session?')) return;
      await Promise.allSettled([...consoleRequests]);
    }
    const response = await fetch(`/api/recording/${state.active ? 'stop' : 'start'}`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(state.active ? {confirmed: true} : {}),
      signal: AbortSignal.timeout(8000)
    });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || 'Recording request failed.');
    renderRecording(result);
    writeOutput('RECORD', `${result.active ? 'Recording started' : 'Recording saved'}: ${result.session}`, 'success');
  } catch (error) {
    writeOutput('RECORD', error.message, 'error');
  } finally {
    recordingPending = false;
    recordButton.disabled = stopping || sessionStopped;
  }
});

async function pollRecording() {
  try {
    const response = await fetch('/api/recording', {signal: AbortSignal.timeout(3000)});
    if (!response.ok) throw new Error('Recording status unavailable.');
    const state = await response.json();
    if (!recordingPending) renderRecording(state);
  } catch (error) {
    if (!stopping && !sessionStopped) {
      document.getElementById('recording-status').textContent = error.message;
      recordButton.disabled = true;
    }
  }
  if (!sessionStopped) setTimeout(pollRecording, 1000);
}

function reportStatus(key, source, message, level) {
  if (reportedStatus.get(key) === message) return;
  reportedStatus.set(key, message);
  if (message) writeOutput(source, message, level);
}

async function post(path, body = {}, quiet = false) {
  const timeout = path === '/api/cmd/stop' ? 12000 : path === '/api/cmd/interrupt' ? 4000 : 1000;
  try {
    const response = await fetch(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(timeout)
    });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || 'Command refused');
    return true;
  } catch (e) {
    if (!quiet) writeOutput('CMD', e.message, 'error');
    return false;
  }
}

const held = new Map();
let keyRequest = Promise.resolve();
let interrupting = false;
let stopping = false;
let sessionStopped = false;

function sendKey(key, down, pointerId) {
  keyRequest = keyRequest.then(() => {
    if (!down || held.get(key) === pointerId) {
      return post('/api/key', {key, down}, !down);
    }
  });
}

document.querySelectorAll('button[data-key]').forEach(btn => {
  const key = btn.dataset.key;
  btn.addEventListener('pointerdown', e => {
    if (e.button !== 0 || btn.disabled || !stateFeedReady || held.has(key) || interrupting || stopping) return;
    e.preventDefault();
    btn.setPointerCapture(e.pointerId);
    held.set(key, e.pointerId);
    btn.dataset.held = 'true';
    sendKey(key, true, e.pointerId);
  });
  const release = e => {
    if (held.get(key) !== e.pointerId) return;
    held.delete(key);
    delete btn.dataset.held;
    sendKey(key, false);
  };
  btn.addEventListener('pointerup', release);
  btn.addEventListener('pointercancel', release);
  btn.addEventListener('lostpointercapture', release);
  btn.addEventListener('pointermove', e => {
    if (e.pointerType !== 'mouse' || held.get(key) !== e.pointerId) return;
    const rect = btn.getBoundingClientRect();
    if (e.clientX < rect.left || e.clientX > rect.right ||
        e.clientY < rect.top || e.clientY > rect.bottom) release(e);
  });
  if (key === 'flex_water' || key === 'flex_gas') {
    btn.addEventListener('keydown', e => {
      if (!['Space', 'Enter'].includes(e.code)) return;
      e.preventDefault();
      if (e.repeat || btn.disabled || !stateFeedReady || held.has(key) || interrupting || stopping) return;
      const owner = `spray:${e.code}`;
      held.set(key, owner);
      btn.dataset.held = 'true';
      sendKey(key, true, owner);
    });
    document.addEventListener('keyup', e => {
      if (held.get(key) !== `spray:${e.code}`) return;
      e.preventDefault();
      held.delete(key);
      delete btn.dataset.held;
      sendKey(key, false);
    });
  }
});

function releaseKeys() {
  held.forEach((_, key) => sendKey(key, false));
  held.clear();
  document.querySelectorAll('[data-held]').forEach(button => delete button.dataset.held);
}
async function stopSession() {
  if (stopping) return;
  stopping = true;
  document.getElementById('stop').disabled = true;
  document.getElementById('interrupt').disabled = true;
  held.clear();
  writeOutput('APP', 'Stop requested. Ending the control session.');
  if (await post('/api/cmd/stop')) {
    sessionStopped = true;
    recordingActive = false;
    recordingClock = null;
    recordButton.disabled = true;
    recordButton.setAttribute('aria-pressed', 'false');
    recordButton.textContent = 'RECORD';
    document.getElementById('recording-status').textContent = 'Session ended; recording is closing.';
    setStatusChip('motion-status', 'Stopped', 'ok');
    writeOutput('APP', 'Control session stopped.', 'success');
  } else {
    stopping = false;
    document.getElementById('stop').disabled = false;
    document.getElementById('interrupt').disabled = interrupting;
  }
}
async function interruptMotion() {
  if (interrupting || stopping) return;
  interrupting = true;
  const button = document.getElementById('interrupt');
  button.disabled = true;
  held.clear();
  writeOutput('APP', 'Waiting for motion to stop.');
  if (await post('/api/cmd/interrupt') && !stopping) {
    writeOutput('APP', 'Motion interrupted. Control session remains open.', 'success');
  }
  interrupting = false;
  button.disabled = stopping;
}
async function calibrateFlex() {
  if (applyingLimits.has('flex')) return;
  const field = document.getElementById('flex-limit');
  const degrees = field.disabled ? undefined : limitFromInput(field);
  if (degrees === null) return;
  const planned = document.getElementById('calibrate').dataset.planned === 'true';
  const prompt = planned ?
    'Is the scope physically straight and the base at its intended zero? This saves the planned position without moving.' :
    'Release the jog controls. Is the scope straight and the base at its intended zero? The scope will stay still.';
  if (window.confirm(prompt)) {
    if (await post('/api/flex/calibrate', {degrees})) delete field.dataset.dirty;
  }
}
window.addEventListener('blur', releaseKeys);
window.addEventListener('pagehide', releaseKeys);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) releaseKeys();
});
setInterval(() => {
  if (held.size) post('/api/key/heartbeat', {held: [...held.keys()]}, true);
}, 250);

async function pollState() {
  try {
    const response = await fetch('/api/state', {signal: AbortSignal.timeout(3000)});
    if (!response.ok) throw new Error('State unavailable');
    renderState(await response.json());
    reportStatus('state-feed', 'APP', '');
  } catch (e) {
    stateFeedReady = false;
    renderMeasurements();
    renderStatusBar({});
    syncDeviceLimits('arm', null, null, null, false, false);
    syncDeviceLimits('flex', null, null, null, false, false);
    pullOutButton.disabled = true;
    ['bird-eye', 'pull-out', 'tracking', 'clean-lens', 'water-spray', 'gas-spray'].forEach(id => {
      document.getElementById(id).disabled = true;
      delete document.getElementById(id).dataset.activity;
    });
    releaseKeys();
    document.querySelectorAll('button[data-key]').forEach(button => { button.disabled = true; });
    if (!stopping) reportStatus('state-feed', 'APP', 'Control state unavailable.', 'error');
  }
  setTimeout(pollState, 200);
}

const speedFieldIds = {arm: 'arm-linear-speed', flex: 'flex-speed'};
const applyingLimits = new Set();

function syncDeviceLimits(kind, current, maximum, speed, angleEditable, speedEditable, canApplyAngle = angleEditable) {
  const field = document.getElementById(`${kind}-limit`);
  const speedField = document.getElementById(speedFieldIds[kind]);
  const applying = applyingLimits.has(kind);
  if (Number.isFinite(maximum)) field.max = maximum;
  if (!field.dataset.dirty) field.value = current ?? '';
  if (!speedField.dataset.dirty) speedField.value = speed ?? '';
  field.disabled = !angleEditable || applying;
  field.dataset.apply = String(canApplyAngle);
  speedField.disabled = !speedEditable || applying;
  field.form.querySelector('button').disabled = applying || (!canApplyAngle && !speedEditable);
}

function limitFromInput(field) {
  if (!field.validity.valid || !Number.isInteger(field.valueAsNumber)) {
    writeOutput('CMD', `Enter a whole angle from ${field.min}° to ${field.max}°.`, 'error');
    return null;
  }
  return field.valueAsNumber;
}

for (const kind of ['arm', 'flex']) {
  const field = document.getElementById(`${kind}-limit`);
  const speedField = document.getElementById(speedFieldIds[kind]);
  const form = field.form;
  const button = form.querySelector('button');
  field.addEventListener('input', () => { field.dataset.dirty = 'true'; });
  speedField.addEventListener('change', () => { speedField.dataset.dirty = 'true'; });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (button.disabled || stopping || interrupting) return;
    const payload = {};
    if (field.dataset.dirty && !field.disabled && field.dataset.apply === 'true') {
      const degrees = limitFromInput(field);
      if (degrees === null) return;
      payload.degrees = degrees;
    }
    if (speedField.dataset.dirty && !speedField.disabled) {
      if (!speedField.validity.valid) {
        writeOutput('CMD', 'Choose a speed preset.', 'error');
        return;
      }
      payload.speed = Number(speedField.value);
    }
    if (!Object.keys(payload).length) {
      if (kind === 'flex' && field.dataset.dirty && !field.disabled && field.dataset.apply === 'false') {
        writeOutput('FLEX', 'Use CALIBRATE to save the first bend limit.');
      }
      return;
    }

    applyingLimits.add(kind);
    button.disabled = field.disabled = speedField.disabled = true;
    try {
      if (await post(`/api/limits/${kind}`, payload)) {
        if ('degrees' in payload) delete field.dataset.dirty;
        if ('speed' in payload) delete speedField.dataset.dirty;
        if (kind === 'arm' || field.dataset.modeled === 'true') {
          writeOutput(kind === 'arm' ? 'UR5' : 'FLEX', 'Limits applied.', 'success');
        }
      }
    } finally {
      applyingLimits.delete(kind);
    }
  });
}

const pullOutButton = document.getElementById('pull-out');
let pullOutPending = false;
pullOutButton.addEventListener('click', async () => {
  if (!pullOutButton.disabled && !pullOutPending && !stopping && !interrupting) {
    pullOutPending = true;
    pullOutButton.disabled = true;
    try { await post('/api/cmd/pull_out'); }
    finally { pullOutPending = false; }
  }
});

function formatReading(value, unit, decimals = 2) {
  return Number.isFinite(value) ? `${value.toFixed(decimals)}${unit}` : null;
}

function renderReadings(id, readings) {
  document.getElementById(id).innerHTML = readings.map(([label, value]) => value === null ?
    `<div class="unavailable"><dt>${label}</dt><dd aria-label="Unavailable">—</dd></div>` :
    `<div><dt>${label}</dt><dd>${value}</dd></div>`).join('');
}

function placeMarker(marker, x, y) {
  marker.style.left = `${50 + x * 40}%`;
  marker.style.top = `${50 - y * 40}%`;
}

function renderMotionPlot(id, point, limit, label, roll = null, outsideCone = null) {
  const plot = document.getElementById(id);
  const field = plot.querySelector('.plot-field');
  const tip = plot.querySelector('.plot-tip');
  const rollAvailable = Number.isFinite(roll);
  const limited = limit > 0;
  const magnitude = point && Math.hypot(...point);
  const outside = outsideCone ?? (limited && magnitude > limit + 0.01);
  field.dataset.limited = String(limited);
  field.dataset.outside = String(outside);
  plot.dataset.available = String(Boolean(point) || rollAvailable);
  tip.hidden = !point;
  if (point) {
    const scale = Math.max(limited ? limit : Math.max(10, Math.ceil(magnitude / 10) * 10), magnitude / 1.15);
    placeMarker(tip, point[0] / scale, point[1] / scale);
  }
  const boundary = limited ? `${Number(limit.toFixed(1))}° limit` :
    limit === 0 ? 'No software limit' : 'Limit —';
  const reading = plot.querySelector('.plot-reading');
  const detail = outsideCone ? ' · Outside limit. Jog inward.' : '';
  reading.textContent = `${label} ${formatReading(magnitude, '°', outsideCone ? 2 : 1) ?? '—'} · ${boundary}${detail}`;
  reading.classList.toggle('unavailable', !point && rollAvailable);
  const rollMarker = plot.querySelector('.plot-roll');
  if (rollMarker) {
    rollMarker.hidden = !rollAvailable;
    if (rollAvailable) {
      const heading = roll * Math.PI / 180;
      placeMarker(rollMarker, -0.88 * Math.sin(heading), -0.88 * Math.cos(heading));
    }
    const rollReading = plot.querySelector('.plot-roll-reading');
    rollReading.textContent = `Roll ${formatReading(roll, '°', 1) ?? '—'}`;
    rollReading.classList.toggle('unavailable', Boolean(point) && !rollAvailable);
  }
}

function renderMeasurements(s = {}) {
  const names = ['J1 pan', 'J2 lift', 'J3 elbow', 'J4 wr1', 'J5 wr2', 'J6 wr3'];
  renderReadings('arm-state', names.map((name, index) => {
    const degrees = s.joints_deg?.[index];
    const angle = formatReading(degrees, '°', 1);
    return [name, degrees >= 0 && angle !== null ? `+${angle}` : angle];
  }));
  const armMode = document.getElementById('arm-mode');
  armMode.textContent = !s.arm_mode ? 'Toggle mode' : s.arm_mode === 'rcm' ? 'RCM' : 'Linear';
  armMode.setAttribute('aria-pressed', String(s.arm_mode === 'rcm'));

  const rcm = s.arm_mode === 'rcm' ? s.rcm_tip_angles : null;
  renderMotionPlot('rcm-plot', rcm, s.rcm_cone_deg, 'Tilt',
    null, s.rcm_outside_cone);
  const insertion = s.arm_mode === 'rcm' && Number.isFinite(s.rcm_insertion_m) ?
    Math.round(s.rcm_insertion_m * 10000) / 10 : null;
  const insertionText = formatReading(insertion, ' mm', 1) ?? '—';
  document.getElementById('rcm-insertion').textContent = `Insertion ${insertion > 0 ? '+' : ''}${insertionText}`;

  const flex = s.flex_hardware;
  const roll = flex ? flex.roll : s.flex_base_yaw;
  const bend = flex ? flex.bend : s.flex_theta_total_deg;
  const limit = flex ? flex.bend_limit : s.flex_model_bend_limit;
  const point = flex ? flex.bend_point : s.flex_model_bend_point;
  const planned = point && flex && (!flex.calibrated || flex.planned_feedback);
  const bendLabel = planned ? 'Planned bend' : point && !flex ? 'Modeled bend' : 'Bend';
  renderMotionPlot('flex-plot', point ? [-point[0], point[1]] : null, limit, bendLabel, roll);

  const flexState = document.getElementById('flex-state');
  flexState.title = planned ? `Planned positions relative to ${flex.planned_zero ? 'the saved zero' : 'startup'}` :
    point && !flex ? 'Modeled FlexEndo positions' : '';
  renderReadings('flex-state', [
    ['Roll', formatReading(roll, '°')],
    ['Bend', formatReading(bend, '°')],
    ['Limit', limit === 0 ? 'Uncapped' : formatReading(limit, '°')]
  ]);
  document.getElementById('tendon-label').textContent = flex ? 'Tendon ΔL (mm)' : 'Tendon length (mm)';
  renderReadings('tendons', ['R', 'G', 'B', 'Y'].map((name, index) => {
    const length = flex ? flex.tendons?.[index] : s.tendons?.[name]?.[0];
    return [flex || !s.tendons ? `T${index + 1}` : name, formatReading(length, '')];
  }));
}

function setStatusChip(id, label, tone = '') {
  const chip = document.getElementById(id);
  chip.dataset.tone = tone;
  chip.querySelector('dd').textContent = label;
}

function renderStatusBar(s) {
  const flex = s.flex_hardware;
  const armFault = s.arm_enabled && !s.stopping && (s.motion_fault || s.kinematics_ready === false);
  const armConnected = s.relay_connected && s.joints_received;
  const flexFault = flex?.fault || flex?.setup_encoder_mismatch;
  if (s.arm_enabled === false) setStatusChip('arm-status', 'Disabled');
  else if (armFault) setStatusChip('arm-status', 'Error', 'error');
  else if (armConnected) setStatusChip('arm-status', 'Connected', 'ok');
  else setStatusChip('arm-status', 'Disconnected');

  if (flexFault) setStatusChip('flex-connection', 'Error', 'error');
  else if (flex?.connected) setStatusChip('flex-connection', 'Connected', 'ok');
  else if (flex?.driver_online) setStatusChip('flex-connection', 'Connecting', 'active');
  else if (!flex && s.arm_enabled !== undefined) {
    setStatusChip('flex-connection', s.arm_enabled ? 'Modeled' : 'Disabled');
  } else setStatusChip('flex-connection', 'Disconnected');

  const armStill = s.arm_enabled === false || (armConnected && !armFault && s.arm_moving === false);
  const flexStill = !flex || (flex.connected && !flexFault && flex.moving === false);
  if (sessionStopped) setStatusChip('motion-status', 'Stopped', 'ok');
  else if (stopping || s.stopping || interrupting || s.interrupting) setStatusChip('motion-status', 'Stopping', 'active');
  else if (flex?.tracking || s.tracking) setStatusChip('motion-status', 'Tracking', 'tracking');
  else if (s.arm_moving === true || flex?.moving === true) setStatusChip('motion-status', 'Moving', 'active');
  else if ((s.arm_enabled || flex) && armStill && flexStill) setStatusChip('motion-status', 'Standstill', 'ok');
  else setStatusChip('motion-status', 'Unavailable');
}

function renderState(s) {
  stateFeedReady = true;
  renderMeasurements(s);
  renderStatusBar(s);
  const flex = s.flex_hardware;
  const hardware = Boolean(flex);
  document.getElementById('flex-model-controls').hidden = hardware;
  document.getElementById('flex-hardware-controls').hidden = !hardware;
  document.getElementById('flex-calibration').hidden = !hardware;
  const tracking = document.getElementById('tracking');
  const flexReady = hardware && flex.connected && flex.positioning_ready && !flex.fault;
  const armReady = s.relay_connected && s.joints_received && s.kinematics_ready && !s.motion_fault;
  const rcmReady = armReady && s.arm_mode === 'rcm' && s.rcm_enabled;
  const sprayBusy = Boolean(flex?.spray_busy);
  const controlsAvailable = !stopping && !interrupting && !s.stopping && !s.interrupting;
  const limitEditable = controlsAvailable && !sprayBusy;
  document.querySelectorAll('button[data-key^="arm_"]').forEach(button => {
    button.disabled = !armReady || !limitEditable;
  });
  document.querySelectorAll('#flex-model-controls button').forEach(button => {
    button.disabled = !s.arm_enabled || !armReady || !limitEditable;
  });
  const armLimitReady = armReady && s.arm_moving === false && limitEditable;
  const armSpeedEditable = armLimitReady && !s.pull_out_active && !s.tracking && !flex?.tracking;
  syncDeviceLimits('arm', s.rcm_cone_deg, s.rcm_cone_max_deg, s.arm_linear_speed_mm_s, armSpeedEditable, armSpeedEditable);
  document.getElementById('arm-reset').disabled = !armReady || !limitEditable;
  document.getElementById('arm-mode').disabled = (!armReady && s.arm_mode !== 'rcm') || !limitEditable;
  document.getElementById('flex-limit').dataset.modeled = String(!hardware);
  reportStatus('arm-fault', 'ARM', s.motion_fault, s.stopping ? 'info' : 'error');
  reportStatus('arm-kinematics', 'ARM', s.arm_enabled && s.kinematics_ready === false ? 'Kinematics unavailable.' : '', 'error');
  reportStatus('arm-mode', 'ARM', armReady ? `Arm mode ${s.arm_mode.toUpperCase()}.` : '');
  tracking.setAttribute('aria-pressed', String(Boolean(flex?.tracking)));
  tracking.disabled = !limitEditable || !flex || (!flex.tracking && (!flexReady ||
    !flex.tracking_horizon_supported || flex.busy || !rcmReady || selectedCategory === null));
  reportStatus('tracking', 'TRACKING', flex?.tracking_state);
  const birdEye = document.getElementById('bird-eye');
  birdEye.disabled = !limitEditable || !armReady || (hardware && (!flexReady || flex.busy || flex.tracking));
  const readyToWithdraw = s.pull_out_state === 'ready';
  pullOutButton.disabled = pullOutPending || !limitEditable || !rcmReady || s.arm_moving !== false ||
    (s.pull_out_active && !readyToWithdraw) ||
    (hardware && (!flexReady || (flex.busy && !readyToWithdraw) || flex.tracking));
  setActivity(birdEye, s.bird_eye_state);
  setActivity(pullOutButton, {preparing: 'moving', retracting: 'moving'}[s.pull_out_state] || s.pull_out_state);
  setActivity(tracking, flex?.tracking_indicator);
  const instruction = {preparing: 'Pull Out: moving to zero.',
    ready: 'At zero. Verify straightness and a clear path; press PULL OUT or Ctrl+P again to withdraw.',
    retracting: 'Pull Out: withdrawing.', complete: 'Pull Out complete.'}[s.pull_out_state] || '';
  document.getElementById('procedure-status').textContent =
    flex?.spray_action === 'clean lens' ? flex.spray_message : instruction;


  if (flex) {
    const ready = flex.connected && !flex.fault;
    const flexSpeedEditable = limitEditable && ready && !flex.busy && !flex.tracking;
    const calibrate = document.getElementById('calibrate');
    calibrate.disabled = !ready || flex.busy || flex.tracking || flex.setup_encoder_mismatch;
    calibrate.dataset.planned = String(flex.planned_feedback);
    const editable = limitEditable && !calibrate.disabled && flex.bend_limit_supported;
    syncDeviceLimits('flex', flex.bend_limit || flex.bend_limit_default, flex.bend_limit_max,
      flex.speed_limit_deg_s, editable, flexSpeedEditable, editable && flex.bend_limit > 0);
    const recheck = document.getElementById('flex-recheck');
    recheck.hidden = !flex.startup_recheck && !flex.recheck_pending;
    recheck.disabled = flex.recheck_pending;
    document.getElementById('flex-return-zero').disabled =
      !ready || flex.busy || flex.tracking || !(flex.calibrated || flex.planned_zero);
    document.querySelectorAll('#flex-hardware-controls button').forEach(button => {
      const needsRollSupport = button.dataset.key.startsWith('flex_roll_') && !flex.roll_jog_supported;
      button.disabled = !limitEditable || !ready || flex.setup_encoder_mismatch || needsRollSupport;
    });
    reportStatus('flex-fault', 'FLEX', flex.fault ? `GTS fault ${flex.fault}${flex.fault_detail ? ' · ' + flex.fault_detail : ''}` : '', 'error');
    reportStatus('flex-recovery', 'FLEX', flex.startup_recheck ?
      'Check Flex drive power, the shared I/O connection, and the alarm and limit inputs. Then recheck Flex inputs.' :
      flex.fault ? 'Resolve the GTS fault and restart the session.' : '');
    reportStatus('flex-encoder', 'FLEX', flex.setup_encoder_mismatch ?
      'Setup blocked. Planned travel and encoder feedback disagree. See the driver console.' : '', 'error');
    reportStatus('flex-setup', 'FLEX', ready && !flex.calibrated && !flex.planned_zero && !flex.setup_encoder_mismatch ?
      'Straighten and align Flex before calibrating. CALIBRATE also saves the bend limit.' : '');
    reportStatus('flex-capability', 'FLEX', ready && (!flex.bend_limit_supported || !flex.roll_jog_supported) ?
      'Rebuild and restart the Flex driver for adjustable bend limits and coordinated roll jogging.' : '');
    reportStatus('tracking-capability', 'TRACKING', ready && !flex.tracking_horizon_supported ?
      'Rebuild and restart the Flex driver for horizon-constrained tracking and the expanded bend range.' : '');
    reportStatus('flex-message', 'FLEX', flex.message);
  } else {
    const editable = Boolean(s.arm_enabled && limitEditable && !s.pull_out_active && !s.tracking);
    syncDeviceLimits('flex', s.flex_model_bend_limit, s.flex_model_bend_max_deg, s.flex_model_speed_deg_s,
      editable, editable);
  }
  const sprayReady = controlsAvailable && flex?.connected && !flex.fault && !flex.spray_error &&
    !flex.busy && flex.moving === false && !flex.tracking && (s.arm_enabled === false || (armReady && s.arm_moving === false));
  const cleanActive = flex?.spray_action === 'clean lens';
  const cleanButton = document.getElementById('clean-lens');
  cleanButton.disabled = !controlsAvailable || (!cleanActive && !(sprayReady && flex.water_ready && flex.gas_ready));
  cleanButton.setAttribute('aria-pressed', String(cleanActive));
  setActivity(cleanButton, cleanActive ? 'moving' : '');
  [['water-spray', 'water spray', 'water_ready'], ['gas-spray', 'gas spray', 'gas_ready']].forEach(([id, action, capability]) => {
    const button = document.getElementById(id);
    const active = flex?.spray_action === action;
    button.disabled = !controlsAvailable || (!active && !(sprayReady && flex[capability]));
    button.setAttribute('aria-pressed', String(active));
    setActivity(button, active ? 'active' : '');
  });
  reportStatus('spray-message', 'SPRAY', flex?.spray_message || '');
  reportStatus('spray-error', 'SPRAY', flex?.spray_error || '', 'error');
}

function setActivity(button, state) {
  if (!state || state === 'idle' || state === 'unavailable') delete button.dataset.activity;
  else button.dataset.activity = state;
}

const cameraImage = document.getElementById('camera-feed');
const categoryButtons = new Map();
document.getElementById('tracking').addEventListener('click', event => {
  post('/api/flex/tracking', {enabled: event.currentTarget.getAttribute('aria-pressed') !== 'true'});
});
let selectedCategory = null;
let viewRevision = -1;
let categoryRequest = Promise.resolve();
let categoryPending = 0;

function renderCamera(camera) {
  if (camera.revision < viewRevision) return;
  viewRevision = camera.revision;
  selectedCategory = camera.category;
  reportStatus('detector-state', 'YOLO', camera.detection_error || '', 'error');
  categoryButtons.forEach((button, id) => {
    button.setAttribute('aria-pressed', String(id === selectedCategory));
  });
  reportStatus('category', 'YOLO', selectedCategory === null ? 'Showing all categories.' :
    `Showing ${categoryButtons.get(selectedCategory).dataset.label.toLowerCase()}.`);

  const url = `/api/camera/stream?revision=${viewRevision}`;
  if (camera.ready) {
    reportStatus('camera-state', 'CAMERA', '');
    if (cameraImage.getAttribute('src') !== url) cameraImage.src = url;
  } else {
    cameraImage.removeAttribute('src');
    reportStatus('camera-state', 'CAMERA', camera.error || 'Waiting for a camera frame…', camera.error ? 'error' : 'info');
  }
}

cameraImage.addEventListener('error', () => {
  cameraImage.removeAttribute('src');
  if (!stopping) reportStatus('camera-stream', 'CAMERA', 'Camera stream interrupted.', 'error');
});
cameraImage.addEventListener('load', () => reportStatus('camera-stream', 'CAMERA', ''));

function changeCategory(id) {
  categoryPending++;
  categoryRequest = categoryRequest.then(async () => {
    try {
      const response = await fetch('/api/camera/category', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({category: selectedCategory === id ? null : id}),
        signal: AbortSignal.timeout(3000)
      });
      const camera = await response.json();
      if (!response.ok) throw new Error(camera.error || 'Could not change the category');
      renderCamera(camera);
    } catch (error) {
      writeOutput('YOLO', error.message, 'error');
    } finally {
      categoryPending--;
    }
  });
}

async function pollCamera() {
  try {
    const response = await fetch('/api/camera/state', {signal: AbortSignal.timeout(3000)});
    if (!response.ok) throw new Error('Camera status unavailable');
    const camera = await response.json();
    if (!categoryPending) renderCamera(camera);
  } catch (error) {
    if (!stopping) reportStatus('camera-state', 'CAMERA', error.message, 'error');
  }
  setTimeout(pollCamera, 400);
}

function renderCategoryButtons(categories) {
  const options = document.getElementById('category-options');
  for (const {id, label, color} of categories) {
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.label = label;
    button.style.setProperty('--class-color', color);
    button.setAttribute('aria-pressed', 'false');
    const swatch = document.createElement('span');
    swatch.className = 'swatch';
    swatch.setAttribute('aria-hidden', 'true');
    const number = document.createElement('span');
    number.className = 'category-id';
    number.textContent = id;
    button.append(swatch, label, number);
    button.addEventListener('click', () => changeCategory(id));
    categoryButtons.set(id, button);
    options.appendChild(button);
  }
}

async function init() {
  pollRecording();
  const cfg = await (await fetch('/api/config')).json();
  renderCategoryButtons(cfg.categories);
  pollState();
  pollCamera();
}
renderMeasurements();
init().catch(error => {
  writeOutput('APP', `Could not load the control panel. ${error.message}`, 'error');
});
