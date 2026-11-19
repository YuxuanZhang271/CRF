// Exercise the actual recording handlers without camera/robot hardware or a browser.
// Run: node tests/test_dashboard_recording.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor() {
    this.attrs = {};
    this.dataset = {};
    this.listeners = {};
    this.children = [];
  }
  setAttribute(key, value) { this.attrs[key] = value; }
  addEventListener(key, fn) { this.listeners[key] = fn; }
  append(...values) { this.children.push(...values); }
}

async function main() {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  let active = false;
  let confirmed = true;
  let prompts = 0;
  const requests = [];
  const context = vm.createContext({
    document: {
      getElementById: get, createElement: () => new Element(), createTextNode: text => text,
      querySelectorAll: () => [], addEventListener: () => {},
    },
    window: {addEventListener: () => {}, confirm: () => { prompts++; return confirmed; }},
    fetch: async (url, options = {}) => {
      requests.push({url, options});
      if (url.endsWith('/start')) active = true;
      if (url.endsWith('/stop')) {
        assert.equal(JSON.parse(options.body).confirmed, true);
        active = false;
      }
      return {ok: true, json: async () => ({ok: true, active, session: 'test-session', dropped: {}})};
    },
    AbortSignal, performance, setTimeout: () => {}, setInterval: () => {},
  });
  const script = fs.readFileSync(path.join(__dirname, '../src/static/dashboard.js'), 'utf8');
  // Load recording and input handlers, before unrelated limit/plot initialization.
  vm.runInContext(script.slice(0, script.indexOf('const speedFieldIds')), context);
  vm.runInContext('stateFeedReady = true; currentTask = {id:0, revision:0}', context);
  vm.runInContext('renderRecording({active:false, dropped:{}})', context);
  const button = get('record');
  assert.equal(button.disabled, false);

  await button.listeners.click();
  assert.equal(active, true);
  assert.equal(button.attrs['aria-pressed'], 'true');
  assert.equal(prompts, 0, 'starting does not ask for confirmation');
  assert.ok(requests.some(item => item.url === '/api/recording/console'));

  confirmed = false;
  await button.listeners.click();
  assert.equal(active, true);
  assert.equal(button.disabled, false);
  assert.equal(requests.filter(item => item.url.endsWith('/stop')).length, 0);

  confirmed = true;
  await button.listeners.click();
  assert.equal(active, false);
  assert.equal(button.attrs['aria-pressed'], 'false');
  assert.equal(button.textContent, 'RECORD');
  assert.equal(prompts, 2);

  vm.runInContext('renderRecording({active:false, error:"disk full", dropped:{camera_frame:7}})', context);
  assert.match(get('recording-status').textContent, /disk full/);
  assert.match(get('recording-status').title, /^$/);
  vm.runInContext('renderRecording({active:true, session:"test", elapsed_s:361, segment_seconds:300, dropped:{}})', context);
  assert.match(get('recording-status').textContent, /Slice 00001 · 01:01/);
  console.log('Recording start, canceled stop, confirmed stop, console forwarding and error display passed.');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
