#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
windows_app.py — HTTP control panel + main entry for the Windows PC.

Usage:
    python windows_app.py --urdf ./ur5_endo_flex.urdf --jetson 192.168.55.1
"""
import argparse, json, threading, time, logging
from http.server import HTTPServer, BaseHTTPRequestHandler

from control_node import ControlNode, TRACK_TARGETS

log = logging.getLogger("WindowsApp")

# Shared across request handlers
_NODE = None
_LAST_KEY_TS = time.monotonic()
_KEY_WATCHDOG_LOCK = threading.Lock()

HTML_PAGE = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>UR5 EndoFlex Control</title>
<style>
:root{--bg:#161624;--card:#232338;--fg:#eaeaf2;--dim:#9292ad;--hi:#ffb86c;--ok:#50fa7b;--bad:#ff5555}
*{box-sizing:border-box}
body{margin:0;padding:14px;font-family:system-ui,Arial;background:var(--bg);color:var(--fg)}
h2{margin:0 0 12px;color:var(--hi)}
.row{display:flex;gap:14px;flex-wrap:wrap}
.col{flex:1 1 380px;min-width:340px}
.card{background:var(--card);border-radius:8px;padding:12px;margin-bottom:12px}
.card h3{margin:0 0 8px;font-size:14px;color:var(--hi);font-weight:600}
button{background:#3a4a6a;color:#fff;border:0;border-radius:5px;padding:8px 12px;cursor:pointer;font-size:13px}
button:hover{background:#4a5c84}
button.estop{background:#b82a2a}button.estop:hover{background:#d03a3a}
button.ok{background:#2a7a4a}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:6px;font:13px ui-monospace,Menlo,Consolas}
.pad{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-top:8px}
.pad button{padding:10px 0}
.status{font:12px ui-monospace,Menlo,Consolas;color:var(--dim);margin:4px 0;white-space:pre-wrap}
pre{background:#1a1a2b;padding:8px;border-radius:4px;font-size:11px;margin:0;max-height:180px;overflow:auto}
.tag-ok{color:var(--ok)}.tag-bad{color:var(--bad)}
.track-btn{padding:8px;font-size:12px}
.track-btn.active{background:#6a4a2a;outline:2px solid var(--hi)}
</style></head><body>
<h2>UR5 + EndoFlex Control Panel</h2>

<div class="card" style="display:flex;gap:6px;flex-wrap:wrap">
  <button onclick="post('/api/cmd/home')">HOME</button>
  <button onclick="post('/api/cmd/reset')">RESET</button>
  <button onclick="post('/api/cmd/bird_eye')">BIRD-EYE</button>
  <button onclick="post('/api/cmd/pull_out')">PULL OUT</button>
  <button class="ok" onclick="post('/api/cmd/stop')">STOP</button>
  <button class="estop" onclick="post('/api/cmd/estop')">E-STOP</button>
</div>

<div class="row">
  <div class="col">
    <div class="card"><h3>Arm state</h3>
      <div class="grid" id="arm-state"></div>
      <div class="status" id="arm-rcm"></div>
      <h3 style="margin-top:10px">Arm controls</h3>
      <button style="width:100%" onclick="post('/api/mode/arm')" id="arm-mode-btn">Toggle mode</button>
      <div class="pad">
        <button data-key="arm_up">↑</button>
        <button data-key="arm_left">←</button>
        <button data-key="arm_down">↓</button>
        <button data-key="arm_right">→</button>
        <button data-key="arm_fwd">Fwd [</button>
        <span></span><span></span>
        <button data-key="arm_bwd">Bwd ]</button>
      </div>
    </div>
  </div>

  <div class="col">
    <div class="card"><h3>Flex state</h3>
      <div class="grid" id="flex-state"></div>
      <pre id="tendons"></pre>
      <h3 style="margin-top:10px">Flex controls</h3>
      <button style="width:100%" onclick="post('/api/mode/flex')" id="flex-mode-btn">Toggle mode</button>
      <div class="pad">
        <button data-key="flex_w">W</button>
        <button data-key="flex_a">A</button>
        <button data-key="flex_s">S</button>
        <button data-key="flex_d">D</button>
        <button data-key="flex_r">R (arc+)</button>
        <span></span><span></span>
        <button data-key="flex_f">F (arc-)</button>
      </div>
    </div>
  </div>
</div>

<div class="card"><h3>Target tracking</h3>
  <div style="display:flex;gap:6px;flex-wrap:wrap" id="track-btns"></div>
  <div style="display:flex;gap:6px;margin-top:8px">
    <button onclick="post('/api/track/stop')">■ STOP</button>
    <button onclick="setTargetsPrompt()">Set targets…</button>
  </div>
  <div class="status" id="track-status"></div>
</div>

<div class="card"><h3>Diagnostics</h3>
  <div class="status" id="diag"></div>
</div>

<script>
const nodeState = {lastState: null};

function post(path, body) {
  return fetch(path, {method:'POST', headers:{'Content-Type':'application/json'},
                      body: body ? JSON.stringify(body) : '{}'}).then(r => r.json());
}

// press-and-hold buttons
document.querySelectorAll('button[data-key]').forEach(btn => {
  const key = btn.dataset.key;
  const dn = e => { e.preventDefault(); post('/api/key', {key, down:true}); };
  const up = e => { e.preventDefault(); post('/api/key', {key, down:false}); };
  btn.addEventListener('mousedown', dn);
  btn.addEventListener('mouseup', up);
  btn.addEventListener('mouseleave', up);
  btn.addEventListener('touchstart', dn);
  btn.addEventListener('touchend', up);
});

// track buttons
const TRACK_TARGETS = [ /* injected below */ ];
function renderTrackButtons() {
  const box = document.getElementById('track-btns');
  box.innerHTML = '';
  TRACK_TARGETS.forEach(([k, label, _rgba]) => {
    const b = document.createElement('button');
    b.className = 'track-btn'; b.textContent = label; b.id = 'track_'+k;
    b.onclick = () => post('/api/track/start', {key:k});
    box.appendChild(b);
  });
}

function setTargetsPrompt() {
  const s = prompt('Enter JSON: {"LN_No3":[x,y,z], ...}');
  if (!s) return;
  try { post('/api/set_targets', JSON.parse(s)); }
  catch (e) { alert('bad JSON: '+e); }
}

// state polling
async function pollState() {
  try {
    const r = await fetch('/api/state'); const s = await r.json();
    nodeState.lastState = s;
    renderState(s);
  } catch (e) {}
  setTimeout(pollState, 200);
}

function jfmt(x){ return x===null ? '—' : (x>=0?'+':'')+x.toFixed(1); }

function renderState(s) {
  // arm joints
  const as = document.getElementById('arm-state');
  const names = ['J1 pan','J2 lift','J3 elbow','J4 wr1','J5 wr2','J6 wr3'];
  as.innerHTML = names.map((n,i)=>`<div>${n}</div><div style="text-align:right">${s.joints_deg?jfmt(s.joints_deg[i])+'°':'—'}</div>`).join('');
  document.getElementById('arm-rcm').innerHTML =
      `Mode: <b>${s.arm_mode}</b><br>RCM: ${s.rcm_enabled ? '<span class="tag-ok">ON</span>' : 'OFF'}` +
      (s.rcm_point ? ` @ (${s.rcm_point.map(x=>x.toFixed(2)).join(',')})` : '');
  document.getElementById('arm-mode-btn').textContent = `Toggle mode (now ${s.arm_mode})`;

  // flex
  const fs = document.getElementById('flex-state');
  const items = [
    ['base_yaw', s.flex_base_yaw.toFixed(2)+'°'],
    ['ext_yaw',  s.flex_ext_yaw.toFixed(2)+'°'],
    ['arc_len',  (s.flex_arc_len*100).toFixed(2)+' cm'],
    ['curv κ',   s.flex_curvature.toFixed(2)+'/m'],
    ['θ_total',  s.flex_theta_total_deg.toFixed(2)+'°'],
    ['θ_seg',    s.flex_theta_seg_deg.toFixed(2)+'°'],
  ];
  fs.innerHTML = items.map(([k,v])=>`<div>${k}</div><div style="text-align:right">${v}</div>`).join('');
  document.getElementById('flex-mode-btn').textContent = `Toggle mode (now ${s.flex_mode})`;
  const t = s.tendons || {};
  const names4 = ['R','G','B','Y'];
  document.getElementById('tendons').textContent =
      'L[mm]  ' + names4.map(n=>`${n}:${t[n]?t[n][0].toFixed(1):'—'}`).join('  ') +
      '\nΔL[mm] ' + names4.map(n=>`${n}:${t[n]?(t[n][1]>=0?'+':'')+t[n][1].toFixed(1):'—'}`).join('  ');

  // tracking
  const zoneLbl = {1:'Flex only',2:'Blending',3:'Arm takeover'};
  const ts = document.getElementById('track-status');
  if (s.tracking) {
    const dist = s.tracking_dist_m===null ? '—' : (s.tracking_dist_m*100).toFixed(1);
    const aim  = s.tracking_aim_deg===null ? '—' : s.tracking_aim_deg.toFixed(1);
    const zone = zoneLbl[s.tracking_zone] || '—';
    ts.innerHTML = `Tracking: <b>${s.tracking}</b> · d=${dist} cm · aim=${aim}° · ${zone}` +
                   (s.tracking_locked ? ' <span class="tag-ok">LOCKED</span>' : '') +
                   (s.tracking_recovering ? ' <span class="tag-bad">RECOVERY</span>' : '');
  } else if (s.tracking_refusal) {
    ts.textContent = `Tracking: ${s.tracking_refusal}`;
  } else {
    ts.textContent = 'Tracking: idle';
  }
  TRACK_TARGETS.forEach(([k])=> {
    const b = document.getElementById('track_'+k);
    if (b) b.classList.toggle('active', k === s.tracking);
  });

  // diag
  document.getElementById('diag').innerHTML =
      `Jetson: <span class="${s.jetson_connected?'tag-ok':'tag-bad'}">${s.jetson_connected?'connected':'offline'}</span> · ` +
      `joints: <span class="${s.joints_received?'tag-ok':'tag-bad'}">${s.joints_received?'ok':'waiting'}</span> · ` +
      `kinematics: <span class="${s.kinematics_ready?'tag-ok':'tag-bad'}">${s.kinematics_ready?'ready':'not ready'}</span>`;
}

async function init() {
  const cfg = await (await fetch('/api/config')).json();
  TRACK_TARGETS.length = 0;
  TRACK_TARGETS.push(...cfg.track_targets);
  renderTrackButtons();
  pollState();
}
init();
</script></body></html>
"""

class _Handler(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, 'application/json', json.dumps(obj).encode('utf-8'))

    def do_GET(self):
        if self.path == '/':
            self._send(200, 'text/html; charset=utf-8', HTML_PAGE.encode('utf-8'))
        elif self.path == '/api/state':
            self._json(_NODE.get_state_dict())
        elif self.path == '/api/config':
            self._json({'track_targets': [[k, l, list(c)] for k, l, c in TRACK_TARGETS]})
        else:
            self._send(404, 'text/plain', b'not found')

    def do_POST(self):
        global _LAST_KEY_TS
        try:
            n = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(n).decode('utf-8')) if n else {}
        except Exception:
            body = {}

        if self.path == '/api/key':
            key = body.get('key'); down = bool(body.get('down'))
            if isinstance(key, str):
                _NODE.set_key(key, down)
                with _KEY_WATCHDOG_LOCK:
                    _LAST_KEY_TS = time.monotonic()
            return self._json({'ok': True})

        if self.path == '/api/mode/arm':
            _NODE.toggle_arm_mode(); return self._json({'ok': True})
        if self.path == '/api/mode/flex':
            _NODE.toggle_flex_mode(); return self._json({'ok': True})
        if self.path == '/api/cmd/home':      _NODE.cmd_home(); return self._json({'ok': True})
        if self.path == '/api/cmd/reset':     _NODE.cmd_reset(); return self._json({'ok': True})
        if self.path == '/api/cmd/bird_eye':  _NODE.cmd_bird_eye_view(); return self._json({'ok': True})
        if self.path == '/api/cmd/pull_out':  _NODE.cmd_pull_out(); return self._json({'ok': True})
        if self.path == '/api/cmd/stop':      _NODE.cmd_stop(); return self._json({'ok': True})
        if self.path == '/api/cmd/estop':     _NODE.cmd_dashboard_stop(); return self._json({'ok': True})

        if self.path == '/api/track/start':
            _NODE.start_tracking(body.get('key')); return self._json({'ok': True})
        if self.path == '/api/track/stop':
            _NODE.stop_tracking(); return self._json({'ok': True})

        if self.path == '/api/set_targets':
            try:
                _NODE.set_targets(body); return self._json({'ok': True})
            except Exception as e:
                return self._json({'error': str(e)}, 400)

        self._send(404, 'text/plain', b'not found')

    def log_message(self, *args):  # silence HTTP access log
        pass


def _key_watchdog():
    """If no key event has come in for 2s, release all keys (browser crashed)."""
    global _LAST_KEY_TS
    while True:
        time.sleep(0.5)
        with _KEY_WATCHDOG_LOCK:
            stale = (time.monotonic() - _LAST_KEY_TS) > 2.0
        if stale and _NODE is not None:
            _NODE.set_all_keys_up()


def main():
    global _NODE
    logging.basicConfig(level=logging.INFO,
                        format='[Windows] %(asctime)s - %(message)s')

    ap = argparse.ArgumentParser()
    ap.add_argument('--urdf', required=True,
                    help='Path to ur5_endo_flex.urdf (or .xacro output)')
    ap.add_argument('--jetson', default='192.168.55.1')
    ap.add_argument('--port-jetson', type=int, default=9000)
    ap.add_argument('--port-web', type=int, default=8080)
    ap.add_argument('--rcm-local', nargs=3, type=float,
                    default=[0.0, 0.0, -0.30])
    ap.add_argument('--tip-local', nargs=3, type=float,
                    default=[0.0, 0.0, 0.20195])
    ap.add_argument('--standoff', type=float, default=0.105)
    args = ap.parse_args()

    _NODE = ControlNode(
        jetson_ip=args.jetson, jetson_port=args.port_jetson,
        urdf_path=args.urdf,
        rcm_point_local=args.rcm_local,
        tip_local=args.tip_local,
        standoff_m=args.standoff,
        flex_state_callback=lambda s: None,  # <-- plug PCIe driver here
    )
    _NODE.start()

    threading.Thread(target=_key_watchdog, daemon=True).start()

    srv = HTTPServer(('0.0.0.0', args.port_web), _Handler)
    log.info(f'Web control panel: http://localhost:{args.port_web}')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _NODE.stop()
        srv.server_close()

if __name__ == '__main__':
    main()