#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Global web UI for the FlexEndoRobo controls and SCOPE camera.

Usage:
    python src/app.py --robot-ip 192.168.1.10
"""
import argparse
import math
import json
import logging
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from flex_endo_robo.control_node import (
    DEFAULT_JOG_SPEED_SCALE,
    JOG_SPEED_SCALES,
    ControlNode,
)
from flex_endo_robo.flex_endo.flex_control import FlexControl
from flex_endo_robo.flex_endo.flex_ipc import FlexClient
from flex_endo_robo.ur5_rtde_server import TCP_HOST, TCP_PORT, UR5_IP, open_relay, serve_relay
from scope.camera import CAMERA_INDEX, CameraFeed
from scope.yolo import CATEGORIES, COLORS, MODEL_PATH

log = logging.getLogger("WindowsApp")
DASHBOARD_CONFIRM_TIMEOUT_S = 10.0
FLEX_STOP_CONFIRM_TIMEOUT_S = 1.0

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>CRF Control</title>
<style>
:root{color-scheme:dark;--bg:#11131c;--card:#1d2130;--edge:#30374a;--fg:#f1f2f6;--dim:#a9b4c8;--hi:#ffbe78;--ok:#6fdf9e;--bad:#ff8585}
*{box-sizing:border-box}
html,body{margin:0;overflow:hidden}
body{height:100dvh;padding:14px;font-family:system-ui,Arial,sans-serif;background:var(--bg);color:var(--fg)}
.workspace{display:grid;height:100%;gap:14px;grid-template-areas:"controls camera categories";
  grid-template-columns:minmax(0,1fr) minmax(0,2fr) minmax(0,1fr);
  grid-template-rows:minmax(0,1fr)}
.panel{min-width:0;min-height:0}
.robot-panel,.category-panel{display:grid;grid-template-rows:auto minmax(0,1fr)}
.robot-panel{grid-area:controls}
.category-panel{grid-area:categories}
.panel-scroll{overflow:auto;overscroll-behavior:contain;scrollbar-gutter:stable}
.panel-scroll>.card:last-child{margin-bottom:0}
.camera-panel{grid-area:camera;display:grid;grid-template-rows:auto minmax(0,1fr) auto;gap:8px}
.camera-panel h2{margin-bottom:0}
h1,h2{font-size:18px;margin:0 0 12px;color:var(--fg);font-weight:650}
.card{background:var(--card);border:1px solid var(--edge);border-radius:10px;padding:12px;margin-bottom:10px}
.card h3{margin:0 0 8px;font-size:13px;color:var(--hi);font-weight:650}
.actions{display:flex;gap:6px;flex-wrap:wrap}
[hidden]{display:none!important}
button{background:#33415d;color:#fff;border:1px solid transparent;border-radius:6px;padding:8px 10px;cursor:pointer;font:600 12px system-ui,sans-serif}
button:hover{background:#435779}
button:focus-visible{outline:2px solid var(--hi);outline-offset:2px}
button:disabled{cursor:default;opacity:.45}
button.estop{background:#a42c3a}button.estop:hover{background:#c53445}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:5px 12px;margin:0;font:12px ui-monospace,Consolas,monospace}
.grid>div{display:flex;justify-content:space-between;gap:4px;min-width:0}
.grid dt{color:var(--dim)}
.grid dd{margin:0;text-align:right;white-space:nowrap}
.control-title{margin-top:12px!important}
.command-grid{display:grid;gap:5px;grid-template-columns:minmax(45px,1.25fr) repeat(3,minmax(24px,1fr)) minmax(46px,1.25fr);
  grid-template-areas:"mode left up right forward" "mode left down right backward";
  grid-template-rows:repeat(2,minmax(32px,auto))}
.command-grid button{padding:4px;min-width:0}
#flex-hardware-controls{grid-template-columns:repeat(3,minmax(0,1fr));
  grid-template-areas:"left up right" "left down right"}
#flex-model-controls{grid-template-columns:repeat(4,minmax(0,1fr));
  grid-template-areas:"left up right forward" "left down right backward"}
.command-grid [data-key]{touch-action:none}
.command-grid .direction{font-size:15px}
.control-mode{grid-area:mode}
.control-left{grid-area:left}.control-up{grid-area:up}.control-down{grid-area:down}.control-right{grid-area:right}
.control-forward{grid-area:forward}.control-backward{grid-area:backward}
.status{font:12px/1.45 ui-monospace,Consolas,monospace;color:var(--dim);margin:4px 0;white-space:pre-wrap;overflow-wrap:anywhere}
.status:empty{display:none}
pre{background:#151926;padding:7px;border-radius:5px;font-size:10px;margin:7px 0 0;overflow:auto}
.tag-ok{color:var(--ok)}.tag-bad{color:var(--bad)}
.camera-stage{container-type:size;display:grid;justify-items:center;align-items:end;min-height:0}
.camera{position:relative;display:grid;place-items:center;width:min(100cqw,100cqh);aspect-ratio:1;border:1px solid var(--edge);border-radius:10px;background:#080a10;overflow:hidden}
.camera img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain}
.camera img:not([src]){visibility:hidden}
.camera p{margin:0;padding:20px;text-align:center;color:var(--dim);max-width:50ch;overflow-wrap:anywhere}
.diagnostics{margin:0;text-align:center}
.robot-alerts{max-height:16dvh;overflow:auto}
.category-options{display:grid;gap:7px}
.category-options button{display:flex;align-items:center;gap:9px;text-align:left;background:#253047;border-color:#37435a;padding:10px}
.category-options button:hover{background:#354461}
.category-options button[aria-pressed="true"]{border-color:var(--class-color);background:#33425d}
.swatch{width:9px;height:9px;flex:none;border-radius:50%;background:var(--class-color)}
.category-id{margin-left:auto;color:var(--dim);font:11px ui-monospace,Consolas,monospace}
.category-status,.category-error{margin:10px 0 0;font-size:12px;color:var(--dim)}
.category-error{color:var(--bad)}
.category-error:empty{display:none}
@media(max-width:920px){
  body{padding:10px}
  .workspace{gap:10px;grid-template-columns:minmax(225px,.9fr) minmax(0,1.1fr);
    grid-template-areas:"controls camera" "controls categories";grid-template-rows:minmax(0,1fr) minmax(0,auto)}
  .category-options{display:flex;overflow:auto;gap:5px;padding:2px}
  .category-options button{flex:none}
}
@media(max-width:560px){
  .workspace{grid-template-columns:minmax(0,1fr);grid-template-areas:"camera" "categories" "controls";
    grid-template-rows:minmax(0,1fr) minmax(0,auto) minmax(0,38%)}
  .category-panel .card{padding:6px}
  .category-panel h2,.robot-panel h1{margin-bottom:6px}
  .category-status,.category-error{margin-top:4px}
}
</style></head><body>
<main class="workspace">
<section class="panel robot-panel" aria-labelledby="control-heading">
<header>
  <h1 id="control-heading">FlexEndoRobo ControlPanel</h1>
  <div class="robot-alerts">
    <div class="status tag-bad" id="motion-fault"></div>
    <div class="status tag-bad" id="command-error" aria-live="polite"></div>
  </div>
</header>
<div class="panel-scroll" tabindex="0" aria-label="Robot controls">

<div class="card actions">
  <button onclick="post('/api/cmd/reset')">RESET</button>
  <button onclick="post('/api/cmd/bird_eye')">BIRD-EYE</button>
  <button onclick="post('/api/cmd/pull_out')">PULL OUT</button>
  <button id="confirm-pull-out" onclick="confirmPullOut()" hidden>Confirm straight and retract</button>
  <button class="estop" onclick="stopSession()">STOP</button>
</div>

    <div class="card"><h3>Arm state</h3>
      <dl class="grid" id="arm-state"></dl>
      <div class="status" id="arm-mode"></div>
      <h3 class="control-title">Arm controls</h3>
      <div class="command-grid">
        <button class="control-mode" onclick="post('/api/mode/arm')">Toggle mode</button>
        <button class="direction control-left" data-key="arm_left" aria-label="Arm left">←</button>
        <button class="direction control-up" data-key="arm_up" aria-label="Arm up">↑</button>
        <button class="direction control-down" data-key="arm_down" aria-label="Arm down">↓</button>
        <button class="direction control-right" data-key="arm_right" aria-label="Arm right">→</button>
        <button class="control-forward" data-key="arm_fwd" aria-label="Arm forward">FWD</button>
        <button class="control-backward" data-key="arm_bwd" aria-label="Arm backward">BWD</button>
      </div>
    </div>

    <div class="card"><h3>Flex state</h3>
      <dl class="grid" id="flex-state"></dl>
      <div class="status" id="flex-mode"></div>
      <pre id="tendons"></pre>
      <div class="actions" id="flex-calibration" hidden>
        <button id="calibrate" onclick="calibrateFlex()">Calibrate</button>
        <button id="flex-return-zero" onclick="post('/api/flex/return_zero')" disabled>Return to Flex zero</button>
        <button id="flex-recheck" onclick="post('/api/flex/recheck_startup')" hidden>Recheck Flex inputs</button>
      </div>
      <p class="status" id="flex-status" role="status"></p>
      <h3 class="control-title">Flex controls</h3>
      <div class="command-grid" id="flex-model-controls">
        <button class="direction control-left" data-key="flex_a">A</button>
        <button class="direction control-up" data-key="flex_w">W</button>
        <button class="direction control-down" data-key="flex_s">S</button>
        <button class="direction control-right" data-key="flex_d">D</button>
        <button class="control-forward" data-key="flex_r">R (arc+)</button>
        <button class="control-backward" data-key="flex_f">F (arc−)</button>
      </div>
      <div class="command-grid" id="flex-hardware-controls" hidden>
        <button class="control-left" data-key="flex_left">Bend left</button>
        <button class="control-up" data-key="flex_up">Bend up</button>
        <button class="control-down" data-key="flex_down">Bend down</button>
        <button class="control-right" data-key="flex_right">Bend right</button>
      </div>
    </div>

</div>
</section>

<section class="panel camera-panel" aria-labelledby="camera-heading">
  <h2 id="camera-heading">SCOPE Camera</h2>
  <div class="camera-stage">
    <div class="camera">
      <img id="camera-feed" alt="Live SCOPE camera with detection boxes">
      <p id="camera-status" role="status">Starting camera…</p>
    </div>
  </div>
  <div class="status diagnostics" id="diag"></div>
</section>

<aside class="panel category-panel" aria-labelledby="categories-heading">
  <h2 id="categories-heading">YOLO Categories</h2>
  <div class="panel-scroll">
    <div class="card">
      <button id="tracking" type="button" aria-pressed="false" disabled>Tracking</button>
      <p id="tracking-status" class="category-status" role="status"></p>
      <div class="category-options" id="category-options"></div>
      <p class="category-status" id="category-status" aria-live="polite">Showing all categories</p>
      <p class="category-error" id="category-error" role="status"></p>
    </div>
  </div>
</aside>
</main>

<script>
async function post(path, body = {}, quiet = false) {
  const error = document.getElementById('command-error');
  try {
    const response = await fetch(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(path === '/api/cmd/stop' ? 12000 : 1000)
    });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || 'Command refused');
    if (!quiet) error.textContent = '';
    return true;
  } catch (e) {
    if (!quiet) error.textContent = e.message;
    return false;
  }
}

const held = new Map();
let keyRequest = Promise.resolve();

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
    if (e.button !== 0 || held.has(key)) return;
    e.preventDefault();
    btn.setPointerCapture(e.pointerId);
    held.set(key, e.pointerId);
    sendKey(key, true, e.pointerId);
  });
  const release = e => {
    if (held.get(key) !== e.pointerId) return;
    held.delete(key);
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
});

function releaseKeys() {
  held.forEach((_, key) => sendKey(key, false));
  held.clear();
}
async function stopSession() {
  held.clear();
  if (await post('/api/cmd/stop')) {
    document.getElementById('command-error').textContent = 'Control session stopped.';
  }
}
function calibrateFlex() {
  const planned = document.getElementById('calibrate').dataset.planned === 'true';
  const prompt = planned ?
    'Is the scope physically straight and the base at its intended zero? This saves the planned position without moving.' :
    'Release the jog controls. Is the scope straight and the base at its intended zero? The scope will stay still.';
  if (window.confirm(prompt)) {
    post('/api/flex/calibrate');
  }
}
function confirmPullOut() {
  if (window.confirm('Is the scope physically straight and the withdrawal path clear? The UR5 will move.')) {
    post('/api/flex/confirm_pull_out');
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
    const r = await fetch('/api/state'); const s = await r.json();
    renderState(s);
  } catch (e) {}
  setTimeout(pollState, 200);
}

function renderReadings(id, readings) {
  document.getElementById(id).innerHTML = readings.map(([label, value]) =>
    `<div><dt>${label}</dt><dd>${value}</dd></div>`).join('');
}

function renderState(s) {
  const names = ['J1 pan', 'J2 lift', 'J3 elbow', 'J4 wr1', 'J5 wr2', 'J6 wr3'];
  renderReadings('arm-state', names.map((name, index) => {
    const degrees = s.joints_deg?.[index];
    return [name, Number.isFinite(degrees) ? `${degrees >= 0 ? '+' : ''}${degrees.toFixed(1)}°` : '—'];
  }));
  document.getElementById('arm-mode').textContent = `Mode: ${s.arm_mode}`;

  const flex = s.flex_hardware;
  const hardware = Boolean(flex);
  document.getElementById('flex-model-controls').hidden = hardware;
  document.getElementById('flex-hardware-controls').hidden = !hardware;
  document.getElementById('flex-calibration').hidden = !hardware;
  const tracking = document.getElementById('tracking');
  const flexReady = hardware && flex.connected && flex.positioning_ready && !flex.fault;
  const flexBlock = hardware && (flex.fault ? 'Resolve the GTS fault shown under Flex state.' :
    !flex.connected ? 'Wait for the Flex driver to connect.' : '');
  const armReady = s.relay_connected && s.joints_received && s.kinematics_ready && !s.motion_fault;
  const rcmReady = armReady && s.arm_mode === 'rcm' && s.rcm_enabled;
  tracking.setAttribute('aria-pressed', String(Boolean(flex?.tracking)));
  tracking.disabled = !flex || (!flex.tracking && (!flexReady ||
    flex.busy || !rcmReady || selectedCategory === null));
  document.getElementById('tracking-status').textContent = flexBlock || flex?.tracking_state || '';
  const birdEye = document.querySelector('[onclick*="/api/cmd/bird_eye"]');
  birdEye.disabled = hardware && (!flexReady || !armReady || flex.busy || flex.tracking);
  birdEye.title = flexBlock || (hardware && !flex.positioning_ready ? 'Calibrate Flex first.' : '');
  document.querySelector('[onclick*="/api/cmd/pull_out"]').disabled =
    hardware && (!flexReady || !flex.pull_out_enabled || !rcmReady ||
    flex.busy || flex.tracking);
  const confirmPullOutButton = document.getElementById('confirm-pull-out');
  confirmPullOutButton.hidden = !flex?.pull_out_confirm;
  confirmPullOutButton.disabled = !flexReady || !rcmReady;
  document.querySelector('[onclick*="/api/cmd/pull_out"]').title =
    flexBlock || (hardware && !flex.pull_out_enabled ? 'Set the bench-verified pull-out clearance at startup.' : '');

  if (flex) {
    const angle = value => Number.isFinite(value) ? value.toFixed(2) + '°' : '—';
    renderReadings('flex-state', [
      [flex.planned_zero ? 'Planned roll from zero' :
        !flex.calibrated || flex.planned_feedback ? 'Planned roll from start' : 'Base roll', angle(flex.roll)],
      [flex.planned_zero ? 'Planned bend from zero' :
        !flex.calibrated || flex.planned_feedback ? 'Planned bend from start' : 'Bend estimate', angle(flex.bend)],
      ['Bend limit', flex.bend_limit === 0 ? 'No software limit' : angle(flex.bend_limit)]
    ]);
    document.getElementById('flex-mode').textContent = 'Mode: fixed';
    document.getElementById('tendons').textContent = flex.tendons ?
      'ΔL [mm]  ' + flex.tendons.map((mm, axis) => `${axis + 1}:${mm.toFixed(2)}`).join('  ') : '';
    const ready = flex.connected && !flex.fault;
    const calibrate = document.getElementById('calibrate');
    calibrate.disabled = !ready || flex.busy || flex.tracking || flex.setup_encoder_mismatch;
    calibrate.dataset.planned = String(flex.planned_feedback);
    calibrate.title = flexBlock || (flex.setup_encoder_mismatch ? 'Calibration requires encoder feedback that follows the setup jog' :
      flex.planned_feedback ? 'Record the current planned position as zero' : '');
    const recheck = document.getElementById('flex-recheck');
    recheck.hidden = !flex.startup_recheck && !flex.recheck_pending;
    recheck.disabled = flex.recheck_pending;
    document.getElementById('flex-return-zero').disabled =
      !ready || flex.busy || flex.tracking || !(flex.calibrated || flex.planned_zero);
    document.querySelectorAll('#flex-hardware-controls button').forEach(button => {
      button.disabled = !ready || flex.setup_encoder_mismatch;
    });
    document.getElementById('flex-status').textContent = [
      !flex.driver_online ? 'Flex driver offline' :
        flex.connected ? (flex.simulated ? 'Simulated driver' : 'Driver connected') :
        flex.fault ? 'Flex driver online' : 'Waiting for Flex driver to acknowledge the session',
      flex.fault ? `GTS fault ${flex.fault}${flex.fault_detail ? ' · ' + flex.fault_detail : ''}` : '',
      flex.startup_recheck ?
        'Check Flex drive power, the shared I/O connection, and the alarm and limit inputs. Then recheck Flex inputs.' :
        flex.fault ? 'Resolve the GTS fault and restart the session.' : '',
      flex.setup_encoder_mismatch ? 'Setup blocked. Planned travel and encoder feedback disagree. See the driver console.' : '',
      flex.planned_feedback ? 'Flex positions are planned. Encoder feedback is unavailable in this mode.' : '',
      ready && !flex.calibrated && !flex.planned_zero && !flex.setup_encoder_mismatch ?
        'Use the jog buttons to straighten and align Flex before calibrating.' : '',
      flex.message
    ].filter(Boolean).join(' · ');
  } else {
    renderReadings('flex-state', [
      ['base_yaw', s.flex_base_yaw.toFixed(2)+'°'],
      ['ext_yaw',  s.flex_ext_yaw.toFixed(2)+'°'],
      ['arc_len',  (s.flex_arc_len*100).toFixed(2)+' cm'],
      ['curv κ',   s.flex_curvature.toFixed(2)+'/m'],
      ['θ_total',  s.flex_theta_total_deg.toFixed(2)+'°'],
      ['θ_seg',    s.flex_theta_seg_deg.toFixed(2)+'°'],
    ]);
    document.getElementById('flex-mode').textContent = 'Mode: fixed';
    const t = s.tendons || {};
    const names4 = ['R','G','B','Y'];
    document.getElementById('tendons').textContent =
      'L[mm]  ' + names4.map(n=>`${n}:${t[n]?t[n][0].toFixed(1):'—'}`).join('  ') +
      '\nΔL[mm] ' + names4.map(n=>`${n}:${t[n]?(t[n][1]>=0?'+':'')+t[n][1].toFixed(1):'—'}`).join('  ');
  }

  document.getElementById('diag').innerHTML =
      `Relay: <span class="${s.relay_connected?'tag-ok':'tag-bad'}">${s.relay_connected?'connected':'offline'}</span> · ` +
      `joints: <span class="${s.joints_received?'tag-ok':'tag-bad'}">${s.joints_received?'ok':'waiting'}</span> · ` +
      `kinematics: <span class="${s.kinematics_ready?'tag-ok':'tag-bad'}">${s.kinematics_ready?'ready':'not ready'}</span>`;
  document.getElementById('motion-fault').textContent = s.motion_fault || '';
}

const cameraImage = document.getElementById('camera-feed');
const cameraStatus = document.getElementById('camera-status');
const categoryStatus = document.getElementById('category-status');
const categoryError = document.getElementById('category-error');
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
  categoryButtons.forEach((button, id) => {
    button.setAttribute('aria-pressed', String(id === selectedCategory));
  });
  categoryStatus.textContent = selectedCategory === null ? 'Showing all categories' :
    `Showing ${categoryButtons.get(selectedCategory).dataset.label.toLowerCase()}`;

  const url = `/api/camera/stream?revision=${viewRevision}`;
  if (camera.ready) {
    cameraStatus.hidden = true;
    if (cameraImage.getAttribute('src') !== url) cameraImage.src = url;
  } else {
    cameraImage.removeAttribute('src');
    cameraStatus.hidden = false;
    cameraStatus.textContent = camera.error || 'Waiting for a camera frame…';
  }
}

cameraImage.addEventListener('error', () => {
  cameraImage.removeAttribute('src');
});

function changeCategory(id) {
  categoryPending++;
  categoryRequest = categoryRequest.then(async () => {
    categoryError.textContent = '';
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
      categoryError.textContent = error.message;
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
    if (!cameraImage.hasAttribute('src')) {
      cameraStatus.hidden = false;
      cameraStatus.textContent = error.message;
    }
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
  const cfg = await (await fetch('/api/config')).json();
  renderCategoryButtons(cfg.categories);
  pollState();
  pollCamera();
}
init().catch(error => {
  cameraStatus.textContent = 'Could not load the control panel.';
  document.getElementById('command-error').textContent = error.message;
});
</script></body></html>
"""

class _Handler(BaseHTTPRequestHandler):
    timeout = 2.0

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, 'application/json', json.dumps(obj).encode('utf-8'))

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == '/':
            self._send(200, 'text/html; charset=utf-8', HTML_PAGE.encode('utf-8'))
        elif url.path == '/api/state':
            with self.server.node.control_lock:
                state = self.server.node.get_state_dict()
            if self.server.relay is not None:
                state['motion_fault'] = self.server.relay.motion_fault()
            elif self.server.node.flex is None:
                state['motion_fault'] = 'Robot connection disabled for camera preview.'
            self._json(state)
        elif url.path == '/api/config':
            self._json({
                'categories': [
                    {'id': class_id, 'label': label.title(), 'color': '#%02x%02x%02x' % COLORS[class_id][::-1]}
                    for class_id, label in CATEGORIES.items()
                ],
            })
        elif url.path == '/api/camera/state':
            self._json(self.server.camera.status())
        elif url.path == '/api/camera/stream':
            try:
                revision = int(parse_qs(url.query).get('revision', [''])[0])
            except ValueError:
                return self._json({'error': 'Expected a camera revision'}, 400)
            self._stream_camera(revision)
        else:
            self._send(404, 'text/plain', b'not found')

    def _stream_camera(self, revision):
        state = self.server.camera.status()
        if revision != state['revision'] or not state['ready']:
            return self._json({'error': 'Camera frame unavailable'}, 503)
        self.send_response(200)
        self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            for jpeg in self.server.camera.frames(revision):
                header = f'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n\r\n'
                self.wfile.write(header.encode('ascii') + jpeg + b'\r\n')
        except OSError:
            pass

    def do_POST(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
            if not 0 <= n <= 4096:
                raise ValueError("Invalid Content-Length")
            if n and self.headers.get_content_type() != 'application/json':
                raise ValueError("Expected application/json")
            body = json.loads(self.rfile.read(n).decode('utf-8')) if n else {}
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object")
        except (ValueError, TimeoutError) as error:
            return self._json({'error': str(error)}, 400)

        origin = self.headers.get('Origin')
        if origin is not None and origin != f"http://{self.headers.get('Host')}":
            return self._json({'error': 'Request must come from the control panel'}, 403)

        if self.path == '/api/camera/category':
            if 'category' not in body:
                return self._json({'error': 'Expected a category ID or null'}, 400)
            try:
                with self.server.node.control_lock:
                    prior = self.server.camera.status()['revision']
                    state = self.server.camera.select_category(body['category'])
                    if self.server.node.flex and state['revision'] != prior:
                        self.server.node.stop_camera_tracking()
            except ValueError as error:
                return self._json({'error': str(error)}, 400)
            return self._json(state)

        if self.path == '/api/cmd/stop':
            return self._stop_session()

        if self.server.relay is None and self.server.node.flex is None:
            return self._json({'error': 'Robot controls are disabled in camera preview.'}, 503)

        with self.server.node.control_lock:
            if self.server.stopping:
                return self._json({'error': 'Control session is stopping.'}, 503)
            try:
                accepted = self._dispatch(body)
            except ValueError as error:
                return self._json({'error': str(error)}, 400)
            except KeyError:
                return self._json({'error': 'Unknown endpoint'}, 404)
        if accepted is False:
            if self.path == '/api/cmd/reset':
                return self._json({'error': 'The UR5 relay did not receive RESET.'}, 409)
            return self._json({'error': 'Command unavailable. Check the current mode and diagnostics.'}, 409)
        self._json({'ok': True})

    def _stop_session(self):
        node = self.server.node
        with node.control_lock:
            if self.server.stopping:
                return self._json({'error': 'Control session is already stopping.'}, 503)
            self.server.stopping = True
            node.cancel_motion()
            flex_request = (node.flex.stop_request
                            if node.flex is not None and node.flex_stop_delivered else None)
        try:
            self._confirm_stop(flex_request)
        finally:
            self.server.shutdown()

    def _confirm_stop(self, flex_request):
        relay = self.server.relay
        try:
            result = relay.submit('estop') if relay is not None else None
        except RuntimeError as error:
            return self._json({'error': str(error), 'flex_stop_sent': flex_request is not None}, 503)
        flex = self.server.node.flex
        flex_stopped = flex is None
        if flex_request is not None:
            deadline = time.monotonic() + FLEX_STOP_CONFIRM_TIMEOUT_S
            while time.monotonic() < deadline:
                with self.server.node.control_lock:
                    if flex.stop_acknowledged(flex_request):
                        flex_stopped = True
                        break
                time.sleep(0.01)
        flex_state = 'disabled' if flex is None else 'confirmed' if flex_stopped else 'unconfirmed'
        if result is None:
            return self._json({'ok': True, 'arm': 'disabled', 'flex': flex_state} if flex_stopped else
                              {'error': 'Flex did not confirm Stop.', 'arm': 'disabled', 'flex': flex_state},
                              200 if flex_stopped else 503)
        try:
            failure = result.result(timeout=DASHBOARD_CONFIRM_TIMEOUT_S)
        except FutureTimeoutError:
            return self._json({'error': 'PolyScope did not confirm Dashboard Stop before the timeout.',
                               'flex': flex_state}, 504)
        if failure:
            return self._json({'error': failure, 'flex': flex_state}, 502)
        if not flex_stopped:
            return self._json({'error': 'PolyScope stopped. Flex did not confirm Stop.'}, 503)
        self._json({'ok': True, 'arm': 'confirmed', 'flex': flex_state})

    def _dispatch(self, body):
        node = self.server.node
        flex = node.flex
        if flex is not None and (
                self.path in ('/api/cmd/bird_eye', '/api/cmd/pull_out', '/api/flex/calibrate',
                              '/api/flex/return_zero', '/api/flex/confirm_pull_out')
                or (self.path == '/api/flex/tracking' and body.get('enabled') is True)
                or (self.path == '/api/key' and body.get('down') is True and
                    isinstance(body.get('key'), str) and body['key'].startswith('flex_'))):
            issue = flex.connection_issue()
            if issue:
                raise ValueError(issue)
        if self.path.startswith('/api/flex/'):
            if node.flex is None:
                raise ValueError("The Flex hardware interface is disabled.")
            if self.path == '/api/flex/calibrate':
                return node.flex.calibrate()
            if self.path == '/api/flex/return_zero':
                return node.flex.return_to_zero()
            if self.path == '/api/flex/recheck_startup':
                return node.flex.recheck_startup_inputs()
            if self.path == '/api/flex/confirm_pull_out':
                return node.flex.confirm_pull_out(node)
            if self.path == '/api/flex/tracking':
                enabled = body.get('enabled')
                if type(enabled) is not bool:
                    raise ValueError("Expected a boolean enabled value.")
                if enabled and any(node._key_state.values()):
                    raise ValueError("Release arm controls before enabling tracking.")
                if not enabled:
                    node.stop_camera_tracking()
                    return True
                return node.flex.set_tracking(True, node)
        if self.path == '/api/key':
            key, down = body.get('key'), body.get('down')
            if not isinstance(key, str) or type(down) is not bool:
                raise ValueError("Expected a key name and a boolean down value")
            return node.set_key(key, down)
        if self.path == '/api/key/heartbeat':
            held = body.get('held')
            if not isinstance(held, list) or not all(isinstance(key, str) for key in held):
                raise ValueError("Expected a list of held keys")
            return node.refresh_keys(held)
        commands = {
            '/api/mode/arm': node.toggle_arm_mode,
            '/api/cmd/reset': node.cmd_reset,
            '/api/cmd/bird_eye': node.cmd_bird_eye_view,
            '/api/cmd/pull_out': node.cmd_pull_out,
        }
        return commands[self.path]()

    def log_message(self, *args):
        pass


def main():
    logging.basicConfig(level=logging.INFO,
                        format='[Windows] %(asctime)s - %(message)s')

    ap = argparse.ArgumentParser()
    ap.add_argument('--urdf', default=Path(__file__).with_name('flex_endo_robo') / 'ur5_endo_flex.urdf',
                    help='Path to ur5_endo_flex.urdf')
    ap.add_argument('--robot-ip', default=UR5_IP, help='UR5 IP address')
    ap.add_argument('--relay-port', type=int, default=TCP_PORT, help='Internal RTDE relay port')
    ap.add_argument('--port-web', type=int, default=8080)
    ap.add_argument('--host-web', default='127.0.0.1')
    ap.add_argument('--rcm-local', nargs=3, type=float, default=[0.0, 0.0, -0.30])
    ap.add_argument('--tip-local', nargs=3, type=float, default=[0.0, 0.0, 0.20195])
    ap.add_argument('--standoff', type=float, default=0.105)
    ap.add_argument('--jog-speed-scale', type=float, default=DEFAULT_JOG_SPEED_SCALE,
                    choices=JOG_SPEED_SCALES,
                    help='Manual arm speed multiplier')
    ap.add_argument('--camera-index', type=int, default=CAMERA_INDEX, help='DirectShow camera index')
    ap.add_argument('--model', type=Path, default=MODEL_PATH, help='Path to YOLO weights')
    run_mode = ap.add_mutually_exclusive_group()
    run_mode.add_argument('--camera-only', action='store_true', help='Passive camera preview')
    run_mode.add_argument('--flex-only', action='store_true', help='Connect Flex hardware without opening RTDE')
    run_mode.add_argument('--arm-only', action='store_true', help='Use RTDE and modeled Flex without a motor driver')
    ap.add_argument('--pull-out-clearance-m', type=float,
                    help='Bench-verified distance behind the RCM along the physical camera axis')
    args = ap.parse_args()
    if args.pull_out_clearance_m is not None and (
            not math.isfinite(args.pull_out_clearance_m) or args.pull_out_clearance_m < 0):
        ap.error('--pull-out-clearance-m must be finite and nonnegative')

    try:
        with ExitStack() as resources:
            srv = resources.enter_context(ThreadingHTTPServer((args.host_web, args.port_web), _Handler))
            srv.stopping = False
            srv.relay = None
            relay_port = args.relay_port
            if not (args.camera_only or args.flex_only):
                relay_server, srv.relay = resources.enter_context(open_relay(args.robot_ip, TCP_HOST, relay_port))
                relay_port = relay_server.getsockname()[1]
            srv.camera = resources.enter_context(CameraFeed(args.camera_index, args.model))
            flex = (None if args.camera_only or args.arm_only else
                    FlexControl(FlexClient(), srv.camera, args.pull_out_clearance_m))
            srv.node = ControlNode(
                relay_host=TCP_HOST,
                relay_port=relay_port,
                urdf_path=args.urdf,
                rcm_point_local=args.rcm_local,
                tip_local=args.tip_local,
                standoff_m=args.standoff,
                jog_speed_scale=args.jog_speed_scale,
                flex=flex,
            )
            if srv.relay is not None:
                relay_shutdown = threading.Event()
                relay_thread = threading.Thread(
                    name='rtde-relay', target=serve_relay,
                    args=(relay_server, srv.relay, relay_shutdown),
                )
                relay_thread.start()
                resources.callback(relay_thread.join)
                resources.callback(relay_shutdown.set)
            if srv.relay is not None or flex is not None:
                resources.callback(srv.node.stop)
                srv.node.start(connect_arm=srv.relay is not None)
            log.info("Web panel: http://%s:%d", args.host_web, srv.server_port)
            try:
                srv.serve_forever()
            finally:
                log.info("Shutting down...")
    except KeyboardInterrupt:
        pass
    except (OSError, RuntimeError, OverflowError) as error:
        log.error("Windows app failed: %s", error)
        return 1
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
