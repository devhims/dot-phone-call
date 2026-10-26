// Only the background worker holds cloud credentials and the native port.
// No page message can request a call or launch an arbitrary native command.
let helperPort = null;
let helperLaunching = false;
let polling = false;
let booting = true;
let recoveryRequired = false;
let instance;
let hiddenDiagnosticRunning=false;
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
async function startHelper(mode, job) {
  if(helperPort || helperLaunching) throw new Error('HELPER_ALREADY_RUNNING');
  helperLaunching=true;
  try {
    await chrome.storage.local.set({helperActive:{mode,job,startedAt:Date.now()}});
    return await new Promise((resolve,reject)=>{
      const port=chrome.runtime.connectNative('dev.dot_phone_call.helper');
      helperPort=port;
      let result;
      const heartbeat=setInterval(()=>port.postMessage({type:'heartbeat'}),5000);
      let forceClose;
      const timeout=setTimeout(()=>{
        port.postMessage({type:'cancel'});
        forceClose=setTimeout(()=>port.disconnect(),20000);
      }, (mode==='call' ? job.duration_seconds+260 : 210)*1000);
      port.onMessage.addListener(async message=>{
        if(message.type==='authenticated') {
          const ended=await endOwned().catch(()=>({error:'DOT_HANGUP_NOT_CONFIRMED'}));
          port.postMessage({type:'ready',ok:!ended.error,version});
        } else if(message.type==='command') {
          let value;
          try {value=await command(message);} catch(error) {
            value={error:browserErrorCode(error),diagnostics:error.diagnostics};
          }
          if(helperPort===port) port.postMessage({type:'result',id:message.id,...value});
        } else if(message.type==='complete') {
          result=message;
          await chrome.storage.local.set({lastHelperResult:message});
          // Let the native process exit itself. A disconnect after this result
          // proves it finished, instead of hiding a leaked process with close().
        }
      });
      port.onDisconnect.addListener(()=>{
        const error=chrome.runtime.lastError?.message;
        clearInterval(heartbeat);clearTimeout(timeout);clearTimeout(forceClose);helperPort=null;
        void (async()=>{
          if(result?.cleanup_ok) {
            recoveryRequired=false;
            await chrome.storage.local.remove('helperActive');
            await chrome.storage.local.set({lastHelperExit:{at:Date.now(),clean:true}});
            resolve(result);
          } else {
            recoveryRequired=true;
            await endOwned().catch(()=>{});
            await chrome.storage.local.set({lastHelperExit:{at:Date.now(),clean:false,error:error||result?.code||'HELPER_DISCONNECTED'}});
            reject(new Error(result?.code||'HELPER_DISCONNECTED'));
          }
        })();
      });
      port.postMessage({type:'run',mode,instance,backend_origin:DOT_PHONE_CONFIG.origin,...(job?{job}:{})});
    });
  } catch(error) {
    recoveryRequired=true;
    throw error;
  } finally {helperLaunching=false;}
}
async function pollCloud() {
  if(booting || polling || helperPort || !DOT_PHONE_CONFIG.callingEnabled) return;
  polling=true;
  try {
    // This also resets Chrome's worker idle timer while unattended calling is
    // enabled. The alarm restarts polling after a worker/browser restart.
    await chrome.storage.local.get('helperActive');
    if(recoveryRequired)await startHelper('recover');
    const hidden=await ensureRegisteredBridge();
    const diagnosticId=hidden?.diagnostic_id||DOT_PHONE_CONFIG.hiddenAudioTestId;
    const diagnosticKey=hidden?.diagnostic_id?'lastRequestedAudioTestId':'lastHiddenAudioTestId';
    if(diagnosticId&&hidden?.microphone_ready&&hidden.registered&&!hiddenDiagnosticRunning){
      const saved=await chrome.storage.local.get(diagnosticKey);
      if(saved[diagnosticKey]!==diagnosticId){
        hiddenDiagnosticRunning=true;
        await chrome.storage.local.set({[diagnosticKey]:diagnosticId,lastHiddenAudioTestId:DOT_PHONE_CONFIG.hiddenAudioTestId});
        try{await startHelper('hidden-audio-test');}finally{hiddenDiagnosticRunning=false;}
      }
    }
    const result=await cloudRequest('pulse',{ready:true,busy:false,audio_guard_version:1});
    if(result.job) {
      try {await startHelper('call',result.job);} catch {
        await cloudRequest('update',{call_id:result.job.id,status:'failed',code:'NATIVE_HELPER_FAILED'});
      }
    }
  } catch(error) {
    await chrome.storage.local.set({lastCloudError:{at:Date.now(),code:error.message}});
  } finally {polling=false;}
}
async function boot() {
  const state=await chrome.storage.local.get(['helperInstance','helperActive','lastDiagnosticId','lastPrepareDiagnosticId']);
  instance=state.helperInstance||crypto.randomUUID();
  await chrome.storage.local.set({helperInstance:instance});
  try {
    if(state.helperActive || await owned()) await startHelper('recover');
    await refreshDotTabs();
    if(DOT_PHONE_CONFIG.prepareTestId && state.lastPrepareDiagnosticId!==DOT_PHONE_CONFIG.prepareTestId) {
      const result=await startHelper('prepare-test');
      if(!result.verified)throw new Error(result.code||'DOT_TAB_NOT_READY');
      await chrome.storage.local.set({lastPrepareDiagnosticId:DOT_PHONE_CONFIG.prepareTestId});
    }
    if(DOT_PHONE_CONFIG.testId && state.lastDiagnosticId!==DOT_PHONE_CONFIG.testId) {
      // Wait for the document-start microphone hook after an extension upgrade.
      // This readiness check cannot open audio or dial the phone.
      const deadline=Date.now()+120000;
      let prepared=false;
      while(Date.now()<deadline) {
        try {
          const result=await command({action:'prepare'});
          if(result.ready) {prepared=true;break;}
        } catch {}
        await sleep(2000);
      }
      if(!prepared) throw new Error('DOT_TAB_NOT_READY');
      await chrome.storage.local.set({lastDiagnosticId:DOT_PHONE_CONFIG.testId});
      await startHelper('audio-test');
    }
  } catch(error) {
    await chrome.storage.local.set({lastHelperError:{at:Date.now(),code:error.message}});
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
