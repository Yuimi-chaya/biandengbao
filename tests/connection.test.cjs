'use strict';
const assert = require('node:assert/strict');
const { create } = require('../web/connection.js');
const flush = () => new Promise(resolve => setImmediate(resolve));
function harness() {
  let time = 0, visible = true, nextTimer = 0;
  const timers = new Map(), sources = [], views = [], statuses = [], reads = [], polls = [];
  const options = {
    visible: () => visible, now: () => time,
    timers: {
      setTimeout: (fn, delay) => { timers.set(++nextTimer, { fn, delay }); return nextTimer; },
      clearTimeout: id => timers.delete(id),
      setInterval: (fn, delay) => { timers.set(++nextTimer, { fn, delay, interval: true }); return nextTimer; },
      clearInterval: id => timers.delete(id)
    },
    snapshot: async (target, signal) => { reads.push({ target, signal }); return { id: target.id, sequence: 1 }; },
    poll: async (target, after, signal) => { polls.push({ target, after, signal }); return { state: null }; },
    url: target => target.id,
    eventSource: url => {
      const handlers = {};
      const source = { url, closed: false, addEventListener: (event, fn) => { handlers[event] = fn; },
        close() { this.closed = true; }, emit: (event, data) => handlers[event]?.({ data: JSON.stringify(data) }) };
      sources.push(source);return source;
    },
    onState: view => views.push(view), onStatus: status => statuses.push(status),
    onLogout: () => { h.loggedOut = true; h.connection.stop(); }
  };
  const h = { options, timers, sources, views, statuses, reads, polls,
    tick(delay) {
      for (const [id, timer] of [...timers]) if (timer.delay === delay) {
        if (!timer.interval) timers.delete(id);
        timer.fn();
      }
    },
    visible(value) { visible = value; },
    advance(ms) { time += ms; }
  };
  h.connection = create(options);
  return h;
}
(async () => {
  let assertions = 0;
  const check = (value, expected) => { assert.deepEqual(value, expected); assertions++; };
  const h = harness();
  await h.connection.start({ id: 'a', host: 'local', transport: 'sse' });
  check(h.sources.length, 1);
  h.sources[0].emit('state', { id: 'a', sequence: 3 });
  h.sources[0].emit('state', { id: 'a', sequence: 2 });
  check(h.views.at(-1).sequence, 3);
  h.sources[0].onerror();
  h.sources[0].emit('heartbeat', {});
  check([...h.timers.values()].some(timer => timer.delay === 5000), false);
  h.sources[0].onerror();
  h.tick(5000);await flush();
  check(h.sources[0].closed, true);
  check(h.polls.length, 1);
  h.connection.stop();
  check(h.timers.size, 0);
  check(h.reads[0].signal.aborted, true);
  await h.connection.start({ id: 'b', host: 'ssh/demo', transport: 'sse' });
  const count = h.views.length;
  h.sources[0].emit('state', { id: 'a', sequence: 999 });
  h.sources[0].emit('logout', {});
  check(h.views.length, count);
  check(h.loggedOut, undefined);
  h.sources[1].emit('state', { id: 'a', host: 'ssh/demo', sequence: 999 });
  h.sources[1].emit('state', { id: 'b', host: 'local', sequence: 999 });
  check(h.views.length, count);
  h.advance(36000);h.tick(10000);await flush();
  check(h.sources[1].closed, true);
  check(h.reads.length, 3);
  h.connection.stop();
  h.visible(false);
  await h.connection.start({ id: 'b', host: 'ssh/demo', transport: 'sse' });
  check(h.connection.matches({ id: 'b', host: 'ssh/demo' }), false);
  check(h.reads.length, 3);

  const race = harness();
  let release;
  race.options.snapshot = (target, signal) => target.id === 'a'
    ? new Promise(resolve => { release = resolve; })
    : Promise.resolve({ id: target.id, sequence: 0 });
  const pending = race.connection.start({ id: 'a', host: 'local', transport: 'sse' });
  await race.connection.start({ id: 'b', host: 'local', transport: 'sse' });
  release({ id: 'a', sequence: 100 });await pending;
  check(race.views.map(view => view.id), ['b']);
  race.visible(false);race.connection.stop();
  check(race.sources.every(source => source.closed), true);
  race.visible(true);
  await race.connection.start({ id: 'b', host: 'local', transport: 'poll' });
  await flush();
  check(race.polls.length, 1);
  race.connection.stop();
  console.log(assertions + ' connection assertions passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
