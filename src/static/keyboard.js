// Commands share button handlers; held motion shares the pointer heartbeat and expiry path.
const shortcutCommands = {KeyB: 'bird-eye', KeyP: 'pull-out', KeyT: 'tracking', KeyR: 'record'};
const armShortcuts = {ArrowUp: 'arm_up', ArrowDown: 'arm_down', ArrowLeft: 'arm_left',
  ArrowRight: 'arm_right', BracketRight: 'arm_fwd', BracketLeft: 'arm_bwd'};
const flexShortcuts = {KeyW: ['flex_w', 'flex_up'], KeyS: ['flex_s', 'flex_down'],
  KeyA: ['flex_a', 'flex_left'], KeyD: ['flex_d', 'flex_right'],
  KeyR: [null, 'flex_roll_plus'], KeyF: [null, 'flex_roll_minus']};
const keyboardHeld = new Map();
const keyboardClient = crypto.randomUUID();

function editing(element) {
  return Boolean(element && (element.isContentEditable || element.closest?.('input, select, textarea, [contenteditable="true"]')));
}

function clickShortcut(id) {
  const button = document.getElementById(id);
  if (button && !button.disabled && !stopping && !sessionStopped) button.click();
}

document.addEventListener('keydown', event => {
  if (event.isComposing || editing(event.target) || event.altKey || event.metaKey) return;
  if (event.ctrlKey && !event.shiftKey && shortcutCommands[event.code]) {
    event.preventDefault();
    if (!event.repeat) clickShortcut(shortcutCommands[event.code]);
    return;
  }
  if (event.ctrlKey || event.shiftKey) return;
  if (event.code === 'KeyI') {
    event.preventDefault();
    if (!event.repeat) clickShortcut('interrupt');
    return;
  }
  const hardware = !document.getElementById('flex-hardware-controls').hidden;
  const key = armShortcuts[event.code] || flexShortcuts[event.code]?.[hardware ? 1 : 0];
  if (!key) return;
  event.preventDefault();
  if (event.repeat || keyboardHeld.has(event.code) || held.has(key) || !stateFeedReady || interrupting || stopping || sessionStopped) return;
  const button = document.querySelector(`button[data-key="${key}"]`);
  if (!button || button.disabled || button.closest('[hidden]')) return;
  const owner = `keyboard:${event.code}`;
  keyboardHeld.set(event.code, {key, owner, button});
  held.set(key, owner);
  button.dataset.held = 'true';
  sendKey(key, true, owner);
});

document.addEventListener('keyup', event => {
  const press = keyboardHeld.get(event.code);
  if (!press) return;
  event.preventDefault();
  keyboardHeld.delete(event.code);
  delete press.button.dataset.held;
  if (held.get(press.key) === press.owner) {
    held.delete(press.key);
    sendKey(press.key, false);
  }
});

window.addEventListener('blur', () => keyboardHeld.clear());
document.addEventListener('visibilitychange', () => {
  if (document.hidden) keyboardHeld.clear();
});

async function pollNativeShortcuts() {
  const enabled = document.hasFocus() && !document.hidden && !editing(document.activeElement) && !stopping && !sessionStopped;
  try {
    const response = await fetch('/api/keyboard', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({client: keyboardClient, enabled}), signal: AbortSignal.timeout(1000)
    });
    if (!response.ok) throw new Error('Keyboard bridge unavailable');
    const state = await response.json();
    if (enabled && document.hasFocus() && !editing(document.activeElement)) {
      state.commands.forEach(clickShortcut);
    }
  } catch (_) { /* Browser key handlers continue to work if the native bridge is unavailable. */ }
  if (!sessionStopped) setTimeout(pollNativeShortcuts, 150);
}
pollNativeShortcuts();
