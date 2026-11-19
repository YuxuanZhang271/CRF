// Exercise task execution requests and manual control ownership using the real handlers.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(id = '') {
    this.id = id;
    this.dataset = {};
    this.attrs = {};
    this.children = [];
    this.listeners = {};
    this.disabled = false;
    this.style = {setProperty() {}};
  }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  setAttribute(name, value) { this.attrs[name] = value; }
  append(...items) { this.children.push(...items); }
  appendChild(item) { this.children.push(item); }
  replaceChildren() { this.children = []; }
}

async function main() {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, new Element(id));
    return elements.get(id);
  };
  const leftIds = ['bird-eye', 'clean-lens', 'pull-out', 'stop', 'interrupt', 'record',
    'arm-reset', 'arm-mode', 'arm-limit', 'arm-linear-speed', 'arm-apply',
    'flex-limit', 'flex-speed', 'flex-apply', 'water-spray', 'gas-spray',
    'calibrate', 'flex-return-zero', 'flex-recheck', 'arm-up', 'flex-up'];
  const left = leftIds.map(get);
  get('arm-up').dataset.key = 'arm_up';
  get('flex-up').dataset.key = 'flex_up';
  const requests = [];
  let revision = 0;
  let finishRequest;
  const context = vm.createContext({
    document: {
      getElementById: get, createElement: () => new Element(), createTextNode: text => text,
      addEventListener() {},
      querySelectorAll(selector) {
        if (selector.startsWith('.robot-panel')) return left;
        if (selector === 'button[data-key]') return [get('arm-up'), get('flex-up')];
        if (selector.startsWith('button[data-key^="arm_')) return [get('arm-up')];
        if (selector === '#flex-hardware-controls button') return [get('flex-up')];
        return [];
      },
    },
    window: {addEventListener() {}}, AbortSignal, performance,
    setTimeout() {}, setInterval() {},
    fetch: async (url, options) => {
      const body = JSON.parse(options.body);
      requests.push({url, body});
      if (url === '/api/task') {
        await new Promise(resolve => { finishRequest = resolve; });
        return {ok: true, json: async () => ({ok: true,
          task: {id: body.id, name: 'Reset', revision: ++revision}})};
      }
      return {ok: true, json: async () => ({ok: true})};
    },
  });
  const source = fs.readFileSync(path.join(__dirname, '../src/static/dashboard.js'), 'utf8');
  vm.runInContext(source.slice(0, source.indexOf('const speedFieldIds')), context);
  vm.runInContext(`
    const pullOutButton = document.getElementById('pull-out');
    let pullOutPending = false, selectedCategory = null;
    const zoomSlider = document.getElementById('camera-zoom'), zoomPresets = [],
      zoomIn = document.getElementById('zoom-in'), zoomOut = document.getElementById('zoom-out');
    let cameraZoom = 1;
    const categoryButtons = new Map();
    function stopZoom() {}
    function setCameraZoom(value) { cameraZoom = value; }
    function renderMeasurements() {}
    function renderStatusBar() {}
    function setStatusChip(id, label, tone) {
      const chip = document.getElementById(id); chip.label = label; chip.tone = tone;
    }
    function syncDeviceLimits(kind, current, max, speed, angleEditable, speedEditable) {
      document.getElementById(kind + '-limit').disabled = !angleEditable;
      document.getElementById(kind + '-speed').disabled = !speedEditable;
      document.getElementById(kind + '-apply').disabled = !angleEditable && !speedEditable;
    }
  `, context);
  vm.runInContext(source.slice(source.indexOf('function renderState('), source.indexOf('const cameraImage')), context);
  context.tasks = Array.from({length: 15}, (_, i) => ({id: i + 1, label: `Task ${i + 1}`, completable: i >= 11}));
  context.state = {task: {id: 0, name: 'Stop', revision: 0}, arm_enabled: true,
    relay_connected: true, joints_received: true, kinematics_ready: true,
    arm_moving: false, arm_mode: 'rcm', rcm_enabled: true,
    flex_hardware: {connected: true, positioning_ready: true, moving: false, fault: 0,
      busy: false, tracking: false, setup_encoder_mismatch: false, recheck_pending: false,
      calibrated: true, bend_limit_supported: true, roll_jog_supported: true,
      tracking_horizon_supported: true, water_ready: true, gas_ready: true}};
  vm.runInContext('renderTaskButtons(tasks); renderRecording({active:false}); renderState(state)', context);
  assert.equal(get('task-options').children.length, 15);
  assert.equal(get('calibrate').disabled, false);
  assert.equal(get('arm-up').disabled, false);
  assert.equal(get('record').disabled, false);

  // Pending selection locks immediately and releases a previously held input.
  vm.runInContext("held.set('arm_up', 1)", context);
  const selected = vm.runInContext('selectTask(12)', context);
  await vm.runInContext('keyRequest', context);
  await Promise.resolve();
  assert.ok(requests.some(item => item.url === '/api/key' && item.body.down === false));
  assert.ok(left.filter(button => button.id !== 'interrupt').every(button => button.disabled));
  assert.equal(get('interrupt').disabled, false);
  finishRequest();
  await selected;
  assert.match(get('task-status').label, /^12/);
  assert.equal(get('task-options').children[11].attrs['aria-pressed'], 'true');

  // Old polling responses and independent recording polls cannot unlock a task.
  vm.runInContext('renderState(state); renderRecording({active:true})', context);
  assert.ok(left.filter(button => button.id !== 'interrupt').every(button => button.disabled));
  assert.equal(vm.runInContext('currentTask.id', context), 12);
  const beforeBlocked = requests.length;
  assert.equal(await vm.runInContext("post('/api/cmd/reset')", context), false);
  assert.equal(requests.length, beforeBlocked);

  // Clicking the active task cannot claim completion; device state restores readiness.
  const beforeRepeat = requests.length;
  await vm.runInContext('selectTask(12)', context);
  assert.equal(requests.length, beforeRepeat);
  assert.equal(vm.runInContext('currentTask.id', context), 12);
  context.state.task = {id: 0, name: 'Stop', revision: 2};
  vm.runInContext('renderState(state)', context);
  assert.equal(get('calibrate').disabled, false);
  assert.equal(get('arm-up').disabled, false);
  assert.equal(get('record').disabled, false);
  assert.equal(get('tracking').disabled, true);
  vm.runInContext('selectedCategory = 39; renderState(state)', context);
  assert.equal(get('tracking').disabled, false, 'Tracking is executable with a selected category and ready devices');
  context.state.task = {id: 3, name: 'Zoom in', revision: 3, zoom: 1.5, phase: 'running'};
  vm.runInContext('renderState(state)', context);
  assert.equal(vm.runInContext('cameraZoom', context), 1.5);
  assert.equal(get('camera-zoom').disabled, true);
  context.state.task = {id: 0, name: 'Stop', revision: 4, zoom: 1.5};
  vm.runInContext('renderState(state)', context);
  assert.equal(get('camera-zoom').disabled, false);
  context.state.task = {id: 16, name: 'Tracking', revision: 5};
  vm.runInContext('renderState(state)', context);
  assert.equal(get('tracking').disabled, false, 'The active tracking button remains available for cancellation');
  assert.equal(get('tracking').attrs['aria-pressed'], 'true');
  assert.equal(get('arm-up').disabled, true);
  context.state.task = {id: 0, name: 'Stop', revision: 6};
  context.state.flex_hardware.fault = 1;
  vm.runInContext('renderState(state)', context);
  assert.equal(get('calibrate').disabled, true);

  vm.runInContext('stateFeedReady = false; applyTaskLock(); renderRecording({active:true})', context);
  assert.equal(get('record').disabled, true);
  assert.ok(left.filter(button => button.id !== 'interrupt').every(button => button.disabled));
  assert.equal(get('interrupt').disabled, false);

  // Categories keep their detector IDs; empty slots have no actions or tab stops.
  vm.runInContext('function changeCategory() {}', context);
  vm.runInContext(source.slice(source.indexOf('function renderCategoryButtons('), source.indexOf('async function init(')), context);
  context.categories = [0, 39, 41, 46, 56].map(id => ({id, label: `Category ${id}`, color: '#fff'}));
  vm.runInContext('renderCategoryButtons(categories)', context);
  assert.equal(get('category-options').children.length, 27);
  assert.deepEqual(get('category-options').children.slice(0, 5).map(button => button.children.at(-1).textContent), [0, 39, 41, 46, 56]);
  assert.ok(get('category-options').children.slice(5).every(slot => slot.attrs['aria-hidden'] === 'true' && !Object.keys(slot.listeners).length));
  // STOP must still send its request after its handler sets the local stopping flag.
  context.state.flex_hardware.fault = 0;
  vm.runInContext('renderState(state)', context);
  await vm.runInContext('stopSession()', context);
  assert.ok(requests.some(item => item.url === '/api/cmd/stop'));
  assert.equal(vm.runInContext('sessionStopped', context), true);
  console.log('Task selection, completion, stale responses, pending locks, releases, readiness restoration and 27 category slots passed.');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
