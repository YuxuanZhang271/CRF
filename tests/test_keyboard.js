// node tests/test_keyboard.js — routing, repeat suppression and release behavior.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const listeners = {};
const elements = new Map();
const sent = [];
const held = new Map();
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
  window: {addEventListener: () => {}}, crypto: {randomUUID: () => 'test'},
  fetch: async () => ({ok: true, json: async () => ({available: true, commands: []})}),
  AbortSignal, setTimeout: () => {}, held, stateFeedReady: true, stopping: false, interrupting: false, sessionStopped: false,
  sendKey: (key, down) => sent.push({key, down}),
});
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
for (const [code, id] of Object.entries({KeyB: 'bird-eye', KeyP: 'pull-out', KeyT: 'tracking', KeyR: 'record'})) {
  key('keydown', code, {ctrlKey: true});
  key('keydown', code, {ctrlKey: true, repeat: true});
  assert.equal(element(id).clicks, 1);
}
key('keydown', 'KeyI');
assert.equal(element('interrupt').clicks, 1);
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
key('keydown', 'KeyR', {ctrlKey: true});
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
console.log('All motion/command mappings, release, repeat, editing, modifier and pointer ownership checks passed.');
