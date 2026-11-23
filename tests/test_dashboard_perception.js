// Exercise real dashboard rendering and mode requests without a browser/server.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
class Element {
  constructor() { this.dataset = {}; this.attrs = {}; this.children = []; this.listeners = {}; this.style = {setProperty() {}}; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  setAttribute(name, value) { this.attrs[name] = value; }
  getAttribute(name) { return this.attrs[name]; }
  removeAttribute(name) { delete this.attrs[name]; }
  append(...items) { this.children.push(...items); }
  appendChild(item) { this.children.push(item); }
  replaceChildren() { this.children = []; }
}
async function main() {
  const elements = new Map();
  const get = id => {
    if (['stage-tip', 'stage-alternatives', 'stage-confidence', 'stage-confidence-fill', 'stage-history', 'stage-reference-minutes'].includes(id)) return null;
    if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id);
  };
  const requests = [];
  let complete;
  const context = vm.createContext({document: {getElementById: get, createElement: () => new Element()},
    AbortSignal, setTimeout() {}, reportStatus() {}, writeOutput() {}, interruptMotion() {}, selectTask() {},
    fetch: async (url, options) => { requests.push({url, body: JSON.parse(options.body)}); return await new Promise(resolve => { complete = resolve; }); },
  });
  vm.runInContext(`let currentTask = {id: 0}, taskPending = false, stopping = false;
    const cameraImage = document.getElementById('camera-feed');`, context);
  const source = fs.readFileSync(path.join(__dirname, '../src/static/dashboard.js'), 'utf8');
  vm.runInContext(source.slice(source.indexOf('const categoryButtons ='), source.indexOf('let diagnosticCursor =')), context);
  const durations = [40.8, 568.8, 101.4, 583.8, 490.2, 830.4, 216.6];
  const phases = durations.map((duration_s, i) => ({id: i + 1, name: `Phase ${i + 1}`, duration_s}));
  context.camera = {mode: 'full', revision: 2, category: 10, ready: false, vision_status: 'ready',
    categories: Array.from({length: 27}, (_, i) => ({id: i + 1, label: `Surgical ${i + 1}`, color: '#abc'})),
    stage: {status: 'ready', phases, top3: [{id: 2, name: 'Phase 2', prob: .8}, {id: 4, name: 'Phase 4', prob: .1}],
      samples: 5, phase_elapsed_s: 120, history: [{id: 2}, {id: 2}, {id: 4}], tip: 'Unused tip'}};
  vm.runInContext('renderCamera(camera)', context);
  assert.equal(get('category-options').children.length, 27);
  assert.equal(vm.runInContext('categoryButtons.has(0)', context), false);
  assert.equal(get('surgical-stage').dataset.inactive, 'false');
  assert.equal(get('stage-rail').children[1].attrs['aria-current'], 'step');
  const rows = get('stage-rail').children;
  assert.equal(rows[1].children[1].children[1].textContent, '80%');
  assert.equal(rows[1].children[1].children[2].style.width, '80%');
  assert.equal(rows[1].children[1].children[3].textContent, '120 / 568.8 s');
  assert.equal(rows[1].children[0].children[1].attrs['aria-valuemax'], '568.8');
  assert.ok(Math.abs(parseFloat(rows[1].children[0].children[1].children[0].style.height) - 120 / 568.8 * 100) < .0001);
  rows.forEach((row, i) => assert.equal(row.children[1].title, `Statistical duration: ${durations[i]} s`));
  assert.equal(rows[0].children[1].children[1].textContent, '—');
  assert.equal(rows[3].children[1].children[1].textContent, '10%');
  assert.equal(rows[3].children[1].children[2].style.width, '10%');
  context.camera.stage.phase_elapsed_s = 730;
  vm.runInContext('renderCamera(camera)', context);
  assert.equal(get('stage-rail').children[1].children[0].children[1].children[0].style.height, '100%');
  assert.equal(get('stage-rail').children[1].children[1].children[3].textContent, '730 / 568.8 s');
  for (const phase of phases) {
    context.camera.stage.top3 = [{id: phase.id, name: phase.name, prob: .8}];
    context.camera.stage.phase_elapsed_s = phase.duration_s / 2;
    vm.runInContext('renderCamera(camera)', context);
    const track = get('stage-rail').children[phase.id - 1].children[0].children[1];
    assert.equal(track.attrs['aria-valuemax'], String(phase.duration_s));
    assert.equal(track.children[0].style.height, '50%');
    get('stage-rail').children.forEach((row, i) => {
      assert.equal(row.dataset.passed, String(i + 1 < phase.id));
      if (i + 1 !== phase.id) {
        assert.equal(row.children[0].children[1].children[0].style.height, i + 1 < phase.id ? '100%' : '0%');
        assert.equal(row.children[1].children[1].textContent, '—'); // Timing fill does not invent confidence.
        assert.equal(row.children[1].children[2].style.width, '0%');
      }
    });
  }
  // A new lower stage clears the previous progression; no maximum-stage latch.
  context.camera.stage.top3 = [{id: 2, prob: .8}];
  vm.runInContext('renderCamera(camera)', context);
  assert.equal(get('stage-rail').children[4].dataset.passed, 'false');
  assert.equal(get('stage-rail').children[4].children[0].children[1].children[0].style.height, '0%');
  // Stale recognition removes both the active and earlier-stage highlights.
  context.camera.stage.top3 = [];
  vm.runInContext('renderCamera(camera)', context);
  assert.ok(get('stage-rail').children.every(row => row.dataset.passed === 'false'));
  assert.ok(get('stage-rail').children.every(row => row.children[0].children[1].children[0].style.height === '0%'));
  context.camera.stage.top3 = [{id: 6, prob: .99}, {id: 2, prob: .005}];
  vm.runInContext('renderCamera(camera)', context);
  assert.equal(get('stage-rail').children[1].children[1].children[1].textContent, '<1%');
  assert.equal(get('stage-rail').children[1].children[0].children[1].children[0].style.height, '100%');
  assert.equal(get('stage-status').hidden, true);
  assert.equal(get('perception-toggle').attrs['aria-pressed'], 'false');
  get('perception-toggle').listeners.click();
  await new Promise(setImmediate);
  assert.equal(requests[0].url, '/api/camera/mode');
  assert.deepEqual(requests[0].body, {mode: 'test', revision: 2});
  assert.equal(get('perception-toggle').disabled, true);
  complete({ok: true, json: async () => ({mode: 'test', revision: 3, category: null, ready: false,
    categories: [0, 39, 41, 46, 56].map(id => ({id, label: `Test ${id}`, color: '#fff'})),
    stage: {status: 'inactive', phases, top3: [], history: []}})});
  await vm.runInContext('categoryRequest', context);
  assert.equal(get('category-options').children.length, 5);
  assert.equal(get('surgical-stage').attrs['aria-disabled'], 'true');
  assert.equal(get('stage-status').hidden, false);
  assert.ok(get('stage-rail').children.every(row => row.dataset.passed === 'false'));
  assert.equal(get('stage-rail').children[0].children[0].children[1].children[0].style.height, '0%');
  vm.runInContext('renderCamera(camera)', context); // Late full-mode response must be ignored.
  assert.equal(get('category-options').children.length, 5);
  vm.runInContext('currentTask = {id: 16}; renderPerception({mode: "test"})', context);
  assert.equal(get('perception-toggle').disabled, true);
  console.log('Stage-box confidence, statistical-duration rail, overrun, inactive stages and mode revisions passed.');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
