'use strict';
const ConnectionUI = (() => {
  function create(options) {
    const timers = options.timers || globalThis;
    const now = options.now || Date.now;
    const eventSource = options.eventSource || (url => new EventSource(url));
    let epoch = 0, target = null, abort = null, source = null, retry = 0, watchdog = 0;
    let sequence = -1, lastBeat = 0, polling = false;
    const visible = () => options.visible();
    const valid = generation => generation === epoch && target && visible() && !abort.signal.aborted;
    function clearRetry() { timers.clearTimeout(retry); retry = 0; }
    function closeSource() { source?.close(); source = null; }
    function stop() {
      epoch++;
      abort?.abort();
      clearRetry();
      timers.clearInterval(watchdog);
      watchdog = 0;
      closeSource();
      target = null;
      polling = false;
    }
    function accept(view, generation) {
      if (!valid(generation)) return;
      if (view.id && view.id !== target.id || view.host && view.host !== target.host) return;
      lastBeat = now();
      clearRetry();
      if (view.sequence !== undefined && view.sequence < sequence) return;
      sequence = view.sequence ?? sequence;
      options.onState(view);
      options.onStatus('live');
    }
    function later(action, delay = 3000) {
      clearRetry();
      retry = timers.setTimeout(() => { retry = 0; action(); }, delay);
    }
    async function poll(generation) {
      if (!valid(generation) || polling) return;
      polling = true;
      try {
        const result = await options.poll(target, sequence, abort.signal);
        if (!valid(generation)) return;
        lastBeat = now();
        if (result.state) accept(result.state, generation);
        else options.onStatus('live');
        later(() => poll(generation), 200);
      } catch (error) {
        if (valid(generation)) {
          options.onStatus('offline');
          later(() => poll(generation));
        }
      } finally {
        if (generation === epoch) polling = false;
      }
    }
    function subscribe(generation) {
      if (!valid(generation)) return;
      if (target.transport === 'poll') { poll(generation); return; }
      const channel = eventSource(options.url(target));
      source = channel;
      channel.addEventListener('state', event => {
        if (!valid(generation) || source !== channel) return;
        try { accept(JSON.parse(event.data), generation); }
        catch { options.onStatus('offline'); }
      });
      channel.addEventListener('heartbeat', () => {
        if (!valid(generation) || source !== channel) return;
        lastBeat = now();
        clearRetry();
        options.onStatus('live');
      });
      channel.addEventListener('logout', () => {
        if (valid(generation) && source === channel) options.onLogout();
      });
      channel.onerror = () => {
        if (!valid(generation) || source !== channel) return;
        options.onStatus('offline');
        if (!retry) later(() => {
          if (!valid(generation) || source !== channel) return;
          closeSource();
          poll(generation);
        }, 5000);
      };
    }
    async function snapshot(generation) {
      try {
        const view = await options.snapshot(target, abort.signal);
        if (!valid(generation)) return;
        accept(view, generation);
        subscribe(generation);
      } catch (error) {
        if (valid(generation)) {
          options.onStatus('offline');
          later(() => snapshot(generation));
        }
      }
    }
    function start(next) {
      stop();
      if (!visible()) return Promise.resolve();
      target = { ...next };
      abort = new AbortController();
      sequence = -1;
      lastBeat = now();
      const generation = epoch;
      options.onStatus('syncing');
      watchdog = timers.setInterval(() => {
        if (valid(generation) && now() - lastBeat > 35000) start(target);
      }, 10000);
      return snapshot(generation);
    }
    function matches(next) {
      return Boolean(target && target.id === next.id && target.host === next.host);
    }
    return { start, stop, matches };
  }
  return { create };
})();
if (typeof module !== 'undefined') module.exports = ConnectionUI;
