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
let resizeCamera;
get('camera-feed').parentElement = new Element();
const context = vm.createContext({document, window,
  ResizeObserver: class {
    constructor(callback) { resizeCamera = callback; }
    observe(element) { assert.equal(element, get('camera-feed').parentElement); }
  },
  setInterval(fn) { timers.set(++nextTimer, fn); return nextTimer; },
  clearInterval(id) { timers.delete(id); },
});
const source = fs.readFileSync(path.join(__dirname, '../src/static/dashboard.js'), 'utf8');
vm.runInContext(source.slice(source.indexOf('const cameraImage'), source.indexOf('const categoryButtons')), context);
const slider = get('camera-zoom'), ratio = get('camera-zoom-ratio');
const zoomIn = get('zoom-in'), zoomOut = get('zoom-out');
const tick = () => [...timers.values()].forEach(fn => fn());
const check = zoom => {
  assert.equal(Number(slider.value), zoom);
  assert.equal(ratio.value, `${zoom}x`);
  assert.equal(slider.attrs['aria-valuetext'], `${zoom}x`);
  assert.equal(get('camera-feed').style.transform, `scale(${zoom})`);
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
check(1.4);
tick(); tick();
check(1.5);
zoomIn.fire('pointerup');
zoomIn.fire('click'); // A pointer click must not add a second step.
tick();
check(1.5);
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
check(1.6);
document.fire('keyup', {code: 'Space'});
assert.equal(timers.size, 0);
zoomOut.fire('click', {detail: 0});
check(1.55);
zoomIn.fire('keydown', {code: 'Enter'});
zoomIn.fire('blur');
assert.equal(timers.size, 0);

for (const [start, button, end] of [[1.95, zoomIn, 2], [1.05, zoomOut, 1]]) {
  slider.value = start;
  slider.fire('input');
  button.fire('pointerdown');
  tick();
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
console.log('Zoom defaults, presets, slider, crop scale, holds, cancellation, keyboard and limits passed.');
