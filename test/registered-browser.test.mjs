import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../browser/app.js',import.meta.url),'utf8').replace(/^import .*;$/gm,'');
const flush=()=>new Promise(r=>setImmediate(r));
async function fixture({prepareError,permission='granted'}={}){
  const events=[],handlers={},windowHandlers={},sessions=[],elements={status:{},end:{},microphone:{}};let state={job:null},denied=false;
  class AudioSession {constructor(){this.socket={};this.destination={stream:{}};sessions.push(this);}async prepare(){events.push('prepare');if(prepareError)throw Error(prepareError);}async remote(){events.push('remote');}async close(){this.closed=true;events.push('audio_closed');}}
  class Device {constructor(token,options){this.options=options;this.state='unregistered';this.audio={incoming(){},outgoing(){},disconnect(){}};Device.last=this;}on(name,fn){handlers[name]=fn;}async register(){this.state='registered';events.push('registered');}updateToken(){events.push('refreshed');}destroy(){this.state='destroyed';}}
  const context=vm.createContext({navigator:{permissions:{query:async()=>({state:permission})},mediaDevices:{getUserMedia:async()=>{events.push('permission_requested');permission='granted';return {getTracks:()=>[{stop:()=>events.push('permission_track_stopped')}]};}}},sessionStorage:{getItem:()=>null,setItem(){}},AudioSession,Twilio:{Device},location:{hash:'#'+'a'.repeat(64),reload:()=>events.push('reload')},crypto:{randomUUID:()=> 'test-browser-instance'},document:{getElementById:id=>elements[id]},window:{addEventListener:(name,fn)=>{windowHandlers[name]=fn;}},setTimeout(){},AbortSignal,Map,Promise,Error,
    fetch:async(url,options)=>{const body=JSON.parse(options.body),action=url.split('/').at(-1);events.push(action);if(action==='failed')events.push(body.code);
      let value={};if(action==='token')value={token:'test-token'};
      if(action==='pulse')value=state;
      if(action==='incoming')value=denied?{error:'INBOUND_AUTHORIZATION_FAILED'}:{job_id:'job'};
      return {ok:!(denied&&action==='incoming'),json:async()=>value,body:{getReader:()=>({read:()=>new Promise(()=>{}),cancel:async()=>{}})}};}});
  vm.runInContext(source,context);await flush();await flush();
  const run=s=>vm.runInContext(s,context);
  function incoming(){const callbacks={};return {parameters:{CallSid:'child'},customParameters:new Map([['pin','123456']]),status:()=> 'pending',on:(name,fn)=>{callbacks[name]=fn;},accept(){events.push('accepted');},reject(){events.push('rejected');},disconnect(){events.push('disconnected');},callbacks};}
  return {elements,events,handlers,windowHandlers,sessions,run,incoming,device:Device.last,setState:x=>{state=x;},deny:()=>{denied=true;}};
}
test('startup registers without user clicks or microphone capture and renews its token',async()=>{
  const f=await fixture();assert.ok(f.events.includes('registered'));assert.equal(f.sessions.length,0);
  f.handlers.tokenWillExpire();await flush();assert.ok(f.events.includes('refreshed'));
  await assert.rejects(f.device.options.getUserMedia(),/AUDIO_NOT_READY/);
});
test('incoming is verified before capture or acceptance, then releases tracks before reporting hangup',async()=>{
  const f=await fixture(),call=f.incoming();f.handlers.incoming(call);await flush();await flush();
  assert.equal(f.sessions.length,0);assert.ok(!f.events.includes('accepted'));
  f.setState({job:{id:'job',prepared:true,child_sid:'child'}});await f.run('step()');
  assert.ok(f.events.indexOf('incoming')<f.events.indexOf('prepare'));
  assert.ok(f.events.indexOf('ready')<f.events.indexOf('accepted'));
  call.callbacks.accept();await flush();assert.ok(f.events.includes('connected'));
  await f.run('cleanup()');assert.ok(f.events.indexOf('audio_closed')<f.events.indexOf('ended'));
});
test('rejected incoming calls never open audio or answer',async()=>{
  const f=await fixture();f.deny();f.handlers.incoming(f.incoming());await flush();await flush();
  assert.equal(f.sessions.length,0);assert.ok(f.events.includes('rejected'));assert.ok(!f.events.includes('accepted'));
});
test('backend cancellation and restart release requests close browser audio',async()=>{
  const f=await fixture();f.setState({job:{id:'job',prepared:true}});await f.run('step()');
  f.setState({job:null,release_job:'job'});await f.run('step()');
  assert.equal(f.sessions[0].closed,true);assert.ok(f.events.includes('released'));
});

test('rotated backend session reloads the document instead of keeping stale credentials',async()=>{
  const f=await fixture();f.windowHandlers.hashchange();assert.ok(f.events.includes('reload'));
});

test('audio startup failures remain visible and are reported before cleanup',async()=>{
  const f=await fixture({prepareError:'MICROPHONE_PERMISSION_REQUIRED'});
  f.setState({job:{id:'job',prepared:true}});await f.run('step()');
  assert.ok(f.events.includes('MICROPHONE_PERMISSION_REQUIRED'));
  assert.ok(f.events.indexOf('failed')<f.events.indexOf('ended'));
  f.setState({job:null});await f.run('step()');
  assert.match(f.run('status.textContent'),/MICROPHONE_PERMISSION_REQUIRED/);
});

test('one-time microphone setup requests access and releases the temporary track without dialing',async()=>{
 const f=await fixture({permission:'prompt'});
 assert.equal(f.elements.microphone.hidden,false);
 await f.elements.microphone.onclick();
 assert.ok(f.events.includes('permission_requested'));assert.ok(f.events.includes('permission_track_stopped'));
 assert.equal(f.elements.microphone.hidden,true);assert.equal(f.sessions.length,0);assert.ok(!f.events.includes('ready'));
});
