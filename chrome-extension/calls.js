// Runs owner calls from the background worker. Only this worker holds the local
// companion credential; no page message can request a call. Audio never passes
// through here: Dot's tab and the hidden Twilio page route it themselves.
let polling = false;
let booting = true;
let callRunning = false;
let diagnosticRunning = false;
let instance;
const CALL_RECORD = 'activeCall';
const PRE_CONNECT_MS = 85000;
const callSleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function cloudRequest(action, data) {
  const response = await fetch(DOT_PHONE_CONFIG.origin+'/companion/'+action, {
    method:'POST', redirect:'error', signal:AbortSignal.timeout(8000),
    headers:{'Content-Type':'application/json',Authorization:'Bearer '+DOT_PHONE_CONFIG.token},
    body:JSON.stringify({...data,instance}),
  });
  if(!response.ok) throw new Error('COMPANION_CLOUD_REQUEST_FAILED');
  const result=await response.json();
  if(result.error) throw new Error(result.error);
  return result;
}

// Dot tab commands report failures as {error}; a call treats both forms alike.
async function browserCommand(message) {
  let value;
  try {value=await command(message);}
  catch(error) {const failure=new Error(browserErrorCode(error));failure.diagnostics=error.diagnostics;throw failure;}
  if(value?.error) {const failure=new Error(value.error);failure.diagnostics=value.diagnostics;throw failure;}
  return value;
}
const failureCode = error => /^[A-Z_]{1,60}$/.test(error?.message||'') ? error.message : 'CALL_FAILED';

// One idempotent finish for every ending: Twilio and browser audio first, then Dot.
async function finishCall(jobId, code, diagnostics) {
  let cleanup=true;
  try {
    let ended=false;
    for(let attempt=0;attempt<15&&!ended;attempt++) {
      ended=(await cloudRequest('registered-finish',{call_id:jobId})).ended===true;
      if(!ended) await callSleep(500);
    }
    if(!ended) throw new Error('BROWSER_AUDIO_RELEASE_UNCONFIRMED');
  } catch {cleanup=false;code=code||'REGISTERED_HANGUP_UNCONFIRMED';}
  try {
    const ended=await endOwned(jobId);
    if(ended?.error) throw new Error(ended.error);
  } catch {cleanup=false;code=code||'DOT_HANGUP_NOT_CONFIRMED';}
  await cloudRequest('update',{call_id:jobId,status:code?'failed':'completed',...(code?{code}:{}),
    ...(diagnostics?{diagnostics}:{})}).catch(()=>{});
  // A failed cleanup keeps the record, so the next worker start retries it.
  if(cleanup) await chrome.storage.local.remove(CALL_RECORD);
  return {code,cleanup};
}

async function runCall(job) {
  callRunning=true;
  let code=null, started=false, output=null, lastPulse=0;
  let deadline=Date.now()+PRE_CONNECT_MS;
  try {
    await chrome.storage.local.set({[CALL_RECORD]:{id:job.id,startedAt:Date.now()}});
    const prepared=await browserCommand({action:'prepare'});
    await cloudRequest('registered-prepare',{call_id:job.id});
    while(true) {
      // Busy pulses keep the backend's ownership fresh and carry cancellations.
      if(Date.now()-lastPulse>=2000) {
        lastPulse=Date.now();
        const state=await cloudRequest('pulse',{ready:false,busy:true,audio_guard_version:1});
        if(state.cancel_call_id===job.id) break;
      }
      const status=await cloudRequest('registered-state',{call_id:job.id});
      if(status.ended) break;
      if(status.connected&&!started) {
        const result=await browserCommand({action:'start',job_id:job.id,tab_id:prepared.tab_id,duration_seconds:job.duration_seconds+30});
        if(!result.connected||!result.microphone_verified) throw new Error('DOT_MICROPHONE_NOT_VERIFIED');
        output=result.output??null;
        started=true;
        deadline=Date.now()+job.duration_seconds*1000;
        await cloudRequest('update',{call_id:job.id,status:'connected'});
      }
      if(started) {
        const inspected=await browserCommand({action:'inspect_owned',job_id:job.id});
        output=inspected.output??output;
        // A Dot player that refused BlackHole 2ch would play on the speakers.
        if(output?.errors?.length) throw new Error('DOT_OUTPUT_NOT_ROUTED');
        if(!inspected.active||!inspected.microphone_verified) break;
      }
      if(Date.now()>deadline) throw new Error('REGISTERED_CALL_TIMEOUT');
      await callSleep(1000);
    }
  } catch(error) {
    code=failureCode(error);
  } finally {
    const routing=output?{dot_output:output}:undefined;
    const result=await finishCall(job.id,code,routing);
    await chrome.storage.local.set({lastCall:{id:job.id,code:result.code,cleanup:result.cleanup,output,at:Date.now()}});
    callRunning=false;
  }
}

// No-phone check requested by verify-setup.py; the hidden page runs it.
async function runAudioDiagnostic(id) {
  diagnosticRunning=true;
  try {
    await chrome.storage.local.set({lastRequestedAudioTestId:id});
    let result;
    try {
      result=await bounded(chrome.runtime.sendMessage({target:'dot-phone-offscreen',action:'audio-test'}),30000);
      if(!result) throw new Error('OFFSCREEN_NOT_READY');
    } catch(error) {result={verified:false,code:browserErrorCode(error)};}
    await cloudRequest('audio-diagnostic-result',{id,result});
  } finally {diagnosticRunning=false;}
}

async function pollCloud() {
  if(booting||polling||callRunning||!DOT_PHONE_CONFIG.callingEnabled) return;
  polling=true;
  try {
    // This also resets Chrome's worker idle timer while unattended calling is
    // enabled. The alarm restarts polling after a worker/browser restart.
    await chrome.storage.local.get(CALL_RECORD);
    const hidden=await ensureRegisteredBridge();
    const diagnosticId=hidden?.diagnostic_id;
    if(diagnosticId&&hidden.microphone_ready&&hidden.registered&&!diagnosticRunning) {
      const saved=await chrome.storage.local.get('lastRequestedAudioTestId');
      if(saved.lastRequestedAudioTestId!==diagnosticId) await runAudioDiagnostic(diagnosticId);
    }
    const result=await cloudRequest('pulse',{ready:true,busy:false,audio_guard_version:1});
    if(result.job) void runCall(result.job);
  } catch(error) {
    await chrome.storage.local.set({lastCloudError:{at:Date.now(),code:error.message}});
  } finally {polling=false;}
}

async function boot() {
  const state=await chrome.storage.local.get(['helperInstance',CALL_RECORD]);
  instance=state.helperInstance||crypto.randomUUID();
  await chrome.storage.local.set({helperInstance:instance});
  try {
    // A worker restart cannot resume a call safely; finish it exactly once.
    if(state[CALL_RECORD]) await finishCall(state[CALL_RECORD].id,'EXTENSION_RESTARTED');
    else if(await owned()) await endOwned();
    await refreshDotTabs();
  } catch(error) {
    await chrome.storage.local.set({lastBootError:{at:Date.now(),code:error.message}});
  } finally {booting=false;void pollCloud();}
}

chrome.action.onClicked.addListener(async tab=>{
  if(DOT_PHONE_CONFIG.hiddenBridge){
    const status=await hiddenBridgeStatus().catch(()=>null);
    if(!status?.microphone_ready)await chrome.runtime.openOptionsPage();
  }
  if(tab.url?.startsWith('https://chatgpt.com/dots/')) await chrome.storage.local.set({preferredTabId:tab.id});
});
chrome.runtime.onStartup.addListener(()=>{void pollCloud();});
chrome.alarms.create('cloud-pulse',{periodInMinutes:.5});
chrome.alarms.onAlarm.addListener(()=>{void pollCloud();});
setInterval(()=>{void pollCloud();},5000);
void boot();

async function ensureRegisteredBridge() {
  const session=await cloudRequest('registered-session',{});
  const prefix=DOT_PHONE_CONFIG.origin+'/bridge/';
  if(!session.url?.startsWith(prefix+'#')||!/^http:\/\/127\.0\.0\.1:8894\/bridge\/#(?:[a-f0-9]{64})$/.test(session.url))throw Error('INVALID_BRIDGE_SESSION');
  if(DOT_PHONE_CONFIG.hiddenBridge)return {...await ensureHiddenBridge(session),diagnostic_id:session.audio_diagnostic_id};
  if(chrome.offscreen){
    const contexts=await chrome.runtime.getContexts({contextTypes:['OFFSCREEN_DOCUMENT']});
    if(contexts.length&&!session.busy)await chrome.offscreen.closeDocument();
  }
  const tabs=await chrome.tabs.query({url:prefix+'*'});
  const exact=tabs.find(t=>t.url===session.url);
  if(exact)return;
  if(tabs.length)await chrome.tabs.update(tabs[0].id,{url:session.url,active:false});
  else await chrome.tabs.create({url:session.url,active:false});
}
