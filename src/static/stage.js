// Shared surgical-stage presentation for the dashboard and passive live view.
function renderSurgicalStage(camera) {
  const full = camera.mode === 'full';
  const stage = camera.stage || {status: 'inactive', phases: [], top3: []};
  const panel = document.getElementById('surgical-stage');
  panel.dataset.inactive = String(!full);
  panel.setAttribute('aria-disabled', String(!full));
  const top = full ? stage.top3 || [] : [];
  const active = top[0]?.id ?? null;
  const status = document.getElementById('stage-status');
  status.hidden = active !== null;
  status.textContent = !full ? 'Inactive in Test mode' : stage.error ||
    (stage.status === 'loading' ? 'Loading stage model…' : 'Waiting for a fresh stage prediction…');
  status.dataset.level = full && stage.error ? 'error' : 'info';
  const percent = value => value > 0 && value < .01 ? '<1%' : `${Math.round(value * 100)}%`;
  const elapsed = active !== null && Number.isFinite(stage.phase_elapsed_s) ? Math.max(0, stage.phase_elapsed_s) : null;
  const rail = document.getElementById('stage-rail');
  rail.replaceChildren();
  for (const phase of stage.phases || []) {
    const item = document.createElement('li');
    const passed = active !== null && phase.id < active;
    item.dataset.passed = String(passed);
    const index = document.createElement('span');
    index.className = 'stage-index';
    const number = document.createElement('span');
    number.className = 'stage-number';
    number.textContent = phase.id;
    index.appendChild(number);
    const track = document.createElement('span');
    track.className = 'stage-time-track';
    const timeFill = document.createElement('span');
    timeFill.style.height = '0%';
    const duration = phase.duration_s;
    const hasDuration = Number.isFinite(duration) && duration > 0;
    if (passed) {
      timeFill.style.height = '100%';
      track.setAttribute('role', 'progressbar');
      track.setAttribute('aria-label', `${phase.name}: earlier than the current stage`);
      track.setAttribute('aria-valuemin', '0');
      track.setAttribute('aria-valuemax', '100');
      track.setAttribute('aria-valuenow', '100');
      track.setAttribute('aria-valuetext', 'Full: earlier than the current recognized stage');
    } else if (phase.id === active && elapsed !== null && hasDuration) {
      timeFill.style.height = `${Math.min(100, elapsed / duration * 100)}%`;
      track.setAttribute('role', 'progressbar');
      track.setAttribute('aria-label', `${phase.name}: elapsed time against statistical duration`);
      track.setAttribute('aria-valuemin', '0');
      track.setAttribute('aria-valuemax', String(duration));
      track.setAttribute('aria-valuenow', String(Math.min(duration, elapsed)));
      track.setAttribute('aria-valuetext', `${Math.floor(elapsed)} seconds elapsed / ${duration} seconds statistical duration`);
    } else track.setAttribute('aria-hidden', 'true');
    track.appendChild(timeFill);
    index.appendChild(track);
    const box = document.createElement('div');
    box.className = 'stage-box';
    if (hasDuration) box.title = `Statistical duration: ${duration} s`;
    const label = document.createElement('span');
    label.className = 'stage-name';
    label.textContent = phase.name;
    const score = document.createElement('span');
    score.className = 'stage-score';
    const prediction = top.find(value => value.id === phase.id);
    score.textContent = prediction ? percent(prediction.prob) : '—';
    box.dataset.predicted = String(Boolean(prediction));
    const confidenceFill = document.createElement('span');
    confidenceFill.className = 'stage-box-confidence';
    confidenceFill.setAttribute('aria-hidden', 'true');
    confidenceFill.style.width = prediction ? `${Math.max(0, Math.min(100, prediction.prob * 100))}%` : '0%';
    box.append(label, score, confidenceFill);
    if (phase.id === active) {
      item.setAttribute('aria-current', 'step');
      const timing = document.createElement('span');
      timing.className = 'stage-elapsed';
      timing.textContent = elapsed === null ? 'Waiting for timing…' : hasDuration ?
        `${Math.floor(elapsed)} / ${duration} s` : `${Math.floor(elapsed)} s elapsed`;
      box.appendChild(timing);
    }
    item.append(index, box);
    rail.appendChild(item);
  }
  document.getElementById('stage-context').textContent = active === null ?
    'Elapsed / statistical duration (seconds).' : 'Elapsed / statistical duration (seconds). Timer resets on stage changes.';
}
