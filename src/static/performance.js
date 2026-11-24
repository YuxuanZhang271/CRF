// Independent, passive polling: never uses motion state, commands or input leases.
(() => {
  const get = id => document.getElementById(id);
  const panel = get('hardware-performance');
  if (!panel) return;
  const colors = {host:'#b296d5',camera:'#3aa8cf',vision:'#e3a33e',stage:'#e06783',
    control:'#47b799',voice:'#8d9fe8',recording:'#bb87bb',other:'#949ca7',
    main:'#3aa8cf',asr:'#47b799',llm:'#e3a33e',children:'#e06783'};
  const gpuColors = ['#47b799','#e3a33e','#3aa8cf','#e06783'];
  const history = [];
  let metric = 'cpu', latest = null, lastSequence = null, stopped = false, timer, controller;
  const percent = value => Number.isFinite(value) ? `${value.toFixed(1)}%` : '—';
  const gib = value => Number.isFinite(value) ? `${(value / 1073741824).toFixed(1)} GB` : '—';

  function series(state) {
    if (!state) return [];
    if (metric === 'gpu') return (state.gpus || []).map((gpu, index) => ({
      id:gpu.id,label:gpu.label,value:gpu.percent,color:gpuColors[index % gpuColors.length]}));
    const rows = [{id:'host',label:'Server total',value:metric === 'cpu' ? state.cpu.percent : state.memory.percent,color:colors.host}];
    for (const part of state.components || []) {
      if (metric === 'cpu' && part.id === 'main') continue;
      if (metric === 'ram' && part.rss_bytes == null) continue;
      rows.push({id:part.id,label:part.label,color:colors[part.id] || colors.other,
        value:metric === 'cpu' ? part.cpu_percent : part.rss_bytes / state.memory.total_bytes * 100,
        bytes:part.rss_bytes});
    }
    return rows;
  }

  function render() {
    const rows = series(latest);
    const lines = get('performance-lines'), legend = get('performance-legend');
    lines.replaceChildren(); legend.replaceChildren();
    get('performance-chart').setAttribute('aria-label', `${metric.toUpperCase()} usage over the last 60 seconds, 0 to 100 percent`);
    const end = latest?.sampled_at;
    for (const row of rows) {
      let path = '', drawing = false, previous = null;
      for (const state of history) {
        const value = series(state).find(item => item.id === row.id)?.value;
        if (previous !== null && state.sampled_at - previous > 5) drawing = false;
        previous = state.sampled_at;
        if (!Number.isFinite(value)) { drawing = false; continue; }
        const x = Math.max(0, Math.min(240, (state.sampled_at - end + 60) * 4));
        const y = 89 - Math.max(0, Math.min(100, value)) * .88;
        path += `${drawing ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)} `;
        drawing = true;
      }
      const curve = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      curve.setAttribute('d', path); curve.setAttribute('stroke', row.color);
      curve.setAttribute('class', 'performance-series');
      if (row.id === 'host') curve.setAttribute('stroke-dasharray', '4 3');
      lines.appendChild(curve);
      const item = document.createElement('li');
      item.title = `${row.label}: ${percent(row.value)}${metric === 'ram' ? ` · ${gib(row.bytes)}` : ''}`;
      const dot = document.createElement('span'); dot.className = 'performance-swatch';
      dot.style.setProperty('--series-color', row.color);
      const label = document.createElement('span'); label.className = 'performance-label'; label.textContent = row.label;
      const value = document.createElement('span'); value.className = 'performance-value'; value.textContent = percent(row.value);
      item.append(dot, label, value); legend.appendChild(item);
    }
    get('performance-note').textContent = metric === 'cpu' ?
      'CPU % of all cores. Colors show CRF threads / worker processes; native pools are shared.' : metric === 'ram' ?
      'RAM % of installed memory. Threads share CRF RAM; process RSS can include shared pages.' :
      'GPU utilization is device-wide; thread / model usage cannot be separated.';
    get('performance-detail').textContent = !latest ? '' : metric === 'ram' ?
      `${gib(latest.memory.used_bytes)} / ${gib(latest.memory.total_bytes)} used on server.` : metric === 'cpu' ?
      `${latest.cpu.cores} logical cores · last 60 seconds · 1 Hz` :
      (latest.gpus || []).map(gpu => `${gpu.label}: ${gib(gpu.memory_used_bytes)} / ${gib(gpu.memory_total_bytes)} VRAM`).join(' · ') ||
      latest.gpu_error || 'No supported NVIDIA GPU detected.';
  }

  function clear(message) {
    latest = null; history.length = 0; lastSequence = null;
    panel.dataset.unavailable = 'true';
    get('performance-status').textContent = message;
    for (const key of ['cpu','ram','gpu']) get(`performance-${key}`).textContent = '—';
    render();
  }

  for (const button of panel.querySelectorAll('[data-performance-metric]')) {
    button.addEventListener('click', () => {
      metric = button.dataset.performanceMetric;
      for (const item of panel.querySelectorAll('[data-performance-metric]'))
        item.setAttribute('aria-pressed', String(item === button));
      render();
    });
  }

  // Replay supplies recorded samples and a cursor-relative history; it must
  // never poll the current machine or compare saved clocks to today's time.
  if (panel.dataset.playback === 'true') {
    window.addEventListener('recording-performance', event => {
      const state = event.detail.state;
      if (!state) { clear('No performance sample at this time.'); return; }
      latest = state;
      history.length = 0;
      history.push(...event.detail.history);
      panel.dataset.unavailable = 'false';
      get('performance-status').textContent = 'Recorded server PC · at cursor';
      get('performance-cpu').textContent = percent(state.cpu?.percent);
      get('performance-ram').textContent = percent(state.memory?.percent);
      const values = (state.gpus || []).map(gpu => gpu.percent).filter(Number.isFinite);
      get('performance-gpu').textContent = values.length ? percent(Math.max(...values)) : '—';
      render();
    });
    clear('Performance is available in new recordings.');
    return;
  }

  async function poll() {
    controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 2500);
    try {
      const response = await fetch('/api/performance', {signal:controller.signal,cache:'no-store'});
      if (!response.ok) throw new Error('Unavailable');
      const state = await response.json();
      if (stopped) return;
      if (state.status !== 'ready' || !Number.isFinite(state.sampled_at) ||
          !Number.isFinite(state.sample_age_s) || state.sample_age_s > 5) {
        clear(state.error || (state.status === 'starting' ? 'Starting monitor…' : 'Performance data unavailable.'));
        return;
      }
      latest = state; panel.dataset.unavailable = 'false';
      get('performance-status').textContent = 'Server PC · live';
      get('performance-cpu').textContent = percent(state.cpu.percent);
      get('performance-ram').textContent = percent(state.memory.percent);
      const gpuValues = (state.gpus || []).map(gpu => gpu.percent).filter(Number.isFinite);
      get('performance-gpu').textContent = gpuValues.length ? percent(Math.max(...gpuValues)) : '—';
      get('performance-gpu').title = 'Busiest monitored GPU';
      if (state.sequence !== lastSequence) {
        if (lastSequence !== null && state.sequence < lastSequence) history.length = 0;
        history.push(state); lastSequence = state.sequence;
        while (history.length > 61 || (history[0] && state.sampled_at - history[0].sampled_at > 60)) history.shift();
      }
      render();
    } catch (error) {
      if (!stopped) clear('Performance monitor disconnected. Reconnecting…');
    } finally {
      clearTimeout(timeout);
      if (!stopped) timer = setTimeout(poll, 1000);
    }
  }
  window.addEventListener('pagehide', () => { stopped = true; clearTimeout(timer); controller?.abort(); });
  window.addEventListener('pageshow', () => { if (stopped) { stopped = false; poll(); } });
  poll();
})();
