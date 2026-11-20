// Commands share button handlers; held motion shares the pointer heartbeat and expiry path.
const shortcutCommands = {KeyB: 'bird-eye', KeyP: 'pull-out', KeyT: 'tracking', KeyR: 'record', KeyM: 'arm-mode'};
const armShortcuts = {ArrowUp: 'arm_up', ArrowDown: 'arm_down', ArrowLeft: 'arm_left',
  ArrowRight: 'arm_right', BracketRight: 'arm_fwd', BracketLeft: 'arm_bwd'};
const flexShortcuts = {KeyW: ['flex_w', 'flex_up'], KeyS: ['flex_s', 'flex_down'],
  KeyA: ['flex_a', 'flex_left'], KeyD: ['flex_d', 'flex_right'],
  KeyR: [null, 'flex_roll_plus'], KeyF: [null, 'flex_roll_minus']};
const keyboardHeld = new Map();
const keyboardClient = crypto.randomUUID();
let keyboardFreedrive = false;
let keyboardZoomCode = null;
let altShortcutHandled = false;

function editing(element) {
  return Boolean(element && (element.isContentEditable || element.closest?.('input, select, textarea, [contenteditable="true"]')));
}

function clickShortcut(id) {
  if (!['interrupt', 'tracking'].includes(id) && manualControlsLocked()) return;
  const button = document.getElementById(id);
  if (button && !button.disabled && !stopping && !sessionStopped) button.click();
}

function releaseKeyboardFreedrive() {
  if (!keyboardFreedrive) return;
  keyboardFreedrive = false;
  releaseFreedrive();
}

function releaseKeyboardZoom() {
  if (keyboardZoomCode === null) return;
  if (zoomOwner === `keyboard:${keyboardZoomCode}`) stopZoom();
  keyboardZoomCode = null;
}

function releaseKeyboardMotion() {
  keyboardHeld.forEach(press => {
    delete press.button.dataset.held;
    if (held.get(press.key) === press.owner) {
      held.delete(press.key);
      sendKey(press.key, false);
    }
  });
  keyboardHeld.clear();
  altShortcutHandled = false;
  releaseKeyboardFreedrive();
  releaseKeyboardZoom();
}

document.addEventListener('keydown', event => {
  if (keyboardFreedrive && (!event.altKey || !event.shiftKey || event.ctrlKey || event.metaKey)) {
    releaseKeyboardFreedrive();
  }
  if (event.isComposing || editing(event.target) || event.metaKey || event.ctrlKey) return;
  if (event.altKey && event.shiftKey && event.code === 'KeyF') {
    altShortcutHandled = true;
    event.preventDefault();
    const button = document.getElementById('arm-freedrive');
    if (!event.repeat && !keyboardFreedrive && !freedriveBusy && !freedrivePending &&
        !manualControlsLocked() && button && !button.disabled) {
      keyboardFreedrive = true;
      toggleFreedrive();
    }
    return;
  }
  if (event.altKey && !event.shiftKey && shortcutCommands[event.code]) {
    altShortcutHandled = true;
    event.preventDefault();
    if (!event.repeat) clickShortcut(shortcutCommands[event.code]);
    return;
  }
  if (event.altKey) return;
  const zoomDirection = (event.key === '=' || event.key === '+' ||
      event.code === 'Equal' || event.code === 'NumpadAdd') ? 1 :
    (event.key === '-' || event.code === 'NumpadSubtract' ||
      (event.code === 'Minus' && !event.shiftKey)) ? -1 : 0;
  if (zoomDirection) {
    event.preventDefault();
    const button = document.getElementById(zoomDirection > 0 ? 'zoom-in' : 'zoom-out');
    if (!event.repeat && !button.disabled && !stopping && !sessionStopped) {
      keyboardZoomCode = event.code;
      startZoom(zoomDirection, `keyboard:${event.code}`);
    }
    return;
  }
  if (event.shiftKey) return;
  if (event.code === 'KeyI') {
    event.preventDefault();
    if (!event.repeat) clickShortcut('interrupt');
    return;
  }
  const hardware = !document.getElementById('flex-hardware-controls').hidden;
  const key = armShortcuts[event.code] || flexShortcuts[event.code]?.[hardware ? 1 : 0];
  if (!key) return;
  event.preventDefault();
  if (event.repeat || keyboardHeld.has(event.code) || held.has(key) || manualControlsLocked()) return;
  const button = document.querySelector(`button[data-key="${key}"]`);
  if (!button || button.disabled || button.closest('[hidden]')) return;
  const owner = `keyboard:${event.code}`;
  keyboardHeld.set(event.code, {key, owner, button});
  held.set(key, owner);
  button.dataset.held = 'true';
  sendKey(key, true, owner);
});

document.addEventListener('keyup', event => {
  // The DOM fallback also consumes Alt release after one of our chords.
  // Native captured chords use a Windows menu mask because their letters
  // never reach this handler. An unused Alt key keeps its browser behavior.
  if ((event.code === 'AltLeft' || event.code === 'AltRight') && altShortcutHandled) {
    event.preventDefault();
    altShortcutHandled = false;
  }
  if (keyboardFreedrive && (event.code === 'KeyF' || !event.altKey || !event.shiftKey || event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    releaseKeyboardFreedrive();
  }
  if (keyboardZoomCode === event.code) {
    event.preventDefault();
    releaseKeyboardZoom();
  }
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

window.addEventListener('blur', releaseKeyboardMotion);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) releaseKeyboardMotion();
});
document.addEventListener('focusin', event => {
  if (editing(event.target)) releaseKeyboardMotion();
});

async function pollNativeShortcuts() {
  const enabled = document.hasFocus() && !document.hidden && !editing(document.activeElement) &&
    (!manualControlsLocked() || currentTask?.id === 16);
  try {
    const response = await fetch('/api/keyboard', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({client: keyboardClient, enabled}), signal: AbortSignal.timeout(1000)
    });
    if (!response.ok) throw new Error('Keyboard bridge unavailable');
    const state = await response.json();
    if (enabled && document.hasFocus() && !document.hidden && !editing(document.activeElement)) {
      state.commands.forEach(clickShortcut);
    }
  } catch (_) { /* Browser key handlers continue to work if the native bridge is unavailable. */ }
  if (!sessionStopped) setTimeout(pollNativeShortcuts, 150);
}
pollNativeShortcuts();
