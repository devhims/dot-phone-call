import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash,createHmac,randomUUID} from 'node:crypto';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {request as httpRequest} from 'node:http';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StreamableHTTPClientTransport} from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import {openLocalQueue} from '../src/local-queue.mjs';
import {createLocalBackend,twilioStatusReader} from '../src/local-backend.mjs';

const mcpToken='m'.repeat(48),companionToken='c'.repeat(48),extensionId='a'.repeat(32);
const publicOrigin='https://phone.example.com',instance='test-native-instance';
const twilio={TWILIO_AUTH_TOKEN:'test-twilio-token',TWILIO_ACCOUNT_SID:'AC'+'0'.repeat(32),
  INBOUND_OWNER_NUMBER:'+15555550123',INBOUND_TWILIO_NUMBER:'+15555550456',
  INBOUND_PIN_SALT:'salt',INBOUND_PIN_HASH:createHash('sha256').update('salt:246810').digest('hex')};
async function fixture(t,callingEnabled=true) {
  const queue=openLocalQueue(':memory:');
  const servers=createLocalBackend({queue,mcpToken,companionToken,extensionId,publicOrigin,twilio,callingEnabled,
    readStatus:async()=>({configured:true,reachable:true,calls_enabled:true})});
  await Promise.all(Object.values(servers).map(s=>new Promise(resolve=>s.listen(0,'127.0.0.1',resolve))));
  t.after(async()=>{await Promise.all(Object.values(servers).map(s=>new Promise(resolve=>{s.close(resolve);s.closeAllConnections();})));queue.close();});
  const origin='http://127.0.0.1:'+servers.privateServer.address().port;
  const external='http://127.0.0.1:'+servers.publicServer.address().port;
  const post=(path,data,token=companionToken,headers={})=>fetch(origin+path,{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json',...headers},body:JSON.stringify(data)});
  const pulse=extra=>post('/companion/pulse',{instance,ready:true,busy:false,audio_guard_version:1,...extra});
  const client=new Client({name:'local-migration-test',version:'1'});
  await client.connect(new StreamableHTTPClientTransport(new URL(origin+'/mcp'),{requestInit:{headers:{Authorization:'Bearer '+mcpToken}}}));
  t.after(()=>client.close());
  const call=async(name,args={})=>(await client.callTool({name,arguments:args})).structuredContent;
  async function webhook(path,extra={},signed=true) {
    const params=new URLSearchParams({AccountSid:twilio.TWILIO_ACCOUNT_SID,From:twilio.INBOUND_OWNER_NUMBER,
      To:twilio.INBOUND_TWILIO_NUMBER,CallSid:'CA'+'1'.repeat(32),CallStatus:'in-progress',Direction:'inbound',...extra});
    const sig=createHmac('sha1',twilio.TWILIO_AUTH_TOKEN).update(publicOrigin+path+[...params.keys()].sort().map(k=>k+params.get(k)).join('')).digest('base64');
    return fetch(external+path,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded','X-Twilio-Signature':signed?sig:'bad'},body:params});
  }
  return {queue,client,post,pulse,call,webhook,origin,external};
}
test('local MCP lifecycle persists one claim, deduplicates starts and cancels through existing controls',async t=>{
  const {client,pulse,call}=await fixture(t);
  assert.equal((await client.listTools()).tools.length,5);
  await pulse();
  assert.equal((await call('get_phone_service_status')).backend,'local');
  assert.equal((await call('prepare_owner_call',{request_key:'preflight-only'})).call_placed,false);
  assert.equal((await pulse()).status,200);
  const first=await call('start_owner_call',{request_key:'isolated-mock-call'});
  assert.equal(first.status,'queued');
  assert.equal((await call('start_owner_call',{request_key:'isolated-mock-call'})).id,first.id);
  assert.equal((await (await pulse()).json()).job.id,first.id);
  assert.equal((await (await pulse({busy:true})).json()).job,undefined);
  assert.equal((await call('end_owner_call',{call_id:randomUUID()})).status,'not_found');
  assert.equal((await call('get_owner_call',{call_id:first.id})).cancel_requested,false);
  assert.equal((await call('end_owner_call')).cancel_requested,true);
  assert.equal((await (await pulse({busy:true})).json()).cancel_call_id,first.id);
});
test('migration gate blocks outbound and inbound even with a ready helper',async t=>{
  const {pulse,call,webhook}=await fixture(t,false);
  await pulse();
  assert.equal((await call('get_phone_service_status')).can_request_native_dot_call,false);
  assert.equal((await call('start_owner_call',{request_key:'must-never-enqueue'})).code,'LOCAL_CALLING_DISABLED');
  assert.match(await (await webhook('/inbound/voice')).text(),/offline/);
  assert.match(await (await webhook('/inbound/pin',{Digits:'246810'})).text(),/offline/);
  assert.equal((await (await pulse()).json()).job,undefined);
});
test('public listener cannot expose private controls; role credentials, origins and input are enforced',async t=>{
  const {post,origin,external,client}=await fixture(t);
  for(const path of ['/mcp','/companion/pulse']) assert.equal((await fetch(external+path)).status,404);
  assert.equal((await post('/mcp',{},companionToken)).status,401);
  assert.equal((await post('/companion/pulse',{},mcpToken)).status,401);
  for(const origin of ['https://evil.test','null','chrome-extension://'+'b'.repeat(32)])
    assert.equal((await post('/companion/pulse',{},companionToken,{Origin:origin})).status,403);
  assert.equal((await post('/companion/pulse',{instance,ready:true},companionToken,{Origin:'chrome-extension://'+extensionId})).status,200);
  assert.equal((await post('/companion/start',{request_key:'never-queue'})).status,404);
  assert.equal((await post('/companion/pulse',{instance,ready:true,unknown:1})).status,400);
  assert.equal((await post('/companion/pulse',{padding:'x'.repeat(9000)})).status,400);
  const hostRejected=await new Promise((resolve,reject)=>{
    const req=httpRequest(origin+'/mcp',{method:'POST',headers:{Host:'evil.test'}},res=>{res.resume();resolve(res.statusCode);});
    req.on('error',reject);req.end();
  });
  assert.equal(hostRejected,403);
  const invalid=await client.callTool({name:'start_owner_call',arguments:{request_key:'bad',duration_seconds:9999}});
  assert.equal(invalid.isError,true);
});
test('local inbound validates signatures and PIN, never dials, and publishes only an owned media route',async t=>{
  const {pulse,webhook,call,post}=await fixture(t);
  await pulse();
  assert.equal((await webhook('/inbound/voice',{},false)).status,403);
  assert.match(await (await webhook('/inbound/voice',{From:'+15555559999'})).text(),/Reject/);
  assert.match(await (await webhook('/inbound/voice')).text(),/Gather/);
  assert.match(await (await webhook('/inbound/pin',{Digits:'246810'})).text(),/Play/);
  assert.match(await (await webhook('/inbound/pin',{Digits:'246810'})).text(),/Play/);
  const job=(await (await pulse()).json()).job;
  assert.equal(job.direction,'inbound');
  const stream='wss://test-media.trycloudflare.com/media/'+'a'.repeat(48);
  assert.equal((await (await post('/companion/inbound-ready',{instance:'wrong-helper-instance',call_id:job.id,stream_url:stream})).json()).accepted,false);
  assert.equal((await (await post('/companion/inbound-ready',{instance,call_id:job.id,stream_url:stream})).json()).accepted,true);
  assert.match(await (await webhook('/inbound/wait')).text(),/Connect/);
  assert.equal((await call('get_owner_call',{call_id:job.id})).stream_url,undefined);
  await webhook('/inbound/status',{CallStatus:'completed'});
  assert.equal((await (await pulse({busy:true})).json()).cancel_call_id,job.id);
  assert.match(await (await webhook('/inbound/pin',{Digits:'246810'})).text(),/Access denied/);
});
test('restart retains history and prevents queued calls from replaying',async t=>{
  const dir=await mkdtemp(join(tmpdir(),'dot-local-'));t.after(()=>rm(dir,{recursive:true,force:true}));
  const path=join(dir,'state.sqlite');let queue=openLocalQueue(path);
  queue.perform('pulse',{instance,ready:true,audio_guard_version:1});
  const job=queue.perform('start',{request_key:'survive-restart',duration_seconds:120});
  queue.perform('pulse',{instance,ready:true,audio_guard_version:1});queue.close();
  queue=openLocalQueue(path);t.after(()=>queue.close());
  assert.equal(queue.perform('status').mac_online,false);
  const after=queue.perform('get',{call_id:job.id});
  assert.equal(after.status,'failed');assert.equal(after.code,'LOCAL_BACKEND_RESTARTED');
  assert.equal(queue.perform('pulse',{instance,ready:false,busy:true,audio_guard_version:1}).cancel_call_id,job.id);
  assert.equal(queue.perform('pulse',{instance,ready:true,audio_guard_version:1}).job,undefined);
  assert.equal(queue.perform('start',{request_key:'survive-restart',duration_seconds:120}).id,job.id);
});
test('Twilio readiness performs only a bounded read and rejects provider failure',async()=>{
  const reads=[];
  const status=twilioStatusReader(twilio,async(url,options)=>{reads.push({url,options});return Response.json({status:'active'});});
  assert.equal((await status()).calls_enabled,true);
  assert.match(reads[0].url,/Accounts\/AC[0-9]+\.json$/);
  assert.equal(reads[0].options.method,undefined);
  assert.equal(reads[0].options.redirect,'error');
  assert.equal((await twilioStatusReader(twilio,async()=>new Response('',{status:401}))()).reachable,false);
});

test('menu-bar status requires companion authentication and never requests phone service data',async t=>{
 const f=await fixture(t);
 const rejected=await f.post('/companion/service-status',{},'wrong');assert.equal(rejected.status,401);
 const wrongRole=await f.post('/companion/service-status',{},mcpToken);assert.equal(wrongRole.status,401);
 const status=await f.post('/companion/service-status',{});assert.equal(status.status,200);
 const value=await status.json();assert.equal(value.backend,'local');assert.equal(value.active_call,null);
 assert.equal(value.phone_service,undefined);assert.ok(!JSON.stringify(value).includes(companionToken));
 assert.equal((await f.post('/companion/service-status',{unexpected:true})).status,400);
});

test('local audio diagnostic requests require disabled calling, an idle session, and a fresh ID',async t=>{
 for(const [enabled,busy,expires,expected] of [[false,false,Date.now()+300000,true],[true,false,Date.now()+300000,false],[false,true,Date.now()+300000,false],[false,false,0,false]]){
  const queue=openLocalQueue(':memory:');
  const registered={helper:async()=>({busy}),status:()=>({}),registered:()=>true};
  const id=randomUUID();
  const servers=createLocalBackend({queue,mcpToken,companionToken,extensionId,twilio,callingEnabled:enabled,registered,readAudioDiagnostic:async()=>({id,expires_at:expires})});
  await new Promise(r=>servers.privateServer.listen(0,'127.0.0.1',r));
  try{
   const response=await fetch(`http://127.0.0.1:${servers.privateServer.address().port}/companion/registered-session`,{method:'POST',headers:{Authorization:'Bearer '+companionToken,'Content-Type':'application/json'},body:JSON.stringify({instance})});
   assert.equal(response.status,200);const data=await response.json();assert.equal(data.audio_diagnostic_id,expected?id:undefined);
  }finally{await new Promise(r=>{servers.privateServer.close(r);servers.privateServer.closeAllConnections();});queue.close();}
 }
});

test('no-phone check results are recorded only for the requested ID and only with known fields',async t=>{
  const queue=openLocalQueue(':memory:'),id=randomUUID(),written=[];
  const servers=createLocalBackend({queue,mcpToken,companionToken,extensionId,twilio,
    readAudioDiagnostic:async()=>({id,expires_at:Date.now()+60000}),writeAudioDiagnostic:async value=>{written.push(value);}});
  await new Promise(r=>servers.privateServer.listen(0,'127.0.0.1',r));
  t.after(async()=>{await new Promise(r=>{servers.privateServer.close(r);servers.privateServer.closeAllConnections();});queue.close();});
  const post=data=>fetch(`http://127.0.0.1:${servers.privateServer.address().port}/companion/audio-diagnostic-result`,{method:'POST',
    headers:{Authorization:'Bearer '+companionToken,'Content-Type':'application/json'},body:JSON.stringify({instance,...data})});
  const result={verified:true,sink:'blackhole-16ch',codec:'audio/opus',packets_received:120,peak_hz:997,rms:0.14,active_ms:2500,phone_dialed:false};
  assert.equal((await post({id:randomUUID(),result})).status,409);
  assert.equal((await post({id,result:{...result,extra:'not allowed'}})).status,400);
  assert.equal((await post({id,result:{...result,phone_dialed:true}})).status,400);
  assert.equal(written.length,0);
  const response=await post({id,result});
  assert.equal(response.status,200);
  assert.equal(written.length,1);assert.equal(written[0].id,id);assert.equal(written[0].peak_hz,997);
});

test('call progress is logged with Dot output routing counts but no free text',async t=>{
  const queue=openLocalQueue(':memory:'),lines=[];
  const servers=createLocalBackend({queue,mcpToken,companionToken,extensionId,twilio,callingEnabled:true,log:line=>lines.push(line)});
  await new Promise(r=>servers.privateServer.listen(0,'127.0.0.1',r));
  t.after(async()=>{await new Promise(r=>{servers.privateServer.close(r);servers.privateServer.closeAllConnections();});queue.close();});
  const post=(path,data)=>fetch(`http://127.0.0.1:${servers.privateServer.address().port}${path}`,{method:'POST',
    headers:{Authorization:'Bearer '+companionToken,'Content-Type':'application/json'},body:JSON.stringify({instance,...data})});
  await post('/companion/pulse',{ready:true,busy:false,audio_guard_version:1});
  queue.perform('start',{request_key:randomUUID(),duration_seconds:60});
  const job=(await (await post('/companion/pulse',{ready:true,busy:false,audio_guard_version:1})).json()).job;
  await post('/companion/update',{call_id:job.id,status:'failed',code:'DOT_OUTPUT_NOT_ROUTED',diagnostics:{dot_output:{
    routed_elements:2,routed_contexts:1,seen_elements:2,seen_contexts:1,errors:['elements:NotAllowedError','<script>'],note:'free text is dropped'}}});
  assert.equal(lines.length,1);
  assert.deepEqual(lines[0],{event:'call_progress',call_id:job.id,status:'failed',code:'DOT_OUTPUT_NOT_ROUTED',
    dot_output:{routed_elements:2,routed_contexts:1,seen_elements:2,seen_contexts:1,errors:['elements:NotAllowedError']}});
});
