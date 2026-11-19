// Held pointer/keyboard ownership and capability rendering using real handlers.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

class Element {
  constructor(key) {
    this.dataset = key ? {key} : {};
    this.listeners = {};
    this.disabled = false;
    this.attrs = {};
    this.children = [];
  }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  setAttribute(name, value) { this.attrs[name] = value; }
  setPointerCapture() {}
  getBoundingClientRect() { return {left: 0, top: 0, right: 100, bottom: 50}; }
  append(...items) { this.children.push(...items); }
  fire(name, event = {}) {
    const e = {pointerId: 1, button: 0, pointerType: 'mouse', preventDefault() {}, ...event};
    for (const fn of this.listeners[name] || []) fn(e);
  }
}

async function main() {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const water = new Element('flex_water'), gas = new Element('flex_gas');
  elements.set('water-spray', water);
  elements.set('gas-spray', gas);
  const doc = new Element(), win = new Element(), intervals = [], sent = [];
  doc.getElementById = get;
  doc.querySelectorAll = selector => selector === 'button[data-key]' ? [water, gas] : [];
  doc.createElement = () => new Element();
  doc.createTextNode = value => value;
  const context = vm.createContext({document: doc, window: win, AbortSignal, performance,
    setTimeout() {}, setInterval(fn) { intervals.push(fn); },
    fetch: async (url, options) => {
      sent.push({url, body: JSON.parse(options.body)});
      return {ok: true, json: async () => ({ok: true})};
    },
  });
  const source = fs.readFileSync(path.join(__dirname, '../src/static/dashboard.js'), 'utf8');
  vm.runInContext(source.slice(0, source.indexOf('const speedFieldIds')), context);
  vm.runInContext('stateFeedReady = true; currentTask = {id:0, revision:0}', context);
  const flush = () => vm.runInContext('keyRequest', context);
  water.fire('pointerdown');
  await flush();
  assert.deepEqual(sent.at(-1).body, {key: 'flex_water', down: true});
  assert.equal(water.dataset.held, 'true');
  intervals.at(-1)();
  assert.deepEqual(sent.at(-1).body, {held: ['flex_water']});
  water.disabled = true; // State changes cannot strand a pressed button.
  water.fire('pointerup');
  await flush();
  assert.deepEqual(sent.at(-1).body, {key: 'flex_water', down: false});

  for (const event of ['pointercancel', 'lostpointercapture']) {
    gas.fire('pointerdown');
    await flush();
    gas.fire(event);
    await flush();
    assert.deepEqual(sent.at(-1).body, {key: 'flex_gas', down: false});
  }
  gas.fire('pointerdown');
  await flush();
  gas.fire('pointermove', {clientX: 101, clientY: 10});
  await flush();
  assert.equal(sent.at(-1).body.down, false);
  gas.fire('pointerdown');
  await flush();
  win.fire('blur');
  await flush();
  assert.equal(sent.at(-1).body.down, false);
  gas.fire('pointerdown', {pointerType: 'touch'});
  await flush();
  doc.hidden = true;
  doc.fire('visibilitychange');
  await flush();
  assert.equal(sent.at(-1).body.down, false);

  gas.fire('keydown', {code: 'Space'});
  await flush();
  gas.disabled = true;
  doc.fire('keyup', {code: 'Space'});
  await flush();
  assert.deepEqual(sent.at(-1).body, {key: 'flex_gas', down: false});

  vm.runInContext(`
    const pullOutButton = document.getElementById('pull-out');
    let pullOutPending = false, selectedCategory = null;
    const zoomSlider = document.getElementById('camera-zoom'), zoomPresets = [],
      zoomIn = document.getElementById('zoom-in'), zoomOut = document.getElementById('zoom-out');
    let cameraZoom = 1;
    const categoryButtons = new Map();
    function stopZoom() {}
    function renderMeasurements() {}
    function renderStatusBar() {}
    function setStatusChip() {}
    function syncDeviceLimits() {}
  `, context);
  vm.runInContext(source.slice(source.indexOf('function renderState('), source.indexOf('const cameraImage')), context);
  context.state = {task: {id:0, name:'Stop', revision:0}, arm_enabled: false, flex_hardware: {connected: true, moving: false,
    busy: false, tracking: false, fault: 0, water_ready: true, gas_ready: true}};
  vm.runInContext('renderState(state)', context);
  assert.equal(water.disabled, false);
  assert.equal(gas.disabled, false);
  assert.equal(get('clean-lens').disabled, false);
  Object.assign(context.state.flex_hardware, {busy: true, spray_busy: true, spray_action: 'water spray'});
  vm.runInContext('renderState(state)', context);
  assert.equal(water.disabled, false, 'Keep active hold releasable');
  assert.equal(gas.disabled, true);
  assert.equal(get('clean-lens').disabled, true);
  assert.equal(get('bird-eye').disabled, true);
  Object.assign(context.state.flex_hardware, {spray_action: 'clean lens', spray_message: 'CLEAN LENS · water (3 s)'});
  vm.runInContext('renderState(state)', context);
  assert.equal(get('clean-lens').disabled, false, 'Active cleaning can be canceled');
  assert.equal(get('procedure-status').textContent, 'CLEAN LENS · water (3 s)');
  assert.equal(water.disabled, true);
  Object.assign(context.state.flex_hardware, {busy: false, spray_busy: false, spray_action: null, gas_ready: false});
  vm.runInContext('renderState(state)', context);
  assert.equal(water.disabled, false);
  assert.equal(gas.disabled, true);
  assert.equal(get('clean-lens').disabled, true);
  delete context.state.flex_hardware;
  vm.runInContext('renderState(state)', context);
  assert.equal(get('clean-lens').disabled, true);

  const html = fs.readFileSync(path.join(__dirname, '../src/static/index.html'), 'utf8');
  assert.ok(html.indexOf('id="bird-eye"') < html.indexOf('id="clean-lens"'));
  assert.ok(html.indexOf('id="clean-lens"') < html.indexOf('id="pull-out"'));
  const flexSection = html.slice(html.indexOf('<h3>FlexEndo</h3>'));
  assert.ok(flexSection.indexOf('>APPLY<') < flexSection.indexOf('id="water-spray"'));
  assert.ok(flexSection.indexOf('id="water-spray"') < flexSection.indexOf('id="gas-spray"'));
  assert.ok(flexSection.indexOf('id="gas-spray"') < flexSection.indexOf('id="calibrate"'));
  console.log('Spray UI placement, holds, release while disabled, cancellation, focus loss and capability states passed.');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
