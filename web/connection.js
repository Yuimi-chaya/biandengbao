'use strict';
const ConnectionUI = (() => {
  function create(options) {
    const timers = options.timers || globalThis;
    const now = options.now || Date.now;
    const eventSource = options.eventSource || (url => new EventSource(url));
    let epoch = 0, target = null, abort = null, source = null, retry = 0, pollTimer = 0, watchdog = 0;
    let sequence = -1, lastBeat = 0, polling = false, baseline = null, snapshotBusy = false, needsSnapshot = false;
    const visible = () => options.visible();
    const valid = generation => generation === epoch && target && visible() && !abort?.signal.aborted;
    function clearRetry() { timers.clearTimeout(retry); retry = 0; }
    function closeSource() { source?.close(); source = null; }
    function stop() {
      epoch++;
      abort?.abort();
      clearRetry();
      timers.clearTimeout(pollTimer);pollTimer=0;
      timers.clearInterval(watchdog);
      watchdog = 0;
      closeSource();
      target = null;
      polling = false;
      baseline = null;
      snapshotBusy=false;needsSnapshot=false;
    }
    function accept(view, generation) {
      if (!valid(generation)) return;
      if (view.id && view.id !== target.id || view.host && view.host !== target.host) return;
      lastBeat = now();
      if (view.sequence !== undefined && view.sequence <= sequence) {
        options.onStatus(needsSnapshot?'syncing':'live');
        return;
      }
      if(view.delta){
        if(!baseline||view.baseSequence!==baseline.sequence){needsSnapshot=true;options.onStatus('syncing');snapshot(generation);return;}
        const turns=new Map(baseline.turns.map(turn=>[turn.id,turn]));
        for(const turn of view.turns){
          if(turn.messagesDelta){
            const prior=turns.get(turn.id);
            if(!prior){needsSnapshot=true;snapshot(generation);return;}
            const messages=new Map(prior.messages.map(message=>[message.id,message]));
            for(const message of turn.messages)messages.set(message.id,message);
            if(turn.messageOrder.some(id=>!messages.has(id))){needsSnapshot=true;snapshot(generation);return;}
            turns.set(turn.id,{...turn,messages:turn.messageOrder.map(id=>messages.get(id))});
          }else turns.set(turn.id,turn);
        }
        if(view.turnOrder.some(id=>!turns.has(id))){needsSnapshot=true;snapshot(generation);return;}
        view={...view,turns:view.turnOrder.map(id=>turns.get(id))};
      }
      options.onState(view);
      baseline=view;
      needsSnapshot=false;
      if(!polling)clearRetry();
      sequence = view.sequence ?? sequence;
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
        else options.onStatus(sequence>=0&&!needsSnapshot?'live':'syncing');
        schedulePoll(generation,200);
      } catch (error) {
        if (valid(generation)) {
          options.onStatus('offline');
          schedulePoll(generation,3000);
        }
      } finally {
        if (generation === epoch) polling = false;
      }
    }
    function schedulePoll(generation,delay){
      timers.clearTimeout(pollTimer);
      pollTimer=timers.setTimeout(()=>{pollTimer=0;poll(generation);},delay);
    }
    function subscribe(generation) {
      if (!valid(generation)) return;
      if (target.transport === 'poll') { poll(generation); return; }
      const channel = eventSource(options.url(target));
      source = channel;
      channel.addEventListener('state', event => {
        if (!valid(generation) || source !== channel) return;
        try { accept(JSON.parse(event.data), generation); }
        catch { needsSnapshot=true;options.onStatus('offline');snapshot(generation); }
      });
      channel.addEventListener('heartbeat', () => {
        if (!valid(generation) || source !== channel) return;
        lastBeat = now();
        if(sequence>=0&&!needsSnapshot){clearRetry();options.onStatus('live');}
        else options.onStatus('syncing');
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
      if(!valid(generation)||snapshotBusy)return;
      snapshotBusy=true;
      try {
        const view = await options.snapshot(target, abort.signal);
        if (!valid(generation)) return;
        accept(view, generation);
      } catch (error) {
        if (valid(generation) && (sequence < 0||needsSnapshot)) {
          options.onStatus('offline');
          later(() => snapshot(generation));
        }
      }finally{if(generation===epoch){snapshotBusy=false;if(needsSnapshot&&!retry)later(()=>snapshot(generation),200);}}
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
      subscribe(generation);
      return snapshot(generation);
    }
    function matches(next) {
      return Boolean(target && target.id === next.id && target.host === next.host);
    }
    function recover() {
      if (target && visible() && now() - lastBeat > 10000) return start(target);
    }
    return { start, stop, matches, recover, generation: () => epoch,
      receive: (view, generation) => accept(view, generation) };
  }
  return { create };
})();
if (typeof module !== 'undefined') module.exports = ConnectionUI;
