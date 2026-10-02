'use strict';
const BridgeUI = (() => {
  let app, creation, creating = false, contextTimer, summaryBusy = false, highlightObserver, rowObserver;
  let turnObserver, navStamp='', viewportFrame=0, uploadCount=0, attachmentKey=null, inputStamp='', inputFrame=0, inputMeasure;
  let fullHeight=window.innerHeight, viewportStamp='';
  const uploadDrafts=new Map();
  const observedTurns=new Set();
  const visibleRows = new Set();
  const numberFormat = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1 });
  const $ = id => document.getElementById(id);
  const number = value => numberFormat.format(value);
  const tokens = value => value >= 1000 ? number(value / 1000) + 'k' : number(value);
  function icon(name) {
    return lucide.createElement(lucide.icons[name], { width: 17, height: 17, 'aria-hidden': 'true' });
  }
  function button(name, label, action) {
    const node = document.createElement('button');
    node.type = 'button';
    node.className = 'icon-button';
    node.title = label;
    node.setAttribute('aria-label', label);
    node.append(icon(name));
    node.onclick = action;
    return node;
  }
  function usageLabel(usage) {
    if (!usage?.available) return '暂无数据';
    return Math.round(usage.percent) + '%' + (usage.live ? '' : ' · 上次');
  }
  function updateUsage(node, usage) {
    node.textContent = usageLabel(usage);
    node.className = 'usage-value ' + (usage?.percent >= 95 ? 'critical' : usage?.percent >= 80 ? 'warning' : '');
    node.title = usage?.available
      ? (usage.live ? '桌面实时数据：' : '已保存的最近一次数据：') + tokens(usage.usedTokens) + ' / ' + tokens(usage.contextWindow) + ' tokens'
      : '桌面尚未提供上下文用量';
  }
  function decorateSession(node, row, observed=node) {
    const footer = document.createElement('div');
    footer.className = 'session-state';
    const status = document.createElement('span');
    status.className = 'thread-state';
    status.textContent = row.status === 'active' ? '运行中' : row.connected ? '已连接' : '';
    const value = document.createElement('span');
    value.dataset.contextValue = 'true';
    updateUsage(value, row.contextUsage);
    footer.append(status, value);
    node.append(footer);
    rowObserver.observe(observed);
  }
  function releaseSession(node) {
    rowObserver.unobserve(node);
    visibleRows.delete(node);
  }
  function releaseTurn(node) {
    node?.querySelectorAll('pre code').forEach(code => highlightObserver.unobserve(code));
  }
  function resetChat() {
    ReadingUI.reset();
    highlightObserver.disconnect();
    turnObserver?.disconnect();
    observedTurns.clear();
    navStamp='';
    $('turn-nav')?.replaceChildren();
    attachmentKey=null;
    inputStamp='';
    $('message').style.height='';
  }
  async function refreshContexts() {
    if (summaryBusy || document.hidden || $('app').hidden) return;
    const bounds = $('sidebar').getBoundingClientRect();
    if (!bounds.width || !bounds.height) return;
    const rows = [...visibleRows].filter(node => node.isConnected)
      .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top).slice(0, 24);
    if (!rows.length) return;
    const hosts = new Map();
    rows.forEach(node => {
      if (!hosts.has(node.dataset.host)) hosts.set(node.dataset.host, []);
      hosts.get(node.dataset.host).push(node.dataset.id);
    });
    summaryBusy = true;
    try {
      for (const [host, ids] of hosts) {
        const value = await app.api('/api/contexts?host=' + encodeURIComponent(host) + '&ids=' + ids.join(','));
        for (const summary of value.contexts) {
          const node = rows.find(node => node.dataset.id === summary.id && node.dataset.host === host);
          if (!node?.isConnected) continue;
          updateUsage(node.querySelector('[data-context-value]'), summary.contextUsage);
          node.querySelector('.thread-state').textContent = summary.status === 'active' ? '运行中' : summary.connected ? '已连接' : '';
          const preview=node.querySelector('[data-prompt]');
          if(preview&&summary.latestPrompt!==undefined){
            const text=summary.latestPrompt??'暂无输入预览';
            if(preview.textContent!==text)preview.textContent=text;
          }
          app.updateRow(summary);
        }
      }
    } catch (error) {
      $('list-status').textContent = '状态暂未同步';
    } finally {
      summaryBusy = false;
    }
  }
  function scheduleContexts() {
    clearTimeout(contextTimer);
    contextTimer = setTimeout(refreshContexts, 200);
  }
  function renderContext(view) {
    const usage = view.contextUsage;
    updateUsage($('context-percent'), usage);
    $('context-tokens').textContent = usage?.available ? tokens(usage.usedTokens) + ' / ' + tokens(usage.contextWindow) : '暂无上下文数据';
    $('context-progress').hidden = !usage?.available;
    $('context-progress').value = usage?.percent || 0;
    $('context-progress').setAttribute('aria-valuetext', usageLabel(usage));
    $('context-strip').dataset.level = usage?.percent >= 95 ? 'critical' : usage?.percent >= 80 ? 'warning' : 'normal';
    const compacting=ThreadUI.isCompacting(view);
    $('compact').disabled = !view.connected || view.status !== 'idle' || compacting;
    $('compact').title = compacting ? '正在压缩上下文，等待桌面更新' : view.status === 'active' ? '任务结束后可压缩' : '手动压缩上下文';
    $('compact').setAttribute('aria-label', $('compact').title);
    $('compacting').hidden = !compacting;
    $('add-attachment').disabled=view.host!=='local';
    $('add-attachment').title=view.host!=='local'?'附件暂仅支持本机线程':'添加附件';
    renderAttachments();
    resizeInput();
    $('connection-indicator').textContent = view.connected ? '桌面已连接' : view.connecting ? '正在连接桌面' : '仅保存的历史';
    $('connection-indicator').dataset.connected = String(view.connected);
  }
  function escape(value) {
    return String(value).replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
  }
  function fileReference(reference, files) {
    return files?.find(file => file.reference === reference || file.reference === decodeSafe(reference));
  }
  function decodeSafe(value) {
    try { return decodeURI(value); } catch { return value; }
  }
  function richText(node, text, files, fileUrl) {
    const renderer = new marked.Renderer();
    renderer.link = function({ href, title, tokens: inline }) {
      const file = fileReference(href, files);
      const safe = file ? fileUrl(file.id) : /^https?:\/\//i.test(href) ? href : null;
      const label = this.parser.parseInline(inline);
      return safe ? '<a href="' + escape(safe) + '" target="_blank" rel="noopener noreferrer">' + label + '</a>' : label;
    };
    renderer.image = function({ href, text: alt }) {
      const file = fileReference(href, files);
      return file?.image ? '<img src="' + escape(fileUrl(file.id)) + '" alt="' + escape(alt) + '" loading="lazy">' : '<span>' + escape(alt || href) + '</span>';
    };
    const html = marked.parse(String(text || ''), { renderer, gfm: true, breaks: true });
    const fragment = DOMPurify.sanitize(html, {
      RETURN_DOM_FRAGMENT: true,
      ALLOWED_TAGS: ['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'em', 'del', 'blockquote',
        'ul', 'ol', 'li', 'pre', 'code', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'a', 'img', 'br', 'hr', 'span'],
      ALLOWED_ATTR: ['href', 'src', 'alt', 'title', 'class', 'target', 'rel', 'loading', 'start'],
      ALLOW_DATA_ATTR: false
    });
    const body = document.createElement('div');
    body.className = 'markdown';
    body.append(fragment);
    body.querySelectorAll('a').forEach(link => {
      const href = link.getAttribute('href') || '';
      if (!/^https?:\/\//i.test(href) && !/^\/api\/sessions\/[a-f0-9-]{36}\/files\/[a-f0-9]{64}/.test(href)) {
        link.replaceWith(document.createTextNode(link.textContent));
      } else {
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
      }
    });
    body.querySelectorAll('img').forEach(image => {
      if (!/^\/api\/sessions\/[a-f0-9-]{36}\/files\/[a-f0-9]{64}/.test(image.getAttribute('src') || '')) image.remove();
    });
    body.querySelectorAll('pre').forEach(pre => {
      const code = pre.querySelector('code');
      if (!code) return;
      const original = code.textContent;
      const language = [...code.classList].find(name => name.startsWith('language-'))?.slice(9);
      if (original.length <= 20000 && language && hljs.getLanguage(language)) highlightObserver.observe(code);
      const tools = document.createElement('div');
      tools.className = 'code-tools';
      const label = document.createElement('span');
      label.textContent = language || '代码';
      const copy = button('Copy', '复制代码', async () => {
        const copied = await copyText(original);
        app.toast(copied ? '已复制' : '复制失败');
      });
      tools.append(label, copy);
      pre.before(tools);
    });
    node.append(body);
  }
  async function copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
      const active = document.activeElement;
      const input = document.createElement('textarea');
      input.value = text;
      input.className = 'clipboard-buffer';
      document.body.append(input);
      input.select();
      const result = document.execCommand('copy');
      input.remove();
      active?.focus({ preventScroll: true });
      return result;
    } catch { return false; }
  }
  function latestButton() {
    const timeline = $('timeline');
    $('latest').hidden = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 110;
    $('latest').style.bottom = $('composer').getBoundingClientRect().height + 8 + 'px';
  }
  function viewport() {
    if(viewportFrame)return;
    viewportFrame=requestAnimationFrame(()=>{
    viewportFrame=0;
    const visual = window.visualViewport;
    // Pinch zoom belongs to the browser, not the keyboard layout controller.
    if(visual&&Math.abs(visual.scale-1)>.02)return;
    const height = Math.round(visual?.height || window.innerHeight);
    const shell=$('app'),timeline=$('timeline');
    const focused=document.activeElement===$('message');
    if(!focused)fullHeight=Math.max(fullHeight,window.innerHeight);
    const keyboard=focused&&height<fullHeight-100;
    const top=Math.round(visual?.offsetTop||0);
    const stamp=height+'|'+top+'|'+keyboard;
    if(stamp===viewportStamp)return;
    viewportStamp=stamp;
    const position=ReadingUI.capture();
    shell.style.height=height+'px';
    shell.style.top=top+'px';
    document.documentElement.style.setProperty('--viewport-height',height+'px');
    shell.classList.toggle('keyboard-open',keyboard);
    document.documentElement.style.setProperty('--keyboard-open',keyboard?'1':'0');
    resizeInput();
    ReadingUI.restore(position);
    latestButton();
    });
  }
  function resizeInput(){
    const input=$('message');
    if(!$('app').classList.contains('chat-open'))return;
    const height=$('app').clientHeight||window.innerHeight;
    const chrome=document.querySelector('.chat-head').offsetHeight;
    const extra=$('composer').offsetHeight-input.offsetHeight+$('notice').offsetHeight+chrome;
    const max=ThreadUI.inputMaxHeight(height,extra),width=input.clientWidth;
    const stamp=currentKey()+'|'+max+'|'+width+'|'+input.value;
    if(stamp===inputStamp)return;
    inputStamp=stamp;
    // Measure offscreen: collapsing the focused textarea makes mobile browsers move its caret.
    if(!inputMeasure){
      inputMeasure=document.createElement('textarea');
      inputMeasure.className='input-measure';inputMeasure.tabIndex=-1;
      inputMeasure.setAttribute('aria-hidden','true');inputMeasure.readOnly=true;
      document.body.append(inputMeasure);
    }
    const style=getComputedStyle(input);
    for(const property of ['font','lineHeight','padding','borderWidth','boxSizing','wordBreak','overflowWrap','letterSpacing']){
      inputMeasure.style[property]=style[property];
    }
    inputMeasure.style.width=width+'px';inputMeasure.value=input.value;
    const next=Math.min(max,Math.max(52,inputMeasure.scrollHeight));
    if(Math.abs(input.getBoundingClientRect().height-next)>.5){
      const position=ReadingUI.capture();
      input.style.height=next+'px';
      ReadingUI.restore(position);
    }
    latestButton();
  }
  function scheduleInput(){
    if(inputFrame)return;
    inputFrame=requestAnimationFrame(()=>{inputFrame=0;resizeInput();});
  }
  function bindDisclosures(){
    const timeline=$('timeline');
    let start=null,moved=false;
    timeline.addEventListener('pointerdown',event=>{start={x:event.clientX,y:event.clientY};moved=false;},{passive:true});
    timeline.addEventListener('pointermove',event=>{
      if(start&&Math.hypot(event.clientX-start.x,event.clientY-start.y)>8)moved=true;
    },{passive:true});
    timeline.addEventListener('pointercancel',()=>{moved=true;});
    function collapse(event){
      if(event.type==='keydown'&&!['Enter',' '].includes(event.key))return;
      if(event.type==='click'&&moved)return;
      const target=event.target;
      if(target.closest('summary,button,a,input,textarea,select')||window.getSelection()?.toString())return;
      const body=target.closest('.collapse-detail');
      if(!body||event.type==='keydown'&&target!==body)return;
      const detail=body.closest('details');
      if(!detail?.open)return;
      event.preventDefault();event.stopPropagation();
      const top=detail.getBoundingClientRect().top;
      detail.open=false;
      timeline.scrollTop+=detail.getBoundingClientRect().top-top;
      if(event.type==='keydown')detail.querySelector(':scope > summary')?.focus({preventScroll:true});
      latestButton();
    }
    timeline.addEventListener('click',collapse);
    timeline.addEventListener('keydown',collapse);
  }
  function currentKey(){const current=app.getCurrent();return current.host+'|'+current.id;}
  function attachments(){return (uploadDrafts.get(currentKey())||[]).map(file=>file.id);}
  function clearAttachments(){uploadDrafts.delete(currentKey());renderAttachments();}
  function renderAttachments(){
    const key=currentKey(),files=uploadDrafts.get(key)||[];
    const stamp=key+JSON.stringify(files);
    if(stamp===attachmentKey)return;
    attachmentKey=stamp;
    $('attachment-pills').replaceChildren();
    for(const file of files){
      const pill=document.createElement('span');pill.className='attachment-pill';
      const label=document.createElement('span');label.textContent=file.name;label.title=file.name;
      const remove=button('X','移除 '+file.name,()=>{
        uploadDrafts.set(key,(uploadDrafts.get(key)||[]).filter(item=>item.id!==file.id));renderAttachments();
      });
      pill.append(icon(file.image?'Image':'FileText'),label,remove);$('attachment-pills').append(pill);
    }
  }
  async function addAttachments(files){
    const current=app.getCurrent(),key=currentKey();
    if(!current.id||current.host!=='local')return;
    if(uploadCount){app.toast('请等待当前附件上传完成');return;}
    if((uploadDrafts.get(key)||[]).length+files.length>8){app.toast('每条消息最多 8 个附件');return;}
    uploadCount++;$('attachment-status').hidden=false;$('send').disabled=true;
    try{
      for(const file of files){
        if(file.size>10*1024*1024||!file.size)throw Error('每个附件须为 1 字节至 10 MB');
        const result=await app.upload(app.sessionUrl(current.id,'upload',current.host),file);
        const list=uploadDrafts.get(key)||[];list.push(result);uploadDrafts.set(key,list);
        if(key===currentKey())renderAttachments();
      }
    }catch(error){app.toast(error.message);}
    finally{uploadCount--;$('attachment-status').hidden=true;$('attachment-input').value='';$('send').disabled=!app.getState()?.connected||app.isSending()||ThreadUI.isCompacting(app.getState()||{});latestButton();}
  }
  function renderTurnNav(turns){
    const stamp=JSON.stringify(turns.map(turn=>[turn.id,turn.status,turn.messages.find(message=>message.role==='user')?.text?.slice(0,100)]));
    const nav=$('turn-nav');
    if(stamp!==navStamp){
      navStamp=stamp;nav.replaceChildren();
      turns.forEach((turn,index)=>{
        const prompt=turn.messages.find(message=>message.role==='user')?.text?.trim()||'第 '+(index+1)+' 轮';
        const dot=button('Circle', '第 '+(index+1)+' 轮：'+prompt.slice(0,100),()=>{
          const target=[...$('messages').children].find(node=>node.dataset.id===turn.id);
          if(target)$('timeline').scrollTop+=target.getBoundingClientRect().top-$('timeline').getBoundingClientRect().top-12;
        });
        dot.className='turn-dot';dot.dataset.id=turn.id;
        dot.replaceChildren();
        const mark=document.createElement('span');mark.className='turn-dot-mark';
        const label=document.createElement('span');label.className='turn-dot-label';label.textContent=(index+1)+'. '+prompt.slice(0,60);
        dot.append(mark,label);dot.classList.toggle('active-turn',turn.status==='inProgress');nav.append(dot);
      });
    }
    for(const turn of observedTurns)if(!turn.isConnected){turnObserver.unobserve(turn);observedTurns.delete(turn);}
    for(const turn of $('messages').children)if(!observedTurns.has(turn)){turnObserver.observe(turn);observedTurns.add(turn);}
    nav.hidden=!turns.length;
    nav.style.bottom=$('composer').getBoundingClientRect().height+14+'px';
  }
  function modelEfforts() {
    const id = $('new-model').value;
    const model = creation?.models.find(model => model.id === id);
    $('new-custom-label').hidden = id !== '__custom__';
    $('new-custom').required = id === '__custom__';
    ModelSettings.fill($('new-effort'), model, null, false);
  }
  async function newThread() {
    $('new-error').textContent = '';
    $('new-create').disabled = true;
    $('new-dialog').showModal();
    try {
      if (!creation || Date.now() - creation.loadedAt > 60000) {
        creation = { ...await app.api('/api/create-options'), loadedAt: Date.now() };
      }
      $('new-project').replaceChildren(new Option('无归属', ''), ...creation.projects.map(project =>
        new Option(project.label + ' · ' + (project.hostDisplayName || '此电脑'), project.projectId)));
      $('new-model').replaceChildren(...creation.models.map(model => new Option(model.name, model.id)), new Option('自定义模型…', '__custom__'));
      modelEfforts();
      $('new-create').disabled = !creation.canCreate || creating;
    } catch (error) {
      $('new-error').textContent = error.message;
    }
  }
  async function createThread(event) {
    event.preventDefault();
    if (creating) return;
    const payload = {
      prompt: $('new-prompt').value, title: $('new-title').value.trim(),
      model: $('new-model').value === '__custom__' ? $('new-custom').value.trim() : $('new-model').value,
      effort: $('new-effort').value, projectId: $('new-project').value || null
    };
    let pending;
    try { pending = JSON.parse(sessionStorage.getItem('new-create-pending')); } catch {}
    if (!pending || JSON.stringify(pending.payload) !== JSON.stringify(payload)) pending = { id: app.uuid(), payload };
    sessionStorage.setItem('new-create-pending', JSON.stringify(pending));
    creating = true;
    $('new-create').disabled = true;
    $('new-error').textContent = '';
    try {
      const result = await app.api('/api/sessions', { id: pending.id, ...payload });
      if (result.status === 'unknown') throw Error('创建结果待确认，请先检查桌面。重复提交不会再次创建。');
      sessionStorage.removeItem('new-create-pending');
      $('new-dialog').close();
      $('new-prompt').value = '';
      $('new-title').value = '';
      await app.loadList(true);
      if (result.threadId) await app.openChat(result.threadId, result.hostId || 'local');
      else app.toast('桌面正在准备线程，请稍后刷新列表');
    } catch (error) {
      $('new-error').textContent = error.message;
    } finally {
      creating = false;
      $('new-create').disabled = false;
    }
  }
  async function compact(event) {
    event.preventDefault();
    const current = app.getCurrent();
    if (!current.id) return;
    $('compact-confirm').disabled = true;
    $('compact-error').textContent = '';
    const key = 'compact:' + current.host + '|' + current.id;
    let id = sessionStorage.getItem(key);
    if (!id) { id = app.uuid(); sessionStorage.setItem(key, id); }
    try {
      const result = await app.api(app.sessionUrl(current.id, 'compact', current.host), { id });
      if (result.status === 'unknown') throw Error('压缩结果待确认，请检查桌面状态，勿重复请求。');
      sessionStorage.removeItem(key);
      $('compact-dialog').close();
      app.toast('已向原线程请求压缩');
    } catch (error) {
      $('compact-error').textContent = error.message;
    } finally { $('compact-confirm').disabled = false; }
  }
  function installControls() {
    const actions = document.createElement('div');
    actions.className = 'sidebar-head-actions';
    const create = document.createElement('button');
    create.id = 'new-thread';
    create.type = 'button';
    create.className = 'new-thread-button primary';
    create.title = '新建线程';
    create.setAttribute('aria-label', '新建线程');
    actions.append($('refresh'));
    document.querySelector('.sidebar-head').append(actions);
    document.querySelector('.sidebar-head').after(create);
    const listStatus = document.createElement('p');
    listStatus.id = 'list-status';
    listStatus.className = 'list-status';
    $('sessions').before(listStatus);
    const strip = document.createElement('div');
    strip.id = 'context-strip';
    strip.className = 'context-strip';
    strip.innerHTML = '<div class="context-reading"><span>上下文</span><progress id="context-progress" max="100" value="0" aria-label="上下文窗口占用"></progress><span id="context-percent"></span><span id="context-tokens" class="context-tokens"></span></div><div class="context-actions"><span id="compacting" class="compacting" hidden>压缩中</span><span id="connection-indicator" class="connection-indicator"></span><button id="compact" class="compact-button" type="button" aria-label="手动压缩上下文" title="手动压缩上下文"></button></div>';
    $('composer').prepend(strip);
    const latest = document.createElement('button');
    latest.id = 'latest';
    latest.type = 'button';
    latest.className = 'icon-button latest-button';
    latest.title = '回到最新消息';
    latest.setAttribute('aria-label', latest.title);
    latest.hidden = true;
    $('chat').append(latest);
    const nav=document.createElement('nav');nav.id='turn-nav';nav.className='turn-nav';nav.setAttribute('aria-label','轮次目录');$('chat').append(nav);
    const dialogs = document.createElement('div');
    dialogs.innerHTML = `
      <dialog id="new-dialog" class="picker">
        <div class="picker-head"><h2>新建线程</h2><button type="button" class="icon-button" data-close="new-dialog" aria-label="关闭新建线程">×</button></div>
        <form id="new-form">
          <label>归属项目<select id="new-project"><option value="">无归属</option></select></label>
          <div class="new-settings">
            <label>模型<select id="new-model" required><option value="">正在读取…</option></select></label>
            <label>推理强度<select id="new-effort" required></select></label>
          </div>
          <label id="new-custom-label" hidden>自定义模型 ID<input id="new-custom" maxlength="200"></label>
          <label>名称<span class="optional">可选</span><input id="new-title" maxlength="200" autocomplete="off"></label>
          <label>首条消息<textarea id="new-prompt" rows="4" required maxlength="100000"></textarea></label>
          <p id="new-error" class="error" role="alert"></p>
          <button id="new-create" class="primary" type="submit">创建并发送</button>
        </form>
      </dialog>
      <dialog id="compact-dialog" class="picker">
        <div class="picker-head"><h2>压缩上下文</h2><button type="button" class="icon-button" data-close="compact-dialog" aria-label="关闭压缩确认">×</button></div>
        <p id="compact-thread" class="compact-thread"></p>
        <form id="compact-form"><p id="compact-error" class="error" role="alert"></p><button id="compact-confirm" type="submit" class="primary">压缩此线程</button></form>
      </dialog>`;
    document.body.append(dialogs);
    dialogs.querySelectorAll('[data-close]').forEach(node => node.onclick = () => $(node.dataset.close).close());
  }
  function init(options) {
    app = options;
    installControls();
    rowObserver = new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (entry.isIntersecting) visibleRows.add(entry.target);
        else visibleRows.delete(entry.target);
        if (!entry.target.isConnected) rowObserver.unobserve(entry.target);
      }
      scheduleContexts();
    }, { root: $('sessions') });
    turnObserver=new IntersectionObserver(entries=>{
      for(const entry of entries){
        const deferred=entry.target.querySelector('.turn-process[data-defer-fold]');
        if(!entry.isIntersecting&&deferred)ReadingUI.mutate(()=>{deferred.open=false;delete deferred.dataset.deferFold;});
        const dot=[...$('turn-nav').children].find(node=>node.dataset.id===entry.target.dataset.id);
        dot?.classList.toggle('in-view',entry.isIntersecting);
        if(dot)dot.setAttribute('aria-current',entry.isIntersecting?'step':'false');
      }
    },{root:$('timeline'),rootMargin:'-15% 0px -55% 0px'});
    highlightObserver = new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const code = entry.target;
        highlightObserver.unobserve(code);
        const language = [...code.classList].find(name => name.startsWith('language-'))?.slice(9);
        try { code.innerHTML = hljs.highlight(code.textContent, { language }).value; } catch {}
      }
    }, { root: $('timeline'), rootMargin: '150px' });
    const input = document.createElement('div');
    input.className = 'composer-input';
    $('message').rows=1;
    const controls = document.createElement('div');
    controls.className = 'compose-controls';
    const attach=button('Plus','添加附件',()=>$('attachment-input').click());attach.id='add-attachment';
    const fileInput=document.createElement('input');fileInput.type='file';fileInput.multiple=true;fileInput.id='attachment-input';fileInput.hidden=true;
    fileInput.accept='.png,.jpg,.jpeg,.gif,.webp,.pdf,.txt,.md,.csv,.tsv,.json,.yaml,.yml,.xml,.html,.css,.js,.ts,.tsx,.jsx,.py,.rs,.go,.java,.c,.cpp,.h,.sql,.log,.toml,.ini,.zip,.docx,.xlsx,.pptx';
    fileInput.onchange=()=>addAttachments([...fileInput.files]);
    controls.append(attach,fileInput,document.querySelector('.composer-tools'), $('send-mode'));
    document.querySelector('.compose-bottom').prepend(controls);
    const pills=document.createElement('div');pills.id='attachment-pills';pills.className='attachment-pills';
    const progress=document.createElement('span');progress.id='attachment-status';progress.className='attachment-status';progress.textContent='正在上传附件…';progress.hidden=true;
    input.append($('skill-pills'),pills,progress,$('send-error'), $('message'), document.querySelector('.compose-bottom'));
    document.querySelector('.compose-foot').before(input);
    for (const [id, name, label] of [['refresh', 'RefreshCw', '刷新线程'], ['back', 'ChevronLeft', '返回线程列表'],
      ['send', 'ArrowUp', '发送消息'], ['stop', 'Square', '停止当前任务']]) {
      $(id).replaceChildren(icon(name));
      $(id).title = label;
      $(id).setAttribute('aria-label', label);
    }
    document.querySelectorAll('.mark').forEach(node => node.replaceChildren(icon('SquareTerminal')));
    document.querySelector('.welcome h1').textContent = 'Codex';
    document.querySelector('.welcome p').remove();
    const welcomeCreate = button('SquarePen', '新建线程', newThread);
    welcomeCreate.className = 'primary';
    welcomeCreate.append(document.createTextNode('新建线程'));
    $('welcome').append(welcomeCreate);
    $('message').addEventListener('input', scheduleInput);
    $('message').addEventListener('focus',viewport);
    $('message').addEventListener('blur',viewport);
    $('message').addEventListener('compositionend',scheduleInput);
    $('send').addEventListener('pointerdown',event=>{
      if(event.isPrimary&&document.activeElement===$('message'))event.preventDefault();
    });
    bindDisclosures();
    $('message').addEventListener('paste',event=>{
      const files=[...event.clipboardData?.files||[]];
      if(files.length){event.preventDefault();addAttachments(files);}
    });
    $('new-thread').append(icon('SquarePen'),document.createTextNode('新建线程'));
    $('new-thread').onclick = newThread;
    $('compact').append(icon('Archive'),document.createTextNode('压缩'));
    $('latest').append(icon('ArrowDown'));
    $('latest').onclick = () => { $('timeline').scrollTop = $('timeline').scrollHeight; latestButton(); };
    $('compact').onclick = () => {
      $('compact-thread').textContent = app.getState()?.title || '';
      $('compact-error').textContent = '';
      $('compact-dialog').showModal();
    };
    $('compact-form').onsubmit = compact;
    $('new-form').onsubmit = createThread;
    $('new-model').onchange = modelEfforts;
    $('timeline').addEventListener('scroll', latestButton, { passive: true });
    $('sessions').addEventListener('scroll', scheduleContexts, { passive: true });
    $('sessions').addEventListener('toggle', scheduleContexts, true);
    window.visualViewport?.addEventListener('resize', viewport);
    window.visualViewport?.addEventListener('scroll', viewport);
    window.addEventListener('resize', viewport);
    viewport();
    setInterval(refreshContexts, 5000);
  }
  return { init, icon, button, richText, renderContext, decorateSession, releaseSession, releaseTurn, resetChat, scheduleContexts, latestButton,
    unobserveCode:code=>highlightObserver.unobserve(code),
    renderTurnNav, resizeInput, attachments, clearAttachments, uploading:()=>uploadCount>0 };
})();
