import {test} from 'node:test';
import assert from 'node:assert/strict';
import {operate} from '../src/call-queue.mjs';
const owner='00000000-0000-0000-0000-000000000001';
const heartbeat=(state, now=1000,fields={})=>operate(state,'pulse',{instance:owner,ready:true,busy:false,audio_guard_version:1,...fields},now);
const start=(state,key='test-call-0001',now=1001)=>operate(state,'start',{request_key:key,duration_seconds:120},now);

test('offline Macs reject calls without leaving delayed jobs',()=>{
 const state={}; assert.equal(start(state).code,'MAC_COMPANION_OFFLINE');
 assert.equal(state.jobs.length,0); heartbeat(state); assert.equal(heartbeat(state).job,undefined);
});
test('one call at a time, idempotent retries and at-most-once delivery',()=>{
 const state={};heartbeat(state);
 const first=start(state);assert.equal(first.status,'queued');
 assert.equal(start(state).id,first.id);assert.equal(state.jobs.length,1);
 assert.equal(start(state,'another-call').code,'CALL_ALREADY_ACTIVE');
 const claimed=heartbeat(state,1002);assert.equal(claimed.job.id,first.id);
 assert.equal(heartbeat(state,1003).job,undefined);
 assert.equal(start(state).status,'starting');
});
test('expired queued request cannot dial when Mac reconnects',()=>{
 const state={};heartbeat(state);const first=start(state);
 assert.equal(heartbeat(state,50000).job,undefined);
 assert.equal(operate(state,'get',{call_id:first.id},50000).status,'expired');
 assert.equal(start(state,'test-call-0001',50001).status,'expired');
});
test('terminal state cannot reopen; stale and different companions cannot update',()=>{
 const state={};heartbeat(state);const first=start(state);heartbeat(state,1002);
 assert.equal(operate(state,'update',{instance:'wrong',call_id:first.id,status:'connected'},1003).accepted,false);
 assert.equal(operate(state,'update',{instance:owner,call_id:first.id,status:'completed'},1004).accepted,true);
 assert.equal(operate(state,'update',{instance:owner,call_id:first.id,status:'connected'},1005).accepted,false);
 assert.equal(operate(state,'get',{call_id:first.id},1006).status,'completed');
});
test('queued cancellation never dials; active cancellation reaches companion',()=>{
 const state={};heartbeat(state);const first=start(state);
 assert.equal(operate(state,'end',{call_id:first.id},1002).status,'canceled');
 assert.equal(heartbeat(state,1003).job,undefined);
 const next=start(state,'second-call',1004);heartbeat(state,1005);
 operate(state,'end',{call_id:next.id},1006);
 assert.equal(heartbeat(state,1007,{busy:true}).cancel_call_id,next.id);
});
test('end without an id cancels only the current owner call',()=>{
 const state={};heartbeat(state);const first=start(state);heartbeat(state,1002);
 assert.equal(operate(state,'end',{},1003).id,first.id);
 assert.equal(heartbeat(state,1004,{busy:true}).cancel_call_id,first.id);
 operate(state,'update',{instance:owner,call_id:first.id,status:'completed'},1005);
 assert.equal(operate(state,'end',{},1006).status,'not_found');
});
test('an explicit stale call id never ends a different call',()=>{
 const state={};heartbeat(state);const first=start(state);heartbeat(state,1002);
 assert.equal(operate(state,'end',{call_id:'stale-id'},1003).status,'not_found');
 assert.equal(state.jobs.find(j=>j.id===first.id).cancel_requested,false);
});
test('a second companion cannot steal the live lease',()=>{
 const state={};heartbeat(state);
 assert.equal(operate(state,'pulse',{instance:'another-companion',ready:true},1002).error,'ANOTHER_COMPANION_CONNECTED');
 assert.equal(state.mac.instance,owner);
});
test('stale heartbeat blocks new calls',()=>{
 const state={};heartbeat(state);assert.equal(start(state,'stale-call',23000).code,'MAC_COMPANION_OFFLINE');
});
test('an online companion without the audio gate cannot claim or start calls',()=>{
 const state={};heartbeat(state,1000,{audio_guard_version:0});
 assert.equal(start(state).code,'AUDIO_CHECK_UPDATE_REQUIRED');
 assert.equal(operate(state,'status',{},1002).can_place_native_dot_call,false);
});
test('online status separates request acceptance from verified audio',()=>{
 const state={};heartbeat(state);
 const status=operate(state,'status',{},1001);
 assert.equal(status.can_request_native_dot_call,true);
 assert.equal(status.can_place_native_dot_call,false);
 assert.equal(status.audio_preflight_required,true);
});
test('failed audio preflight exposes bounded challenge diagnostics without arbitrary transcript',()=>{
 const state={};heartbeat(state);const job=start(state);heartbeat(state,1002);
 const diagnostics={stage:'browser_audio_preflight',phone_dialed:false,
   expected_response:'dolphin river purple',heard_challenge_words:['dolphin'],
   reply_peak:.2,active_seconds:1.2,transcript:'private unrelated speech'};
 operate(state,'update',{instance:owner,call_id:job.id,status:'failed',code:'DOT_AUDIO_REPLY_MISMATCH',diagnostics},1003);
 const result=operate(state,'get',{call_id:job.id},1004);
 assert.equal(result.diagnostics.expected_response,'dolphin river purple');
 assert.deepEqual(result.diagnostics.heard_challenge_words,['dolphin']);
 assert.equal(result.diagnostics.phone_dialed,false);
 assert.equal(result.diagnostics.transcript,undefined);
});
test('malformed diagnostics and diagnostic data on non-audio failures are discarded',()=>{
 for(const code of ['DOT_AUDIO_REPLY_MISMATCH','PHONE_MEDIA_NOT_CONNECTED']) {
  const state={};heartbeat(state);const job=start(state);heartbeat(state,1002);
  operate(state,'update',{instance:owner,call_id:job.id,status:'failed',code,
   diagnostics:{stage:'browser_audio_preflight',phone_dialed:false,expected_response:'arbitrary personal text'}},1003);
  assert.equal(operate(state,'get',{call_id:job.id},1004).diagnostics,undefined);
 }
});
test('public bridge failure exposes bounded cause without URL or credentials',()=>{
 const state={};heartbeat(state);const job=start(state);heartbeat(state,1002);
 operate(state,'update',{instance:owner,call_id:job.id,status:'failed',code:'PUBLIC_BRIDGE_UNREACHABLE',diagnostics:{stage:'public_bridge_preflight',phone_dialed:false,reason:'dns_lookup_failed',attempts:39,elapsed_seconds:20,url:'https://private.test/probe/secret',token:'secret'}},1003);
 assert.deepEqual(operate(state,'get',{call_id:job.id},1004).diagnostics,{stage:'public_bridge_preflight',phone_dialed:false,reason:'dns_lookup_failed',attempts:39,elapsed_seconds:20});
});
test('malformed public bridge causes cannot leak arbitrary text',()=>{
 for(const extra of [{reason:'private arbitrary text'},{attempts:-1},{elapsed_seconds:Infinity},{http_status:900},{phone_dialed:true}]) {
  const state={};heartbeat(state);const job=start(state);heartbeat(state,1002);
  operate(state,'update',{instance:owner,call_id:job.id,status:'failed',code:'PUBLIC_BRIDGE_UNREACHABLE',diagnostics:{stage:'public_bridge_preflight',phone_dialed:false,reason:'http_error',attempts:2,elapsed_seconds:3,http_status:502,...extra}},1003);
  assert.equal(operate(state,'get',{call_id:job.id},1004).diagnostics,undefined);
 }
});
