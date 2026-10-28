import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,readFile,readdir,rm,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {createCallLog,localTime} from '../src/call-log.mjs';
import {contextEvents,diagnose,groupCalls,parseLog} from '../src/call-timeline.mjs';
import {openLocalQueue} from '../src/local-queue.mjs';
import {createLocalBackend} from '../src/local-backend.mjs';

async function logDir(t) {
  const dir=await mkdtemp(join(tmpdir(),'dot-call-log-'));
  t.after(()=>rm(dir,{recursive:true,force:true}));
  return dir;
}
const read=async dir=>parseLog((await Promise.all((await readdir(dir)).map(f=>readFile(join(dir,f),'utf8')))).join('\n'));

test('the call log keeps flat primitive fields only and prunes old days', async t => {
  const dir=await logDir(t),now=Date.parse('2026-10-28T14:42:38+05:30');
  await writeFile(join(dir,'calls-2026-10-01.jsonl'),'{}\n');
  await writeFile(join(dir,'calls-2026-10-27.jsonl'),'');
  const log=createCallLog({dir,now:()=>now});
  log.event('backend','call_ending',{call:'job-1',reason:'phone_completed',nested:{pin:'123456'},list:[1],Bad:1,t:'spoofed',long:'x'.repeat(500),n:1.23456,nan:NaN});
  assert.deepEqual((await readdir(dir)).sort(),['calls-2026-10-27.jsonl',`calls-${localTime(now).slice(0,10)}.jsonl`].sort());
  const [line]=await read(dir);
  assert.equal(line.ev,'call_ending');assert.equal(line.src,'backend');assert.equal(line.t,localTime(now));
  assert.equal(line.reason,'phone_completed');assert.equal(line.n,1.23);assert.equal(line.long.length,200);
  for(const key of ['nested','list','Bad','nan'])assert.equal(key in line,false,key);
});

test('extension and bridge batches are validated and keep their own timestamps', async t => {
  const dir=await logDir(t),now=Date.now(),log=createCallLog({dir,now:()=>now});
  assert.equal(log.events('bridge',[{ev:'call_accepted',at:now-1500,child:'CA1'},{ev:'audio',at:now-10*86400000,seconds:5}]),2);
  assert.equal(log.events('bridge',[{ev:'Bad Name'}]),0);
  assert.equal(log.events('bridge',[]),0);
  assert.equal(log.events('bridge','not a list'),0);
  const lines=await read(dir);
  assert.equal(lines.length,2);
  assert.equal(lines[0].at,now-1500);
  assert.equal(lines[1].at,now,'a clearly wrong client clock falls back to the backend time');
});

test('the backend accepts log batches only from the paired extension and bridge', async t => {
  const dir=await logDir(t),queue=openLocalQueue(':memory:'),log=createCallLog({dir});
  const servers=createLocalBackend({queue,mcpToken:'m'.repeat(48),companionToken:'c'.repeat(48),extensionId:'a'.repeat(32),
    publicOrigin:'https://phone.example.com',twilio:{},readStatus:async()=>({}),callLog:log});
  await new Promise(resolve=>servers.privateServer.listen(0,'127.0.0.1',resolve));
  t.after(()=>{servers.privateServer.close();servers.privateServer.closeAllConnections();queue.close();});
  const origin='http://127.0.0.1:'+servers.privateServer.address().port;
  const post=(body,token='c'.repeat(48))=>fetch(origin+'/companion/log',{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify(body)});
  const events=[{ev:'dot_start_requested',at:Date.now(),call:randomUUID()}];
  assert.equal((await post({instance:'extension-instance-0001',events})).status,200);
  assert.equal((await post({instance:'extension-instance-0001',events},'x'.repeat(48))).status,401);
  assert.equal((await post({events})).status,400);
  const lines=await read(dir);
  assert.deepEqual(lines.map(l=>[l.src,l.ev]),[['extension','dot_start_requested']]);
});

const T0=Date.parse('2026-10-28T14:42:19+05:30');
const at=(s,src,ev,fields={})=>JSON.stringify({t:localTime(T0+s*1000),src,ev,...fields});
const JOB='b5450315-ce75-47d7-a857-4f42945c1000',CHILD='CA'+'3'.repeat(32),PARENT='CA'+'2'.repeat(32);

test('a call Dot never answered is explained, including a Dot tab that had just loaded', () => {
  // The failed incoming call from 2026-10-28, as the new log would record it.
  const events=parseLog([
    at(0,'backend','backend_started',{calling_enabled:true}),
    at(9,'bridge','incoming_offered',{child:CHILD}),
    at(9.2,'backend','incoming_ringing',{child:CHILD}),
    at(9.6,'backend','pin_checked',{ok:true,parent:PARENT}),
    at(9.7,'backend','incoming_authorized',{call:JOB,child:CHILD,parent:PARENT}),
    at(10,'extension','call_job_received',{call:JOB,direction:'inbound'}),
    at(16,'extension','dot_tab_prepared',{call:JOB,tab_created:true,tab_reloaded:false,page_age_ms:4100}),
    at(17,'bridge','call_accepted',{call:JOB,child:CHILD}),
    at(19,'extension','dot_start_requested',{call:JOB}),
    at(33,'extension','call_failed',{call:JOB,code:'DOT_VOICE_ENDED_BEFORE_CONNECT',page_age_ms:7200,active_after_ms:1800,waited_ms:14200}),
    at(34,'backend','call_ending',{call:JOB,reason:'extension_finished'}),
    at(35,'backend','phone_leg_ended',{call:JOB,status:'completed',duration_s:34}),
  ].join('\n'));
  const calls=groupCalls(events);
  assert.deepEqual([...calls.keys()],[JOB]);
  const list=calls.get(JOB);
  assert.equal(list[0].ev,'incoming_offered','bridge events before authorization join the call through its SID');
  const d=diagnose(list,contextEvents(events,list));
  assert.equal(d.incoming,true);assert.equal(d.answered,false);
  assert.equal(d.outcome,'Dot did not answer.');
  const text=d.findings.join('\n');
  assert.match(text,/opened and then closed before it connected/);
  assert.match(text,/loaded only 7 s before Start call/);
  assert.match(text,/opened one for this call/);
  assert.match(text,/backend started/);
});

test('an answered call reports how it ended and where Dot went silent', () => {
  const audio=(s,caller,dot,sent)=>at(s,'bridge','audio',{call:JOB,seconds:5,caller_peak_db:caller,caller_active_s:caller>-45?3:0,
    dot_peak_db:dot,dot_active_s:dot>-45?3:0,sent_peak_db:sent,sent_active_s:sent>-45?3:0,rtt_ms:180,jitter_ms:4,loss_pct:0,mos_min:4.3,audio_context:'running'});
  const events=parseLog([
    at(0,'backend','dot_tool_start_call',{call:JOB,status:'queued',time_limit_s:120}),
    at(1,'extension','call_job_received',{call:JOB,direction:'outbound'}),
    at(30,'extension','dot_connected',{call:JOB}),
    audio(35,-20,-18,-14),audio(40,-60,-15,-12),
    audio(45,-22,-80,-90),audio(50,-25,-80,-90),audio(55,-21,-80,-90),audio(60,-30,-80,-90),
    at(64,'extension','call_loop_ended',{call:JOB,reason:'dot_hung_up'}),
    at(64.5,'backend','dot_tool_end_call',{call:JOB}),
    at(65,'backend','call_ending',{call:JOB,reason:'extension_finished'}),
  ].join('\n'));
  const d=diagnose(groupCalls(events).get(JOB));
  assert.equal(d.answered,true);
  assert.match(d.outcome,/answered after 30 s and the call lasted 35 s/);
  const text=d.findings.join('\n');
  assert.match(text,/How it ended: Dot used its hang-up tool/);
  assert.match(text,/Dot made no sound from 0:40 to 1:00 while you were speaking/);
  assert.doesNotMatch(text,/No sound from your phone/);
});

test('ending reasons from the backend read as plain language', () => {
  for(const [reason,expected] of [['twilio_client_lost',/stopped reporting as registered/],['extension_unresponsive',/no heartbeat for 15 seconds/],
    ['phone_completed',/you hung up, or the phone network dropped/],['time_limit',/time limit/]]) {
    const events=parseLog([at(0,'extension','dot_connected',{call:JOB}),at(20,'backend','call_ending',{call:JOB,reason})].join('\n'));
    assert.match(diagnose(groupCalls(events).get(JOB)).findings[0],expected,reason);
  }
});

test('the two live calls of 28 October read correctly', () => {
  // Shapes taken from the first logged live calls: Dot calling the owner, then the owner calling Dot.
  const outgoing=parseLog([
    at(0,'backend','dot_tool_start_call',{call:JOB,status:'queued',time_limit_s:120}),
    at(4,'extension','call_job_received',{call:JOB,direction:'outbound'}),
    at(4.7,'backend','dialing_owner',{call:JOB,time_limit_s:120}),
    at(10,'bridge','incoming_offered',{child:CHILD}),
    at(10,'backend','incoming_ringing',{child:CHILD}),
    at(11.6,'backend','incoming_authorized',{call:JOB,child:CHILD,parent:PARENT}),
    at(14.8,'extension','dot_connected',{call:JOB}),
    at(14.8,'extension','dot_state',{call:JOB,microphone_track:'live',suspended_contexts:1}),
    at(30,'bridge','audio',{call:JOB,seconds:5,caller_peak_db:-13,caller_active_s:5,dot_peak_db:-90,dot_active_s:0,sent_peak_db:-43,sent_active_s:1}),
    at(35,'bridge','audio',{call:JOB,seconds:5,caller_peak_db:-14,caller_active_s:5,dot_peak_db:-90,dot_active_s:0,sent_peak_db:-44,sent_active_s:1}),
    at(40,'bridge','audio',{call:JOB,seconds:5,caller_peak_db:-14,caller_active_s:5,dot_peak_db:-90,dot_active_s:0,sent_peak_db:-46,sent_active_s:0}),
    at(74.7,'bridge','call_disconnected',{call:JOB,child:CHILD,connected_s:62}),
    at(74.7,'backend','call_ending',{call:JOB,reason:'twilio_client_disconnected'}),
    at(75.6,'extension','call_loop_ended',{call:JOB,reason:'end_requested'}),
  ].join('\n'));
  const d=diagnose(groupCalls(outgoing).get(JOB));
  assert.equal(d.incoming,false,'a call Dot placed is outgoing even though Chrome is rung as a Twilio leg');
  const text=d.findings.join('\n');
  assert.match(text,/How it ended: The call ended from the phone side/);
  assert.doesNotMatch(text,/suspended/);
  assert.match(text,/Dot made no sound from 0:25 to 0:40/,'hiss in the sent audio does not hide a silent Dot');
  const dropped=parseLog([at(0,'extension','dot_connected',{call:JOB}),at(5,'bridge','call_reconnecting',{call:JOB,code:53405}),
    at(9,'backend','call_ending',{call:JOB,reason:'twilio_client_disconnected'})].join('\n'));
  assert.match(diagnose(groupCalls(dropped).get(JOB)).findings[0],/connection to Twilio dropped/);
});
