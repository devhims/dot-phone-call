import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';

const source=readFileSync(new URL('../chrome-extension/background.js',import.meta.url),'utf8');
function fixture({microphoneVerified=true, hookPresent=true, delayedStart=false, activeInitially=false, draft=false, newTabLoading=false, config=''}={}) {
  const events=[], values={};
  const tabs=[{id:1,windowId:9,url:'https://chatgpt.com/dots/home',active:false,lastAccessed:1},
    {id:2,windowId:9,url:'https://chatgpt.com/dots/home',active:true,lastAccessed:2}];
  if(newTabLoading)tabs.splice(0);
  let loadChecks=0;
  let installed=false, active=activeInitially, connected=false, pendingStart=0;
  const noop=()=>{};
  const chrome={
    runtime:{getManifest:()=>({version:'0.1.7'}),onStartup:{addListener:noop},onInstalled:{addListener:noop}},
    action:{onClicked:{addListener:noop}},alarms:{create:noop,onAlarm:{addListener:noop}},
    storage:{local:{get:async key=>({[key]:values[key]}),set:async v=>Object.assign(values,v),remove:async k=>{delete values[k];}}},
    tabs:{query:async()=>tabs,get:async id=>{const tab=tabs.find(t=>t.id===id);if(tab?.status==='loading'&&++loadChecks>=3){tab.status='complete';tab.url='https://chatgpt.com/dots/home';}return tab;},
      create:async options=>{const tab={id:3,windowId:9,url:'about:blank',pendingUrl:options.url,status:'loading'};tabs.push(tab);events.push(['create',3]);return tab;},
      reload:async id=>{events.push(['reload',id]);hookPresent=true;},
      update:async(id,changes)=>{events.push(['activate',id]);return Object.assign(tabs.find(t=>t.id===id),changes);},
      sendMessage:async(id,message)=>{
        events.push(['message',id,message.action]);
        if(tabs.find(t=>t.id===id)?.status==='loading')throw Error('Could not establish connection. Receiving end does not exist.');
        if(message.action.endsWith('inspect') && pendingStart) {
          pendingStart--; if(!pendingStart) {active=true;connected=true;}
        }
        if(message.action.endsWith('inspect')) return {ready:true,active,connected,has_draft:draft,version:installed?'0.1.7':'0.1.0'};
        if(message.action.endsWith('start')) {if(delayedStart) pendingStart=2; else {active=true;connected=true;} return {started:true};}
        if(message.action.endsWith('end')) {active=false;connected=false;return {ended:true};}
      }},
    windows:{update:async(id)=>{events.push(['focus',id]);}},
    scripting:{executeScript:async options=>{
      if(tabs.find(t=>t.id===options.target.tabId)?.status==='loading')throw Error('Cannot access contents of url about:blank. Extension manifest must request permission.');
      if(options.world==='MAIN') {
        const action=options.args[0];events.push(['microphone',action]);
        if(action==='arm')events.push(['arm_options',options.args[3]]);
        if(!hookPresent) return [{result:{error:'DOT_MICROPHONE_RELOAD_REQUIRED'}}];
        return [{result:action==='status'
          ? {microphone_verified:microphoneVerified,...(!microphoneVerified?{error:'DOT_MICROPHONE_DEVICE_MISMATCH'}:{})}
          : {armed:true,released:true}}];
      }
      installed=true;events.push(['inject',options.target.tabId]);
    }},
  };
  const context=vm.createContext({chrome,importScripts:noop,DOT_PHONE_INITIAL_TAB_ID:1,
    WebSocket:class {static OPEN=1;static CONNECTING=0;readyState=0;},
    setTimeout,clearTimeout,setInterval,clearInterval,Date});
  vm.runInContext(config+source,context);
  return {events,values,run:expression=>vm.runInContext(expression,context),refresh:()=>vm.runInContext('refreshDotTabs()',context),invoke:message=>{context.message=message;return vm.runInContext('command(message)',context);}};
}
test('prepare chooses the active Dot tab and activates it before inspecting',async()=>{
  const f=fixture();const result=await f.invoke({action:'prepare'});
  assert.equal(result.tab_id,2);
  assert.ok(f.events.findIndex(e=>e[0]==='activate') < f.events.findIndex(e=>e[0]==='message'));
});
test('prepare replaces stale page controls before starting a call',async()=>{
  const f=fixture();const prepared=await f.invoke({action:'prepare'});
  assert.ok(f.events.some(e=>e[0]==='inject'));
  assert.equal(prepared.version,'0.1.7');
  const started=await f.invoke({action:'start',job_id:'job',tab_id:prepared.tab_id,duration_seconds:120});
  assert.equal(started.connected,true);
  assert.equal((await f.invoke({action:'end',job_id:'job'})).ended,true);
  assert.equal(f.values.owned,undefined);
});

test('microphone is armed before Start call and released after hangup',async()=>{
  const f=fixture();
  await f.invoke({action:'start',job_id:'job',tab_id:2,duration_seconds:120});
  assert.ok(f.events.findIndex(e=>e[0]==='microphone'&&e[1]==='arm') <
    f.events.findIndex(e=>e[0]==='message'&&e[2].endsWith('start')));
  await f.invoke({action:'end',job_id:'job'});
  assert.ok(f.events.findIndex(e=>e[0]==='message'&&e[2].endsWith('end')) <
    f.events.findIndex(e=>e[0]==='microphone'&&e[1]==='disarm'));
});
test('connected timer cannot bypass microphone verification',async()=>{
  const f=fixture({microphoneVerified:false});
  await assert.rejects(f.invoke({action:'start',job_id:'job',tab_id:2,duration_seconds:120}),
    /DOT_MICROPHONE_DEVICE_MISMATCH/);
  assert.ok(f.values.owned);
  await f.invoke({action:'end',job_id:'job'});
  assert.equal(f.values.owned,undefined);
});
test('starting voice directly still refuses a missing document-start hook',async()=>{
  const f=fixture({hookPresent:false});
  await assert.rejects(f.invoke({action:'start',job_id:'job',tab_id:2,duration_seconds:120}),
    /DOT_MICROPHONE_RELOAD_REQUIRED/);
  assert.equal(f.events.some(e=>e[0]==='message'&&e[2].endsWith('start')),false);
});

test('voice startup waits for asynchronous controls instead of declaring an immediate hangup',async()=>{
  const f=fixture({delayedStart:true});
  const result=await f.invoke({action:'start',job_id:'job',tab_id:2,duration_seconds:120});
  assert.equal(result.connected,true);
  await f.invoke({action:'end',job_id:'job'});
});

test('extension startup refreshes stale microphone hooks automatically',async()=>{
  const f=fixture({hookPresent:false});await f.refresh();
  assert.ok(f.events.some(e=>e[0]==='reload'));
  assert.equal((await f.invoke({action:'prepare'})).ready,true);
});
test('up-to-date pages are not refreshed unnecessarily',async()=>{
  const f=fixture();await f.refresh();
  assert.equal(f.events.some(e=>e[0]==='reload'),false);
});
test('automatic refresh preserves an active call and an unsent draft',async()=>{
  for(const options of [{activeInitially:true},{draft:true}]) {
    const f=fixture({...options,hookPresent:false});await f.refresh();
    assert.equal(f.events.some(e=>e[0]==='reload'),false);
  }
});

test('prepare waits for a newly created Dot document before injecting controls',async()=>{
 const f=fixture({newTabLoading:true});const result=await f.invoke({action:'prepare'});
 assert.equal(result.ready,true);assert.equal(result.tab_id,3);
 assert.equal(f.events.some(e=>e[0]==='microphone'&&e[1]==='arm'),false);
});

test('browser API failures return useful codes without exposing raw URLs',()=>{
 const f=fixture();
 assert.equal(f.run("browserErrorCode(new Error('No tab with id: 99'))"),'DOT_TAB_CLOSED');
 assert.equal(f.run("browserErrorCode(new Error('Cannot access contents of url https://private.example'))"),'DOT_TAB_ACCESS_DENIED');
 assert.equal(f.run("browserErrorCode(new Error('Receiving end does not exist.'))"),'DOT_CONTROLS_UNAVAILABLE');
});

test('direct-audio builds ask Dot to route its output; normal builds do not',async()=>{
  // Matches pairing.js: a top-level const, not a globalThis property.
  for(const [config,expected] of [["const DOT_PHONE_CONFIG={directAudio:true};\n",{output:'blackhole-2ch'}],["const DOT_PHONE_CONFIG={directAudio:false};\n",null],['',null]]){
    const f=fixture({config});
    await f.invoke({action:'start',job_id:'job',tab_id:2,duration_seconds:120});
    assert.equal(JSON.stringify(f.events.find(e=>e[0]==='arm_options')?.[1]),JSON.stringify(expected));
  }
});

