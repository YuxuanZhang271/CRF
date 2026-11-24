// Passive duplicate: only consume existing status and annotated JPEG streams.
(() => {
  const image = document.getElementById('camera-feed');
  const placeholder = document.getElementById('camera-placeholder');
  const status = document.getElementById('live-status');
  const mode = document.getElementById('perception-mode-label');
  let revision = -1, camera = null, closed = false, timer = null;
  let request = null;
  let systemRequest = null, systemTimer = null, speechSequence = 0;

  function renderChip(id, chip) {
    const node = document.getElementById(id);
    node.dataset.tone = chip.tone;
    node.querySelector('dd').textContent = chip.label;
    node.title = chip.title || chip.label;
  }

  async function pollSystem() {
    systemRequest = new AbortController();
    const signal = AbortSignal.any([systemRequest.signal, AbortSignal.timeout(3000)]);
    const read = async url => {
      const response = await fetch(url, {signal, cache: 'no-store'});
      if (!response.ok) throw new Error('Status unavailable');
      return response.json();
    };
    const [robot, speech] = await Promise.allSettled([
      read('/api/state'), read(`/api/llm?after=${speechSequence}`)
    ]);
    if (!closed) {
      if (robot.status === 'fulfilled') {
        for (const [id, chip] of Object.entries(systemStatus(robot.value))) renderChip(id, chip);
        renderChip('task-status', taskStatus(robot.value.task));
      } else {
        for (const id of ['arm-status', 'flex-connection', 'motion-status', 'task-status']) {
          renderChip(id, {label: 'Unavailable', tone: 'warning', title: 'Live system state unavailable.'});
        }
      }
      if (speech.status === 'fulfilled') {
        speechSequence = speech.value.sequence ?? speechSequence;
        renderChip('llm-status', speechStatus(speech.value));
      } else renderChip('llm-status', {label: 'Unavailable', tone: 'warning', title: 'Speech service status unavailable.'});
    }
    systemRequest = null;
    if (!closed) systemTimer = setTimeout(pollSystem, 200);
  }

  function showStatus(message, error = false) {
    status.textContent = message;
    status.dataset.level = error ? 'error' : 'info';
  }
  function clearImage(message) {
    image.removeAttribute('src');
    placeholder.textContent = message;
    placeholder.hidden = false;
  }
  function render(state) {
    if (state.revision < revision) return;
    revision = state.revision;
    camera = state;
    if (Number.isFinite(state.zoom)) {
      image.style.transform = `scale(${Math.min(2, Math.max(1, state.zoom))})`;
    }
    renderSurgicalStage(state);
    const full = state.mode === 'full';
    mode.textContent = `${full ? 'Full · masks' : 'Test · boxes'}${state.vision_status === 'loading' ? ' · Loading…' : state.detection_error ? ' · Unavailable' : ''}`;
    if (!state.ready) {
      const message = state.error || 'Waiting for a camera frame…';
      clearImage(message);
      showStatus(message, Boolean(state.error));
      return;
    }
    const url = `/api/camera/stream?revision=${revision}`;
    if (image.getAttribute('src') !== url) {
      placeholder.textContent = 'Connecting to camera…';
      placeholder.hidden = false;
      image.src = url;
    }
    showStatus(state.detection_error || 'Live annotated camera', Boolean(state.detection_error));
  }
  image.addEventListener('load', () => { placeholder.hidden = true; });
  image.addEventListener('error', () => {
    clearImage('Camera stream interrupted. Reconnecting…');
    showStatus('Camera stream interrupted. Reconnecting…', true);
  });

  async function poll() {
    request = new AbortController();
    try {
      const response = await fetch('/api/camera/state', {
        signal: AbortSignal.any([request.signal, AbortSignal.timeout(3000)]), cache: 'no-store'
      });
      if (!response.ok) throw new Error('Live camera status unavailable');
      const state = await response.json();
      if (!closed) render(state);
    } catch (error) {
      if (closed) return;
      clearImage('Live session disconnected. Reconnecting…');
      showStatus(error.message, true);
      mode.textContent = 'Disconnected';
      renderSurgicalStage({mode: camera?.mode, stage: {
        ...camera?.stage, status: 'waiting', top3: [], phase_elapsed_s: null,
        error: 'Live session disconnected. Waiting for fresh stage data…'
      }});
    } finally {
      request = null;
      if (!closed) timer = setTimeout(poll, 200);
    }
  }
  window.addEventListener('pagehide', () => {
    closed = true;
    clearTimeout(timer);
    clearTimeout(systemTimer);
    request?.abort();
    systemRequest?.abort();
    image.removeAttribute('src');
  });
  window.addEventListener('pageshow', event => {
    if (event.persisted && closed) { closed = false; poll(); pollSystem(); }
  });
  poll();
  pollSystem();
})();
