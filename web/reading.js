'use strict';
const ReadingUI = (() => {
  const streams = new Map();
  const rendered = new WeakMap();
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  let timer = 0;
  let anchorTurn = null;
  let anchorIndex = 0;
  function blocks(turn) {
    return [...turn.querySelectorAll('.markdown:not(.stream-tail) > *, .stream-tail, .activity-heading, .activity > summary, .diff-line, .tool-call, .tool-output, .turn-process > summary')];
  }
  function capture(initial = false) {
    const timeline = document.getElementById('timeline'), top = timeline.getBoundingClientRect().top;
    const bottom = initial || timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight <= 16;
    const saved = { bottom, scrollTop: timeline.scrollTop, top };
    if (bottom) return saved;
    const turns=document.getElementById('messages').children;
    let start=0;
    if(anchorTurn?.isConnected&&anchorTurn.getBoundingClientRect().top<=top&&anchorTurn.getBoundingClientRect().bottom>top){
      start=turns[anchorIndex]===anchorTurn?anchorIndex:Array.prototype.indexOf.call(turns,anchorTurn);
    }else{
      let low=0,high=turns.length;
      while(low<high){const middle=(low+high)>>1;if(turns[middle].getBoundingClientRect().bottom<=top)low=middle+1;else high=middle;}
      start=low;
    }
    for (let index=start;index<turns.length;index++) {
      const turn=turns[index];
      if (turn.getBoundingClientRect().bottom <= top) continue;
      const node = blocks(turn).find(node => node.getBoundingClientRect().height && node.getBoundingClientRect().bottom > top);
      if (!node) continue;
      anchorTurn=turn;
      anchorIndex=index;
      const message = node.closest('.message');
      return { ...saved, node, turnId: turn.dataset.id, prompt: turn.dataset.prompt,
        messageId: message?.dataset.messageId, role: message?.dataset.role,
        preview: message?.dataset.preview, blockIndex: message ? blocks(message).indexOf(node) : -1,
        text: node.textContent.slice(0, 80), offset: node.getBoundingClientRect().top - top };
    }
    return saved;
  }
  function restore(saved) {
    const timeline = document.getElementById('timeline');
    if (saved.bottom) { timeline.scrollTop = timeline.scrollHeight; return; }
    let node = saved.node?.isConnected ? saved.node : null;
    if (!node && saved.turnId) {
      const turns = [...document.getElementById('messages').children];
      const turn = turns.find(turn => turn.dataset.id === saved.turnId)
        || turns.find(turn => saved.prompt && turn.dataset.prompt === saved.prompt);
      if (turn) {
        const messages = [...turn.querySelectorAll('.message')];
        const message = messages.find(message => saved.preview && message.dataset.preview === saved.preview && message.dataset.role === saved.role)
          || messages.find(message => message.dataset.messageId === saved.messageId && message.dataset.role === saved.role);
        node = message && (blocks(message)[saved.blockIndex] || message)
          || blocks(turn).find(node => saved.text && node.textContent.startsWith(saved.text));
      }
    }
    timeline.scrollTop = node ? timeline.scrollTop + node.getBoundingClientRect().top
      - timeline.getBoundingClientRect().top - saved.offset : saved.scrollTop;
  }
  function mutate(action) {
    const position = capture();
    action();
    restore(position);
  }
  function paint(entry, done = false) {
    const text = entry.target.slice(0, entry.visible);
    rendered.set(entry.node, { text, format: entry.format });
    if (entry.format === 'diff') {
      const lines = text.split('\n');
      for (let index = Math.max(0, (entry.diffLineCount || 0) - 1); index < lines.length; index++) {
        let line = entry.node.children[index];
        if (!line) {
          line = document.createElement('span');
          line.className = done ? 'diff-line' : 'diff-line stream-chunk';
          entry.node.append(line);
        }
        if (line.textContent !== lines[index]) line.textContent = lines[index];
        const kind = ThreadUI.diffLineKind(lines[index]);
        if (line.dataset.kind !== kind) line.dataset.kind = kind;
      }
      entry.diffLineCount = lines.length;
      while (entry.node.children.length > lines.length) entry.node.lastChild.remove();
      if (done) streams.delete(entry.key);
      return;
    }
    if (done) {
      if (entry.format === 'plain') {
        entry.node.replaceChildren(document.createTextNode(entry.target));
        streams.delete(entry.key);
        return;
      }
      entry.node.querySelectorAll('pre code').forEach(code => BridgeUI.unobserveCode(code));
      entry.node.querySelector('.stream-prefix')?.remove();
      entry.node.querySelector('.stream-tail')?.remove();
      BridgeUI.richText(entry.node, entry.target, entry.files, entry.fileUrl);
      streams.delete(entry.key);
      return;
    }
    let prefix = entry.node.querySelector('.stream-prefix'), tail = entry.node.querySelector('.stream-tail');
    if (!prefix) {
      prefix = document.createElement('div'); prefix.className = 'stream-prefix';
      tail = document.createElement('div'); tail.className = 'markdown stream-tail';
      const merged = document.createTextNode(''); tail.append(merged);
      entry.node.append(prefix, tail);
    }
    // Parse only completed blocks; token-sized updates stay cheap text appends.
    const now = performance.now();
    const boundary = entry.format === 'plain' ? 0 : now - entry.checkedAt >= 400 ? ThreadUI.settledPrefix(text) : entry.committed;
    if (now - entry.checkedAt >= 400) entry.checkedAt = now;
    if (boundary > entry.committed && performance.now() - entry.parsedAt >= 400) {
      prefix.querySelectorAll('pre code').forEach(code => BridgeUI.unobserveCode(code));
      prefix.replaceChildren();
      BridgeUI.richText(prefix, text.slice(0, boundary), entry.files, entry.fileUrl);
      entry.committed = boundary; entry.parsedAt = performance.now();
      tail.replaceChildren(document.createTextNode('')); entry.painted = boundary;
    }
    while (tail.children.length && now - Number(tail.children[0].dataset.at) > 200) {
      tail.firstChild.appendData(tail.children[0].textContent);
      tail.children[0].remove();
    }
    if (entry.visible > entry.painted) {
      const chunk = document.createElement('span'); chunk.className = 'stream-chunk';
      chunk.dataset.at = String(now); chunk.textContent = text.slice(entry.painted);
      tail.append(chunk); entry.painted = entry.visible;
    }
  }
  function tick() {
    clearTimeout(timer);
    timer = 0;
    const now = performance.now(), position = capture();
    for (const entry of streams.values()) {
      if (!entry.node.isConnected) { streams.delete(entry.key); continue; }
      if (document.hidden || reduced.matches) entry.visible = entry.target.length;
      else {
        const next = ThreadUI.revealStep(entry.visible, entry.target, Math.min(now - entry.at, 80), entry.rate);
        if (next === entry.visible) continue;
        entry.visible = next;
      }
      entry.at = now;
      paint(entry, entry.complete && entry.visible >= entry.target.length);
    }
    restore(position);
    if ([...streams.values()].some(entry => entry.visible < entry.target.length)) timer = setTimeout(tick, 32);
  }
  function update(key, node, text, complete, files, fileUrl, instant = false, format = 'markdown') {
    let entry = streams.get(key);
    let seeded = false;
    if (entry && !text.startsWith(entry.target)) { streams.delete(key); entry = null; node.replaceChildren(); }
    if (!entry) {
      entry = { key, node, format, target: '', visible: 0, committed: 0, painted: 0, parsedAt: 0, checkedAt: 0, at: performance.now(), rate: 100 };
      streams.set(key, entry);
      const previous = rendered.get(node);
      if (previous?.format === format && text.startsWith(previous.text)) {
        entry.visible = previous.text.length;
        seeded = true;
      }
      node.replaceChildren();
      if (instant) entry.visible = text.length;
    }
    entry.node = node; entry.files = files; entry.fileUrl = fileUrl;
    if (entry.target !== text) entry.rate = Math.max(100, (text.length - entry.visible) / 1.2);
    entry.target = text; entry.complete = complete;
    if ((instant || seeded) && entry.visible && !complete) mutate(() => paint(entry));
    if (complete && entry.visible >= text.length || reduced.matches) mutate(() => {
      entry.visible = text.length; paint(entry, true);
    });
    else if (!timer) timer = setTimeout(tick, 32);
    return node;
  }
  function has(key) { return streams.has(key); }
  function node(key) { return streams.get(key)?.node; }
  function prune() {
    for (const [key, entry] of streams) if (!entry.node.isConnected) streams.delete(key);
  }
  function reset() { clearTimeout(timer); timer = 0; streams.clear();anchorTurn=null;anchorIndex=0; }
  document.addEventListener('visibilitychange', () => {
    if (document.hidden && streams.size) tick();
  });
  return { capture, restore, mutate, update, has, node, prune, reset };
})();
