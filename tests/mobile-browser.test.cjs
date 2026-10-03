'use strict';
// Optional browser regression suite: loopback synthetic API only, no Codex/App access.
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const WEB = path.resolve(__dirname, '../web');
const ID = '11111111-1111-4111-8111-111111111111';
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const output = process.env.MOBILE_UI_OUTPUT;
const errors = [], sends = [], results = [];
let authenticated = true, snapshotDelay = 0, eventDelay = 0, snapshotReads = 0, eventStarts = 0, activations = 0;
const streams = new Set();
const fixture = {
  id: ID, title: '手机布局与输入稳定性合成验证', host: 'local', hostLabel: '此电脑', cwd: 'C:/Demo',
  connected: true, status: 'active', model: 'demo-codex', provider: 'synthetic', effort: 'high',
  historyComplete: true, requests: [], submissions: [], files: [], sequence: 1,
  contextUsage: { available: true, percent: 32, usedTokens: 64000, contextWindow: 200000 },
  turns: [
    { id: 'done', status: 'completed', completedAt: 1790864000000, durationMs: 12000, messages: [
      { id: 'u1', role: 'user', kind: 'userMessage', text: '检查工具摘要和文件差异。' },
      { id: 'r1', role: 'activity', kind: 'reasoning', summary: '**检查变更**', detail: '这是合成思考详情。' },
      { id: 't1', role: 'activity', kind: 'dynamicToolCall', title: 'exec_command', status: 'completed',
        text: '{"cmd":"node tests/thread-ui.test.cjs","workdir":"C:/Demo","yield_time_ms":1000}',
        output: '[{"type":"text","text":"45 assertions passed"}]' },
      { id: 'f1', role: 'activity', kind: 'fileChange', status: 'completed', title: '文件变更',
        text: 'app.js\n--- a/app.js\n+++ b/app.js\n@@ -1 +1 @@\n-old\n+new\n unchanged' },
      { id: 'f2', role: 'activity', kind: 'fileChange', status: 'completed', text: 'new.js',
        changes: [{ path: 'new.js', kind: { type: 'add' }, diff: 'const created = true;\n\nexport { created };\n' }] },
      { id: 'm1', role: 'activity', kind: 'mcpToolCall', title: 'demo · read', status: 'completed',
        text: '{"path":"demo/resource"}', output: '"Synthetic MCP result"' },
      { id: 'a1', role: 'assistant', kind: 'agentMessage', phase: 'final_answer', text: '摘要与差异已准备。' }
    ] },
    { id: 'live', status: 'inProgress', messages: [
      { id: 'u2', role: 'user', kind: 'userMessage', text: '现在检查手机键盘和持续同步。' },
      { id: 'r2', role: 'activity', kind: 'reasoning', summary: '**正在检查**', detail: '可见的合成详情。' },
      { id: 't2', role: 'activity', kind: 'commandExecution', title: '执行命令',
        status: 'inProgress', text: 'node synthetic-check.cjs', output: 'Starting check.\n' }
    ] }
  ]
};
const sessions = Array.from({ length: 18 }, (_, index) => ({
  id: index ? ID.replace(/^11111111/, String(index).padStart(8, '0')) : ID,
  title: index ? '合成线程 ' + index : fixture.title, host: 'local', hostLabel: '此电脑',
  recency: 1790864000000 - index * 10000, projectId: 'demo', projectKey: 'local/demo',
  projectName: '测试工作区', latestPrompt: '检查手机上的发送按钮和阅读位置。', contextUsage: fixture.contextUsage
}));
const mime = { '.html': 'text/html; charset=utf-8', '.css': 'text/css', '.js': 'text/javascript', '.svg': 'image/svg+xml' };
const server = http.createServer(async (request, response) => {
  try {
    const url = new URL(request.url, 'http://127.0.0.1');
    const json = data => { response.writeHead(200, { 'Content-Type': 'application/json' }); response.end(JSON.stringify(data)); };
    if (url.pathname === '/api/auth') return json({ authenticated, csrf: 'synthetic', passwordless: false, transport: 'sse' });
    if (url.pathname === '/api/login') { authenticated = true; return json({ csrf: 'synthetic' }); }
    if (!authenticated && url.pathname.startsWith('/api/')) {
      response.writeHead(401, { 'Content-Type': 'application/json' });return response.end('{"error":"登录已过期"}');
    }
    if (url.pathname === '/api/sessions') return json({ sessions, unavailableHosts: [] });
    if (url.pathname === '/api/contexts') return json({ contexts: [] });
    const models=[{id:'demo-codex',name:'Demo Codex',efforts:['low','medium','high'],defaultEffort:'high',description:'合成模型，仅用于界面回归'}];
    if(url.pathname==='/api/create-options')return json({canCreate:true,projects:[{projectId:'demo',label:'测试工作区',hostDisplayName:'此电脑'}],models});
    if(url.pathname.endsWith('/catalog'))return json({models,currentModel:'demo-codex',currentEffort:'high',skills:[{id:'demo-skill',name:'review',displayName:'代码审查',description:'用于验证深色界面的合成 Skill'}]});
    if (url.pathname.endsWith('/events')) {
      eventStarts++;
      if(eventDelay)await pause(eventDelay);
      if(response.destroyed)return;
      const threadId = url.pathname.split('/')[3], host = url.searchParams.get('host') || 'local';
      const view = { ...fixture, id: threadId, host, title: threadId === ID ? fixture.title : '另一条合成线程' };
      response.writeHead(200, { 'Content-Type': 'text/event-stream' });
      response.write('event: state\ndata: ' + JSON.stringify(view) + '\n\n');
      streams.add(response);
      const timer = setInterval(() => response.write('event: heartbeat\ndata: {}\n\n'), 1000);
      response.on('close', () => { clearInterval(timer);streams.delete(response); });
      return;
    }
    if (url.pathname.endsWith('/poll')) return json({ state: fixture });
    if (url.pathname.endsWith('/activate')) {
      activations++;fixture.connected=true;fixture.activationRequired=false;fixture.sequence++;return json(fixture);
    }
    if (url.pathname.endsWith('/send')) {
      let body = '';
      for await (const chunk of request) body += chunk;
      sends.push(JSON.parse(body));
      return json({ status: 'sent' });
    }
    if (/^\/api\/sessions\/[0-9a-f-]{36}$/.test(url.pathname)) {
      snapshotReads++;
      if (snapshotDelay) await pause(snapshotDelay);
      return json({ ...fixture, id: url.pathname.split('/')[3], host: url.searchParams.get('host') || 'local',
        title: url.pathname.endsWith(ID) ? fixture.title : '另一条合成线程' });
    }
    if (url.pathname.startsWith('/api/')) throw Error('Unexpected synthetic request: ' + request.url);
    const file = path.resolve(WEB, '.' + (url.pathname === '/' ? '/index.html' : url.pathname));
    if (!file.startsWith(WEB + path.sep)) throw Error('Invalid static path');
    response.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream' });
    response.end(await fs.readFile(file));
  } catch (error) {
    errors.push(error.message); response.writeHead(500); response.end('{}');
  }
});
function check(condition, message) { assert.ok(condition, message); }
async function geometry(page) {
  return page.evaluate(() => {
    const box = id => {
      const { top, bottom, left, right, height } = document.getElementById(id).getBoundingClientRect();
      return { top, bottom, left, right, height };
    };
    return {
      send: box('send'), input: box('message'), timeline: box('timeline'), composer: box('composer'),
      shell: box('app'), scrollY, keyboard: document.getElementById('app').classList.contains('keyboard-open'),
      horizontal: document.documentElement.scrollWidth > innerWidth,
      active: document.activeElement.id, caret: document.getElementById('message').selectionStart
    };
  });
}
async function open(page, base) {
  page.on('pageerror', error => { errors.push(error.message); console.error('pageerror:', error.message); });
  await page.goto(base);
  await page.locator('.session').first().waitFor();
  const borders = await page.evaluate(() => ({
    top: getComputedStyle(document.getElementById('sessions')).borderTopWidth,
    bottom: getComputedStyle(document.getElementById('sessions')).borderBottomWidth,
    project: getComputedStyle(document.querySelector('.project-heading')).borderBottomWidth
  }));
  assert.deepEqual(borders, { top: '1px', bottom: '1px', project: '1px' });
  if (output && page.viewportSize().width === 1366) await page.screenshot({ path: path.join(output, 'desktop-original.png') });
  await page.locator('.session').first().click();
  await page.waitForFunction(() => state?.connected && document.querySelectorAll('.turn').length === 2);
  await page.evaluate(() => { connection.stop(); });
}
async function recovery(page, base) {
  await open(page, base);
  await page.evaluate(() => resumePage(true));
  await page.waitForFunction(() => transportReady);
  await page.locator('#message').fill('息屏后保留的中文草稿');
  const process = page.locator('[data-id=done] .turn-process');
  await process.locator(':scope > summary').click();
  await page.locator('#timeline').evaluate(node => { node.scrollTop = 120; });
  const position = await page.locator('#timeline').evaluate(node => node.scrollTop);
  const loads = snapshotReads;
  await page.evaluate(id => openChat(id, 'local'), ID);
  assert.equal(snapshotReads, loads, 'Selecting the current thread does not reopen its transport');
  await page.locator('#back').click();
  snapshotDelay = 350;
  eventDelay = 350;
  const reopening = page.evaluate(id => openChat(id, 'local'), ID);
  await pause(80);
  assert.equal(await page.locator('#chat-title').innerText(), fixture.title, 'Cached title is immediate');
  check(await page.locator('.turn').count() === 2, 'Cached content remains visible during fresh read');
  check(await process.evaluate(node => node.open), 'Disclosure survives reentry');
  check(await page.locator('#send').isDisabled(), 'Cached state alone does not allow sending');
  await reopening;
  snapshotDelay = 0;
  eventDelay = 0;
  assert.equal(await page.locator('#message').inputValue(), '息屏后保留的中文草稿');
  check(Math.abs(await page.locator('#timeline').evaluate(node => node.scrollTop) - position) < 3, 'Reading position survives reentry');
  const readBeforeWake = snapshotReads, eventsBeforeWake = eventStarts;
  await page.evaluate(() => {
    window.syntheticHidden = true;
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => window.syntheticHidden });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  fixture.sequence++;
  fixture.turns[1].messages.push({ id: 'wake-answer', role: 'assistant', kind: 'agentMessage',
    text: '息屏期间完成的合成回复。' });
  await pause(100);
  await page.evaluate(() => {
    window.syntheticHidden = false;
    document.dispatchEvent(new Event('visibilitychange'));
    window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));
    window.dispatchEvent(new Event('focus'));
  });
  await page.waitForFunction(() => transportReady && state.turns[1].messages.some(message => message.id === 'wake-answer'));
  await pause(200);
  assert.equal(snapshotReads - readBeforeWake, 1, 'Wake lifecycle events are coalesced');
  assert.equal(eventStarts - eventsBeforeWake, 1, 'Only one SSE channel after wake');
  check(streams.size <= 1, 'Old channels are closed');
  assert.equal(await page.locator('#message').inputValue(), '息屏后保留的中文草稿');
  await page.context().setOffline(true);
  await page.evaluate(() => window.dispatchEvent(new Event('offline')));
  check(await page.locator('#send').isDisabled(), 'Offline send is disabled');
  await page.context().setOffline(false);
  await page.evaluate(() => window.dispatchEvent(new Event('online')));
  await page.waitForFunction(() => transportReady);
  const second = ID.replace(/^11111111/, '22222222');
  await page.evaluate(async ({ id, second }) => {
    await openChat(second, 'ssh/demo');
    const old = openChat(id, 'local');
    const newer = openChat(second, 'ssh/demo');
    await Promise.all([old, newer]);
  }, { id: ID, second });
  await page.waitForFunction(() => transportReady && state.host === 'ssh/demo');
  assert.equal(await page.evaluate(() => state.id), second, 'Rapid navigation cannot apply stale snapshots');
  await page.evaluate(id => openChat(id, 'local'), ID);
  authenticated = false;
  await page.evaluate(() => window.dispatchEvent(new Event('online')));
  await page.locator('#login').waitFor({ state: 'visible' });
  await page.locator('#password').fill('synthetic-password');
  await page.locator('#login-button').click();
  await page.waitForFunction(() => transportReady && !document.getElementById('app').hidden);
  assert.equal(await page.locator('#message').inputValue(), '息屏后保留的中文草稿', 'Expired login retains draft');
  assert.equal(await page.locator('#composer').evaluate(node => getComputedStyle(node).borderTopWidth), '1px');
  fixture.connected=false;fixture.activationRequired=true;fixture.canActivate=true;fixture.sequence++;
  await page.evaluate(()=>resumePage(true));
  await page.waitForFunction(() => state.connected && !activatingKey);
  assert.equal(activations, 1, 'A lost native owner is activated once after a previous successful connection');
  if (output) await page.screenshot({ path: path.join(output, 'recovery-original.png') });
  await page.evaluate(() => connection.stop());
  fixture.turns[1].messages.pop();
  results.push({ recovery: true, cachedReentry: true, offlineRecovery: true, expiredLogin: true, isolatedHost: true });
}
async function details(page) {
  const process = page.locator('[data-id=done] .turn-process');
  await process.locator(':scope > summary').click();
  const group = process.locator('.activity-group.tools');
  await group.locator(':scope > summary').click();
  const tools = group.locator('.activity');
  await tools.first().locator(':scope > summary').click();
  assert.equal((await tools.first().locator('.tool-call').innerText()).trim(), 'node tests/thread-ui.test.cjs');
  assert.match(await tools.first().locator('.tool-metadata').textContent(), /目录：C:\/Demo/);
  check(!(await tools.first().innerText()).includes('yield_time_ms'), 'No raw parameter structure');
  await tools.first().locator('.tool-body').click();
  check(!await tools.first().evaluate(node => node.open), 'Detail body collapses its own tool');
  check(await group.evaluate(node => node.open), 'Parent group stays open');
  await tools.nth(1).locator(':scope > summary').click();
  check(await tools.nth(1).locator('.diff-line[data-kind=add]').count() === 1, 'Green addition');
  check(await tools.nth(1).locator('.diff-line[data-kind=remove]').count() === 1, 'Red removal');
  check(await tools.nth(1).locator('.diff-line[data-kind=meta]').count() === 2, 'File headers not colored as changes');
  if (output && page.viewportSize().width === 390) {
    await pause(200);
    await page.screenshot({ path: path.join(output, 'tool-diff-original.png') });
  }
  await tools.nth(1).locator('.tool-body').focus();
  await page.keyboard.press('Enter');
  check(!await tools.nth(1).evaluate(node => node.open), 'Keyboard detail collapse');
  await process.locator('.activity-group.reasoning > summary').click();
  await process.locator('.reasoning-body').click();
  check(!await process.locator('.activity-group.reasoning').evaluate(node => node.open), 'Reasoning body collapse');
  await tools.nth(2).locator(':scope > summary').click();
  assert.equal(await tools.nth(2).locator('.file-change-label').innerText(), '创建文件');
  assert.equal(await tools.nth(2).locator('.diff-line[data-kind=add]').count(), 3);
  assert.equal(await tools.nth(2).locator('.diff-line[data-kind=context]').count(), 0);
  assert.equal(await tools.nth(3).locator('summary svg').first().evaluate(node=>node.outerHTML),
    await page.evaluate(()=>BridgeUI.icon('PlugZap').outerHTML),'MCP has dedicated icon');
  check(await page.locator('#skills-button svg').count() === 1, 'Skill selector has icon');
  if (output && page.viewportSize().width === 390) {
    await pause(200); await page.screenshot({ path: path.join(output, 'creation-original.png') });
  }
  await process.locator(':scope > summary').click();
}
async function appearance(page, base) {
  await page.goto(base);
  await page.locator('.session').first().waitFor();
  const width=page.viewportSize().width;
  await page.locator('#sidebar [data-appearance-button]').click();
  await page.locator('[name=appearance][value=dark]').check();
  assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');
  await page.getByRole('button',{name:'关闭外观设置'}).click();
  assert.equal(await page.locator('.session').nth(1).evaluate(node=>getComputedStyle(node).backgroundColor),'rgb(33, 33, 33)');
  assert.equal(await page.locator('#new-thread').evaluate(node=>getComputedStyle(node).backgroundColor),'rgb(236, 236, 236)');
  if(output)await page.screenshot({path:path.join(output,'workspace-dark-'+width+'.png')});
  await page.locator('#new-thread').click();
  await page.waitForFunction(()=>!document.getElementById('new-create').disabled);
  assert.equal(await page.locator('#new-dialog').evaluate(node=>getComputedStyle(node).backgroundColor),'rgb(38, 38, 38)');
  if(output&&width===390)await page.screenshot({path:path.join(output,'create-dark.png')});
  await page.locator('[data-close=new-dialog]').click();
  await page.reload();
  await page.locator('.session').first().waitFor();
  assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');
  await page.emulateMedia({colorScheme:'light'});
  assert.equal(await page.locator('html').getAttribute('data-theme'),'dark','Explicit theme overrides system');
  await page.evaluate(()=>ThemeUI.set('system'));
  assert.equal(await page.locator('html').getAttribute('data-theme'),'light');
  await page.emulateMedia({colorScheme:'dark'});
  await page.waitForFunction(()=>document.documentElement.dataset.theme==='dark');
  await page.evaluate(()=>ThemeUI.set('light'));
  assert.equal(await page.locator('.session').nth(1).evaluate(node=>getComputedStyle(node).backgroundColor),'rgb(255, 255, 255)');
  if(output)await page.screenshot({path:path.join(output,'workspace-light-'+width+'.png')});
  await open(page,base);
  await page.locator('#message').fill('切换主题保留草稿与光标');
  const caret=await page.locator('#message').evaluate(node=>node.selectionStart);
  await page.evaluate(()=>ThemeUI.set('dark'));
  assert.equal(await page.locator('#message').evaluate(node=>node.selectionStart),caret);
  assert.equal(await page.locator('#message').inputValue(),'切换主题保留草稿与光标');
  const darkColors=await page.evaluate(()=>({canvas:getComputedStyle(document.getElementById('timeline')).backgroundColor,composer:getComputedStyle(document.querySelector('.composer-input')).backgroundColor,text:getComputedStyle(document.getElementById('message')).color}));
  assert.deepEqual(darkColors,{canvas:'rgb(33, 33, 33)',composer:'rgb(43, 43, 43)',text:'rgb(236, 236, 236)'});
  await page.locator('#message').blur();
  await page.evaluate(()=>{
    const view=structuredClone(state);view.status='idle';
    view.turns[1].status='completed';
    for(let i=0;i<3;i++)view.turns.push({id:'compact-'+i,status:'completed',messages:[{id:'c'+i,role:'activity',kind:'contextCompaction'}]});
    view.compactionPending=true;renderState(view);
  });
  assert.equal(await page.locator('.compaction-record').count(),0,'Pending owns one indicator');
  assert.equal(await page.locator('#working:not([hidden])').count(),1);
  assert.equal(await page.locator('#turn-nav .turn-dot').count(),2,'Empty compaction-only turns have no phantom navigation');
  await page.evaluate(()=>renderState({...state,compactionPending:false}));
  assert.equal(await page.locator('.compaction-record').count(),1);
  assert.match(await page.locator('.compaction-record').textContent(),/连续 3 次/);
  assert.equal(await page.locator('#working:not([hidden])').count(),0);
  assert.equal(await page.locator('.compaction-record details').count(),0);
  await page.locator('.turn-process > summary').first().click();
  await page.locator('.activity-group.tools > summary').click();
  await page.locator('.activity > summary').first().click();
  assert.equal(await page.locator('.activity').first().locator('.tool-section-head').count(),2);
  assert.match(await page.locator('.activity').first().locator('.tool-metadata').textContent(),/C:\/Demo/);
  assert.equal(await page.locator('.activity').first().locator('.tool-call').textContent(),'node tests/thread-ui.test.cjs');
  assert.equal(await page.evaluate(()=>{
    const before=document.querySelector('.activity');
    const view=structuredClone(state);view.turns[0].messages.find(message=>message.id==='m1').output='Updated synthetic MCP output';
    renderState(view);
    return document.querySelector('.activity')===before;
  }),true,'Unchanged tool details reuse their DOM across sibling output updates');
  if(output)await page.screenshot({path:path.join(output,'tools-dark-'+width+'.png')});
  await page.locator('#chat [data-appearance-button]').click();
  if(output)await page.screenshot({path:path.join(output,'appearance-dark-'+width+'.png')});
  await page.getByRole('button',{name:'关闭外观设置'}).click();
  await page.locator('#model-button').click();
  await page.waitForFunction(()=>document.getElementById('effort-select').value==='high');
  if(output&&width===390)await page.screenshot({path:path.join(output,'model-dark.png')});
  await page.locator('[data-close=model-dialog]').click();
  await page.locator('#skills-button').click();
  await page.locator('.skill-option').waitFor();
  if(output&&width===390)await page.screenshot({path:path.join(output,'skills-dark.png')});
  await page.locator('[data-close=skills-dialog]').first().click();
  await page.evaluate(()=>ThemeUI.set('light'));
  await page.locator('#message').fill('');
  assert.equal((await geometry(page)).horizontal,false);
  results.push({viewport:page.viewportSize(),appearance:true,compaction:true});
}
async function typing(page, base) {
  await open(page, base);
  await details(page);
  const input = page.locator('#message');
  await input.fill('键盘开启时保持发送按钮可见。');
  await input.focus();
  const original = page.viewportSize();
  let keyboardPanRange=null;
  if (original.width < 721) {
    await page.setViewportSize({ width: original.width, height: 350 });
    await pause(200);
    const small = await geometry(page);
    check(small.keyboard, 'Resizes-content keyboard detected');
    check(small.send.bottom <= 350 && small.send.top >= 0, 'Send visible above keyboard');
    check(small.timeline.bottom <= small.composer.top + 1 && small.timeline.height >= 60, 'Timeline and composer do not overlap');
    await input.fill(Array(8).fill('连续输入，滚动只发生在输入框内部。').join('\n'));
    await page.evaluate(() => { const input = $('message'); input.setSelectionRange(12, 12); });
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const baseline = await geometry(page);
    await input.fill('short');
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert.equal((await geometry(page)).input.height, baseline.input.height, 'Focused input does not grow/shrink with content');
    await input.fill(Array(8).fill('连续输入，滚动只发生在输入框内部。').join('\n'));
    await page.evaluate(() => $('message').setSelectionRange(12, 12));
    for (let index = 0; index < 8; index++) {
      await page.evaluate(index => {
        const view = structuredClone(state);
        view.sequence++;
        view.turns[1].messages[2].output += 'Update ' + index + '\n';
        renderState(view);
      }, index);
      await pause(40);
      const next = await geometry(page);
      assert.equal(next.caret, 12);
      assert.equal(next.active, 'message');
      check(Math.abs(next.input.top - baseline.input.top) < 1, 'Stable textarea top during native snapshots: ' + JSON.stringify({ index, baseline, next }));
      check(next.send.bottom <= 350 && !next.horizontal && next.scrollY === 0, 'Stable keyboard layout');
    }
    const beforeComposition = sends.length;
    await page.evaluate(() => {
      const input = $('message');
      input.dispatchEvent(new CompositionEvent('compositionstart', { data: '中', bubbles: true }));
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true, isComposing: true, bubbles: true }));
      input.dispatchEvent(new CompositionEvent('compositionend', { data: '中', bubbles: true }));
    });
    await pause(50);
    assert.equal(sends.length, beforeComposition, 'IME confirmation does not send a message');
    for (let index = 0; index < 6; index++) {
      await page.keyboard.insertText('中');
      await page.evaluate(() => { renderState(structuredClone(state)); });
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)));
      const next = await geometry(page);
      assert.equal(next.caret, 13 + index);
      assert.equal(next.active, 'message');
      check(Math.abs(next.input.top - baseline.input.top) < 1 && next.scrollY === 0, 'Typing and snapshots preserve input geometry');
    }
    await page.setViewportSize({ width: original.width, height: 300 });
    await pause(200);
    const shorter = await geometry(page);
    check(shorter.send.bottom <= 300 && shorter.timeline.height >= 60 && shorter.scrollY === 0, '300px keyboard viewport remains usable');
    await page.setViewportSize({ width: original.width, height: 350 });
    await pause(100);
    if (output && original.width === 390) await page.screenshot({ path: path.join(output, 'keyboard-original.png') });
    const before = sends.length;
    await page.locator('#send').click();
    await page.waitForFunction(() => !sending && $('message').value === '');
    assert.equal(sends.length, before + 1);
    assert.equal((await geometry(page)).active, 'message');
    check((await geometry(page)).keyboard, 'Sending does not dismiss keyboard');
    await page.setViewportSize(original);
    await pause(100);
    // Safari-style visual viewport: layout stays full height; visual height/top fluctuate.
    await page.evaluate(() => {
      Object.defineProperty(visualViewport, 'height', { configurable: true, get: () => window.testHeight ?? innerHeight });
      Object.defineProperty(visualViewport, 'offsetTop', { configurable: true, get: () => window.testTop ?? 0 });
      window.testHeight = 340; window.testTop = 8;
      visualViewport.dispatchEvent(new Event('resize'));
    });
    await pause(100);
    const visual = await geometry(page);
    check(visual.send.bottom <= 348 && visual.keyboard, 'Offset visual viewport keeps send in bounds');
    const panTops=[];
    for (const offset of [8.1, 28, 0, 34, 8.1]) {
      await page.evaluate(offset => { window.testTop = offset; visualViewport.dispatchEvent(new Event('scroll')); }, offset);
      await pause(40);
      panTops.push((await geometry(page)).input.top);
      check(Math.abs((await geometry(page)).input.top - visual.input.top) < 1, 'Caret-pan scroll events cannot move shell');
    }
    for(const [height,top] of [[328,28],[350,0],[330,34],[340,8]]){
      await page.evaluate(([height,top])=>{
        window.testHeight=height;window.testTop=top;
        visualViewport.dispatchEvent(new Event('resize'));visualViewport.dispatchEvent(new Event('scroll'));
      },[height,top]);
      await pause(35);
      const next=await geometry(page);
      check(Math.abs(next.input.top-visual.input.top)<1&&next.active==='message','IME resize storms do not reflow the focused input');
    }
    await pause(180);
    check(Math.abs((await geometry(page)).input.top-visual.input.top)<1,'Settled candidate-bar cycle stays stable');
    keyboardPanRange=Math.max(...panTops)-Math.min(...panTops);
    await page.evaluate(() => {
      window.testHeight = undefined; window.testTop = undefined;
      visualViewport.dispatchEvent(new Event('resize'));
    });
  }
  check(!(await geometry(page)).horizontal, 'No horizontal overflow');
  const live = page.locator('[data-id=live] .activity-group.tools');
  await live.locator(':scope > summary').click();
  await live.locator('.activity > summary').click();
  await page.evaluate(() => {
    const view = structuredClone(state);
    view.turns[1].messages[1].summary += '，' + '逐步接收实时摘要。'.repeat(50);
    view.turns[1].messages[1].detail += '\n' + '合成思考详情。'.repeat(60);
    view.turns[1].messages[2].output += '工具输出逐步到达。\n'.repeat(80);
    renderState(view);
  });
  await pause(100);
  const partial = await page.evaluate(() => ({
    preview: document.querySelector('[data-id=live] .reasoning-preview').textContent.length,
    output: document.querySelector('[data-id=live] .tool-output').textContent.length,
    chunks: document.querySelectorAll('[data-id=live] .stream-chunk').length,
    open: document.querySelector('[data-id=live] .activity').open
  }));
  check(partial.preview < 450 && partial.output < 850 && partial.chunks > 0 && partial.open, 'Non-body content paced with preserved disclosure');
  await pause(1800);
  check((await live.locator('.tool-output').textContent()).includes('工具输出逐步到达'), 'Tool output drains');
  const count = (await live.locator('.tool-output').textContent()).match(/工具输出逐步到达/g)?.length;
  assert.equal(count, 80, 'No duplicated stream content');
  await page.evaluate(() => {
    const view = structuredClone(state);
    view.turns[1].messages.push({
      id: 'live-diff', role: 'activity', kind: 'fileChange', status: 'inProgress',
      text: 'app.js\n@@ -1,50 +1,50 @@\n' + '-old\n+new\n'.repeat(50)
    });
    renderState(view);
  });
  const patch = live.locator('.activity').last();
  await patch.locator(':scope > summary').click();
  await pause(80);
  check(await patch.locator('.diff-line').count() < 102, 'Live diff is paced');
  await pause(1500);
  assert.equal(await patch.locator('.diff-line[data-kind=add]').count(), 50);
  assert.equal(await patch.locator('.diff-line[data-kind=remove]').count(), 50);
  await patch.locator('.tool-body').click({ position: { x: 12, y: 12 } });
  check(!await patch.evaluate(node => node.open), 'Streamed diff body collapses');
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.evaluate(() => {
    const view = structuredClone(state);
    view.turns[1].messages[2].output += 'Reduced motion immediately delivered.';
    renderState(view);
  });
  check((await live.locator('.tool-output').textContent()).endsWith('Reduced motion immediately delivered.'), 'Reduced motion flushes immediately');
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  const answer = '## 正文回归\n\n' + '正常回复继续平滑输出。'.repeat(55)
    + '\n\n```js\nconst safe = true;\n```\n\n<script>window.syntheticXss = 1</script>';
  await page.evaluate(text => {
    const view = structuredClone(state);
    view.turns[1].messages.push({ id: 'a2', role: 'assistant', kind: 'agentMessage', phase: 'final_answer', text });
    renderState(view);
  }, answer);
  await pause(100);
  check((await page.locator('[data-id=live] .message.assistant').textContent()).length < answer.length, 'Assistant body still paced');
  await pause(1600);
  await page.evaluate(() => {
    const view = structuredClone(state);
    view.status = 'idle'; view.turns[1].status = 'completed';
    view.turns[1].completedAt = 1790864000000; view.turns[1].durationMs = 14000;
    renderState(view);
  });
  await page.waitForFunction(() => document.querySelector('[data-id=live] .message.assistant .markdown h2'));
  check(await page.locator('[data-id=live] .message.assistant code').count() === 1, 'Final Markdown preserved');
  check(await page.evaluate(() => !window.syntheticXss && !document.querySelector('#messages script')), 'Final content sanitized');
  if (output && original.width === 390) {
    await page.locator('#message').blur();
    await page.locator('#timeline').evaluate(node => { node.scrollTop = node.scrollHeight; });
    await page.screenshot({ path: path.join(output, 'thread-original.png') });
  }
  results.push({ viewport: original, keyboard: original.width < 721, keyboardPanRange, details: true, pacedActivity: true, reducedMotion: true });
}
(async () => {
  if (output) await fs.mkdir(output, { recursive: true });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = 'http://127.0.0.1:' + server.address().port;
  let browser;
  try {
    browser = await chromium.launch({ channel: 'chrome', headless: true });
    const recoveryContext = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: 'zh-CN' });
    try { await recovery(await recoveryContext.newPage(), base); }
    finally { await recoveryContext.close(); }
    for (const viewport of [{ width: 390, height: 844 }, { width: 320, height: 568 }, { width: 1366, height: 900 }]) {
      const context = await browser.newContext({ viewport, locale: 'zh-CN', timezoneId: 'Asia/Shanghai' });
      try { const page=await context.newPage();await appearance(page,base);await typing(page, base); }
      finally { await context.close(); }
    }
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ results, syntheticSends: sends.length, syntheticActivations: activations, errors }));
  } finally {
    if (browser) await browser.close();
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
