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
    FLEX_MAX_BEND,
    JOG_SPEED_SCALES,
    ControlNode,
)
from flex_endo_robo.flex_endo.flex_control import MAX_FLEX_BEND_DEG, FlexControl
from flex_endo_robo.flex_endo.flex_ipc import FlexClient
from flex_endo_robo.ur5_rtde_server import TCP_HOST, TCP_PORT, UR5_IP, open_relay, serve_relay
from scope.camera import CAMERA_INDEX, CameraFeed
from scope.yolo import CATEGORIES, COLORS, MODEL_PATH

log = logging.getLogger("WindowsApp")
DASHBOARD_CONFIRM_TIMEOUT_S = 10.0
FLEX_STOP_CONFIRM_TIMEOUT_S = 1.0
ARM_INTERRUPT_CONFIRM_TIMEOUT_S = 2.0


def angle_limit(body, maximum, name):
    degrees = body.get('degrees')
    if type(degrees) not in (int, float) or not 1 <= degrees <= maximum or degrees != int(degrees):
        raise ValueError(f'{name} must be a whole number from 1° to {maximum}°.')
    return int(degrees)

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
.primary-actions{position:sticky;top:0;z-index:1;display:grid;grid-template-columns:minmax(0,1fr) auto;
  align-items:start;gap:6px;padding:8px}
.primary-actions button{padding-inline:6px;white-space:nowrap}
.stop-actions{justify-content:flex-end;min-width:0}
#confirm-pull-out{grid-column:1/-1;justify-self:start}
.control-settings{margin-top:8px;align-items:center}
.limit-editor{display:flex;align-items:center;gap:5px;min-width:0}
.limit-editor label{color:var(--dim);font-size:11px}
.limit-editor input{width:50px;padding:6px 3px 6px 6px;border:1px solid var(--edge);border-radius:5px;
  background:var(--bg);color:var(--fg);font:12px ui-monospace,Consolas,monospace}
.limit-editor input:disabled{opacity:.45}
.limit-editor button{padding-inline:6px}
[hidden]{display:none!important}
button{background:#33415d;color:#fff;border:1px solid transparent;border-radius:6px;padding:8px 10px;cursor:pointer;font:600 12px system-ui,sans-serif}
button:hover{background:#435779}
button:focus-visible{outline:2px solid var(--hi);outline-offset:2px}
button:disabled{cursor:default;opacity:.45}
button.estop{background:#a42c3a}button.estop:hover{background:#c53445}
button.interrupt{background:#8a5423}button.interrupt:hover{background:#a6682f}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:5px 12px;margin:0;font:12px ui-monospace,Consolas,monospace}
.grid>div{display:flex;justify-content:space-between;gap:4px;min-width:0}
.grid dt{color:var(--dim)}
.grid dd{margin:0;text-align:right;white-space:nowrap}
.flex-readings{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;align-items:start}
.flex-readings .grid{grid-template-columns:minmax(0,1fr)}
.flex-readings .tendon-readings{padding-left:9px;border-left:1px solid var(--edge)}
.reading-label{margin:0 0 5px;color:var(--dim);font-size:11px}
.unavailable,.motion-plot[data-available="false"]{color:var(--dim);opacity:.55}
.control-title{margin-top:12px!important}
.command-grid{display:grid;gap:5px;grid-template-columns:minmax(45px,1.25fr) repeat(3,minmax(24px,1fr)) minmax(46px,1.25fr);
  grid-template-areas:"mode left up right forward" "mode left down right backward";
  grid-template-rows:repeat(2,minmax(32px,auto))}
.command-grid button{padding:4px;min-width:0}
#flex-hardware-controls,#flex-model-controls{grid-template-columns:repeat(4,minmax(0,1fr));
  grid-template-areas:"left up right forward" "left down right backward"}
.command-grid [data-key]{touch-action:none}
.command-grid .direction{font-size:15px}
.control-mode{grid-area:mode}
.control-left{grid-area:left}.control-up{grid-area:up}.control-down{grid-area:down}.control-right{grid-area:right}
.control-forward{grid-area:forward}.control-backward{grid-area:backward}
.status{font:12px/1.45 ui-monospace,Consolas,monospace;color:var(--dim);margin:4px 0;white-space:pre-wrap;overflow-wrap:anywhere}
.status:empty{display:none}
.camera-stage{container-type:size;display:grid;justify-items:center;align-items:end;min-height:0}
.camera{position:relative;display:grid;place-items:center;width:min(100cqw,100cqh);aspect-ratio:1;border:1px solid var(--edge);border-radius:10px;background:#080a10;overflow:hidden}
.camera img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain}
.camera img:not([src]){visibility:hidden}
.motion-plots{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
.motion-plot{display:grid;justify-items:center;align-content:start;min-width:0;
  margin:0;padding:6px;background:var(--card);border:1px solid var(--edge);border-radius:8px;text-align:center}
.motion-plot figcaption{font-size:12px;font-weight:650}
.motion-plot .status{margin:0}
.plot-field{position:relative;width:min(100%,clamp(64px,14dvh,126px));aspect-ratio:1}
.plot-ring{position:absolute;inset:10%;border:1px solid var(--dim);border-radius:50%;
  background:linear-gradient(transparent 49%,var(--edge) 49%,var(--edge) 51%,transparent 51%),
    linear-gradient(90deg,transparent 49%,var(--edge) 49%,var(--edge) 51%,transparent 51%)}
.plot-ring::before{position:absolute;content:"";inset:25%;border:1px solid var(--edge);border-radius:50%}
.plot-field[data-limited="false"] .plot-ring{border-style:dashed;border-color:var(--edge)}
.plot-tip,.plot-roll{position:absolute;transform:translate(-50%,-50%);border-radius:50%;left:50%;top:50%}
.plot-tip{width:9px;height:9px;background:var(--ok);box-shadow:0 0 0 2px var(--bg)}
.plot-field[data-outside="true"] .plot-tip{background:var(--bad)}
.plot-roll{width:7px;height:7px;border:2px solid var(--hi);background:var(--bg)}
.roll-label{color:var(--hi)}
.status-bar{display:flex;justify-content:center;flex-wrap:wrap;gap:5px;margin:0}
.status-chip{display:flex;align-items:center;gap:5px;padding:4px 7px;border:1px solid var(--edge);
  border-radius:5px;background:var(--card);font:11px ui-monospace,Consolas,monospace}
.status-chip dt{color:var(--dim)}.status-chip dd{margin:0;color:var(--dim)}
.status-chip dd::before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:currentColor;margin-right:4px}
.status-chip[data-tone="ok"] dd{color:var(--ok)}
.status-chip[data-tone="active"] dd{color:var(--hi)}
.status-chip[data-tone="error"] dd{color:var(--bad)}
.console{margin-top:10px}
.console-output{height:clamp(84px,18dvh,210px);overflow:auto;padding:7px;border-radius:5px;
  background:#151926;font:11px/1.5 ui-monospace,Consolas,monospace;overflow-wrap:anywhere}
.console-line+.console-line{margin-top:3px}
.console-source{color:var(--dim);margin-right:6px}
.console-line[data-level="error"]{color:var(--bad)}
.console-line[data-level="success"]{color:var(--ok)}
.category-options{display:grid;gap:7px}
.category-options button{display:flex;align-items:center;gap:9px;text-align:left;background:#253047;border-color:#37435a;padding:10px}
.category-options button:hover{background:#354461}
.category-options button[aria-pressed="true"]{border-color:var(--class-color);background:#33425d}
.swatch{width:9px;height:9px;flex:none;border-radius:50%;background:var(--class-color)}
.category-id{margin-left:auto;color:var(--dim);font:11px ui-monospace,Consolas,monospace}
#category-options{margin-top:8px}
@media(max-width:920px){
  body{padding:10px}
  .workspace{gap:10px;grid-template-columns:minmax(225px,.9fr) minmax(0,1.1fr);
    grid-template-areas:"controls camera" "controls categories";grid-template-rows:minmax(0,1fr) minmax(0,auto)}
  .category-panel{max-height:44dvh}
  .category-options{display:flex;overflow:auto;gap:5px;padding:2px}
  .category-options button{flex:none}
}
@media(max-width:560px){
  .workspace{grid-template-columns:minmax(0,1fr);grid-template-areas:"camera" "categories" "controls";
    grid-template-rows:minmax(0,1fr) minmax(0,auto) minmax(0,38%)}
  .category-panel{max-height:34dvh}
  .category-panel .card{padding:6px}
  .category-panel h2,.robot-panel h1{margin-bottom:6px}
}
</style></head><body>
<main class="workspace">
<section class="panel robot-panel" aria-labelledby="control-heading">
<h1 id="control-heading">FlexEndoRobot ControlPanel</h1>
<div class="panel-scroll" tabindex="0" aria-label="Robot controls">
  <div class="card primary-actions">
    <div class="actions">
      <button onclick="post('/api/cmd/bird_eye')">BIRD-EYE</button>
      <button onclick="post('/api/cmd/pull_out')">PULL OUT</button>
    </div>
    <div class="actions stop-actions">
      <button id="interrupt" class="interrupt" onclick="interruptMotion()" title="Cancel motion and keep the session open">INTERRUPT</button>
      <button id="stop" class="estop" onclick="stopSession()" title="Stop motion and shut down the app">STOP</button>
    </div>
    <button id="confirm-pull-out" onclick="confirmPullOut()" hidden>Confirm straight and retract</button>
  </div>

    <div class="card"><h3>Arm state</h3>
      <dl class="grid" id="arm-state"></dl>
      <div class="actions control-settings">
        <form class="limit-editor" id="arm-limit-form" novalidate>
          <label for="arm-limit">Tip limit (°)</label>
          <input id="arm-limit" type="number" min="1" step="1" required disabled>
          <button type="submit" disabled>APPLY</button>
        </form>
        <button id="arm-reset" onclick="post('/api/cmd/reset')" aria-label="Reset arm">RESET</button>
      </div>
      <h3 class="control-title">Arm controls</h3>
      <div class="command-grid">
        <button id="arm-mode" class="control-mode" onclick="post('/api/mode/arm')" aria-label="Toggle arm mode" aria-pressed="false">Toggle mode</button>
        <button class="direction control-left" data-key="arm_left" aria-label="Arm left">←</button>
        <button class="direction control-up" data-key="arm_up" aria-label="Arm up">↑</button>
        <button class="direction control-down" data-key="arm_down" aria-label="Arm down">↓</button>
        <button class="direction control-right" data-key="arm_right" aria-label="Arm right">→</button>
        <button class="control-forward" data-key="arm_fwd" aria-label="Arm forward">FWD</button>
        <button class="control-backward" data-key="arm_bwd" aria-label="Arm backward">BWD</button>
      </div>
    </div>

    <div class="card"><h3>Flex state</h3>
      <div class="flex-readings">
        <div>
          <p class="reading-label" id="angle-label">Angle</p>
          <dl class="grid" id="flex-state" aria-labelledby="angle-label"></dl>
        </div>
        <div class="tendon-readings">
          <p class="reading-label" id="tendon-label">Tendons (mm)</p>
          <dl class="grid" id="tendons" aria-labelledby="tendon-label"></dl>
        </div>
      </div>
      <div class="actions control-settings">
        <form class="limit-editor" id="flex-limit-form" novalidate>
          <label for="flex-limit">Bend limit (°)</label>
          <input id="flex-limit" type="number" min="1" step="1" required disabled>
          <button type="submit" disabled>APPLY</button>
        </form>
        <div class="actions" id="flex-calibration" hidden>
          <button id="calibrate" onclick="calibrateFlex()">CALIBRATE</button>
          <button id="flex-return-zero" onclick="post('/api/flex/return_zero')" aria-label="Reset Flex to saved zero" disabled>RESET</button>
          <button id="flex-recheck" onclick="post('/api/flex/recheck_startup')" hidden>Recheck Flex inputs</button>
        </div>
      </div>
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
        <button class="direction control-left" data-key="flex_left" aria-label="Bend left">←</button>
        <button class="direction control-up" data-key="flex_up" aria-label="Bend up">↑</button>
        <button class="direction control-down" data-key="flex_down" aria-label="Bend down">↓</button>
        <button class="direction control-right" data-key="flex_right" aria-label="Bend right">→</button>
        <button class="control-forward" data-key="flex_roll_plus" aria-label="Jog positive base roll" disabled>roll+</button>
        <button class="control-backward" data-key="flex_roll_minus" aria-label="Jog negative base roll" disabled>roll-</button>
      </div>
    </div>

</div>
</section>

<section class="panel camera-panel" aria-labelledby="camera-heading">
  <h2 id="camera-heading">SCOPE Camera</h2>
  <div class="camera-stage">
    <div class="camera">
      <img id="camera-feed" alt="Live SCOPE camera with detection boxes">
    </div>
  </div>
  <dl class="status-bar" aria-label="Robot status">
    <div class="status-chip" id="arm-status"><dt>ARM</dt><dd>Disconnected</dd></div>
    <div class="status-chip" id="flex-connection"><dt>FLEX</dt><dd>Disconnected</dd></div>
    <div class="status-chip" id="motion-status"><dt>MOTION</dt><dd>Unavailable</dd></div>
  </dl>
</section>

<aside class="panel category-panel" aria-labelledby="categories-heading">
  <h2 id="categories-heading">YOLO Categories</h2>
  <div class="panel-scroll">
    <div class="card">
      <button id="tracking" type="button" aria-pressed="false" disabled>Tracking</button>
      <div class="category-options" id="category-options"></div>
    </div>
    <div class="motion-plots">
      <figure class="motion-plot" id="rcm-plot" data-available="false">
        <figcaption>UR5 RCM tip</figcaption>
        <div class="plot-field" aria-hidden="true">
          <div class="plot-ring"></div><div class="plot-tip" hidden></div>
        </div>
        <div class="status plot-reading">Tilt —</div>
      </figure>
      <figure class="motion-plot" id="flex-plot" data-available="false">
        <figcaption>FlexEndo</figcaption>
        <div class="plot-field" aria-hidden="true">
          <div class="plot-ring"></div><div class="plot-roll" hidden></div><div class="plot-tip" hidden></div>
        </div>
        <div class="status plot-reading">Bend —</div>
        <div class="status plot-roll-reading roll-label">Roll —</div>
      </figure>
    </div>
    <section class="card console" aria-labelledby="console-heading">
      <h3 id="console-heading">Console</h3>
      <div class="console-output" id="console-output" role="log" aria-labelledby="console-heading" tabindex="0"></div>
    </section>
  </div>
</aside>
</main>

<script>
const consoleOutput = document.getElementById('console-output');
const reportedStatus = new Map();

function writeOutput(source, message, level = 'info') {
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
    if (e.button !== 0 || held.has(key) || interrupting || stopping) return;
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
  if (stopping) return;
  stopping = true;
  document.getElementById('stop').disabled = true;
  document.getElementById('interrupt').disabled = true;
  held.clear();
  writeOutput('APP', 'Stop requested. Ending the control session.');
  if (await post('/api/cmd/stop')) {
    sessionStopped = true;
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
    const response = await fetch('/api/state', {signal: AbortSignal.timeout(3000)});
    if (!response.ok) throw new Error('State unavailable');
    renderState(await response.json());
    reportStatus('state-feed', 'APP', '');
  } catch (e) {
    renderMeasurements();
    renderStatusBar({});
    syncLimitEditor('arm', null, null, false);
    syncLimitEditor('flex', null, null, false);
    if (!stopping) reportStatus('state-feed', 'APP', 'Control state unavailable.', 'error');
  }
  setTimeout(pollState, 200);
}

function syncLimitEditor(kind, current, maximum, editable, canApply = editable) {
  const field = document.getElementById(`${kind}-limit`);
  const form = document.getElementById(`${kind}-limit-form`);
  if (Number.isFinite(maximum)) field.max = maximum;
  if (!field.dataset.dirty) field.value = current ?? '';
  field.disabled = !editable;
  form.querySelector('button').disabled = !canApply;
}

function limitFromInput(field) {
  if (!field.validity.valid || !Number.isInteger(field.valueAsNumber)) {
    writeOutput('CMD', `Enter a whole angle from 1° to ${field.max}°.`, 'error');
    return null;
  }
  return field.valueAsNumber;
}

for (const kind of ['arm', 'flex']) {
  const field = document.getElementById(`${kind}-limit`);
  const form = document.getElementById(`${kind}-limit-form`);
  field.addEventListener('input', () => { field.dataset.dirty = 'true'; });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (form.querySelector('button').disabled || stopping || interrupting) return;
    const degrees = limitFromInput(field);
    if (degrees === null) return;
    if (await post(`/api/limits/${kind}`, {degrees})) {
      delete field.dataset.dirty;
      if (kind === 'arm') writeOutput('ARM', `RCM tip limit set to ${degrees}°.`, 'success');
      else if (field.dataset.modeled === 'true') writeOutput('FLEX', `Modeled bend limit set to ${degrees}°.`, 'success');
    }
  });
}

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

function renderMotionPlot(id, point, limit, label, roll = null) {
  const plot = document.getElementById(id);
  const field = plot.querySelector('.plot-field');
  const tip = plot.querySelector('.plot-tip');
  const rollAvailable = Number.isFinite(roll);
  const limited = limit > 0;
  const magnitude = point && Math.hypot(...point);
  const outside = limited && magnitude > limit + 0.01;
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
  reading.textContent = `${label} ${formatReading(magnitude, '°', 1) ?? '—'} · ${boundary}`;
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
  renderMotionPlot('rcm-plot', rcm ? [rcm[0], -rcm[1]] : null, s.rcm_cone_deg, 'Tilt');

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
  else if (s.arm_moving === true || flex?.moving === true) setStatusChip('motion-status', 'Moving', 'active');
  else if ((s.arm_enabled || flex) && armStill && flexStill) setStatusChip('motion-status', 'Standstill', 'ok');
  else setStatusChip('motion-status', 'Unavailable');
}

function renderState(s) {
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
  const limitEditable = !stopping && !interrupting && !s.stopping && !s.interrupting;
  const armLimitReady = armReady && s.arm_moving === false && limitEditable;
  syncLimitEditor('arm', s.rcm_cone_deg, s.rcm_cone_max_deg, armLimitReady);
  document.getElementById('arm-reset').disabled = !armReady || !limitEditable;
  document.getElementById('arm-mode').disabled = (!armReady && s.arm_mode !== 'rcm') || !limitEditable;
  document.getElementById('flex-limit').dataset.modeled = String(!hardware);
  reportStatus('arm-fault', 'ARM', s.motion_fault, s.stopping ? 'info' : 'error');
  reportStatus('arm-kinematics', 'ARM', s.arm_enabled && s.kinematics_ready === false ? 'Kinematics unavailable.' : '', 'error');
  reportStatus('arm-mode', 'ARM', armReady ? `Arm mode ${s.arm_mode.toUpperCase()}.` : '');
  tracking.setAttribute('aria-pressed', String(Boolean(flex?.tracking)));
  tracking.disabled = !flex || (!flex.tracking && (!flexReady ||
    flex.busy || !rcmReady || selectedCategory === null));
  reportStatus('tracking', 'TRACKING', flex?.tracking_state);
  const birdEye = document.querySelector('[onclick*="/api/cmd/bird_eye"]');
  birdEye.disabled = hardware && (!flexReady || !armReady || flex.busy || flex.tracking);
  document.querySelector('[onclick*="/api/cmd/pull_out"]').disabled =
    hardware && (!flexReady || !flex.pull_out_enabled || !rcmReady ||
    flex.busy || flex.tracking);
  const confirmPullOutButton = document.getElementById('confirm-pull-out');
  confirmPullOutButton.hidden = !flex?.pull_out_confirm;
  confirmPullOutButton.disabled = !flexReady || !rcmReady;
  reportStatus('pull-out-setup', 'ARM', hardware && !flex.pull_out_enabled ?
    'Pull Out requires a bench-verified clearance at startup.' : '');

  if (flex) {
    const ready = flex.connected && !flex.fault;
    const calibrate = document.getElementById('calibrate');
    calibrate.disabled = !ready || flex.busy || flex.tracking || flex.setup_encoder_mismatch;
    calibrate.dataset.planned = String(flex.planned_feedback);
    const editable = limitEditable && !calibrate.disabled && flex.bend_limit_supported;
    syncLimitEditor('flex', flex.bend_limit || flex.bend_limit_max, flex.bend_limit_max,
      editable, editable && flex.bend_limit > 0);
    const recheck = document.getElementById('flex-recheck');
    recheck.hidden = !flex.startup_recheck && !flex.recheck_pending;
    recheck.disabled = flex.recheck_pending;
    document.getElementById('flex-return-zero').disabled =
      !ready || flex.busy || flex.tracking || !(flex.calibrated || flex.planned_zero);
    document.querySelectorAll('#flex-hardware-controls button').forEach(button => {
      const needsRollSupport = button.dataset.key.startsWith('flex_roll_') && !flex.roll_jog_supported;
      button.disabled = !ready || flex.setup_encoder_mismatch || needsRollSupport;
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
      'Rebuild and restart the Flex driver for adjustable bend limits and roll jogging.' : '');
    reportStatus('flex-message', 'FLEX', flex.message);
  } else {
    syncLimitEditor('flex', s.flex_model_bend_limit, s.flex_model_bend_max_deg, Boolean(s.arm_enabled && limitEditable));
  }
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
  const cfg = await (await fetch('/api/config')).json();
  renderCategoryButtons(cfg.categories);
  pollState();
  pollCamera();
}
renderMeasurements();
init().catch(error => {
  writeOutput('APP', `Could not load the control panel. ${error.message}`, 'error');
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
                state['stopping'] = self.server.stopping
                state['interrupting'] = self.server.interrupting
            state['arm_enabled'] = self.server.relay is not None
            if self.server.relay is not None:
                state['motion_fault'] = self.server.relay.motion_fault()
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
        if self.path == '/api/cmd/interrupt':
            return self._interrupt_motion()

        if self.server.relay is None and self.server.node.flex is None:
            return self._json({'error': 'Robot controls are disabled in camera preview.'}, 503)

        with self.server.node.control_lock:
            if self.server.stopping:
                return self._json({'error': 'Control session is stopping.'}, 503)
            if self.server.interrupting:
                return self._json({'error': 'Waiting for motion to stop.'}, 409)
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

    def _interrupt_motion(self):
        node = self.server.node
        started = False
        try:
            with node.control_lock:
                if self.server.stopping or self.server.interrupting:
                    return self._json({'error': 'A stop request is already in progress.'}, 409)
                if self.server.relay is None and node.flex is None:
                    return self._json({'error': 'Robot controls are disabled in camera preview.'}, 503)
                self.server.interrupting = started = True
                arm_result = node.cmd_interrupt(self.server.relay is not None)
                flex_request = node.flex.stop_request if node.flex_stop_delivered else None
            flex = self._wait_for_flex_stop(flex_request)
            arm_failure = None
            if arm_result is not None:
                try:
                    arm_failure = arm_result.result(timeout=ARM_INTERRUPT_CONFIRM_TIMEOUT_S)
                except FutureTimeoutError:
                    arm_failure = 'The UR5 did not confirm that motion stopped.'
        finally:
            if started:
                with node.control_lock:
                    self.server.interrupting = False
        arm = 'disabled' if arm_result is None else 'unconfirmed' if arm_failure else 'confirmed'
        errors = [arm_failure, 'Flex did not confirm that motion stopped.' if flex == 'unconfirmed' else None]
        error = ' '.join(filter(None, errors))
        self._json({'error': error, 'arm': arm, 'flex': flex} if error else
                   {'ok': True, 'arm': arm, 'flex': flex}, 503 if error else 200)

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
        flex_state = self._wait_for_flex_stop(flex_request)
        flex_stopped = flex_state != 'unconfirmed'
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

    def _wait_for_flex_stop(self, flex_request):
        flex = self.server.node.flex
        if flex is None:
            return 'disabled'
        if flex_request is not None:
            deadline = time.monotonic() + FLEX_STOP_CONFIRM_TIMEOUT_S
            while time.monotonic() < deadline:
                with self.server.node.control_lock:
                    if flex.stop_acknowledged(flex_request):
                        return 'confirmed'
                time.sleep(0.01)
        return 'unconfirmed'

    def _dispatch(self, body):
        node = self.server.node
        flex = node.flex
        if self.path == '/api/limits/arm':
            if self.server.relay is None:
                raise ValueError('The arm is disabled for this session.')
            maximum = round(math.degrees(node.RCM_CONE_HALF_ANGLE))
            return node.set_rcm_cone_limit(angle_limit(body, maximum, 'RCM tip limit'))
        if self.path == '/api/limits/flex':
            maximum = MAX_FLEX_BEND_DEG if flex else round(math.degrees(FLEX_MAX_BEND))
            degrees = angle_limit(body, maximum, 'Flex bend limit')
            return flex.set_bend_limit(degrees) if flex else node.set_model_bend_limit(degrees)
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
                degrees = angle_limit(body, MAX_FLEX_BEND_DEG, 'Flex bend limit') if 'degrees' in body else None
                return node.flex.calibrate(degrees)
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
            srv.interrupting = False
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
