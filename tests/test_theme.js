// Dashboard theme changes propagate to passive live tabs without a theme button.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/static/theme.js'), 'utf8');
const key = 'abml-color-theme';

function browser(saved, dark = false, blocked = false) {
  const tabs = [];
  return {
    tab(withButton) {
      const events = {}, documentEvents = {}, systemEvents = {}, clicks = {};
      const root = {dataset: {}};
      const button = withButton ? {
        attrs: {}, setAttribute(name, value) { this.attrs[name] = value; },
        addEventListener(name, fn) { clicks[name] = fn; }
      } : null;
      const media = {matches: dark, addEventListener(name, fn) { systemEvents[name] = fn; }};
      const storage = {
        getItem() { if (blocked) throw new Error('Storage blocked'); return saved; },
        setItem(name, value) {
          if (blocked) throw new Error('Storage blocked');
          saved = value;
          for (const tab of tabs) {
            if (tab.storage !== storage) tab.events.storage({key: name, newValue: value, storageArea: tab.storage});
          }
        }
      };
      const tab = {events, storage, root, button, clicks,
        system(value) { media.matches = value; systemEvents.change({matches: value}); },
        receive(name, value, area = storage) { events.storage({key: name, newValue: value, storageArea: area}); }
      };
      vm.runInNewContext(source, {
        document: {documentElement: root, getElementById: () => button,
          addEventListener(name, fn) { documentEvents[name] = fn; }},
        window: {matchMedia: () => media, addEventListener(name, fn) { events[name] = fn; }},
        localStorage: storage
      });
      documentEvents.DOMContentLoaded();
      tabs.push(tab);
      return tab;
    }
  };
}

const session = browser('dark');
const dashboard = session.tab(true), live = session.tab(false);
assert.equal(live.root.dataset.theme, 'dark');
dashboard.clicks.click();
assert.equal(live.root.dataset.theme, 'light');
assert.equal(dashboard.button.attrs['aria-pressed'], 'true');
assert.equal(session.tab(false).root.dataset.theme, 'light', 'new live tabs restore the dashboard choice');
live.system(true);
assert.equal(live.root.dataset.theme, 'light', 'explicit choice overrides the system theme');
dashboard.clicks.click();
assert.equal(live.root.dataset.theme, 'dark');
live.receive('unrelated-key', 'light');
live.receive(key, 'light', {});
assert.equal(live.root.dataset.theme, 'dark', 'unrelated storage events do not change the theme');
live.receive(key, 'light');
live.receive(null, null);
assert.equal(live.root.dataset.theme, 'dark', 'clearing storage restores the system theme');
live.system(false);
assert.equal(live.root.dataset.theme, 'light');
live.receive(key, 'invalid');
live.system(true);
assert.equal(live.root.dataset.theme, 'dark', 'invalid preferences fall back to the system');

const unavailable = browser(null, false, true);
assert.equal(unavailable.tab(false).root.dataset.theme, 'light');
const localDashboard = unavailable.tab(true);
localDashboard.clicks.click();
assert.equal(localDashboard.root.dataset.theme, 'dark', 'dashboard switching survives blocked storage');
console.log('Shared dashboard/live theme sync, preference fallback and blocked-storage checks passed.');
