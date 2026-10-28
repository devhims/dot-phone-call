import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash,randomUUID} from 'node:crypto';
import {RegisteredCalls,accessToken,incomingTwiml,twilioProvider} from '../src/registered-calls.mjs';
import {openLocalQueue} from '../src/local-queue.mjs';
const ACCOUNT='AC'+'1'.repeat(32),PARENT='CA'+'2'.repeat(32),CHILD='CA'+'3'.repeat(32);
const config={TWILIO_ACCOUNT_SID:ACCOUNT,TWILIO_AUTH_TOKEN:'test-only',TWILIO_API_KEY_SID:'SK'+'4'.repeat(32),TWILIO_API_KEY_SECRET:'test-key',CLIENT_IDENTITY:'dot_phone_test',INBOUND_OWNER_NUMBER:'+15555550101',INBOUND_TWILIO_NUMBER:'+15555550102',INBOUND_PIN_SALT:'salt',INBOUND_PIN_HASH:createHash('sha256').update('salt:123456').digest('hex')};
async function fixture(t,{allow=true,enabled=true,createError=false,log}={}) {
  const queue=openLocalQueue(':memory:');t.after(()=>queue.close());
  let now=Date.now();const calls=new Map(),created=[],ended=[];
  const provider={get:async id=>{assert.ok(calls.has(id));return {...calls.get(id)};},create:async options=>{
    created.push(options);if(createError)throw Error('NETWORK_TIMEOUT');
    calls.set(PARENT,{sid:PARENT,account_sid:ACCOUNT,from:config.INBOUND_TWILIO_NUMBER,to:config.INBOUND_OWNER_NUMBER,status:'ringing',direction:'outbound-api'});return {sid:PARENT};
  },end:async id=>{ended.push(id);calls.get(id).status='completed';}};
  const gateway=new RegisteredCalls({queue,config,provider,enabled,allowPhoneDialing:allow,now:()=>now,log});
  const client=randomUUID(),instance=randomUUID();
  const browser=(action,input={})=>gateway.browser(action,{client,...input});
  await browser('token');await browser('pulse',{registered:true});
  queue.perform('pulse',{instance,ready:true,audio_guard_version:1});
  const helper=(action,job,input={})=>gateway.helper(action,{call_id:job.id,instance,...input});
  function start(){const requested=queue.perform('start',{request_key:randomUUID(),duration_seconds:60});assert.equal(requested.status,'queued');return queue.perform('pulse',{instance,ready:true,audio_guard_version:1}).job;}
  function incomingRecords(){calls.set(PARENT,{sid:PARENT,account_sid:ACCOUNT,from:config.INBOUND_OWNER_NUMBER,to:config.INBOUND_TWILIO_NUMBER,status:'in-progress',direction:'inbound'});child();}
  function child(){calls.set(CHILD,{sid:CHILD,account_sid:ACCOUNT,to:'client:'+config.CLIENT_IDENTITY,parent_call_sid:PARENT,status:'in-progress'});}
  return {queue,gateway,browser,helper,start,incomingRecords,child,calls,created,ended,provider,client,instance,advance:ms=>{now+=ms;}};
}
test('outgoing waits for helper and browser audio, dials once, then confirms audio release before finishing',async t=>{
  const f=await fixture(t),job=f.start();
  await assert.rejects(f.browser('ready',{job_id:job.id}),/CALL_NOT_OWNED/);
  await f.helper('prepare',job,{audio_capability:'a'.repeat(43)});
  assert.equal(f.created.length,0);
  await f.browser('ready',{job_id:job.id});await f.browser('ready',{job_id:job.id});
  assert.equal(f.created.length,1);assert.equal(f.created[0].To,config.INBOUND_OWNER_NUMBER);
  assert.match(f.created[0].Twiml,/<Client>dot_phone_test<\/Client>/);assert.ok(!f.created[0].Twiml.includes('http'));
  f.child();await f.browser('incoming',{sid:CHILD});
  await f.browser('connected',{job_id:job.id,sid:CHILD});
  assert.equal((await f.helper('state',job)).connected,true);
  assert.equal((await f.gateway.finish(job.id)).ended,false);
  assert.deepEqual(f.ended,[PARENT]);
  assert.equal((await f.browser('pulse',{registered:true})).job,null);
  await f.browser('released',{job_id:job.id});
  assert.equal((await f.gateway.finish(job.id)).ended,true);assert.equal(f.queue.readRegistered(),null);
});
test('incoming verifies provider parent, PIN and ownership, queues once and never creates an outbound call',async t=>{
  const f=await fixture(t);f.incomingRecords();
  const accepted=await f.browser('incoming',{sid:CHILD,pin:'123456'});
  const again=await f.browser('incoming',{sid:CHILD,pin:'123456'});assert.equal(again.job_id,accepted.job_id);
  const job=f.queue.perform('pulse',{instance:f.instance,ready:true,audio_guard_version:1}).job;
  assert.equal(job.direction,'inbound');assert.equal(job.inbound_call_sid,PARENT);
  await f.helper('prepare',job,{audio_capability:'a'.repeat(43)});
  await f.browser('ready',{job_id:job.id});assert.equal(f.created.length,0);
  await f.browser('connected',{job_id:job.id,sid:CHILD});assert.equal((await f.helper('state',job)).connected,true);
  await f.browser('ended',{job_id:job.id});assert.deepEqual(f.ended,[PARENT]);assert.equal(f.gateway.record,null);
});
test('forged SID relationships and caller identities cannot queue or terminate unrelated calls',async t=>{
  for(const mutation of [p=>p.account_sid='AC'+'9'.repeat(32),p=>p.from='+15555559999',p=>p.to='+15555559999',p=>p.direction='outbound-api']) {
    const f=await fixture(t);f.incomingRecords();mutation(f.calls.get(PARENT));
    await assert.rejects(f.browser('incoming',{sid:CHILD,pin:'123456'}),/CALL_IDENTITY_MISMATCH/);
    assert.equal(f.queue.perform('status').active_call,null);assert.equal(f.ended.length,0);
  }
});
test('incorrect PINs are rate limited and never persisted in the call journal',async t=>{
  const f=await fixture(t);f.incomingRecords();
  for(let i=0;i<3;i++)await assert.rejects(f.browser('incoming',{sid:CHILD,pin:'999999'}),/INBOUND_AUTHORIZATION_FAILED/);
  await assert.rejects(f.browser('incoming',{sid:CHILD,pin:'123456'}),/INBOUND_NOT_AVAILABLE/);
  assert.equal(f.queue.readRegistered(),null);assert.equal(f.created.length,0);
});
test('no-phone configuration blocks provider creation even with working audio',async t=>{
  const f=await fixture(t,{allow:false}),job=f.start();await f.helper('prepare',job,{audio_capability:'a'.repeat(43)});
  await assert.rejects(f.browser('ready',{job_id:job.id}),/PHONE_DIALING_DISABLED/);assert.equal(f.created.length,0);
});
test('uncertain creation is durable and is never automatically retried',async t=>{
  const f=await fixture(t,{createError:true}),job=f.start();await f.helper('prepare',job,{audio_capability:'a'.repeat(43)});
  await assert.rejects(f.browser('ready',{job_id:job.id}),/NETWORK_TIMEOUT/);
  await assert.rejects(f.browser('ready',{job_id:job.id}),/CALL_CREATION_OUTCOME_UNKNOWN/);
  assert.equal(f.created.length,1);assert.equal(f.queue.readRegistered().unknown,true);
  const recovered=new RegisteredCalls({queue:f.queue,config,provider:f.provider,enabled:true});
  await assert.rejects(recovered.monitor(),/CALL_CREATION_OUTCOME_UNKNOWN/);assert.equal(recovered.status().recovery_required,true);
});
test('browser loss, provider hangup and tool cancellation all stop the owned call',async t=>{
  for(const reason of ['browser','provider','tool']) {
    const f=await fixture(t),job=f.start();await f.helper('prepare',job,{audio_capability:'a'.repeat(43)});await f.browser('ready',{job_id:job.id});
    if(reason==='browser')f.advance(13000);
    if(reason==='provider'){f.calls.get(PARENT).status='completed';f.advance(4000);}
    if(reason==='tool')f.queue.perform('end',{call_id:job.id});
    await f.gateway.monitor();assert.equal(f.gateway.record.ended,true);
    assert.equal((await f.helper('state',job)).ended,true);
    await f.browser('released',{job_id:job.id});assert.equal(f.gateway.record,null);
  }
});
test('startup recovery closes a known owned call and waits for browser release',async t=>{
  const f=await fixture(t),job=f.start();await f.helper('prepare',job,{audio_capability:'a'.repeat(43)});await f.browser('ready',{job_id:job.id});
  const recovered=new RegisteredCalls({queue:f.queue,config,provider:f.provider,enabled:true});
  await recovered.monitor();assert.deepEqual(f.ended,[PARENT]);assert.equal(recovered.record.ended,true);
  assert.equal(recovered.status().recovery_required,true);
});
test('a second browser cannot steal a fresh registration lease',async t=>{
  const f=await fixture(t);await assert.rejects(f.gateway.browser('token',{client:randomUUID()}),/ANOTHER_BROWSER_REGISTERED/);
  f.advance(13000);assert.ok((await f.gateway.browser('token',{client:randomUUID()})).token);
});
test('token refresh grants only incoming access and static route forwards PIN for local verification',()=>{
  const token=accessToken(config,1700000000000),data=JSON.parse(Buffer.from(token.split('.')[1],'base64url'));
  assert.deepEqual(data.grants,{identity:'dot_phone_test',voice:{incoming:{allow:true}}});assert.equal(data.exp-data.iat,3600);
  assert.notEqual(token,accessToken(config,1700000000000));
  const xml=incomingTwiml(config.CLIENT_IDENTITY,'https://handler.twilio.com/twiml/EH'+'8'.repeat(32));
  assert.match(xml,/numDigits="6"/);assert.match(xml,/value="{{.}}"/);assert.match(xml,/{{\^Digits}}/);assert.ok(!xml.includes('123456'));
});
test('provider adapter uses bounded authenticated requests without redirects',async()=>{
  const seen=[];const provider=twilioProvider(config,async(url,options)=>{seen.push({url,options});return {ok:true,json:async()=>({sid:PARENT})};});
  await provider.create({To:config.INBOUND_OWNER_NUMBER});await provider.get(PARENT);await provider.end(PARENT);
  assert.deepEqual(seen.map(s=>s.options.method),['POST','GET','POST']);assert.ok(seen.every(s=>s.options.redirect==='error'&&s.options.signal));
});

test('browser startup failure remains failed after helper cleanup',async t=>{
 const f=await fixture(t),job=f.start();await f.helper('prepare',job,{audio_capability:'a'.repeat(43)});
 await f.browser('failed',{job_id:job.id,code:'MICROPHONE_PERMISSION_REQUIRED'});
 await f.browser('ended',{job_id:job.id});
 f.queue.perform('update',{instance:f.instance,call_id:job.id,status:'completed'});
 const result=f.queue.perform('get',{call_id:job.id});
 assert.equal(result.status,'failed');assert.equal(result.code,'MICROPHONE_PERMISSION_REQUIRED');assert.equal(f.created.length,0);
});

test('native busy heartbeats keep an answered call alive while Dot startup takes over 15 seconds',async t=>{
 const {createLocalBackend}=await import('../src/local-backend.mjs');
 const f=await fixture(t),job=f.start();await f.helper('prepare',job,{audio_capability:'a'.repeat(43)});
 await f.browser('ready',{job_id:job.id});f.child();await f.browser('incoming',{sid:CHILD});await f.browser('connected',{job_id:job.id,sid:CHILD});
 const token='c'.repeat(48);
 const {privateServer:server}=createLocalBackend({queue:f.queue,registered:f.gateway,mcpToken:'m'.repeat(48),companionToken:token,extensionId:'a'.repeat(32),twilio:config,callingEnabled:true});
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));t.after(()=>{server.close();server.closeAllConnections();});
 for(let n=0;n<12;n++){
   f.advance(2000);
   const response=await fetch('http://127.0.0.1:'+server.address().port+'/companion/pulse',{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify({instance:f.instance,ready:false,busy:true,audio_guard_version:1})});
   assert.equal(response.status,200);await f.browser('pulse',{registered:true});await f.gateway.monitor();
 }
 assert.equal(f.queue.perform('get',{call_id:job.id}).cancel_requested,false);
 assert.equal(f.ended.length,0);
 // A different helper instance must not keep this call alive.
 for(let n=0;n<9;n++){
   f.advance(2000);
   await fetch('http://127.0.0.1:'+server.address().port+'/companion/pulse',{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify({instance:'wrong-helper-instance',ready:false,busy:true,audio_guard_version:1})});
   await f.browser('pulse',{registered:true});
 }
 assert.equal(f.queue.perform('get',{call_id:job.id}).cancel_requested,true);
});

test('only the paired hidden extension can use authenticated bridge requests',async t=>{
 const {createLocalBackend}=await import('../src/local-backend.mjs');const f=await fixture(t);
 const extension='a'.repeat(32),origin='chrome-extension://'+extension;
 const {privateServer:server}=createLocalBackend({queue:f.queue,registered:f.gateway,mcpToken:'m'.repeat(48),companionToken:'c'.repeat(48),extensionId:extension,twilio:config,callingEnabled:true});
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));t.after(()=>{server.close();server.closeAllConnections();});
 const url='http://127.0.0.1:'+server.address().port+'/bridge/pulse';
 const post=(Origin,token=f.gateway.capability)=>fetch(url,{method:'POST',headers:{Origin,Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify({client:f.client,registered:true,context:'offscreen',microphone_ready:false})});
 const good=await post(origin);assert.equal(good.status,200);assert.equal(good.headers.get('access-control-allow-origin'),origin);
 assert.equal(f.gateway.status().browser_context,'offscreen');assert.equal(f.gateway.status().microphone_ready,false);
 assert.equal((await post('chrome-extension://'+'b'.repeat(32))).status,403);
 assert.equal((await post(origin,'wrong')).status,403);
 assert.equal((await post('https://evil.example')).status,403);
});

test('the hidden page learns a call is prepared without any audio capability',async t=>{
  const f=await fixture(t),job=f.start();
  assert.equal((await f.browser('pulse',{registered:true})).job,null);
  await f.helper('prepare',job);
  const pulsed=(await f.browser('pulse',{registered:true})).job;
  assert.equal(pulsed.id,job.id);assert.equal(pulsed.prepared,true);
  assert.equal('audio_capability' in pulsed,false);
});
test('the call log records why each call ended, and never the PIN or phone numbers',async t=>{
  const cases={browser:'twilio_client_lost',provider:'phone_completed',tool:'end_requested',heartbeat:'extension_unresponsive'};
  for(const [cause,reason] of Object.entries(cases)) {
    const lines=[],f=await fixture(t,{log:(src,ev,fields)=>lines.push({src,ev,...fields})}),job=f.start();
    await f.helper('prepare',job,{audio_capability:'a'.repeat(43)});await f.browser('ready',{job_id:job.id});
    if(cause==='browser')f.advance(13000);
    if(cause==='provider'){f.calls.get(PARENT).status='completed';f.advance(4000);}
    if(cause==='tool')f.queue.perform('end',{call_id:job.id});
    if(cause==='heartbeat'){f.advance(10000);await f.browser('pulse',{registered:true});f.advance(6000);await f.browser('pulse',{registered:true});}
    await f.gateway.monitor();await f.gateway.monitor();
    const endings=lines.filter(l=>l.ev==='call_ending');
    assert.deepEqual(endings.map(l=>l.reason),[reason],cause);
    assert.equal(endings[0].call,job.id);
  }
  const lines=[],f=await fixture(t,{log:(src,ev,fields)=>lines.push({src,ev,...fields})});f.incomingRecords();
  await f.browser('incoming',{sid:CHILD,pin:'123456'});
  assert.deepEqual(lines.filter(l=>l.ev.startsWith('incoming')||l.ev==='pin_checked').map(l=>l.ev),['incoming_ringing','pin_checked','incoming_authorized']);
  assert.equal(lines.find(l=>l.ev==='pin_checked').ok,true);
  const text=JSON.stringify(lines);
  for(const secret of ['123456',config.INBOUND_OWNER_NUMBER,config.INBOUND_TWILIO_NUMBER,config.TWILIO_AUTH_TOKEN])assert.ok(!text.includes(secret),secret);
});
