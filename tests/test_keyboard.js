// node tests/test_keyboard.js — routing, repeat suppression and release behavior.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const listeners = {};
const windowListeners = {};
const elements = new Map();
const sent = [];
const held = new Map();
const freedrive = [];
const zoom = [];
const editable = {closest: () => true};
const target = {closest: () => null};
function element(id) {
  if (!elements.has(id)) elements.set(id, {disabled: false, hidden: false, dataset: {}, clicks: 0,
    click() { this.clicks++; }, closest() { return null; }});
  return elements.get(id);
}
const context = vm.createContext({
  document: {getElementById: element, querySelector: element, activeElement: target, hidden: false,
    hasFocus: () => true, addEventListener: (name, fn) => { listeners[name] = fn; }},
  window: {addEventListener: (name, fn) => { windowListeners[name] = fn; }}, crypto: {randomUUID: () => 'test'},
  fetch: async () => ({ok: true, json: async () => ({available: true, commands: []})}),
  AbortSignal, setTimeout: () => {}, held, stateFeedReady: true, stopping: false, interrupting: false, sessionStopped: false,
  sendKey: (key, down) => sent.push({key, down}), freedrive, zoom,
});
vm.runInContext(`let taskLocked = false;
  let currentTask = {id:0}, freedriveBusy = false, freedrivePending = false, zoomOwner = null;
  function toggleFreedrive() { freedrive.push(true); freedriveBusy = true; }
  function releaseFreedrive() { freedrive.push(false); freedriveBusy = false; }
  function startZoom(direction, owner) { zoom.push(direction); zoomOwner = owner; }
  function stopZoom() { zoom.push(0); zoomOwner = null; }
  function manualControlsLocked() { return taskLocked || !stateFeedReady || stopping || interrupting || sessionStopped; }`, context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../src/static/keyboard.js'), 'utf8'), context);
function key(type, code, values = {}) {
  const event = {code, target, repeat: false, preventDefault() { this.prevented = true; }, ...values};
  listeners[type](event);
  return event;
}

for (const [code, keyName] of Object.entries({ArrowUp: 'arm_up', ArrowDown: 'arm_down',
  ArrowLeft: 'arm_left', ArrowRight: 'arm_right', BracketRight: 'arm_fwd', BracketLeft: 'arm_bwd',
  KeyW: 'flex_up', KeyS: 'flex_down', KeyA: 'flex_left', KeyD: 'flex_right',
  KeyR: 'flex_roll_plus', KeyF: 'flex_roll_minus'})) {
  assert.ok(key('keydown', code).prevented);
  const count = sent.length;
  key('keydown', code, {repeat: true});
  assert.equal(sent.length, count);
  key('keyup', code, {target: editable}); // Changing focus cannot strand a held motion.
  assert.deepEqual(sent.slice(-2), [{key: keyName, down: true}, {key: keyName, down: false}]);
  assert.equal(held.size, 0);
}
for (const [code, id] of Object.entries({KeyB: 'bird-eye', KeyP: 'pull-out', KeyT: 'tracking', KeyR: 'record', KeyM: 'arm-mode'})) {
  key('keydown', code, {altKey: true});
  key('keydown', code, {altKey: true, repeat: true});
  assert.equal(element(id).clicks, 1);
  assert.ok(!key('keydown', code, {ctrlKey: true}).prevented, 'Old Ctrl shortcuts remain browser actions');
  key('keydown', code, {altKey: true, shiftKey: true});
  assert.equal(element(id).clicks, 1, 'Extra modifiers cannot dispatch a command');
  key('keyup', code, {altKey:true});
  assert.ok(key('keyup', 'AltLeft').prevented, 'Handled Alt chords must not activate the browser menu');
  assert.ok(!key('keyup', 'AltLeft').prevented, 'An unused Alt release keeps browser behavior');
}
assert.ok(!key('keyup', 'AltRight').prevented);
key('keydown', 'KeyM', {altKey:true, target:editable});
assert.ok(!key('keyup', 'AltRight', {target:editable}).prevented, 'Editing must not claim an Alt chord');
key('keydown', 'KeyI');
assert.equal(element('interrupt').clicks, 1);
vm.runInContext('taskLocked = true', context);
const beforeTaskLock = sent.length;
key('keydown', 'ArrowUp');
key('keydown', 'KeyB', {altKey: true});
key('keydown', 'KeyR', {altKey: true});
key('keydown', 'KeyI');
assert.equal(sent.length, beforeTaskLock);
assert.equal(element('bird-eye').clicks, 1);
assert.equal(element('record').clicks, 1);
assert.equal(element('interrupt').clicks, 2);
vm.runInContext('taskLocked = false', context);
const count = sent.length;
key('keydown', 'KeyW', {target: editable});
key('keydown', 'KeyW', {isComposing: true});
key('keydown', 'KeyW', {metaKey: true});
key('keydown', 'KeyW', {ctrlKey: true});
context.interrupting = true;
key('keydown', 'KeyW');
context.interrupting = false;
context.stateFeedReady = false;
key('keydown', 'KeyW');
context.stateFeedReady = true;
assert.equal(sent.length, count);
element('record').disabled = true;
key('keydown', 'KeyR', {altKey: true});
assert.equal(element('record').clicks, 1);
held.set('arm_up', 99); // A keyboard release cannot release someone else's pointer hold.
key('keydown', 'ArrowUp');
key('keyup', 'ArrowUp');
assert.equal(held.get('arm_up'), 99);
held.clear();
element('flex-hardware-controls').hidden = true;
key('keydown', 'KeyW');
key('keyup', 'KeyW');
assert.deepEqual(sent.slice(-2), [{key: 'flex_w', down: true}, {key: 'flex_w', down: false}]);
const beforeRoll = sent.length;
key('keydown', 'KeyR');
assert.equal(sent.length, beforeRoll, 'R must never change modeled arc length');

// Freedrive is a hold, with release handled even after focus or readiness changes.
for (const [code, flags] of [['KeyF', {altKey:true, shiftKey:true}],
    ['AltLeft', {shiftKey:true}], ['ShiftRight', {altKey:true}]]) {
  const before = freedrive.length;
  assert.ok(key('keydown', 'KeyF', {altKey:true, shiftKey:true}).prevented);
  key('keydown', 'KeyF', {altKey:true, shiftKey:true, repeat:true});
  assert.equal(freedrive.length, before + 1);
  context.stateFeedReady = false;
  key('keyup', code, {target:editable, ...flags});
  context.stateFeedReady = true;
  assert.deepEqual(freedrive.slice(-2), [true, false]);
}
key('keydown', 'KeyF', {altKey:true, shiftKey:true});
key('keydown', 'ControlLeft', {altKey:true, shiftKey:true, ctrlKey:true});
assert.equal(freedrive.at(-1), false, 'Changing the chord releases Freedrive');
key('keydown', 'KeyF', {altKey:true, shiftKey:true});
listeners.focusin({target:editable});
assert.equal(freedrive.at(-1), false);
key('keydown', 'KeyF', {altKey:true, shiftKey:true});
windowListeners.blur();
assert.equal(freedrive.at(-1), false);
const beforePointerFreedrive = freedrive.length;
vm.runInContext('freedriveBusy = true', context);
key('keydown', 'KeyF', {altKey:true, shiftKey:true});
key('keyup', 'KeyF', {altKey:true, shiftKey:true});
assert.equal(freedrive.length, beforePointerFreedrive, 'A keyboard release cannot end a pointer toggle');
vm.runInContext('freedriveBusy = false; freedrivePending = true', context);
key('keydown', 'KeyF', {altKey:true, shiftKey:true});
assert.equal(freedrive.length, beforePointerFreedrive);
vm.runInContext('freedrivePending = false', context);
key('keydown', 'KeyF', {altKey:true, shiftKey:true, target:editable});
key('keydown', 'KeyF', {altKey:true, shiftKey:true, ctrlKey:true});
assert.equal(freedrive.length, beforePointerFreedrive);

// Main and numpad zoom keys share smooth hold behavior and preserve pointer ownership.
for (const [code, flags, direction] of [['Equal', {key:'='}, 1], ['Equal', {key:'+', shiftKey:true}, 1],
    ['NumpadAdd', {}, 1], ['Minus', {key:'-'}, -1], ['NumpadSubtract', {}, -1]]) {
  const before = zoom.length;
  assert.ok(key('keydown', code, flags).prevented);
  key('keydown', code, {...flags, repeat:true});
  assert.equal(zoom.length, before + 1);
  key('keyup', code, {target:editable});
  assert.deepEqual(zoom.slice(-2), [direction, 0]);
}
key('keydown', 'Equal', {key:'='});
vm.runInContext('zoomOwner = 99', context);
const beforePointerZoom = zoom.length;
key('keyup', 'Equal');
assert.equal(zoom.length, beforePointerZoom, 'Keyboard release cannot stop a pointer zoom');
element('zoom-in').disabled = true;
key('keydown', 'Equal', {key:'='});
key('keydown', 'Minus', {ctrlKey:true});
key('keydown', 'Minus', {target:editable});
assert.equal(zoom.length, beforePointerZoom);

// Losing focus releases keyboard jogs without affecting an unrelated pointer.
held.set('arm_left', 99);
key('keydown', 'ArrowUp');
windowListeners.blur();
assert.equal(held.get('arm_left'), 99);
assert.deepEqual(sent.at(-1), {key:'arm_up', down:false});
console.log('Alt commands, arm mode, held Freedrive, zoom, repeats, modifiers, focus cleanup and pointer ownership passed.');

// Listening stays reachable during a task and ignores repeats and editable inputs.
vm.runInContext('taskLocked = true', context);
key('keydown', 'KeyL', {altKey: true});
key('keydown', 'KeyL', {altKey: true, repeat: true});
key('keydown', 'KeyL', {altKey: true, target: editable});
assert.equal(element('listen').clicks, 1);
vm.runInContext('taskLocked = false', context);
