// Event cursor survives temporary disconnects; partials replace their own segment.
(() => {
  const button = document.getElementById('listen');
  const status = document.getElementById('llm-status');
  const output = document.getElementById('llm-output');
  const loadTime = document.getElementById('llm-load-time');
  const segments = new Map();
  let cursor = 0, generation = null, enabled = false, pending = false;
  let lastStateError = null, transportError = null;
  function trim() {
    while (output.childElementCount > 160) {
      const old = output.firstElementChild;
      for (const [id, node] of segments) if (node === old) segments.delete(id);
      old.remove();
    }
  }
  function message(text, level = 'info', type = 'LLM') {
    const atEnd = output.scrollHeight - output.scrollTop - output.clientHeight < 25;
    const line = document.createElement('div');
    line.className = 'llm-message'; line.dataset.level = level;
    line.textContent = `${new Date().toLocaleTimeString()} ${type} · ${text}`;
    output.append(line); trim();
    if (atEnd) output.scrollTop = output.scrollHeight;
  }
  function unavailable(error) {
    loadTime.textContent = 'Load time unavailable';
    setStatusChip('llm-status', 'Error', 'error');
    if (transportError !== error.message) {
      message(error.message, 'error');
      writeOutput('LLM', error.message, 'error');
    }
    transportError = error.message;
  }
  function render(state) {
    const loading = state.model_loading || {};
    const seconds = Number.isFinite(loading.elapsed_s) && loading.elapsed_s >= 0
      ? `${loading.elapsed_s.toFixed(1)}s` : null;
    const timingLabels = {loading: 'Loading', ready: 'Loaded in', error: 'Load failed after', cancelled: 'Load cancelled after'};
    loadTime.textContent = seconds && timingLabels[loading.phase]
      ? `${timingLabels[loading.phase]} ${seconds}`
      : ['idle', 'loading'].includes(state.phase) ? 'Loading…' : 'Load time unavailable';
    enabled = state.enabled;
    button.setAttribute('aria-pressed', String(enabled));
    button.textContent = enabled ? 'LISTEN ●' : 'LISTEN';
    button.disabled = pending || stopping || sessionStopped;
    const {label, tone, title} = speechStatus(state);
    setStatusChip('llm-status', label, tone);
    status.title = title;
    for (const event of state.events || []) {
      const stream = event.stream_id ?? event.generation;
      if (generation !== stream) { segments.clear(); generation = stream; }
      const atEnd = output.scrollHeight - output.scrollTop - output.clientHeight < 25;
      const key = `${stream}:${event.segment_id}`;
      let line = segments.get(key);
      if (event.type.startsWith('transcript.')) {
        if (!line) {
          line = document.createElement('div'); line.className = 'llm-line';
          const text = document.createElement('span');
          const detail = document.createElement('small'); detail.className = 'llm-detail';
          line.append(text, detail); output.append(line); segments.set(key, line);
        }
        line.firstChild.textContent = event.text;
        line.dataset.partial = String(event.type === 'transcript.partial');
      } else if (event.type.startsWith('command.') && line) {
        if (event.slots && line.dataset.fastStop !== 'true') {
          line.dataset.command = String(event.command === 'Y' || event.resolved || event.clarification_required);
          const question = event.question || event.pending_question;
          line.lastChild.textContent = `${event.status || 'Preview'} · ${Object.entries(event.slots).map(([k,v]) => `${k}=${v ?? '?'}`).join(' · ')}${question ? ' · ' + question : ''}`;
        }
        if (event.type === 'command.fast_stop') {
          line.dataset.fastStop = 'true'; line.dataset.command = 'true';
          line.lastChild.textContent = 'Fast STOP · Task 0 requested without LLM parsing';
        }
        if (event.type === 'command.dispatch') line.lastChild.textContent += event.result.ok ? ` · Task ${event.task_id} accepted` : ` · ${event.result.error}`;
        if (event.type === 'command.rejected' || event.type === 'command.error') line.lastChild.textContent += ' · Failed; details below';
        if (event.type === 'command.suppressed' && line.dataset.fastStop !== 'true') line.lastChild.textContent += ' · Task triggering disabled';
      }
      if (event.diagnostic) message(event.diagnostic, event.level, event.type);
      else if (event.type.endsWith('.error')) message(event.message || event.type, 'error', event.type);
      trim();
      if (atEnd) output.scrollTop = output.scrollHeight;
    }
    if (state.error && state.error !== lastStateError &&
        !(state.events || []).some(event => event.message === state.error && event.type.endsWith('.error'))) {
      message(state.error, 'error');
    }
    lastStateError = state.error || null;
    cursor = state.sequence;
  }
  button.addEventListener('click', async () => {
    if (pending || stopping || sessionStopped) return;
    pending = true; button.disabled = true;
    try {
      const response = await fetch('/api/llm/listen', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({enabled: !enabled}), signal: AbortSignal.timeout(4000)});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error);
      // Poll owns the event cursor; a toggle response must not skip queued events.
      enabled = result.enabled;
      button.setAttribute('aria-pressed', String(enabled));
    } catch (error) { unavailable(error); }
    finally { pending = false; button.disabled = stopping || sessionStopped; }
  });
  async function poll() {
    try {
      const response = await fetch(`/api/llm?after=${cursor}`, {signal: AbortSignal.timeout(3000)});
      if (!response.ok) throw new Error('Speech service unavailable');
      render(await response.json());
      transportError = null;
    } catch (error) { unavailable(error); }
    if (!sessionStopped) setTimeout(poll, 200);
    else button.disabled = true;
  }
  poll();
})();
