// Exercise the real zoom controls without camera/robot hardware.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(dataset = {}) {
    this.dataset = dataset;
    this.style = {};
    this.attrs = {};
    this.listeners = {};
  }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  setAttribute(name, value) { this.attrs[name] = value; }
  setPointerCapture() {}
  fire(type, values = {}) {
    const event = {button: 0, pointerId: 1, detail: 1, preventDefault() {}, ...values};
    for (const handler of this.listeners[type] || []) handler(event);
  }
}

const elements = new Map();
const get = id => {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
};
const presets = [1, 1.5, 2].map(zoom => new Element({zoom: String(zoom)}));
const document = new Element(), window = new Element();
document.getElementById = get;
document.querySelectorAll = () => presets;
const timers = new Map();
let nextTimer = 0;
let time = 0;
let resizeCamera;
get('camera-feed').parentElement = new Element();
const context = vm.createContext({document, window,
  ResizeObserver: class {
    constructor(callback) { resizeCamera = callback; }
    observe(element) { assert.equal(element, get('camera-feed').parentElement); }
  },
  performance: {now: () => time},
  requestAnimationFrame(fn) { timers.set(++nextTimer, fn); return nextTimer; },
  cancelAnimationFrame(id) { timers.delete(id); },
});
const source = fs.readFileSync(path.join(__dirname, '../src/static/dashboard.js'), 'utf8');
vm.runInContext(source.slice(source.indexOf('const cameraImage'), source.indexOf('const categoryButtons')), context);
const slider = get('camera-zoom'), ratio = get('camera-zoom-ratio');
const zoomIn = get('zoom-in'), zoomOut = get('zoom-out');
const tick = (elapsed = 50) => {
  time += elapsed;
  for (const [id, fn] of [...timers]) {
    timers.delete(id);
    fn(time);
  }
};
const check = zoom => {
  assert.ok(Math.abs(Number(slider.value) - zoom) < 1e-9);
  const label = `${Number(Number(slider.value).toFixed(2))}x`;
  assert.equal(ratio.value, label);
  assert.equal(slider.attrs['aria-valuetext'], label);
  const scale = Number(get('camera-feed').style.transform.slice(6, -1));
  assert.ok(Math.abs(scale - zoom) < 1e-9);
};

for (const width of [600, 320]) {
  resizeCamera([{borderBoxSize: [{inlineSize: width}]}]);
  assert.equal(get('camera-zoom-controls').style.width, `${width}px`);
}
check(1);
assert.equal(zoomOut.disabled, true);
for (const [index, zoom] of [1, 1.5, 2].entries()) {
  presets[index].fire('click');
  check(zoom);
  assert.equal(presets[index].attrs['aria-pressed'], 'true');
}
assert.equal(zoomIn.disabled, true);
slider.value = '1.35';
slider.fire('input');
check(1.35);
assert.ok(presets.every(button => button.attrs['aria-pressed'] === 'false'));

zoomIn.fire('pointerdown');
check(1.355);
tick(); tick();
check(1.375);
zoomIn.fire('pointerup');
zoomIn.fire('click'); // A pointer click must not add a second step.
tick();
check(1.375);
assert.equal(timers.size, 0);

for (const event of ['pointercancel', 'lostpointercapture']) {
  zoomOut.fire('pointerdown');
  zoomOut.fire(event);
  assert.equal(timers.size, 0);
}
zoomIn.fire('pointerdown');
zoomIn.fire('pointerup', {pointerId: 2});
assert.equal(timers.size, 1, 'another pointer cannot release the current hold');
window.fire('blur');
assert.equal(timers.size, 0);
zoomOut.fire('pointerdown');
document.hidden = true;
document.fire('visibilitychange');
assert.equal(timers.size, 0);

presets[1].fire('click');
zoomIn.fire('keydown', {code: 'Space'});
zoomIn.fire('keydown', {code: 'Space', repeat: true});
tick();
check(1.515);
document.fire('keyup', {code: 'Space'});
assert.equal(timers.size, 0);
zoomOut.fire('click', {detail: 0});
check(1.505);
zoomIn.fire('keydown', {code: 'Enter'});
zoomIn.fire('blur');
assert.equal(timers.size, 0);

for (const [start, button, end] of [[1.95, zoomIn, 2], [1.05, zoomOut, 1]]) {
  slider.value = start;
  slider.fire('input');
  button.fire('pointerdown');
  for (let frame = 0; frame < 10; frame++) tick();
  check(end);
  assert.equal(button.disabled, true);
  assert.equal(timers.size, 0, 'holding at a limit stops the timer');
}
zoomIn.fire('pointerdown');
presets[1].fire('click');
tick();
check(1.5);
assert.equal(timers.size, 0, 'presets cancel a held adjustment');
zoomIn.fire('pointerdown');
slider.value = 1.75;
slider.fire('input');
tick();
check(1.75);
assert.equal(timers.size, 0, 'slider input cancels a held adjustment');
for (const [value, expected] of [[0, 1], [6, 2]]) {
  slider.value = value;
  slider.fire('input');
  check(expected);
}
// Smooth progress must depend on elapsed time, not monitor refresh rate.
for (const frameMs of [10, 20]) {
  presets[0].fire('click');
  zoomIn.fire('pointerdown');
  for (let elapsed = 0; elapsed < 1000; elapsed += frameMs) tick(frameMs);
  check(1.205);
  zoomIn.fire('pointerup');
}
presets[0].fire('click');
zoomIn.fire('pointerdown');
tick(16);
check(1.0082);
tick(2000);
check(1.0282); // A delayed frame cannot cause a large jump.
zoomIn.fire('pointerup');
console.log('Zoom defaults, presets, slider, crop scale, holds, cancellation, keyboard and limits passed.');
