import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {randomUUID} from 'node:crypto';
const source=readFileSync(new URL('../chrome-extension/helper-client.js',import.meta.url),'utf8');
const flush=()=>new Promise(resolve=>setImmediate(resolve));
async function fixture() {
  const values={},ports=[],cloud=[];
  const noop=()=>{};
  const chrome={storage:{local:{
    get:async keys=>Object.fromEntries((Array.isArray(keys)?keys:[keys]).map(k=>[k,values[k]])),
    set:async v=>Object.assign(values,v),remove:async k=>{delete values[k];},
  }}, runtime:{onStartup:{addListener:noop},connectNative:name=>{
    const port={name,messages:[],postMessage:m=>port.messages.push(m),
      onMessage:{addListener:f=>{port.message=f;}},onDisconnect:{addListener:f=>{port.disconnect=f;}}};
    ports.push(port);return port;
  }}, action:{onClicked:{addListener:noop}},alarms:{create:noop,onAlarm:{addListener:noop}}};
  const context=vm.createContext({chrome,crypto:{randomUUID},DOT_PHONE_CONFIG:{callingEnabled:false},
    fetch:async()=>{cloud.push(true);throw Error('network forbidden');},setInterval:noop,clearInterval:noop,
    setTimeout:noop,clearTimeout:noop,Date,AbortSignal,version:'0.1.7',owned:async()=>null,
    endOwned:async()=>({ended:true}),refreshDotTabs:async()=>{},command:async()=>({ready:true}),sleep:async()=>{}});
  vm.runInContext(source,context);await flush();
  return {values,ports,cloud,run:expression=>vm.runInContext(expression,context)};
}
test('idle diagnostic build never launches a helper or polls for calls',async()=>{
  const f=await fixture();assert.equal(f.ports.length,0);assert.equal(f.cloud.length,0);
});
test('helper starts through native messaging, then exits after cleanup without a cloud call',async()=>{
  const f=await fixture();const done=f.run("startHelper('audio-test')");await flush();
  const p=f.ports[0];assert.equal(p.name,'dev.dot_phone_call.helper');assert.equal(p.messages[0].mode,'audio-test');
  await p.message({type:'authenticated'});assert.equal(p.messages.at(-1).type,'ready');
  await p.message({type:'complete',cleanup_ok:true,verified:true,dialed:false});
  assert.ok(f.values.helperActive); // Receipt alone is not process-exit proof.
  p.disconnect();assert.equal((await done).verified,true);
  assert.equal(f.values.helperActive,undefined);assert.equal(f.values.lastHelperExit.clean,true);
  assert.equal(f.cloud.length,0);
});
test('concurrent requests launch only one native helper',async()=>{
  const f=await fixture();const first=f.run("startHelper('audio-test')");
  await assert.rejects(f.run("startHelper('audio-test')"),/HELPER_ALREADY_RUNNING/);
  await flush();assert.equal(f.ports.length,1);
  await f.ports[0].message({type:'complete',cleanup_ok:true});f.ports[0].disconnect();await first;
});
test('native crash retains recovery marker and blocks later cloud polling',async()=>{
  const f=await fixture();const done=f.run("startHelper('audio-test')");await flush();
  f.ports[0].disconnect();await assert.rejects(done,/HELPER_DISCONNECTED/);
  assert.ok(f.values.helperActive);assert.equal(f.values.lastHelperExit.clean,false);
  assert.equal(f.run('recoveryRequired'),true);assert.equal(f.cloud.length,0);
});

test('registered bridge startup reuses its tab and replaces only stale session URLs',async()=>{
  const f=await fixture();
  f.run("DOT_PHONE_CONFIG.origin='http://127.0.0.1:8894'; globalThis.changes=[]; globalThis.tabs=[]; chrome.tabs={query:async()=>tabs,create:async v=>changes.push(['create',v]),update:async(id,v)=>changes.push(['update',id,v])}; fetch=async()=>({ok:true,json:async()=>({url:'http://127.0.0.1:8894/bridge/#'+'a'.repeat(64)})})");
  await f.run('ensureRegisteredBridge()');assert.equal(f.run('changes[0][0]'),'create');
  f.run("changes=[];tabs=[{id:7,url:'http://127.0.0.1:8894/bridge/#'+'a'.repeat(64)}]");
  await f.run('ensureRegisteredBridge()');assert.equal(f.run('changes.length'),0);
  f.run("tabs[0].url='http://127.0.0.1:8894/bridge/#'+'b'.repeat(64)");
  await f.run('ensureRegisteredBridge()');assert.equal(f.run('changes[0][0]'),'update');assert.equal(f.run('changes[0][1]'),7);
});

test('bridge startup refuses a remote or malformed session URL',async()=>{
  const f=await fixture();f.run("DOT_PHONE_CONFIG.origin='http://127.0.0.1:8894'; fetch=async()=>({ok:true,json:async()=>({url:'https://evil.test/bridge/'})})");
  await assert.rejects(f.run('ensureRegisteredBridge()'),/INVALID_BRIDGE_SESSION/);
});
