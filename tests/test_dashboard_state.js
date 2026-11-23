// State polling must lock safely on failure and recover without obsolete DOM nodes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function main() {
  const elements = new Map();
  const get = id => {
    if (id === 'task-message') return null; // This node is absent from the dashboard.
    if (!elements.has(id)) elements.set(id, {disabled: false, dataset: {activity: 'moving'}});
    return elements.get(id);
  };
  const timers = [], reports = [];
  let calls = 0, releases = 0, locked = false;
  const context = vm.createContext({
    document: {getElementById: get, querySelectorAll: () => [get('arm-up'), get('flex-up')]},
    AbortSignal,
    fetch: async () => {
      if (++calls === 1) throw new Error('Temporary state disconnect');
      return {ok: true, json: async () => ({task: {id: 0, revision: 1}})};
    },
    setTimeout: (fn, delay) => timers.push({fn, delay}),
    renderMeasurements() {}, renderStatusBar() {}, renderTask() {}, syncDeviceLimits() {},
    releaseKeys: () => { releases++; }, releaseFreedrive() {},
    applyTaskLock: () => { locked = !vm.runInContext('stateFeedReady', context); },
    reportStatus: (...args) => reports.push(args),
    renderState: () => { vm.runInContext('stateFeedReady = true', context); locked = false; },
  });
  vm.runInContext(`let stateFeedReady = true, stopping = false;
    const pullOutButton = document.getElementById('pull-out');`, context);
  const source = fs.readFileSync(path.join(__dirname, '../src/static/dashboard.js'), 'utf8');
  vm.runInContext(source.slice(source.indexOf('async function pollState()'), source.indexOf('const speedFieldIds')), context);
  await vm.runInContext('pollState()', context);
  assert.equal(locked, true);
  assert.equal(get('arm-up').disabled, true);
  assert.equal(get('flex-up').disabled, true);
  assert.equal(releases, 1);
  assert.match(get('task-status').title, /manual controls locked/);
  assert.match(reports.at(-1)[2], /Temporary state disconnect/);
  assert.equal(timers.length, 1);
  assert.equal(timers[0].delay, 200);
  await timers[0].fn();
  assert.equal(calls, 2);
  assert.equal(locked, false);
  assert.equal(timers.length, 2);
  assert.equal(reports.at(-1)[2], '');
  console.log('State disconnect locks controls, reports the cause and continues polling to recovery.');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
