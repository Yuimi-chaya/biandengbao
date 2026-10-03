'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/theme.js'), 'utf8');
function setup(saved = null, dark = false, blocked = false) {
  const listeners = {}, root = { dataset: {}, style: {} };
  const media = { matches: dark, addEventListener: (key, callback) => { listeners.media = callback; } };
  let stored = saved, meta;
  const context = { window: { matchMedia: () => media, addEventListener: (key, cb) => { listeners[key] = cb; } },
    document: { documentElement: root, querySelectorAll: () => [], querySelector: () => ({ setAttribute: (_, value) => { meta = value; } }) },
    localStorage: { getItem: () => { if (blocked) throw Error('blocked'); return stored; }, setItem: (_, value) => { if (blocked) throw Error('blocked'); stored = value; } } };
  const theme = vm.runInNewContext(source + '\nThemeUI;', context);
  return { theme, root, media, listeners, saved: () => stored, meta: () => meta };
}
const auto = setup(null, true);
assert.equal(auto.root.dataset.theme, 'dark');
assert.equal(auto.meta(), '#212121');
auto.media.matches = false; auto.listeners.media();
assert.equal(auto.root.dataset.theme, 'light');
auto.theme.set('dark');
assert.equal(auto.saved(), 'dark');
assert.equal(auto.root.style.colorScheme, 'dark');
auto.media.matches = false; auto.listeners.media();
assert.equal(auto.root.dataset.theme, 'dark');
auto.theme.set('invalid');
assert.equal(auto.theme.preference(), 'dark');
auto.listeners.storage({ key: 'unrelated', newValue: 'light' });
assert.equal(auto.theme.preference(), 'dark');
auto.listeners.storage({ key: 'biandengbao:appearance', newValue: 'light' });
assert.equal(auto.root.dataset.theme, 'light');
auto.media.matches = true; auto.listeners.storage({ key: null, newValue: null });
assert.equal(auto.theme.preference(), 'system');
assert.equal(auto.root.dataset.theme, 'dark');
assert.equal(setup('light', true).root.dataset.theme, 'light');
assert.equal(setup('invalid', true).theme.preference(), 'system');
const restricted = setup(null, false, true);
restricted.theme.set('dark');
assert.equal(restricted.root.dataset.theme, 'dark');
assert.equal(setup('dark', false).root.dataset.theme, 'dark');
console.log('15 theme assertions passed');
