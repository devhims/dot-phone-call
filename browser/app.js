import {AudioSession} from './session.js';
import {testHiddenAudio} from './diagnostic.js';
const extensionBridge=typeof chrome!=='undefined' && !!chrome.runtime?.id && location.protocol==='chrome-extension:';
const bridgeBase=extensionBridge?'http://127.0.0.1:8894':'';
let microphoneReady=false,diagnosing=false;
const capability=location.hash.slice(1),client=sessionStorage.getItem('client')||crypto.randomUUID();
sessionStorage.setItem('client',client);
const status=document.getElementById('status'),endButton=document.getElementById('end'),microphoneButton=document.getElementById('microphone');
let device,session,job,call,preparing=false,closing=false,stopped=false;
const pending=new Map();
let lastFailure;
async function reportFailure(error) {
  const code=/^[A-Z_]{1,60}$/.test(error.message)?error.message:({NotAllowedError:"MICROPHONE_PERMISSION_REQUIRED",NotFoundError:"AUDIO_DEVICE_NOT_FOUND",NotReadableError:"AUDIO_DEVICE_UNAVAILABLE"}[error.name]||"BROWSER_AUDIO_START_FAILED");
  lastFailure=code;status.textContent=code;
  if(job)await post("failed",{job_id:job.id,code}).catch(()=>{});
  await cleanup();
}
async function post(action,data={}) {
  const response=await fetch(bridgeBase+'/bridge/'+action,{method:'POST',redirect:'error',signal:AbortSignal.timeout(15000),headers:{'Content-Type':'application/json',Authorization:'Bearer '+capability},body:JSON.stringify({...data,client})});
  const result=await response.json();if(!response.ok)throw Error(result.error||'LOCAL_SERVICE_FAILED');return result;
}
async function cleanup(notify=true) {
  if(closing)return;closing=true;
  const id=job?.id;job=null;
  call?.disconnect();call=null;
  for(const c of pending.values())c.reject();pending.clear();
  await session?.close();session=null;endButton.disabled=true;
  if(id)await post(notify?'ended':'released',{job_id:id}).catch(()=>{});
  closing=false;
}
async function accept() {
  if(!session||!job?.child_sid||!pending.has(job.child_sid)||call)return;
  call=pending.get(job.child_sid);pending.delete(job.child_sid);
  const current=call,id=job.id;
  current.on('audio',audio=>{audio.muted=true;});
  current.on('accept',()=>{void (async()=>{
    try {await session.remote(current);
      for(let attempt=0;;attempt++){try{await post('connected',{job_id:id,sid:current.parameters.CallSid});break;}catch(error){if(error.message!=='CALL_NOT_CONNECTED'||attempt>=4)throw error;await new Promise(r=>setTimeout(r,500));}}
      status.textContent='Connected to Dot';}
    catch(error){await reportFailure(error);}
  })();});
  current.on('disconnect',()=>{void cleanup();});current.on('error',()=>{void cleanup();});
  current.accept({rtcConstraints:{audio:{echoCancellation:false,noiseSuppression:false,autoGainControl:false}}});
}
async function step() {
    try {
      const state=await post('pulse',{registered:device.state==='registered',context:extensionBridge?'offscreen':'tab',microphone_ready:microphoneReady});
      if(state.token)device.updateToken(state.token);
      if(job&&state.job?.id!==job.id)await cleanup(false);
      if(state.release_job&&!job)await post('released',{job_id:state.release_job});
      job=state.job;
      if(job?.audio_capability&&!session&&!preparing){
        preparing=true;
        try {
          lastFailure=null;session=new AudioSession({capability:job.audio_capability});await session.prepare();
          session.socket.onclose=()=>{if(!closing)void cleanup();};
          await post('ready',{job_id:job.id});endButton.disabled=false;
        }finally{preparing=false;}
      }
      if(job)await accept();else status.textContent=lastFailure|| (device.state==='registered'?(state.calling_enabled?'Ready for calls':'Registered. Calling is disabled.'):'Registering with Twilio…');
    }catch(error){await reportFailure(error);}
}
async function poll() {
  // Server-driven events keep idle registration healthy even when Chrome
  // throttles background-tab timers. No microphone is held open to keep time.
  await step();
  while(!stopped){
    try {
      const response=await fetch(bridgeBase+'/bridge/events',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+capability},body:JSON.stringify({client})});
      if(!response.ok)throw Error('REGISTRATION_STREAM_LOST');
      const reader=response.body.getReader();
      try {while(!stopped){const part=await reader.read();if(part.done)break;await step();}}
      finally {await reader.cancel().catch(()=>{});}
    }catch(error){status.textContent=error.message;}
    if(!stopped)await new Promise(r=>setTimeout(r,1000));
  }
}
async function setupMicrophone() {
  const permission=await navigator.permissions.query({name:'microphone'});
  const update=()=>{microphoneReady=permission.state==='granted';microphoneButton.hidden=microphoneReady;};
  permission.onchange=update;update();
}
microphoneButton.onclick=async()=>{
  microphoneButton.disabled=true;
  try {
    const stream=await navigator.mediaDevices.getUserMedia({audio:true});
    stream.getTracks().forEach(track=>track.stop());
    lastFailure=null;await setupMicrophone();status.textContent='Microphone enabled. Ready for calls.';
  } catch(error) {await reportFailure(error);}
  finally {microphoneButton.disabled=false;}
};
async function start() {
  await setupMicrophone();
  if(!/^[a-f0-9]{64}$/.test(capability))throw Error('Open this page through the paired extension.');
  const {token}=await post('token');
  device=new Twilio.Device(token,{codecPreferences:['opus'],maxAverageBitrate:40000,edge:'singapore',logLevel:'error',tokenRefreshMs:120000,
    getUserMedia:async()=>{if(!session?.destination||session.closed)throw Error('AUDIO_NOT_READY');return session.destination.stream;}});
  device.audio.incoming(false);device.audio.outgoing(false);device.audio.disconnect(false);
  device.on('tokenWillExpire',()=>{void post('token').then(v=>device.updateToken(v.token)).catch(async e=>{status.textContent=e.message;await cleanup();device.destroy();location.reload();});});
  device.on('unregistered',()=>{if(!stopped)setTimeout(()=>device.register().catch(()=>{}),2000);});
  device.on('error',e=>{status.textContent='Twilio error '+e.code;});
  device.on('incoming',incoming=>{void (async()=>{
    const sid=incoming.parameters.CallSid;
    incoming.on('cancel',()=>{pending.delete(sid);if(job?.child_sid===sid)void cleanup();});
    try {
      const authorized=await post('incoming',{sid,pin:incoming.customParameters.get('pin')});
      if(stopped||closing||incoming.status()==='closed'){incoming.reject();await post('ended',{job_id:authorized.job_id}).catch(()=>{});return;}
      pending.set(sid,incoming);
    }catch{incoming.reject();}
  })();});
  await device.register();void poll();
}
endButton.onclick=()=>{void cleanup();};
window.addEventListener('hashchange',()=>location.reload());
window.addEventListener('pagehide',()=>{stopped=true;device?.destroy();void session?.close();});
if(extensionBridge)chrome.runtime.onMessage.addListener((message,sender,reply)=>{
  if(sender.id!==chrome.runtime.id||message.target!=='dot-phone-offscreen')return;
  if(message.action==='status')reply({registered:device?.state==='registered',microphone_ready:microphoneReady,busy:!!job||diagnosing,status:lastFailure||status.textContent});
  if(message.action==='audio-test'&&!sender.tab){
    if(job||session||diagnosing){reply({error:'AUDIO_ROUTE_ALREADY_IN_USE'});return;}
    diagnosing=true;
    void testHiddenAudio(message.capability).then(reply,error=>reply({error:/^[A-Z_]{1,60}$/.test(error.message)?error.message:'OFFSCREEN_AUDIO_FAILED'})).finally(()=>{diagnosing=false;});
    return true;
  }
});
start().catch(error=>{status.textContent=error.message;setTimeout(()=>location.reload(),15000);});
