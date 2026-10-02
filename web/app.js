'use strict';
const $ = id => document.getElementById(id);
let csrf='', currentId=null, state=null, events=null, listOffset=0, listQuery='', approvalStamp='', sending=false;
let currentHost='local', listRows=[], listGeneration=0;
let activationAttempted=false, activatingKey=null;
let modelDirty=false, queueStamp='';
let stopTarget=null;
const catalogCache=new Map(), sessionNodes=new Map(), groupNodes=new Map();
const seenTurns=new Map();
const renderStamps=new WeakMap();
function chatKey(id=currentId,host=currentHost){return host+'|'+id;}
function sessionUrl(id,action='',host=currentHost){return '/api/sessions/'+id+(action?'/'+action:'')+'?host='+encodeURIComponent(host);}
function hostUrl(path,host=currentHost){return /^\/api\/sessions\/[0-9a-f-]{36}/.test(path)&&!/[?&]host=/.test(path)?path+(path.includes('?')?'&':'?')+'host='+encodeURIComponent(host):path;}
let transport='sse', streamGeneration=0, catalogData=null, selectedSkills=new Set();
function el(tag,cls,text){const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node;}
function toast(text){$('toast').textContent=text;$('toast').hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('toast').hidden=true,4200);}
async function api(path,body){const options={credentials:'same-origin',cache:'no-store',headers:{}};if(body!==undefined){options.method='POST';options.headers={'Content-Type':'application/json','X-CSRF-Token':csrf};options.body=JSON.stringify(body);}const response=await fetch(hostUrl(path),options);const data=await response.json();if(!response.ok){if(response.status===401)showLogin();throw Error(data.error||'请求失败');}return data;}
function showLogin(passwordless=false){ReadingUI.reset();document.querySelectorAll('dialog[open]').forEach(d=>d.close());streamGeneration++;events?.close();events=null;$('app').hidden=true;$('login').hidden=false;$('credentials').hidden=passwordless;$('noauth').hidden=!passwordless;$('username').required=!passwordless;$('password').required=!passwordless;$('password').value='';}
async function start(){const auth=await api('/api/auth');transport=auth.transport||'sse';if(!auth.authenticated){showLogin(auth.passwordless);return;}csrf=auth.csrf;await enter();}
async function enter(){$('login').hidden=true;$('app').hidden=false;const list=loadList(true).catch(e=>toast(e.message));const [id,host='local']=location.hash.slice(1).split('~');if(/^[0-9a-f-]{36}$/.test(id))await openChat(id,decodeURIComponent(host));await list;}
$('login-form').addEventListener('submit',async event=>{event.preventDefault();$('login-button').disabled=true;$('login-error').textContent='';try{const result=await api('/api/login',{username:$('username').value,password:$('password').value});csrf=result.csrf;$('password').value='';await enter();}catch(error){$('login-error').textContent=error.message;}finally{$('login-button').disabled=false;}});
$('logout').onclick=async()=>{await api('/api/logout',{});csrf='';state=null;currentId=null;seenTurns.clear();$('messages').replaceChildren();$('approvals').replaceChildren();$('queued').replaceChildren();$('sessions').replaceChildren();$('message').value='';sessionStorage.clear();showLogin();};
function dateText(value){if(!value)return '';return new Date(value*1000).toLocaleDateString('zh-CN',{month:'numeric',day:'numeric'});}
$('list-mode').value=localStorage.getItem('list-mode-v2')||'project';
$('list-mode').onchange=()=>{localStorage.setItem('list-mode-v2',$('list-mode').value);loadList(true).catch(e=>toast(e.message));};
function renderList(){
  const fragment=document.createDocumentFragment(), liveNodes=new Set();
  const grouped=$('list-mode').value==='project', groups=new Map();
  const rows=grouped?[...listRows.filter(row=>row.projectId),...listRows.filter(row=>!row.projectId)]:listRows;
  const counts=new Map();
  for(const row of rows){const key=row.projectId?row.projectKey:'unassigned';counts.set(key,(counts.get(key)||0)+1);}
  for(const chat of rows){
    let parent=fragment;
    if(grouped){
      const key=chat.projectId?chat.projectKey:'unassigned';
      if(!groups.has(key)){
        const details=groupNodes.get(key)||el('details','project-group');
        groupNodes.set(key,details);
        details.classList.toggle('unassigned',!chat.projectId);
        details.open=Boolean(listQuery.trim())||localStorage.getItem('group:'+key)!=='closed';
        const summary=el('summary','project-heading');
        const chevron=BridgeUI.icon('ChevronRight');chevron.classList.add('project-chevron');
        summary.append(BridgeUI.icon(chat.projectId?'Folder':'Inbox'),el('span','project-name',chat.projectId?chat.projectName:'无归属线程'),chevron);
        summary.append(el('span','project-count',String(counts.get(key))));
        details.replaceChildren(summary,el('div','project-threads'));
        details.ontoggle=()=>{if(!listQuery.trim())localStorage.setItem('group:'+key,details.open?'open':'closed');};
        groups.set(key,details);fragment.append(details);
      }
      parent=groups.get(key).querySelector('.project-threads');
    }
    const rowKey=chatKey(chat.id,chat.host), stamp=JSON.stringify(chat);
    liveNodes.add(rowKey);
    let cached=sessionNodes.get(rowKey),button=cached?.button;
    if(!button||cached.stamp!==stamp){
    button=el('button','session');
    button.dataset.id=chat.id;button.dataset.host=chat.host;
    const identity=el('span','session-identity');
    identity.append(el('strong','',chat.title));
    const meta=el('small');meta.append(el('span','',chat.projectName+' · '+chat.hostLabel),el('time','',dateText(chat.recency/1000)));
    identity.append(meta);BridgeUI.decorateSession(identity,chat,button);
    const preview=el('span','prompt-preview');
    preview.append(el('span','prompt-label','最近输入'),el('span','prompt-text',chat.latestPrompt??''));
    preview.querySelector('.prompt-text').dataset.prompt='true';
    button.append(identity,preview);button.onclick=()=>openChat(chat.id,chat.host).catch(e=>toast(e.message));parent.append(button);
    if(cached)BridgeUI.releaseSession(cached.button);
    sessionNodes.set(rowKey,{button,stamp});
    }else parent.append(button);
    button.classList.toggle('selected',chat.id===currentId&&chat.host===currentHost);
  }
  for(const key of sessionNodes.keys())if(!liveNodes.has(key)){BridgeUI.releaseSession(sessionNodes.get(key).button);sessionNodes.delete(key);}
  for(const key of groupNodes.keys())if(!groups.has(key))groupNodes.delete(key);
  if(!listRows.length)fragment.append(el('p','muted','没有找到聊天。'));
  const top=$('sessions').scrollTop;
  $('sessions').replaceChildren(fragment);
  $('sessions').scrollTop=top;
  BridgeUI.scheduleContexts();
}
async function loadList(reset=false){
  const generation=++listGeneration;
  if(reset){listOffset=0;listRows=[];}
  listQuery=$('search').value;
  let result;
  $('list-status').textContent='正在读取线程…';
  do{
    result=await api('/api/sessions?q='+encodeURIComponent(listQuery)+'&offset='+listOffset+'&archived='+$('archived').checked);
    if(generation!==listGeneration)return;
    const rows=new Map(listRows.map(row=>[chatKey(row.id,row.host),row]));
    for(const row of result.sessions)rows.set(chatKey(row.id,row.host),row);
    listRows=[...rows.values()];listRows.sort((a,b)=>b.recency-a.recency);
    listOffset+=result.sessions.length;
    $('more').hidden=result.sessions.length<100||$('list-mode').value==='project';
    $('host-errors').textContent=(result.unavailableHosts||[]).map(h=>h.label+'：'+h.error).join('\n');
    $('host-errors').hidden=!(result.unavailableHosts||[]).length;
    if(listOffset===result.sessions.length||result.sessions.length<100||$('list-mode').value!=='project')renderList();
  }while($('list-mode').value==='project'&&result.sessions.length===100);
  $('list-status').textContent=new Set(listRows.filter(row=>row.projectId).map(row=>row.projectKey)).size+' 项目 · '+listRows.length+' 条线程';
}
$('search').oninput=()=>{clearTimeout(loadList.timer);loadList.timer=setTimeout(()=>loadList(true).catch(e=>toast(e.message)),300);};$('refresh').onclick=()=>loadList(true).catch(e=>toast(e.message));$('archived').onchange=$('refresh').onclick;$('more').onclick=()=>loadList().catch(e=>toast(e.message));
function saveDraft(){if(currentId)sessionStorage.setItem('draft:'+chatKey(),$('message').value);}
$('message').oninput=saveDraft;
async function openChat(id,host="local"){document.querySelectorAll('dialog[open]').forEach(d=>d.close());BridgeUI.resetChat();streamGeneration++;saveDraft();events?.close();events=null;currentId=id;currentHost=host;state=null;activationAttempted=false;catalogData=null;modelDirty=false;queueStamp='';$('model-button').disabled=true;$('model-button').textContent='正在读取模型…';try{selectedSkills=new Set(JSON.parse(sessionStorage.getItem('skills:'+chatKey(id,host))||'[]'));}catch{selectedSkills=new Set();}renderSkillPills();approvalStamp='';seenTurns.clear();$('messages').replaceChildren();$('approvals').replaceChildren();$('queued').replaceChildren();$('message').value=sessionStorage.getItem('draft:'+chatKey(id,host))||'';$('send-error').textContent='';$('chat-title').textContent='正在连接…';$('chat-meta').textContent='';$('status').textContent='连接中';$('welcome').hidden=true;$('chat').hidden=false;$('app').classList.add('chat-open');$('send').disabled=true;history.replaceState(null,'','#'+id+'~'+encodeURIComponent(host));document.querySelectorAll('.session').forEach(n=>n.classList.toggle('selected',n.dataset.id===id&&n.dataset.host===host));const view=await api('/api/sessions/'+id);if(id!==currentId||host!==currentHost)return;renderState(view,true);connectUpdates(id,host);}
async function activateChat(id=currentId,host=currentHost){
  const key=chatKey(id,host);
  if(activatingKey===key)return;
  activationAttempted=true;activatingKey=key;$('reconnect').disabled=true;
  try{
    const view=await api(sessionUrl(id,'activate',host),{});
    if(id===currentId&&host===currentHost&&$('app').classList.contains('chat-open'))renderState(view);
  }catch(error){
    if(id===currentId&&host===currentHost){$('notice-text').textContent=error.message;toast(error.message);}
  }finally{
    if(activatingKey===key)activatingKey=null;
    if(id===currentId&&host===currentHost)$('reconnect').disabled=false;
  }
}
function connectUpdates(id,host){const generation=++streamGeneration;events?.close();if(transport==='poll'){pollUpdates(id,generation,host);return;}events=new EventSource(sessionUrl(id,'events',host));let retryTimer;events.addEventListener('state',event=>{clearTimeout(retryTimer);if(id===currentId&&host===currentHost&&generation===streamGeneration)renderState(JSON.parse(event.data));});events.addEventListener('logout',()=>showLogin());events.onerror=()=>{if(id!==currentId||host!==currentHost||generation!==streamGeneration)return;$('status').textContent='重新连接中';$('status').classList.add('offline');if(!retryTimer)retryTimer=setTimeout(()=>{if(generation!==streamGeneration||id!==currentId||host!==currentHost)return;events?.close();transport='poll';pollUpdates(id,generation,host);},5000);};}
async function pollUpdates(id,generation,host){let after=state?.sequence??-1;while(generation===streamGeneration&&id===currentId&&host===currentHost&&!$('app').hidden){try{const result=await api(sessionUrl(id,'poll',host)+'&after='+after);if(generation!==streamGeneration||id!==currentId||host!==currentHost)return;if(result.state){after=result.state.sequence;renderState(result.state);}await new Promise(resolve=>setTimeout(resolve,200));}catch(error){if(generation!==streamGeneration)return;$('status').textContent='重新连接中';await new Promise(resolve=>setTimeout(resolve,3000));}}}
$('back').onclick=()=>{ReadingUI.reset();$('app').classList.remove('chat-open');history.replaceState(null,'',location.pathname);loadList(true).catch(e=>toast(e.message));};
function richText(node,text){BridgeUI.richText(node,text,state?.files,id=>sessionUrl(currentId,'files/'+id));}
function renderTurn(turn,previous,options={}){
  const section=el('section','turn');section.dataset.id=turn.id;
  section.dataset.prompt=turn.messages.find(message=>message.role==='user')?.text?.trim().slice(0,160)||'';
  section.dataset.status=turn.status;
  let parent=section;
  const completed=options.completed??(turn.status==='completed'||turn.status==='interrupted');
  const final=completed?ThreadUI.finalMessage(turn):null;
  const process=completed&&turn.messages.some(message=>message!==final&&message.role!=='user'&&message.kind!=='contextCompaction')?el('details','turn-process'):null;
  const processBody=process?el('div','turn-process-body'):null;
  if(process){
    process.dataset.key='process:'+turn.id;
    const summary=el('summary','');
    const chevron=BridgeUI.icon('ChevronDown');chevron.classList.add('disclosure-chevron');
    summary.append(BridgeUI.icon('Check'),el('span','','本轮过程'),el('span','turn-time',ThreadUI.completedText(turn)),chevron);
    process.append(summary,processBody);
    if(options.keepProcess)process.open=true;
    if(options.keepProcess||previous?.querySelector('.turn-process')?.dataset.deferFold)process.dataset.deferFold='true';
    process.ontoggle=()=>{
      if(!process.open)delete process.dataset.deferFold;
      if(!process.open||processBody.children.length)return;
      const content=renderTurn({...turn,status:'process',error:null,messages:turn.messages.filter(message=>message.role!=='user'&&message!==final&&message.kind!=='contextCompaction')},null);
      content.className='turn-process-content';
      processBody.append(content);
    };
  }
  const oldMessages=new Map([...previous?.querySelectorAll('.message')||[]].map(node=>[node.dataset.messageId,node]));
  const oldGroups=new Map([...previous?.querySelectorAll('.activity-group')||[]].map(node=>[node.dataset.key,node]));
  const oldSlots=new Map([...previous?.querySelectorAll('[data-stream-slot]')||[]].map(node=>[node.dataset.streamSlot,node]));
  function activityText(parent,slot,text,className,format='plain',complete=!options.live){
    if(!text)return;
    const key=turn.id+'|activity|'+slot;
    const node=ReadingUI.node(key)||oldSlots.get(key)||el('div',className);
    node.dataset.streamSlot=key;
    parent.append(node);
    const stamp=format+'|'+text;
    if(renderStamps.get(node)!==stamp||ReadingUI.has(key)){
      renderStamps.set(node,stamp);
      ReadingUI.update(key,node,text,complete,null,null,options.instant||!options.live&&!ReadingUI.has(key),format);
    }
  }
  function collapseBody(node,label){
    node.classList.add('collapse-detail');
    node.tabIndex=0;node.setAttribute('role','button');node.setAttribute('aria-label','收起'+label);
    return node;
  }
  let activities=[];
  function flush(){
    if(!activities.length)return;
    const first=String(activities[0].id||activities[0].index);
    for(const [category,label,name] of [['reasoning','思考过程','Brain'],['tools','工具调用','Wrench'],['compact','上下文压缩','Archive']]){
      const rows=activities.filter(message=>category==='reasoning'?message.kind==='reasoning':category==='compact'?message.kind==='contextCompaction':!['reasoning','contextCompaction'].includes(message.kind));
      if(!rows.length)continue;
      const key='group:'+category+':'+first;
      const stamp=JSON.stringify(rows);
      const cached=oldGroups.get(key);
      if(cached&&renderStamps.get(cached)===stamp){(category==='compact'?section:parent).append(cached);continue;}
      const parts=category==='reasoning'?rows.map(ThreadUI.reasoningParts).filter(part=>part.headline||part.detail):[];
      const hasDetail=category==='tools'||category==='reasoning'&&parts.some(part=>part.detail);
      if(category==='reasoning'&&!parts.some(part=>part.headline||part.detail))continue;
      const group=el(hasDetail?'details':'div','activity-group '+category);group.dataset.key=key;
      renderStamps.set(group,stamp);
      const heading=el(hasDetail?'summary':'div','activity-heading');
      heading.append(BridgeUI.icon(name),el('span','activity-label',label));
      if(category==='reasoning'){
        activityText(heading,key+':summary',parts.at(-1).headline,'reasoning-preview');
        for(const [index,part] of parts.entries()){
          if(!part.detail)continue;
          const body=collapseBody(el('div','reasoning-body'),'思考过程');
          if(parts.length>1&&part.headline)body.append(el('strong','reasoning-title',part.headline));
          activityText(body,key+':detail:'+index,part.detail,'reasoning-text','markdown');group.append(body);
        }
      }else if(category==='compact'){
        group.classList.add('compaction-record');
        heading.append(el('span','compaction-caption',rows.some(message=>message.status==='inProgress')?'正在整理上下文':'上下文已整理'));
      }else{
        heading.append(el('span','activity-summary-count',String(rows.length)));
        const groupBody=collapseBody(el('div','activity-body'),'工具调用');
        for(const message of rows){
          const detail=el('details','activity');detail.dataset.key=category+':'+String(message.id||message.index);
          const presentation=ThreadUI.activityPresentation(message);
          const title=el('summary','');
          const itemIcon=({fileChange:'FileDiff',contextCompaction:'Archive',commandExecution:'Terminal',webSearch:'Search',imageView:'Image',mcpToolCall:'Wrench',dynamicToolCall:'Wrench',collabToolCall:'Users'})[message.kind]||'Wrench';
          title.append(BridgeUI.icon(itemIcon),
            el('span','tool-title',presentation.title+(message.status==='inProgress'?' · 进行中':'')));
          detail.append(title);
          const body=collapseBody(el('div','tool-body'),presentation.title);
          const complete=!options.live||message.status==='completed'||message.status==='failed';
          activityText(body,detail.dataset.key+':call',presentation.call,'tool-call','plain',complete);
          activityText(body,detail.dataset.key+':diff',presentation.diff,'tool-diff','diff',complete);
          activityText(body,detail.dataset.key+':output',presentation.output,'tool-output','plain',complete);
          if(message.exitCode!=null)body.append(el('small',message.exitCode?'tool-failure':'tool-success','退出码 '+message.exitCode));
          detail.append(body);groupBody.append(detail);
        }
        group.append(groupBody);
      }
      if(hasDetail){const chevron=BridgeUI.icon('ChevronDown');chevron.classList.add('disclosure-chevron');heading.append(chevron);}
      group.prepend(heading);
      (category==='compact'?section:parent).append(group);
    }
    activities=[];
  }
  const deferWork=process&&!options.keepProcess&&!previous?.querySelector('.turn-process')?.open;
  const messages=deferWork?turn.messages.filter(message=>message.role==='user'||message===final||message.kind==='contextCompaction'):turn.messages;
  for(const [index,message] of messages.entries()){
    if(message.role==='activity'){activities.push({...message,index});continue;}
    flush();
    parent=process&&message.role!=='user'&&message!==final?processBody:section;
    if(process&&!process.isConnected&&message.role!=='user'&&!section.contains(process))section.append(process);
    if(!message.text&&!message.attachments?.length)continue;
    const key=String(message.id||index),stamp=JSON.stringify(message),cached=oldMessages.get(key);
    const streamKey=turn.id+'|'+key;
    const streaming=message.role==='assistant'&&(options.live||ReadingUI.has(streamKey));
    if(cached&&renderStamps.get(cached)===stamp&&!streaming){parent.append(cached);parent=process?processBody:section;continue;}
    if(streaming){
      const row=ReadingUI.has(streamKey)&&cached?cached:el('div','message '+message.role);
      row.dataset.messageId=key;row.dataset.role=message.role;row.dataset.preview=message.text.slice(0,100);
      renderStamps.set(row,stamp);
      parent.append(row);
      ReadingUI.update(streamKey,row,message.text,!options.live,state?.files,id=>sessionUrl(currentId,'files/'+id),options.instant||Boolean(cached&&!ReadingUI.has(streamKey)));
      parent=process?processBody:section;continue;
    }
    const row=el('div','message '+message.role);row.dataset.messageId=key;renderStamps.set(row,stamp);
    row.dataset.role=message.role;row.dataset.preview=message.text?.slice(0,100)||'';
    row.append(el('span','who',message.role==='user'?'你':'CODEX'));richText(row,message.text);
    if(message.attachments?.length)row.append(el('small','muted','附件：'+message.attachments.map(a=>a.name||a.path||a.type).join('、')));
    parent.append(row);
    parent=process?processBody:section;
  }
  flush();
  if(process&&!section.contains(process))section.append(process);
  if(!process&&turn.status==='completed')section.append(el('p','turn-time',ThreadUI.completedText(turn)));
  if(turn.error)section.append(el('p','error',typeof turn.error==='string'?turn.error:turn.error.message||JSON.stringify(turn.error)));
  BridgeUI.releaseTurn(previous);
  return section;
}
function renderState(view,initial=false){
  const timeline=$('timeline');
  const position=ReadingUI.capture(initial||!seenTurns.size);
  const firstNative=!state?.connected&&view.connected;
  state=view;
  $('execution-host').textContent='在 '+(view.host==='local'?'电脑':view.hostLabel||'SSH 主机')+' 运行';
  $('chat-title').textContent=view.loadingHistory?'正在读取聊天…':view.title;
  $('chat-meta').textContent=(view.hostLabel||'此电脑')+' · '+(view.cwd||'电脑上的聊天');
  $('model-button').textContent=[view.model||'模型',view.effort].filter(Boolean).join(' · ');
  $('model-button').disabled=view.loadingHistory||!view.model;
  if($('model-dialog').open&&catalogData&&!modelDirty){syncModelPicker();}
  $('provider').textContent=[view.model,view.provider].filter(Boolean).join(' · ');
  const compacting=ThreadUI.isCompacting(view);
  $('status').textContent=!view.connected?(view.activating||activatingKey===chatKey()?'加载线程中':view.loadingHistory?'读取历史中':view.connecting?'连接桌面中':'历史记录'):compacting?'压缩上下文中':view.requests.length?'等待回应':view.status==='active'?'运行中':'已连接';
  $('status').classList.toggle('offline',!view.connected);
  $('notice').hidden=view.connected;
  $('notice-text').textContent=view.activating||activatingKey===chatKey()?'正在按需加载此线程，可先阅读已保存的历史。':view.connecting?'正在连接桌面，可先阅读已保存的历史。':view.connectionError||'当前显示已保存的历史。';
  $('reconnect').textContent=view.canActivate?'连接此线程':'重新连接';
  $('reconnect').title=view.canActivate?'仅加载此线程，电脑端会切换到此线程':'重新查询原线程连接';
  $('reconnect').disabled=view.activating||activatingKey===chatKey();
  $('history').hidden=view.historyComplete||!view.connected;
  $('working').hidden=view.status!=='active'&&!compacting;
  const workingMode=compacting?'compact':'task';
  if($('working').dataset.mode!==workingMode){
    $('working').dataset.mode=workingMode;
    $('working').replaceChildren(BridgeUI.icon(compacting?'Archive':'LoaderCircle'),document.createTextNode(compacting?'正在压缩上下文…':'Codex 正在工作…'));
  }
  $('working').classList.toggle('is-compacting',compacting);
  $('stop').hidden=view.status!=='active'||!view.connected||compacting;
  $('send').disabled=!view.connected||sending||BridgeUI.uploading()||compacting;
  $('send-mode').querySelector('[value=steer]').disabled=view.status!=='active';
  if(view.status==='active'&&$('send-mode').value==='send')$('send-mode').value='queue';
  if(view.status!=='active'&&['steer','queue'].includes($('send-mode').value))$('send-mode').value='send';
  const desired=new Set();
  for(const turn of view.turns){
    desired.add(turn.id);
    const completed=ThreadUI.completedTurn(turn,view);
    const live=view.connected&&!completed&&turn.status==='inProgress';
    const stamp=JSON.stringify(turn)+'|'+completed+'|'+live;
    if(seenTurns.get(turn.id)?.stamp===stamp)continue;
    const old=seenTurns.get(turn.id)?.node;
    const opened=new Map(old?[...old.querySelectorAll('details')].map(d=>[d.dataset.key,d.open]):[]);
    const keepProcess=!position.bottom&&old?.dataset.status==='inProgress'&&completed
      &&position.node?.closest('.turn')===old&&(position.node.closest('.activity-group')
        ||position.node.closest('.message.assistant')?.dataset.messageId!==String(ThreadUI.finalMessage(turn)?.id)
          &&position.node.closest('.message.assistant'));
    const node=renderTurn(turn,old,{completed,live,instant:firstNative||initial,keepProcess});
    for(const detail of node.querySelectorAll('details'))if(opened.has(detail.dataset.key))detail.open=opened.get(detail.dataset.key);
    if(old)old.replaceWith(node);else $('messages').append(node);
    seenTurns.set(turn.id,{stamp,node});
  }
  for(const [id,value] of seenTurns)if(!desired.has(id)){BridgeUI.releaseTurn(value.node);value.node.remove();seenTurns.delete(id);}
  let sibling=$('messages').firstElementChild;
  for(const turn of view.turns){const node=seenTurns.get(turn.id).node;if(node!==sibling)$('messages').insertBefore(node,sibling);sibling=node.nextElementSibling;}
  ReadingUI.prune();
  renderApprovals(view.requests);renderQueue(view.submissions||[]);
  BridgeUI.renderContext(view);
  ReadingUI.restore(position);
  BridgeUI.latestButton();
  BridgeUI.renderTurnNav(view.turns);
  if($('stop-dialog').open&&(view.status!=='active'||stopTarget?.turn!==activeTurnId()))$('stop-dialog').close();
  if(view.activationRequired&&view.canActivate&&!activationAttempted&&$('app').classList.contains('chat-open')){
    activateChat();
  }
}
function jsonPretty(value){return typeof value==='string'?value:JSON.stringify(value,null,2);}
function renderApprovals(requests){const stamp=JSON.stringify(requests);if(stamp===approvalStamp)return;approvalStamp=stamp;$('approvals').replaceChildren();for(const request of requests){const card=el('section','approval');card.dataset.request=String(request.id);const params=request.params||{}, method=request.method;const heading=method.includes('requestUserInput')?'Codex 需要你的回复':method.includes('fileChange')?'允许修改文件？':method.includes('commandExecution')?'允许运行此命令？':method.includes('permissions')?'允许本轮权限？':'待确认请求';card.append(el('h3','',heading));const controls={};if(!request.supported){card.append(el('p','',params.message||'请在桌面 App 处理此请求'));$('approvals').append(card);continue;}if(params.reason)card.append(el('p','',params.reason));if(params.command)card.append(el('pre','',jsonPretty(params.command)));if(params.cwd)card.append(el('p','muted',params.cwd));for(const key of ['changes','grantRoot','permissions','networkApprovalContext'])if(params[key])card.append(el('pre','',jsonPretty(params[key])));const form=el('form');if(method.includes('requestUserInput')){for(const question of params.questions||[]){const label=el('label','',question.question||question.header||question.id);if(question.options?.length){const select=el('select');select.append(new Option('请选择…',''));for(const option of question.options)select.append(new Option(option.label+(option.description?' — '+option.description:''),option.label));select.append(new Option('自行填写','__custom__'));label.append(select);const custom=el('textarea');custom.rows=2;custom.hidden=true;custom.placeholder='输入你的回复';select.onchange=()=>custom.hidden=select.value!=='__custom__';label.append(custom);controls[question.id]=()=>select.value==='__custom__'?custom.value:select.value;}else{const input=el('textarea');input.rows=2;input.required=true;label.append(input);controls[question.id]=()=>input.value;}form.append(label);}}else if(method==='mcpServer/elicitation/request'){if(params.message)form.append(el('p','',params.message));const schema=params.requestedSchema||{};for(const [key,field] of Object.entries(schema.properties||{})){const label=el('label','',field.title||key);let input;if(field.enum){input=el('select');input.append(new Option('请选择…',''));for(const [i,value] of field.enum.entries())input.append(new Option(field.enumNames?.[i]||String(value),String(value)));}else{input=el('input');input.type=field.type==='boolean'?'checkbox':['number','integer'].includes(field.type)?'number':'text';if(input.type==='number')input.step=field.type==='integer'?'1':'any';}input.required=(schema.required||[]).includes(key)&&input.type!=='checkbox';if(field.description)label.append(el('p','muted',field.description));label.append(input);form.append(label);controls[key]=()=>input.type==='checkbox'?input.checked:input.value===''?undefined:['number','integer'].includes(field.type)?Number(input.value):input.value;}}const actions=el('div','actions'),error=el('p','error');async function submit(response){const buttons=card.querySelectorAll('button');buttons.forEach(b=>b.disabled=true);error.textContent='';try{await api('/api/sessions/'+currentId+'/respond',{requestId:request.id,response});toast('已发送回应');card.append(el('p','muted','已发送，等待桌面确认…'));}catch(e){error.textContent=e.message;buttons.forEach(b=>b.disabled=false);}}const isInput=method.includes('requestUserInput'),isMcp=method==='mcpServer/elicitation/request';const yes=el('button','primary',isInput?'提交回复':'本次允许');yes.type='submit';const available=params.availableDecisions;const canAccept=!available||available.includes('accept');if(isInput||isMcp||canAccept)actions.append(yes);if(!isInput){const no=el('button','secondary','拒绝');no.type='button';no.onclick=()=>submit(isMcp?{action:'decline'}:{decision:'decline'});if(!available||available.includes('decline'))actions.append(no);if(method.includes('commandExecution')||method.includes('fileChange')||isMcp){const cancel=el('button','secondary','取消');cancel.type='button';cancel.onclick=()=>submit(isMcp?{action:'cancel'}:{decision:'cancel'});if(!available||available.includes('cancel'))actions.append(cancel);}}form.onsubmit=event=>{event.preventDefault();if(isInput){const answers={};for(const [key,get] of Object.entries(controls)){const value=get();if(!value.trim()){error.textContent='请回答所有问题';return;}answers[key]=[value];}submit({answers});}else if(isMcp){const content={};for(const [key,get] of Object.entries(controls)){const value=get();if(value!==undefined)content[key]=value;}submit({action:'accept',content});}else submit({decision:'accept'});};form.append(actions,error);card.append(form);$('approvals').append(card);}}
function renderQueue(rows){const stamp=JSON.stringify(rows);if(stamp===queueStamp)return;queueStamp=stamp;$('queued').replaceChildren();for(const row of rows){const card=el('div','queue-card');const reply=row.text.startsWith('<send_user_message_question_reply>');card.append(el('strong','',row.status==='queued'?'等待当前任务完成':'发送结果待确认'),el('p','',reply?'问题回复':row.text));if(row.status==='queued'){const cancel=el('button','plain','撤回待发送消息');cancel.onclick=async()=>{try{await api('/api/sessions/'+currentId+'/queue',{id:row.id});toast('已撤回');}catch(e){toast(e.message);}};card.append(cancel);}else card.append(el('p','muted','请检查聊天记录。网关不会自动重发这条消息。'));$('queued').append(card);}}
function uuid(){const bytes=crypto.getRandomValues(new Uint8Array(16));bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;const h=[...bytes].map(x=>x.toString(16).padStart(2,'0')).join('');return h.slice(0,8)+'-'+h.slice(8,12)+'-'+h.slice(12,16)+'-'+h.slice(16,20)+'-'+h.slice(20);}
$('composer').onsubmit=async event=>{
  event.preventDefault();if(sending||!currentId||BridgeUI.uploading())return;
  const text=$('message').value,attachments=BridgeUI.attachments();
  if(!text.trim()&&!attachments.length)return;
  const mode=$('send-mode').value,skills=[...selectedSkills].sort(),key='pending:'+chatKey();
  let pending;try{pending=JSON.parse(sessionStorage.getItem(key));}catch{}
  if(!pending||pending.text!==text||pending.mode!==mode||JSON.stringify(pending.skills||[])!==JSON.stringify(skills)||JSON.stringify(pending.attachments||[])!==JSON.stringify(attachments))pending={id:uuid(),text,mode,skills,attachments};
  sessionStorage.setItem(key,JSON.stringify(pending));sending=true;$('send').disabled=true;$('send-error').textContent='';
  const target=currentId,targetHost=currentHost;
  try{
    const result=await api(sessionUrl(target,'send',targetHost),pending);
    if(result.status==='unknown')throw Error('消息可能已送达，请检查聊天记录。再次点击不会重复提交。');
    sessionStorage.removeItem(key);sessionStorage.removeItem('draft:'+chatKey(target,targetHost));
    if(currentId===target&&currentHost===targetHost){$('message').value='';selectedSkills.clear();renderSkillPills();BridgeUI.clearAttachments();BridgeUI.resizeInput();}
    toast(result.status==='queued'?'已加入待发送队列':'已发送到会话');
  }catch(error){if(currentId===target&&currentHost===targetHost)$('send-error').textContent=error.message;}
  finally{sending=false;$('send').disabled=!state?.connected||BridgeUI.uploading()||ThreadUI.isCompacting(state||{});}
};
$('message').onkeydown=event=>{if(!event.isComposing&&event.key==='Enter'&&(event.metaKey||event.ctrlKey)){event.preventDefault();$('composer').requestSubmit();}};
function activeTurnId(){return [...state?.turns||[]].reverse().find(turn=>turn.status==='inProgress')?.id;}
$('stop').onclick=()=>{stopTarget={id:currentId,host:currentHost,turn:activeTurnId()};$('stop-error').textContent='';$('stop-thread').textContent=state?.title||'';$('stop-dialog').showModal();};
$('stop-confirm').onclick=async()=>{
  if(!stopTarget||stopTarget.id!==currentId||stopTarget.host!==currentHost||stopTarget.turn!==activeTurnId()||state?.status!=='active'){$('stop-dialog').close();return;}
  $('stop-confirm').disabled=true;
  try{await api(sessionUrl(stopTarget.id,'stop',stopTarget.host),{expectedTurnId:stopTarget.turn});$('stop-dialog').close();toast('已请求停止');}
  catch(error){$('stop-error').textContent=error.message;}finally{$('stop-confirm').disabled=false;}
};
$('history').onclick=async()=>{$('history').disabled=true;try{await api('/api/sessions/'+currentId+'/history',{});}catch(e){toast(e.message);}finally{$('history').disabled=false;}};
$('reconnect').onclick=async()=>{if(state?.canActivate)return activateChat();try{renderState(await api('/api/sessions/'+currentId+'/reconnect',{}));}catch(e){toast(e.message);}};
document.querySelectorAll('[data-close]').forEach(button=>button.onclick=()=>$(button.dataset.close).close());
async function loadCatalog(refresh=false){const target=currentId,targetHost=currentHost,key=chatKey(),cached=catalogCache.get(key);const value=!refresh&&cached&&Date.now()-cached.time<60000?cached.value:await api(sessionUrl(target,'catalog',targetHost)+(refresh?'&refresh=true':''));if(currentId!==target||currentHost!==targetHost)throw Error('聊天已切换');catalogData=value;catalogCache.set(key,{time:Date.now(),value});if(catalogCache.size>32)catalogCache.delete(catalogCache.keys().next().value);return value;}
function renderEfforts(){const id=$('model-select').value,model=catalogData?.models.find(m=>m.id===id),current=state?.model||catalogData?.currentModel;const effort=state&&'effort' in state?state.effort:catalogData?.currentEffort;$('custom-model-label').hidden=id!=='__custom__';$('custom-model').required=id==='__custom__';ModelSettings.fill($('effort-select'),model,effort,id===current||id==='__custom__'&&$('custom-model').value===current);$('model-description').textContent=model?.description||'自定义模型的档位未验证，请确认当前 API 服务支持。';}
function syncModelPicker(){const current=state?.model||catalogData.currentModel;$('model-select').value=catalogData.models.some(m=>m.id===current)?current:'__custom__';$('custom-model').value=current||'';renderEfforts();$('model-save').disabled=!state?.connected;}
$('model-button').onclick=async()=>{if(!currentId||!state)return;modelDirty=false;catalogData=null;$('model-error').textContent='';$('model-select').replaceChildren(new Option('读取模型列表…',''));$('effort-select').replaceChildren(new Option('读取推理档位…',''));$('model-save').disabled=true;$('model-dialog').showModal();try{const catalog=await loadCatalog();$('model-select').replaceChildren(...catalog.models.map(m=>new Option(m.name,m.id)),new Option('自定义模型…','__custom__'));syncModelPicker();}catch(e){$('model-error').textContent=e.message;}};
$('model-select').onchange=()=>{modelDirty=true;renderEfforts();};
$('effort-select').onchange=()=>{modelDirty=true;};
$('custom-model').oninput=()=>{modelDirty=true;};
$('model-form').onsubmit=async event=>{event.preventDefault();$('model-save').disabled=true;$('model-error').textContent='';const model=$('model-select').value==='__custom__'?$('custom-model').value.trim():$('model-select').value;try{const result=await api('/api/sessions/'+currentId+'/settings',{model,effort:$('effort-select').value});$('model-dialog').close();toast(result.confirmed?'桌面模型设置已同步':'桌面已接受设置，等待同步');}catch(e){$('model-error').textContent=e.message;}finally{$('model-save').disabled=false;}};
function renderSkillPills(){$('skill-count').textContent=selectedSkills.size?'('+selectedSkills.size+')':'';$('skill-pills').replaceChildren();for(const id of selectedSkills){const skill=catalogData?.skills.find(s=>s.id===id);const pill=el('button','skill-pill',(skill?.displayName||'已选 Skill')+' ×');pill.type='button';pill.onclick=()=>{selectedSkills.delete(id);renderSkillPills();renderSkills();};$('skill-pills').append(pill);}if(currentId)sessionStorage.setItem('skills:'+chatKey(),JSON.stringify([...selectedSkills]));}
function renderSkills(){$('skill-list').replaceChildren();const search=$('skill-search').value.toLocaleLowerCase();for(const skill of catalogData?.skills||[]){if(![skill.name,skill.displayName,skill.description].join(' ').toLocaleLowerCase().includes(search))continue;const label=el('label','skill-option');const input=el('input');input.type='checkbox';input.value=skill.id;input.checked=selectedSkills.has(skill.id);input.onchange=()=>{if(input.checked&&selectedSkills.size>=8){input.checked=false;toast('最多选择 8 个 Skill');return;}if(input.checked)selectedSkills.add(skill.id);else selectedSkills.delete(skill.id);renderSkillPills();};const content=el('span');content.append(el('strong','',skill.displayName),el('small','',skill.description));label.append(input,content);$('skill-list').append(label);}if(!$('skill-list').children.length)$('skill-list').append(el('p','muted','没有匹配的 Skill'));}
async function openSkills(refresh=false){if(!currentId)return;$('skills-error').textContent='';$('skill-list').textContent='正在读取已安装 Skill…';if(!$('skills-dialog').open)$('skills-dialog').showModal();try{await loadCatalog(refresh);renderSkills();renderSkillPills();}catch(e){$('skills-error').textContent=e.message;$('skill-list').replaceChildren();}}
$('skills-button').onclick=()=>openSkills();$('skills-refresh').onclick=()=>openSkills(true);$('skill-search').oninput=renderSkills;
BridgeUI.init({api,openChat,loadList,sessionUrl,toast,uuid,getState:()=>state,
  isSending:()=>sending,
  upload:async(path,file)=>{const response=await fetch(path,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/octet-stream','X-CSRF-Token':csrf,'X-Filename':encodeURIComponent(file.name)},body:file});const result=await response.json();if(!response.ok)throw Error(result.error||'上传失败');return result;},
  getCurrent:()=>({id:currentId,host:currentHost}),
  updateRow:summary=>{const row=listRows.find(row=>row.id===summary.id&&row.host===summary.host);if(row)Object.assign(row,summary);}});
window.addEventListener('pagehide',saveDraft);
start().catch(error=>{showLogin();$('login-error').textContent=error.message;});
