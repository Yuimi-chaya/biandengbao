'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const titles = {overview:'一切，就在手边',connection:'让手机，连上你的电脑',devices:'登录设备',settings:'按你的习惯来'};
  let token = location.hash.slice(1);
  try { if (token) sessionStorage.setItem('manager-capability', token); else token = sessionStorage.getItem('manager-capability') || ''; } catch {}
  history.replaceState(null, '', location.pathname);
  let state = null, initialized = false, busy = false, polling = false, timer, toastTimer;
  let epoch = 0, refreshQueued = false;
  let addressSignature = '', deviceSignature = '';
  const icons = () => window.lucide?.createIcons();
  const node = (tag, className, text) => { const n = document.createElement(tag); if(className)n.className=className; if(text !== undefined)n.textContent=text; return n; };
  const icon = (name) => { const n=node('i'); n.dataset.lucide=name; return n; };
  const time = (value) => value ? new Intl.DateTimeFormat('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).format(new Date(value*1000)) : '—';
  function toast(text) { clearTimeout(toastTimer); $('toast').textContent=text; $('toast').hidden=false; toastTimer=setTimeout(()=>$('toast').hidden=true,4500); }
  function notice(text, error=false) { $('notice').textContent=text; $('notice').hidden=!text; $('notice').classList.toggle('error',error); }
  function page(name) { if(!titles[name])return; document.querySelectorAll('.page').forEach(el=>el.hidden=el.id!==name); document.querySelectorAll('[data-page]').forEach(el=>{el.classList.toggle('selected',el.dataset.page===name); if(el.dataset.page===name)el.setAttribute('aria-current','page'); else el.removeAttribute('aria-current');}); $('page-title').textContent=titles[name]; window.scrollTo(0,0); }
  async function api(action, data={}) {
    const control=new AbortController(), timeout=setTimeout(()=>control.abort(),60000);
    try { const response=await fetch('/api/v1/'+action,{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json','Authorization':'Bearer '+token},body:JSON.stringify(data),signal:control.signal}); const value=await response.json(); if(!response.ok||!value.ok)throw new Error(value.error||'管理操作失败'); return value.result; }
    catch(error){ if(error.name==='AbortError')throw new Error('操作超时，请先刷新状态确认结果，不要重复提交。'); throw error; }
    finally{clearTimeout(timeout);}
  }
  function confirm(title, text, label='确认') { const dialog=$('confirm-dialog'); $('confirm-title').textContent=title; $('confirm-body').textContent=text; $('confirm-button').textContent=label; dialog.returnValue=''; dialog.showModal(); return new Promise(resolve=>dialog.addEventListener('close',()=>resolve(dialog.returnValue==='confirm'),{once:true})); }
  async function act(action, data, message) {
    if(busy)return; busy=true; epoch++; document.body.setAttribute('aria-busy','true');
    const controls=[...document.querySelectorAll('button,input,select')].filter(el=>!el.closest('dialog'));
    const disabled=controls.map(el=>el.disabled); controls.forEach(el=>el.disabled=true);
    try{const result=await api(action,data); if(message)toast(message); return result;}
    catch(error){notice(error.message,true); throw error;}
    finally{busy=false;epoch++;controls.forEach((el,i)=>el.disabled=disabled[i]);document.body.removeAttribute('aria-busy');await refresh();}
  }
  function badge(id,text,good=false,error=false){const el=$(id);el.textContent=text;el.classList.toggle('good',good);el.classList.toggle('error',error);}
  function active(){return !!(state?.gateway.running || (state?.worker.running && !['stopped_until_app_restart','error','disabled'].includes(state.worker.state)));}
  function modeFields(){const mode=document.querySelector('[name=mode]:checked').value;$('tunnel-fields').hidden=mode!=='tunnel';$('proxy-fields').hidden=mode!=='proxy';$('cloudflared').required=mode==='tunnel';$('proxy-origin').required=mode==='proxy';}
  function empty(container, glyph, text){const box=node('div','empty-state');box.append(icon(glyph),node('p','',text));container.replaceChildren(box);}
  function renderAddresses(gateway, settings){
    const addresses=(gateway.addresses||[]).filter(url=>/^https?:[/][/]/.test(url)&&!url.includes('localhost')&&!url.includes('127.0.0.1'));
    const signature=JSON.stringify(addresses);if(signature===addressSignature)return;addressSignature=signature;
    const list=$('addresses');if(!addresses.length){empty(list,'wifi',gateway.running?'外网入口尚未连接；请检查连接设置或日志':'启动服务后，访问地址会显示在这里');list.firstChild.classList.add('compact');icons();return;}
    const rows=addresses.map(url=>{const row=node('div','address-row');row.append(icon(url.startsWith('https:')?'globe-2':'wifi'));const info=node('div','address-content');info.append(node('small','',url.startsWith('https:')?'HTTPS 外网入口':'局域网访问'));const link=node('a','',url);link.href=url;link.target='_blank';link.rel='noopener noreferrer';info.append(link);const copy=node('button','icon-button');copy.type='button';copy.title='复制访问地址';copy.setAttribute('aria-label','复制访问地址');copy.append(icon('copy'));copy.addEventListener('click',()=>navigator.clipboard.writeText(url).then(()=>toast('地址已复制')).catch(()=>toast('无法访问剪贴板，可选中地址复制')));row.append(info,copy);return row;});list.replaceChildren(...rows);icons();
  }
  function deviceName(ua){const platform=/iPhone|iPad/.test(ua)?'iPhone / iPad':/Android/.test(ua)?'Android':/Windows/.test(ua)?'Windows':/Macintosh/.test(ua)?'Mac':'浏览器';const browser=/Edg|EdgiOS/.test(ua)?'Edge':/Chrome|CriOS/.test(ua)?'Chrome':/Firefox|FxiOS/.test(ua)?'Firefox':/Safari/.test(ua)?'Safari':'';return platform+(browser?' · '+browser:'');}
  function renderDevices(gateway){
    const devices=gateway.devices||[];$('device-count').textContent=gateway.running?String(devices.length):'—';$('revoke-all').disabled=busy||!devices.length;
    const signature=JSON.stringify([gateway.running,devices.map(d=>[d.id,Math.floor(d.lastSeen/60)])]);if(signature===deviceSignature)return;deviceSignature=signature;
    const list=$('devices-list');if(!devices.length){empty(list,'monitor-smartphone',gateway.running?'还没有已登录设备':'启动新版网关后，可管理已登录设备');icons();return;}
    list.replaceChildren(...devices.map(device=>{const row=node('article','device-row'), visual=node('div','device-icon');visual.append(icon(/iPhone|iPad|Android/.test(device.userAgent)?'smartphone':'monitor'));const text=node('div','device-text');text.append(node('h3','',deviceName(device.userAgent||'')),node('p','', '最近活动 '+time(device.lastSeen)+' · 来源 '+device.address),node('small','', '登录 '+time(device.createdAt)+' · 到期 '+time(device.expires)));const button=node('button','secondary','解除登录');button.addEventListener('click',async()=>{if(await confirm('解除这次登录？','该浏览器将失去读取和操作聊天的权限。','解除登录'))await act('devices/revoke',{id:device.id},'已解除登录').catch(()=>{});});row.append(visual,text,button);return row;}));icons();
  }
  function render(value){
    state=value;const {app,gateway,worker,manager,settings}=value;
    $('sidebar-version').textContent='v'+manager.version;$('build-version').textContent='便蹬宝 '+manager.version;$('build-revision').textContent=manager.revision.slice(0,10)+(manager.dirty?' · 本地修改':'');
    $('config-path').textContent=value.configPath;$('log-path').textContent=value.logDirectory;
    badge('health','管理端已连接',true);badge('app-state',app.running?'运行中':'未打开',app.running);badge('gateway-state',gateway.running?'运行中':worker.running?'等待 / 已暂停':'已停止',gateway.running);
    $('app-version').textContent=app.version?'版本 '+app.version:'版本暂不可用';$('gateway-version').textContent='版本 '+(gateway.version||manager.version)+(gateway.running?' · 端口 '+gateway.port:'');
    const toggle=$('service-toggle');toggle.replaceChildren(icon(active()?'square':'play'),node('span','',active()?'停止服务':'启动服务'));toggle.disabled=busy||!value.configured;
    $('autostart').checked=value.autostart.enabled===true;$('autostart').disabled=busy||value.autostart.supported===false||value.autostart.enabled===null;
    const states={waiting_for_app:'正在等待 Codex App 和桌面工具通道',monitoring:'已连接桌面 App，会持续跟随 App 的重新打开',started:'服务已启动',stopped_until_app_restart:'已手动停止；下次打开 App 后恢复，或点击启动服务',waiting_for_channel:'正在等待桌面工具通道恢复',existing_instance:'检测到现有网关，不会启动第二个',existing_listener:'端口已被其他程序占用',startup_failed:'启动未确认，请检查日志',error:worker.error||'启动失败，请检查日志'};
    $('worker-note').textContent=(worker.running?states[worker.state]:worker.state==='error'?states.error:'')||value.autostart.message||'';
    $('network-note').textContent=settings.network.mode==='lan'?'局域网模式 · 手机和电脑连接同一网络':settings.network.mode==='tunnel'?'临时外网 · HTTPS 地址随隧道重启而变化':'已有域名 · 由你的反向代理提供 HTTPS';
    renderAddresses(gateway,settings);renderDevices(gateway);
    if(!initialized){$('username').value=value.username;$('port').value=settings.port;$('codex-home').value=settings.codexHome;$('cloudflared').value=settings.network.cloudflared||'';$('proxy-origin').value=settings.network.origin||'';document.querySelector('[name=mode][value='+settings.network.mode+']').checked=true;if(settings.callerThread){const option=new Option('已绑定 · '+settings.callerThread,settings.callerThread);$('caller').append(option);$('caller').value=settings.callerThread;}modeFields();initialized=true;}
    if(!value.configured)notice('先到“设置”创建网关账号，再选择一个已有聊天作为桌面调用上下文。');
    else if(gateway.tunnel?.error)notice(gateway.tunnel.error,true);
    else if(gateway.message)notice(gateway.message,true);
    else if(!$('notice').classList.contains('error'))notice('');
    if(value.update)renderUpdate(value.update);icons();
  }
  function renderUpdate(result){const messages={identical:'与 main 一致',ahead:'main 有新提交',behind:'本地版本领先 main',diverged:'本地与 main 存在分叉',unknown:'无法判断版本先后'};const label=messages[result.relation]||messages.unknown;$('update-result').textContent=label+' · '+result.remoteSha.slice(0,10)+' · '+time(result.checkedAt)+'。仅检查，未自动更新。';}
  async function refresh(){if(polling){refreshQueued=true;return;}polling=true;const current=epoch;clearTimeout(timer);try{const value=await api('status');if(current===epoch)render(value);else refreshQueued=true;}catch(error){badge('health','管理端未连接',false,true);notice(error.message,true);}finally{polling=false;if(!document.hidden){const delay=refreshQueued?0:4000;refreshQueued=false;timer=setTimeout(refresh,delay);}}}
  function applyTheme(mode){const dark=mode==='dark'||(mode==='system'&&matchMedia('(prefers-color-scheme: dark)').matches);document.documentElement.dataset.theme=dark?'dark':'light';}
  let theme='system';try{theme=localStorage.getItem('biandengbao:manager-theme')||'system';}catch{}if(!['system','light','dark'].includes(theme))theme='system';$('appearance').value=theme;applyTheme(theme);
  $('appearance').addEventListener('change',()=>{theme=$('appearance').value;applyTheme(theme);try{localStorage.setItem('biandengbao:manager-theme',theme);}catch{}});matchMedia('(prefers-color-scheme: dark)').addEventListener('change',()=>applyTheme(theme));
  document.querySelectorAll('[data-page]').forEach(el=>el.addEventListener('click',()=>page(el.dataset.page)));document.querySelectorAll('[data-go]').forEach(el=>el.addEventListener('click',()=>page(el.dataset.go)));
  document.querySelectorAll('[name=mode]').forEach(el=>el.addEventListener('change',modeFields));$('refresh').addEventListener('click',()=>{notice('');refresh();});
  $('service-toggle').addEventListener('click',async()=>{const stop=active();if(stop&&!await confirm('停止便蹬宝服务？','手机连接会断开。不会关闭 Codex App，也不会停止桌面聊天中的任务。','停止服务'))return;await act(stop?'service/stop':'service/start',stop?{confirm:true}:{},stop?'已请求停止网关':'已开始等待 App 连接').catch(()=>{});});
  $('autostart').addEventListener('change',async()=>{const enabled=$('autostart').checked;if(enabled&&!await confirm('开启登录自启动？','登录电脑后，后台等待 Codex App，并使用当前保存的连接模式。外网模式会启动已配置的隧道。','开启')){$('autostart').checked=false;return;}await act('autostart',{enabled},enabled?'已开启登录自启动':'已关闭自启动；当前网关不受影响').catch(()=>{});});
  $('load-contexts').addEventListener('click',async()=>{try{const rows=await api('contexts'),old=$('caller').value;$('caller').replaceChildren(new Option('请选择一个已有的 Codex 聊天',''));rows.forEach(row=>$('caller').append(new Option(row.title,row.id)));if(old&&!rows.some(row=>row.id===old))$('caller').append(new Option('已绑定 · '+old,old));$('caller').value=old;toast('已读取聊天列表，未发送消息');}catch(error){notice(error.message,true);}});
  $('connection-form').addEventListener('submit',async event=>{event.preventDefault();const mode=document.querySelector('[name=mode]:checked').value,network={mode};if(mode==='tunnel')network.cloudflared=$('cloudflared').value.trim();if(mode==='proxy')network.origin=$('proxy-origin').value.trim();const restart=!!(state?.gateway.running||state?.worker.running);if(restart&&!await confirm('保存并重启网关？','手机登录会失效，需要重新登录。Codex App 和正在执行的聊天任务不受影响。','保存并重启'))return;await act('settings',{port:Number($('port').value),codexHome:$('codex-home').value.trim(),callerThread:$('caller').value,network,confirmRestart:restart},'连接设置已保存').catch(()=>{});});
  $('account-form').addEventListener('submit',async event=>{event.preventDefault();if($('password').value!==$('password-confirm').value){notice('两次输入的密码不一致',true);return;}if(!await confirm('保存账号并退出所有设备？','所有手机浏览器需要使用新账号密码重新登录。不会更改 Codex 模型认证。','保存'))return;try{await act('account',{username:$('username').value.trim(),password:$('password').value,confirm:true},'账号已保存，旧会话已失效');$('password').value='';$('password-confirm').value='';notice('');}catch{}});
  $('show-password').addEventListener('click',()=>{const show=$('password').type==='password';$('password').type=show?'text':'password';$('password-confirm').type=show?'text':'password';$('show-password').setAttribute('aria-pressed',String(show));});
  $('revoke-all').addEventListener('click',async()=>{if(await confirm('退出全部登录设备？','所有手机浏览器将需要重新登录。','全部退出'))await act('devices/revoke-all',{confirm:true},'全部设备已退出登录').catch(()=>{});});
  $('check-update').addEventListener('click',async()=>{try{const result=await act('updates/check',{},'已完成 main 更新检查');if(result)renderUpdate(result);}catch{}});
  document.addEventListener('visibilitychange',()=>{clearTimeout(timer);if(!document.hidden)refresh();});window.addEventListener('focus',()=>{if(!document.hidden)refresh();});
  icons();refresh();
})();
