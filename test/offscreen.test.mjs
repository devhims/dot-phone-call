import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../chrome-extension/offscreen-control.js',import.meta.url),'utf8');
function fixture({busy=false,existing=false,microphone=true}={}){
 const events=[];let contexts=existing?[{documentUrl:'chrome-extension://test/offscreen.html#'+'a'.repeat(64)}]:[];
 const chrome={runtime:{getURL:p=>'chrome-extension://test/'+p,getContexts:async()=>contexts,sendMessage:async()=>({registered:true,microphone_ready:microphone}),openOptionsPage:async()=>events.push('setup')},offscreen:{closeDocument:async()=>{events.push('close');contexts=[];},createDocument:async v=>{events.push(['create',v]);contexts=[{documentUrl:v.url}];}},tabs:{query:async()=>[{id:7}],remove:async ids=>events.push(['remove',ids])},storage:{local:{set:async()=>{}}}};
 const context=vm.createContext({chrome,Date,DOT_PHONE_CONFIG:{origin:'http://127.0.0.1:8894'},bounded:p=>p});vm.runInContext(source,context);
 return {events,run:()=>{context.session={url:'http://127.0.0.1:8894/bridge/#'+'a'.repeat(64),busy};return vm.runInContext('ensureHiddenBridge(session)',context);}};
}
test('hidden bridge replaces only the legacy bridge tab with packaged WebRTC document',async()=>{
 const f=fixture();await Promise.all([f.run(),f.run()]);
 assert.equal(f.events.filter(e=>e[0]==='create').length,1);
 assert.deepEqual(Array.from(f.events.find(e=>e[0]==='remove')[1]),[7]);
 assert.deepEqual(Array.from(f.events.find(e=>e[0]==='create')[1].reasons),['USER_MEDIA','WEB_RTC']);
});
test('existing hidden bridge is reused; a live call prevents migration',async()=>{
 const f=fixture({existing:true});await f.run();assert.equal(f.events.length,0);
 const busy=fixture({busy:true});await assert.rejects(busy.run(),/WAITING_FOR_CALL/);assert.equal(busy.events.length,0);
});
test('missing microphone permission opens one visible setup page, never a call',async()=>{
 const f=fixture({microphone:false});await f.run();await f.run();assert.equal(f.events.filter(e=>e==='setup').length,1);
});

test('extension executable sources stay packaged, including worker-src',()=>{
 const manifest=JSON.parse(readFileSync(new URL('../chrome-extension/manifest.json',import.meta.url),'utf8'));
 const directives=new Map(manifest.content_security_policy.extension_pages.split(';').filter(s=>s.trim()).map(s=>{const [name,...values]=s.trim().split(/\s+/);return [name,values];}));
 for(const name of ['script-src','worker-src','object-src']) {
  assert.deepEqual(directives.get(name),["'self'"],`${name} must allow only packaged resources`);
 }
});
