import test from 'node:test';
import assert from 'node:assert/strict';
import {createHmac,createHash} from 'node:crypto';
import {inbound} from '../src/inbound.mjs';
import {operate} from '../src/call-queue.mjs';

const origin='http://127.0.0.1:8894';
const sid='CA'+'1'.repeat(32), other='CA'+'2'.repeat(32), instance='test-native-instance';
const stream='wss://test-example.trycloudflare.com/media/'+'a'.repeat(48);
function fixture() {
  const state={}, now=Date.now();
  operate(state,'pulse',{instance,ready:true,busy:false,audio_guard_version:1},now);
  const env={TWILIO_AUTH_TOKEN:'test-token',TWILIO_ACCOUNT_SID:'AC'+'0'.repeat(32),
    INBOUND_OWNER_NUMBER:'+15555550123',INBOUND_TWILIO_NUMBER:'+15555550456',INBOUND_PIN_SALT:'test-salt',
    INBOUND_PIN_HASH:createHash('sha256').update('test-salt:246810').digest('hex'),
    OWNER_CALLS:{getByName:()=>({perform:(action,input)=>operate(state,action,input,now+1)})}};
  async function post(path, extra={}, signed=true) {
    const params=new URLSearchParams({AccountSid:env.TWILIO_ACCOUNT_SID,From:env.INBOUND_OWNER_NUMBER,
      To:env.INBOUND_TWILIO_NUMBER,CallSid:sid,CallStatus:'in-progress',Direction:'inbound',...extra});
    const signature=createHmac('sha1',env.TWILIO_AUTH_TOKEN).update(origin+path+[...params.keys()].sort().map(k=>k+params.get(k)).join('')).digest('base64');
    const request=new Request(origin+path,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded',
      'X-Twilio-Signature':signed?signature:'invalid'},body:params});
    return inbound(request,env,req=>req.text());
  }
  return {state,now,env,post};
}

test('inbound PIN queues once, claims once and connects the existing call',async()=>{
  const {state,post,now}=fixture();
  assert.match(await (await post('/inbound/voice')).text(),/<Gather/);
  assert.equal(state.jobs.length,0);
  assert.match(await (await post('/inbound/pin',{Digits:'246810'})).text(),/<Play>/);
  assert.equal(state.jobs.length,1);
  const job=state.jobs[0];
  assert.equal(job.direction,'inbound');assert.equal(job.inbound_call_sid,sid);
  assert.match(await (await post('/inbound/pin',{Digits:'246810'})).text(),/<Play>/);
  assert.equal(state.jobs.length,1);
  assert.equal(operate(state,'pulse',{instance,ready:true,audio_guard_version:1},now+2).job.id,job.id);
  assert.equal(operate(state,'pulse',{instance,ready:true,audio_guard_version:1},now+3).job,undefined);
  assert.equal(operate(state,'inbound_ready',{instance,call_id:job.id,stream_url:stream},now+4).accepted,true);
  assert.match(await (await post('/inbound/wait')).text(),new RegExp('<Connect><Stream url="'+stream.replaceAll('.','\\.')+'"'));
  assert.equal(operate(state,'get',{call_id:job.id},now+5).stream_url,undefined);
  assert.equal(operate(state,'status',{},now+5).active_call.stream_url,undefined);
});

test('unsigned, wrong-account and wrong-caller requests never enqueue or reveal a PIN prompt',async()=>{
  const {state,post}=fixture();
  assert.equal((await post('/inbound/voice',{},false)).status,403);
  for(const fields of [{From:'+15555559999'},{To:'+15555559999'},{AccountSid:'AC'+'9'.repeat(32)}]) {
    assert.match(await (await post('/inbound/voice',fields)).text(),/<Reject/);
  }
  assert.equal(state.jobs.length,0);
});

test('three incorrect PINs lock this call, and PIN-less direct callbacks cannot queue',async()=>{
  const {state,post}=fixture();
  assert.match(await (await post('/inbound/pin',{Digits:'246810'})).text(),/Access denied/);
  await post('/inbound/voice');
  for(let n=0;n<3;n++) await post('/inbound/pin',{Digits:'999999'});
  assert.match(await (await post('/inbound/pin',{Digits:'246810'})).text(),/Access denied/);
  assert.equal(state.jobs.length,0);
  assert.ok(!JSON.stringify(state).includes('999999'));
  assert.ok(!JSON.stringify(state).includes('246810'));
});

test('PIN failures are rate limited across calls',()=>{
  const {state,now}=fixture();
  for(let i=0;i<10;i++) {
    const call='CA'+i.toString(16).padStart(32,'0');
    assert.equal(operate(state,'inbound_open',{inbound_call_sid:call},now).status,'pin_required');
    operate(state,'inbound_pin',{inbound_call_sid:call,pin_ok:false},now);
  }
  assert.equal(operate(state,'inbound_open',{inbound_call_sid:other},now+1).status,'denied');
});

test('offline and busy calls do not launch helpers',async()=>{
  const {state,post,now}=fixture();
  state.mac.ready=false;
  assert.match(await (await post('/inbound/voice')).text(),/Mac is offline/);
  state.mac.ready=true;
  operate(state,'start',{request_key:'outbound-busy',duration_seconds:120},now);
  assert.match(await (await post('/inbound/voice')).text(),/already on a call/);
  assert.equal(state.jobs.length,1);
});

test('hangup before pickup cancels the job and late retries cannot resurrect it',async()=>{
  const {state,post,now}=fixture();
  await post('/inbound/voice');await post('/inbound/pin',{Digits:'246810'});
  await post('/inbound/status',{CallStatus:'completed'});
  assert.equal(state.jobs[0].status,'canceled');
  assert.equal(operate(state,'pulse',{instance,ready:true,audio_guard_version:1},now+2).job,undefined);
  assert.match(await (await post('/inbound/pin',{Digits:'246810'})).text(),/Access denied/);
  assert.equal(state.jobs.length,1);
});

test('hangup during setup reaches the helper and prevents bridge publication',async()=>{
  const {state,post,now}=fixture();
  await post('/inbound/voice');await post('/inbound/pin',{Digits:'246810'});
  const job=operate(state,'pulse',{instance,ready:true,audio_guard_version:1},now+2).job;
  await post('/inbound/status',{CallStatus:'completed'});
  assert.equal(operate(state,'pulse',{instance,ready:false,busy:true,audio_guard_version:1},now+3).cancel_call_id,job.id);
  assert.equal(operate(state,'inbound_ready',{instance,call_id:job.id,stream_url:stream},now+4).accepted,false);
  assert.match(await (await post('/inbound/wait')).text(),/<Hangup/);
});

test('bridge publication requires owning helper and a fixed tunnel route',async()=>{
  const {state,post,now}=fixture();
  await post('/inbound/voice');await post('/inbound/pin',{Digits:'246810'});
  const job=operate(state,'pulse',{instance,ready:true,audio_guard_version:1},now+2).job;
  for(const fields of [{instance:'wrong',stream_url:stream},{instance,stream_url:'wss://evil.example/media/x'},
    {instance,stream_url:stream+'?token=secret'},{instance,stream_url:'https://test.trycloudflare.com/'}]) {
    assert.equal(operate(state,'inbound_ready',{call_id:job.id,...fields},now+3).accepted,false);
  }
});

test('waiting is bounded and cancels a claimed helper after 90 seconds',async()=>{
  const {state,post,now}=fixture();
  await post('/inbound/voice');await post('/inbound/pin',{Digits:'246810'});
  const job=operate(state,'pulse',{instance,ready:true,audio_guard_version:1},now+2).job;
  assert.equal(operate(state,'inbound_wait',{inbound_call_sid:sid},now+90002).status,'timeout');
  assert.equal(operate(state,'pulse',{instance,ready:false,busy:true},now+90003).cancel_call_id,job.id);
});

test('a completion arriving before voice cannot be undone by a delayed webhook',async()=>{
  const {state,post}=fixture();
  await post('/inbound/status',{CallStatus:'completed'});
  assert.match(await (await post('/inbound/voice')).text(),/Access denied/);
  assert.match(await (await post('/inbound/pin',{Digits:'246810'})).text(),/Access denied/);
  assert.equal(state.jobs.length,0);
});

test('forged completion cannot cancel an authenticated incoming call',async()=>{
  const {state,post}=fixture();
  await post('/inbound/voice');await post('/inbound/pin',{Digits:'246810'});
  assert.equal((await post('/inbound/status',{CallStatus:'completed'},false)).status,403);
  assert.equal(state.jobs[0].cancel_requested,false);
});
