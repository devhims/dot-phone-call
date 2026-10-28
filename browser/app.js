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
// Call log events for the local backend: Twilio states, error codes and audio levels only.
const traceQueue=[];let traceFlushing=false;
function trace(ev,fields={}) {
  if(traceQueue.length>=200)return;
  traceQueue.push({ev,at:Date.now(),call:job?.id,...fields});
  void flushTrace();
}
async function flushTrace() {
  if(traceFlushing)return;traceFlushing=true;
  try {while(traceQueue.length)await post('log',{events:traceQueue.splice(0,20)}).catch(()=>{});}
  finally {traceFlushing=false;}
}
// Twilio reports levels from 0 to 32767 once a second. "Active" means louder than -45 dBFS.
const db=level=>Number.isFinite(level)&&level>0?Math.max(-100,Math.round(20*Math.log10(level/32767))):-100;
function audioSummary(list) {
  const values=key=>list.map(s=>s[key]).filter(Number.isFinite);
  const peak=key=>values(key).length?Math.max(...values(key)):null,active=key=>values(key).filter(v=>v>-45).length;
  return {seconds:list.length,caller_peak_db:peak('caller'),caller_active_s:active('caller'),dot_peak_db:peak('dot'),dot_active_s:active('dot'),
    sent_peak_db:peak('sent'),sent_active_s:active('sent'),rtt_ms:peak('rtt'),jitter_ms:peak('jitter'),loss_pct:peak('loss'),
    mos_min:values('mos').length?Math.min(...values('mos')):null,audio_context:session?.context?.state??null};
}
async function reportFailure(error) {
  const code=/^[A-Z_]{1,60}$/.test(error.message)?error.message:({NotAllowedError:"MICROPHONE_PERMISSION_REQUIRED",NotFoundError:"AUDIO_DEVICE_NOT_FOUND",NotReadableError:"AUDIO_DEVICE_UNAVAILABLE"}[error.name]||"BROWSER_AUDIO_START_FAILED");
  lastFailure=code;status.textContent=code;
  trace('bridge_failure',{code});
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
  const current=call,id=job.id,child=job.child_sid;
  let samples=[],acceptedAt=null;
  const flushSamples=()=>{if(samples.length)trace('audio',audioSummary(samples));samples=[];};
  trace('call_accepting',{child});
  current.on('audio',audio=>{audio.muted=true;});
  current.on('accept',()=>{acceptedAt=Date.now();trace('call_accepted',{child});void (async()=>{
    try {await session.remote(current);
      for(let attempt=0;;attempt++){try{await post('connected',{job_id:id,sid:current.parameters.CallSid});break;}catch(error){if(error.message!=='CALL_NOT_CONNECTED'||attempt>=4)throw error;await new Promise(r=>setTimeout(r,500));}}
      trace('call_audio_connected',{child});
      status.textContent='Connected to Dot';}
    catch(error){await reportFailure(error);}
  })();});
  current.on('sample',sample=>{
    samples.push({caller:db(sample.audioOutputLevel),sent:db(sample.audioInputLevel),dot:session?.dotPeakDb()??null,
      rtt:sample.rtt,jitter:sample.jitter,loss:sample.packetsLostFraction,mos:sample.mos});
    if(samples.length>=5)flushSamples();
  });
  current.on('warning',name=>trace('call_warning',{child,name:String(name)}));
  current.on('warning-cleared',name=>trace('call_warning_cleared',{child,name:String(name)}));
  current.on('reconnecting',error=>trace('call_reconnecting',{child,code:error?.code??null}));
  current.on('reconnected',()=>trace('call_reconnected',{child}));
  current.on('disconnect',()=>{flushSamples();trace('call_disconnected',{child,connected_s:acceptedAt?Math.round((Date.now()-acceptedAt)/1000):null});void cleanup();});
  current.on('error',error=>{flushSamples();trace('call_error',{child,code:error?.code??null,name:error?.name??null});void cleanup();});
  current.accept({rtcConstraints:{audio:{echoCancellation:false,noiseSuppression:false,autoGainControl:false}}});
}
async function step() {
    try {
      const state=await post('pulse',{registered:device.state==='registered',context:extensionBridge?'offscreen':'tab',microphone_ready:microphoneReady});
      if(state.token)device.updateToken(state.token);
      if(job&&state.job?.id!==job.id)await cleanup(false);
      if(state.release_job&&!job)await post('released',{job_id:state.release_job});
      job=state.job;
      if(job?.prepared&&!session&&!preparing){
        preparing=true;
        try {
          lastFailure=null;session=new AudioSession();await session.prepare();
          await post('ready',{job_id:job.id});endButton.disabled=false;
          trace('bridge_audio_prepared');
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
  device.on('tokenWillExpire',()=>{void post('token').then(v=>device.updateToken(v.token)).catch(async e=>{status.textContent=e.message;trace('twilio_token_refresh_failed',{code:e.message});await cleanup();device.destroy();location.reload();});});
  device.on('registered',()=>trace('twilio_registered'));
  device.on('unregistered',()=>{trace('twilio_unregistered');if(!stopped)setTimeout(()=>device.register().catch(()=>{}),2000);});
  device.on('error',e=>{status.textContent='Twilio error '+e.code;trace('twilio_device_error',{code:e?.code??null});});
  device.on('incoming',incoming=>{void (async()=>{
    const sid=incoming.parameters.CallSid;
    trace('incoming_offered',{child:sid});
    // Canceled before answer: the caller hung up, or Twilio stopped ringing the browser.
    incoming.on('cancel',()=>{trace('incoming_canceled',{child:sid});pending.delete(sid);if(job?.child_sid===sid)void cleanup();});
    try {
      const authorized=await post('incoming',{sid,pin:incoming.customParameters.get('pin')});
      if(stopped||closing||incoming.status()==='closed'){trace('incoming_rejected',{child:sid,code:'BRIDGE_CLOSING'});incoming.reject();await post('ended',{job_id:authorized.job_id}).catch(()=>{});return;}
      pending.set(sid,incoming);
    }catch(error){trace('incoming_rejected',{child:sid,code:error.message});incoming.reject();}
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
    void testHiddenAudio().then(reply,error=>reply({error:/^[A-Z_]{1,60}$/.test(error.message)?error.message:'OFFSCREEN_AUDIO_FAILED'})).finally(()=>{diagnosing=false;});
    return true;
  }
});
start().catch(error=>{status.textContent=error.message;trace('bridge_start_failed',{code:error.message});setTimeout(()=>location.reload(),15000);});
