'use strict';
// Isolated long-history fixture; never accesses the user's App or gateway.
const assert=require('node:assert/strict');
const http=require('node:http');
const fs=require('node:fs/promises');
const path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const WEB=path.resolve(__dirname,'../web');
const ID='33333333-3333-4333-8333-333333333333';
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
let count=300,sequence=1,delay=0,historyDelay=0,historyReads=0,detailReads=0;
const streams=new Set(),errors=[];
const message=index=>({id:'a'+index,role:'assistant',kind:'agentMessage',phase:'final_answer',text:'Synthetic answer '+index});
const turn=index=>({id:'t'+index,historyIndex:index-1,status:'completed',processAvailable:true,durationMs:2000,
  completedAt:1790990000000+index*3000,messages:[
    {id:'u'+index,role:'user',kind:'userMessage',text:'Synthetic prompt '+index},message(index)]});
function view(){
  return {id:ID,host:'local',hostLabel:'Synthetic',title:'Long history',cwd:'C:/Synthetic',
    connected:true,status:'idle',model:'fixture',effort:'high',provider:'mock',requests:[],submissions:[],files:[],
    contextUsage:{available:false},hasSavedHistory:true,historyComplete:true,historyCursor:count>12?'t'+(count-11):null,sequence,
    turns:Array.from({length:Math.min(12,count)},(_,index)=>turn(count-Math.min(12,count)+index+1))};
}
function publish(){for(const stream of streams)stream.write('event: state\ndata: '+JSON.stringify(view())+'\n\n');}
const server=http.createServer(async(request,response)=>{
  try{
    const url=new URL(request.url,'http://127.0.0.1');
    const json=value=>{response.writeHead(200,{'Content-Type':'application/json'});response.end(JSON.stringify(value));};
    if(url.pathname==='/api/auth')return json({authenticated:true,csrf:'fixture',transport:'sse'});
    if(url.pathname==='/api/sessions')return json({sessions:[{id:ID,host:'local',title:'Long history',recency:1,
      projectName:'Unassigned',hostLabel:'Synthetic',contextUsage:{available:false}}],unavailableHosts:[]});
    if(url.pathname==='/api/contexts')return json({contexts:[]});
    if(url.pathname.endsWith('/events')){
      response.writeHead(200,{'Content-Type':'text/event-stream'});
      response.write('event: state\ndata: '+JSON.stringify(view())+'\n\n');
      streams.add(response);response.on('close',()=>streams.delete(response));return;
    }
    if(url.pathname.endsWith('/history')){
      const id=url.searchParams.get('turn'),item=url.searchParams.get('message');
      if(id){
        detailReads++;
        const index=Number(id.slice(1));
        if(item==='reason')return json({message:{id:'reason',role:'activity',kind:'reasoning',
          summary:'**Synthetic summary**',detail:'Reasoning detail '.repeat(300)+'REASON-END'},sequence});
        if(item==='tool')return json({message:{id:'tool',role:'activity',kind:'commandExecution',title:'执行命令',
          text:'echo synthetic',output:'Synthetic output\n'.repeat(6000)+'OUTPUT-END',status:'completed'},sequence});
        return json({turn:{...turn(index),processAvailable:false,messages:[
          turn(index).messages[0],
          {id:'reason',role:'activity',kind:'reasoning',summary:'**Synthetic summary**',detail:'',
            detailAvailable:true,detailKey:'reason'},
          {id:'tool',role:'activity',kind:'commandExecution',title:'执行命令',text:'echo synthetic',
            output:'Preview',detailAvailable:true,detailKey:'tool',status:'completed'},message(index)]},sequence});
      }
      historyReads++;
      const before=Number(url.searchParams.get('before').slice(1));
      const end=before-1,start=Math.max(1,end-11),capturedSequence=sequence;
      const result={id:ID,host:'local',sequence:capturedSequence,historyCursor:start>1?'t'+start:null,
        turns:Array.from({length:end-start+1},(_,index)=>turn(start+index))};
      if(historyDelay)await pause(historyDelay);
      return json(result);
    }
    if(url.pathname==='/api/sessions/'+ID){
      const result=view();
      if(delay)await pause(delay);
      return json(result);
    }
    if(url.pathname.startsWith('/api/'))throw Error('Unexpected request '+request.url);
    const file=path.resolve(WEB,'.'+(url.pathname==='/'?'/index.html':url.pathname));
    if(!file.startsWith(WEB+path.sep))throw Error('Invalid path');
    response.writeHead(200,{'Content-Type':file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html'});
    response.end(await fs.readFile(file));
  }catch(error){errors.push(error.message);response.writeHead(500);response.end('{}');}
});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  let browser;
  try{
    browser=await chromium.launch({channel:'chrome',headless:true});
    const page=await browser.newPage({viewport:{width:390,height:844},isMobile:true,deviceScaleFactor:1});
    page.on('pageerror',error=>errors.push(error.message));
    await page.addInitScript(()=>{
      window.longTasks=[];
      new PerformanceObserver(list=>longTasks.push(...list.getEntries().map(entry=>entry.duration))).observe({type:'longtask',buffered:true});
    });
    delay=800;
    await page.goto('http://127.0.0.1:'+server.address().port+'/#'+ID+'~local');
    await page.waitForFunction(()=>state?.connected&&state.turns.length===12,{},{timeout:1500});
    assert.equal(await page.locator('.turn').count(),12);
    await pause(900);delay=0;
    historyDelay=350;
    const loading=page.evaluate(()=>loadEarlier());
    while(!historyReads)await pause(5);
    count++;sequence++;publish();
    await loading;historyDelay=0;
    const ids=await page.evaluate(()=>state.turns.map(turn=>turn.id));
    assert.deepEqual(ids,Array.from({length:25},(_,index)=>'t'+(277+index)),'No lost boundary turn while tail advances');
    while(await page.evaluate(()=>Boolean(historyCursor)))await page.evaluate(()=>loadEarlier());
    assert.equal(await page.evaluate(()=>state.turns.length),301);
    assert.equal(new Set(await page.evaluate(()=>state.turns.map(turn=>turn.id))).size,301);
    assert.equal(await page.locator('.turn').count(),301);
    const process=page.locator('[data-id=t301] .turn-process');
    await process.locator(':scope > summary').click();
    await process.locator('.activity-group.reasoning > summary').click();
    await page.waitForFunction(()=>document.querySelector('[data-id=t301] .reasoning-body')?.textContent.includes('REASON-END'));
    await process.locator('.activity-group.tools > summary').click();
    await process.locator('.activity > summary').click();
    await page.waitForFunction(()=>document.querySelector('[data-id=t301] .tool-output')?.textContent.includes('OUTPUT-END'));
    assert.ok(detailReads>=3);
    await page.locator('#message').fill('Synthetic draft');
    await page.evaluate(()=>{
      window.syntheticHidden=true;
      Object.defineProperty(document,'hidden',{configurable:true,get:()=>window.syntheticHidden});
      document.dispatchEvent(new Event('visibilitychange'));
    });
    count+=25;sequence=0;delay=800;
    await page.evaluate(()=>{
      window.syntheticHidden=false;
      document.dispatchEvent(new Event('visibilitychange'));
      window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));
    });
    await page.waitForFunction(()=>transportReady&&state.sequence===0&&state.turns.length===326,{},{timeout:2500});
    assert.deepEqual(await page.evaluate(()=>state.turns.map(turn=>turn.id)),
      Array.from({length:326},(_,index)=>'t'+(index+1)),'Wake fills multi-page gap and retains all older pages');
    assert.equal(await page.locator('#message').inputValue(),'Synthetic draft');
    await pause(850);delay=0;
    await page.locator('#back').click();
    delay=800;
    const started=Date.now();
    const reopening=page.evaluate(id=>openChat(id,'local'),ID);
    await page.waitForFunction(()=>document.querySelectorAll('.turn').length===326);
    const reentryMs=Date.now()-started;
    assert.ok(reentryMs<700,'Cached long-thread reentry must not wait for snapshot GET');
    await reopening;
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({historyTurns:326,noBoundaryLoss:true,multiPageWakeGap:true,fullToolAndReasoning:true,
      wakeSequenceReset:true,cachedReentryMs:reentryMs,historyReads,detailReads,
      maxLongTaskMs:Math.round(Math.max(0,...await page.evaluate(()=>longTasks)))}));
  }finally{
    if(browser)await browser.close();
    for(const stream of streams)stream.end();
    await new Promise(resolve=>server.close(resolve));
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
