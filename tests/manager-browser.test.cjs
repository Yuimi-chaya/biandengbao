/* Isolated desktop UI acceptance. No real App, credentials or gateway. */
const assert=require('node:assert/strict');
const http=require('node:http');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..');
const output=path.resolve(process.env.MANAGER_QA_OUTPUT||path.join(root,'.tmp/manager-qa'));
fs.mkdirSync(output,{recursive:true});
const state={manager:{version:'0.2.0-rc.1',revision:'f3a97cb0123456789012345678901234567890123456',dirty:false},platform:'win32',configPath:'C:/Users/Demo/AppData/Local/Biandengbao/config.json',logDirectory:'C:/Users/Demo/AppData/Local/Biandengbao/logs',configured:true,username:'admin',app:{running:true,version:'26.930.3930.0'},gateway:{running:true,version:'0.2.0-rc.1',port:8787,addresses:['http://127.0.0.1:8787','http://192.168.1.24:8787'],devices:[{id:'a'.repeat(32),userAgent:'iPhone EdgiOS Safari',address:'192.168.1.28',createdAt:1791000000,lastSeen:1791000600,expires:1791043200},{id:'b'.repeat(32),userAgent:'Windows Chrome',address:'192.168.1.36',createdAt:1790900000,lastSeen:1791000500,expires:1791040000}]},worker:{running:true,state:'monitoring'},autostart:{enabled:true,supported:true},settings:{port:8787,codexHome:'C:/Users/Demo/.codex',callerThread:'demo-thread',network:{mode:'lan'}}};
state.appearance='system';
const calls=[];let unauthorized=false;
const assets={'/':['manager-web/index.html','text/html'],'/app.js':['manager-web/app.js','text/javascript'],'/style.css':['manager-web/style.css','text/css'],'/lucide.js':['web/vendor/lucide.js','text/javascript'],'/icon.svg':['assets/manager-icon.svg','image/svg+xml']};
const server=http.createServer(async(req,res)=>{
  if(req.method==='GET'&&assets[req.url]){const [file,mime]=assets[req.url];res.setHeader('Content-Type',mime);res.end(fs.readFileSync(path.join(root,file)));return;}
  if(req.headers.authorization!=='Bearer fixture-token'||unauthorized){res.writeHead(403,{'Content-Type':'application/json'});res.end(JSON.stringify({ok:false,error:'管理授权已失效，请重新打开管理端'}));return;}
  let raw='';for await(const chunk of req)raw+=chunk;const body=JSON.parse(raw||'{}'),action=req.url.replace('/api/v1/','');calls.push({action,body});let result={};
  if(action==='status')result=state;
  else if(action==='contexts')result=[{id:'demo-thread',title:'网站首页改版'},{id:'demo-two',title:'账单整理'}];
  else if(action==='settings'){state.settings={...state.settings,...body};result={saved:true};}
  else if(action==='devices/revoke'){state.gateway.devices=state.gateway.devices.filter(d=>d.id!==body.id);result={revoked:1};}
  else if(action==='devices/revoke-all'){state.gateway.devices=[];result={revoked:2};}
  else if(action==='account'){state.username=body.username;state.gateway.devices=[];result={sessionsRevoked:true};}
  else if(action==='service/stop'){state.gateway.running=false;state.gateway.addresses=[];state.worker.running=false;result={state:'stopped'};}
  else if(action==='autostart'){state.autostart.enabled=body.enabled;result=state.autostart;}
  else if(action==='appearance'){state.appearance=body.mode;result={appearance:body.mode};}
  else if(action==='updates/check'){state.update={relation:'ahead',remoteSha:'b'.repeat(40),checkedAt:1791000600};result=state.update;}
  else if(action==='service/start'){state.gateway.running=true;result={state:'started'};}
  res.setHeader('Content-Type','application/json');res.end(JSON.stringify({ok:true,result}));
});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const base='http://127.0.0.1:'+server.address().port;let browser;
  try{
    browser=await chromium.launch({channel:'chrome',headless:true});const page=await browser.newPage({viewport:{width:1120,height:790},deviceScaleFactor:1});const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto(base+'/#fixture-token');await page.locator('#gateway-state').filter({hasText:'运行中'}).waitFor();
    assert.equal(new URL(page.url()).hash,'');assert.equal(await page.locator('#device-count').textContent(),'2');
    await page.screenshot({path:path.join(output,'overview-light.png')});
    await page.click('[data-page=connection]');await page.click('#load-contexts');await page.locator('#caller option[value=demo-two]').waitFor({state:'attached'});
    await page.click('[name=mode][value=tunnel]');assert.equal(await page.locator('#tunnel-fields').isVisible(),true);await page.fill('#cloudflared','C:/Tools/cloudflared.exe');
    await page.click('#connection-form button[type=submit]');await page.click('#confirm-dialog button[value=cancel]');assert.equal(calls.filter(c=>c.action==='settings').length,0);
    await page.click('#connection-form button[type=submit]');await page.click('#confirm-button');await page.waitForFunction(()=>!document.body.hasAttribute('aria-busy'));assert.equal(calls.find(c=>c.action==='settings').body.network.mode,'tunnel');
    await page.screenshot({path:path.join(output,'connection-light.png')});
    await page.click('[data-page=devices]');await page.locator('.device-row button').first().click();await page.click('#confirm-button');await page.waitForFunction(()=>document.querySelectorAll('.device-row').length===1);assert.equal(state.gateway.devices[0].id,'b'.repeat(32));
    await page.screenshot({path:path.join(output,'devices-light.png')});
    await page.click('[data-page=settings]');await page.fill('#password','synthetic-password-123');await page.fill('#password-confirm','different-password-123');await page.click('#account-form button[type=submit]');assert.equal(await page.locator('#confirm-dialog').isVisible(),false);assert.equal(calls.filter(c=>c.action==='account').length,0);
    await page.fill('#password-confirm','synthetic-password-123');await page.click('#account-form button[type=submit]');await page.click('#confirm-button');await page.waitForFunction(()=>document.getElementById('password').value==='');assert.equal(state.gateway.devices.length,0);
    const appearanceSaved=page.waitForResponse(response=>response.url().endsWith('/api/v1/appearance'));await page.selectOption('#appearance','dark');await appearanceSaved;assert.equal(state.appearance,'dark');assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');await page.click('#check-update');await page.locator('#update-result').filter({hasText:'main 有新提交'}).waitFor();
    await page.screenshot({path:path.join(output,'settings-dark.png')});await page.click('[data-page=overview]');await page.screenshot({path:path.join(output,'overview-dark.png')});
    await page.click('#service-toggle');await page.click('#confirm-dialog button[value=cancel]');assert.equal(calls.filter(c=>c.action==='service/stop').length,0);await page.click('#service-toggle');const stopped=page.waitForResponse(response=>response.url().endsWith('/api/v1/service/stop'));await page.click('#confirm-button');await stopped;await page.locator('#gateway-state').filter({hasText:'已停止'}).waitFor();assert.equal(state.gateway.running,false);
    for(const [width,height] of [[850,720],[390,844]]){await page.setViewportSize({width,height});for(const tab of ['overview','connection','devices','settings']){await page.click('[data-page='+tab+']');assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),tab+' overflow');}await page.screenshot({path:path.join(output,'settings-'+width+'.png'),fullPage:true});}
    await page.emulateMedia({reducedMotion:'reduce'});assert.equal(await page.locator('#refresh').evaluate(el=>getComputedStyle(el).transitionDuration),'0s');
    await page.evaluate(()=>localStorage.clear());await page.reload();await page.waitForFunction(()=>document.documentElement.dataset.theme==='dark');assert.equal(await page.locator('#appearance').inputValue(),'dark');
    unauthorized=true;await page.click('#refresh');await page.locator('#notice').filter({hasText:'管理授权已失效'}).waitFor();assert.deepEqual(errors,[]);
    // Deterministic repository icon rasterization from its vector source.
    const asset=await browser.newPage({viewport:{width:1024,height:1024}});await asset.goto(base+'/icon.svg');await asset.screenshot({path:path.join(root,'assets/manager-icon.png'),omitBackground:true});await asset.setViewportSize({width:256,height:256});await asset.evaluate(()=>{document.documentElement.setAttribute('width','256');document.documentElement.setAttribute('height','256');});await asset.screenshot({path:path.join(root,'assets/manager-icon-256.png'),omitBackground:true});
    console.log(JSON.stringify({passed:true,scriptErrors:errors.length,viewports:[1120,850,390],mutations:calls.filter(c=>c.action!=='status'&&c.action!=='contexts').map(c=>c.action),screenshots:output},null,2));
  }finally{if(browser)await browser.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});
