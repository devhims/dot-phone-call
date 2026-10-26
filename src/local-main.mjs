import {mkdir,readFile,writeFile} from 'node:fs/promises';
import {RegisteredCalls} from './registered-calls.mjs';
import {openLocalQueue} from './local-queue.mjs';
import {createLocalBackend,twilioStatusReader} from './local-backend.mjs';

const root=new URL('../',import.meta.url);
try {
  const config=JSON.parse(await readFile(new URL('.secrets/local-backend.json',root),'utf8'));
  const paired=JSON.parse(await readFile(new URL('.secrets/mac-companion.json',root),'utf8'));
  const twilio=JSON.parse(await readFile(new URL('.secrets/inbound-worker-secrets.json',root),'utf8'));
  await mkdir(new URL('.runtime/',root),{recursive:true,mode:0o700});
  const queue=openLocalQueue(new URL('.runtime/local-backend.sqlite',root).pathname);
  const registered=new RegisteredCalls({queue,config:twilio,enabled:config.calling_enabled===true,allowPhoneDialing:config.allow_phone_dialing===true});
  const servers=createLocalBackend({queue,registered,mcpToken:config.mcp_token,companionToken:paired.cloud_token,
    readAudioDiagnostic:async()=>{try{return JSON.parse(await readFile(new URL('.runtime/hidden-audio-test-request.json',root),'utf8'));}catch{return null;}},
    writeAudioDiagnostic:value=>writeFile(new URL('.runtime/audio-diagnostic-result.json',root),JSON.stringify(value),{mode:0o600}),
    extensionId:paired.extension_id,publicOrigin:config.public_origin,twilio,readStatus:twilioStatusReader(twilio),callingEnabled:config.calling_enabled===true});
  const listeners=[servers.privateServer];
  await Promise.all(listeners.map((server,index)=>new Promise((resolve,reject)=>{
    server.once('error',reject);server.listen(index===0?8894:8895,'127.0.0.1',resolve);
  })));
  console.error(JSON.stringify({event:'local_backend_ready',private_port:8894,registered_client:true}));
  let monitoring=false;
  const monitor=setInterval(()=>{if(monitoring)return;monitoring=true;void registered.monitor().catch(()=>{}).finally(()=>{monitoring=false;});},2000);
  let closing=false;
  const close=async()=>{
    if(closing)return;closing=true;clearInterval(monitor);
    await registered.close().catch(()=>{});
    for(const server of listeners){server.close();server.closeAllConnections();}
    queue.close();process.exit(0);
  };
  process.on('SIGTERM',close);process.on('SIGINT',close);
} catch {
  console.error('LOCAL_BACKEND_START_FAILED: check private configuration and listener availability');
  process.exit(1);
}
