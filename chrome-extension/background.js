importScripts('pairing.js');
const sleep = ms => new Promise(resolve => setTimeout(resolve,ms));
const version = chrome.runtime.getManifest().version;
const canceled = new Set();
async function bounded(promise, ms=3000) {
  let timer;
  try {return await Promise.race([promise,new Promise((_,reject)=>{
    timer=setTimeout(()=>reject(new Error('DOT_TAB_UNRESPONSIVE')),ms);
  })]);} finally {clearTimeout(timer);}
}
async function messageTab(tabId, action) {
  return bounded(chrome.tabs.sendMessage(tabId,{action:'dot_phone_v2_'+action}));
}
async function inspect(tabId) {
  return messageTab(tabId,'inspect');
}
async function focusTab(tab) {
  await chrome.tabs.update(tab.id,{active:true});
  await chrome.windows.update(tab.windowId,{focused:true});
}
async function ensureControls(tabId) {
  let result=await inspect(tabId).catch(()=>null);
  if(result?.version!==version) {
    await bounded(chrome.scripting.executeScript({target:{tabId},files:['content.js']}));
    result=await inspect(tabId);
  }
  if(result?.version!==version) throw new Error('DOT_CONTROLS_VERSION_MISMATCH');
  return result;
}
async function waitForDotDocument(tabId, deadline=Date.now()+12000) {
  while(Date.now()<deadline) {
    const tab=await chrome.tabs.get(tabId);
    if(!tab)throw new Error('DOT_TAB_CLOSED');
    if(tab.status!=='loading' && !tab.discarded) {
      if(!tab.url?.startsWith('https://chatgpt.com/dots/'))throw new Error('DOT_TAB_CHANGED');
      return;
    }
    await sleep(100);
  }
  throw new Error('DOT_TAB_LOAD_TIMEOUT');
}
function browserErrorCode(error) {
  const message=error?.message||'';
  if(/^[A-Z_]{1,60}$/.test(message))return message;
  if(/No tab with id|Invalid tab ID/i.test(message))return 'DOT_TAB_CLOSED';
  if(/No window with id/i.test(message))return 'DOT_WINDOW_CLOSED';
  if(/Cannot access|host permission/i.test(message))return 'DOT_TAB_ACCESS_DENIED';
  if(/Receiving end does not exist|message port closed|Frame.*removed/i.test(message))return 'DOT_CONTROLS_UNAVAILABLE';
  return 'BROWSER_COMMAND_FAILED';
}
async function refreshTabIfNeeded(tabId) {
  const state=await ensureControls(tabId);
  // Never discard an active call or unsent draft during an extension upgrade.
  if(state.active || state.has_draft || !state.ready) return false;
  try {await microphone(tabId,'status','');}
  catch(error) {
    if(error.message!=='DOT_MICROPHONE_RELOAD_REQUIRED') throw error;
    await chrome.tabs.reload(tabId);
    await chrome.storage.local.set({lastAutomaticRefresh:{tabId,version,at:Date.now()}});
    return true;
  }
  return false;
}
async function refreshDotTabs() {
  for(const tab of await chrome.tabs.query({url:'https://chatgpt.com/dots/*'})) {
    await refreshTabIfNeeded(tab.id).catch(()=>{});
  }
}
// Dot's playback goes to BlackHole 2ch per tab; the Mac's default output is never changed.
const DOT_OUTPUT = {output:'blackhole-2ch'};
// Dot may create its players only once voice connects. Tests shorten this wait.
const outputRouteTimeout = () => globalThis.DOT_OUTPUT_ROUTE_TIMEOUT_MS ?? 12000;
async function owned() { return (await chrome.storage.local.get('owned')).owned; }
async function microphone(tabId, action, jobId, deadline, options) {
  const results = await bounded(chrome.scripting.executeScript({
    target:{tabId}, world:'MAIN',
    func:async (action, jobId, deadline, options) => {
      const route = globalThis.dotPhoneMicrophone;
      if (!route || route.version !== '0.1.9') {
        return action === 'disarm' ? {released:true} : {error:'DOT_MICROPHONE_RELOAD_REQUIRED'};
      }
      try {return await route[action](jobId, deadline, options);}
      catch(error) {return {error:/^[A-Z_]{1,60}$/.test(error.message)
        ? error.message : 'DOT_MICROPHONE_SETUP_FAILED', diagnostics:error.diagnostics};}
    },
    args:[action, jobId, deadline ?? null, options ?? null],
  }));
  const result = results?.[0]?.result;
  if (!result) throw new Error('DOT_MICROPHONE_CHECK_FAILED');
  if (result.error) {
    const error = new Error(result.error);
    error.diagnostics = result.diagnostics;
    throw error;
  }
  return result;
}
async function endOwned(jobId) {
  const value = await owned();
  if (!value || (jobId && value.jobId !== jobId)) return {ended:true};
  canceled.add(value.jobId);
  let hangupConfirmed = false;
  try {
    const tab=await chrome.tabs.get(value.tabId);
    await focusTab(tab);
    await ensureControls(value.tabId);
    await messageTab(value.tabId,'end');
    const deadline=Date.now()+3000;
    while(Date.now()<deadline) {
      if (!(await inspect(value.tabId)).active) break;
      await sleep(100);
    }
    if ((await inspect(value.tabId)).active) return {error:'DOT_HANGUP_NOT_CONFIRMED'};
    hangupConfirmed = true;
  } catch {
    // A closed/navigated tab cannot retain this browser call.
    const tab = await chrome.tabs.get(value.tabId).catch(()=>null);
    if(tab?.url?.startsWith('https://chatgpt.com/dots/')) return {error:'DOT_HANGUP_NOT_CONFIRMED'};
    hangupConfirmed = true;
  } finally {
    // Stop owned tracks even if the UI is unresponsive. Retain ownership on
    // failure so recovery can still confirm browser hangup before restoration.
    const tab = await chrome.tabs.get(value.tabId).catch(()=>null);
    if(tab?.url?.startsWith('https://chatgpt.com/dots/')) {
      await microphone(value.tabId,hangupConfirmed ? 'disarm' : 'suspend',value.jobId);
    }
  }
  await chrome.storage.local.remove('owned');
  return {ended:true,version};
}
async function command(message) {
  if(message.action === 'end') return endOwned(message.job_id);
  if(message.action === 'inspect_owned') {
    const value = await owned();
    if (!value || value.jobId !== message.job_id) return {active:false};
    const state = await inspect(value.tabId).catch(()=>({active:false}));
    return {...state, ...await microphone(value.tabId,'status',value.jobId)};
  }
  if(message.action === 'prepare') {
    if(await owned()) return {error:'DOT_VOICE_ALREADY_OWNED'};
    const tabs = await chrome.tabs.query({url:'https://chatgpt.com/dots/*'});
    const preferred=(await chrome.storage.local.get('preferredTabId')).preferredTabId;
    const selected=tabs.find(t=>t.id===preferred) || tabs.find(t=>t.active)
      || tabs.sort((a,b)=>(b.lastAccessed||0)-(a.lastAccessed||0))[0];
    const tab = (!message.diagnostic_new_tab && selected) || await chrome.tabs.create({url:'https://chatgpt.com/dots/home',active:true});
    const deadline=Date.now()+15000;
    await focusTab(tab);
    await waitForDotDocument(tab.id,deadline);
    await refreshTabIfNeeded(tab.id);
    await waitForDotDocument(tab.id,deadline);
    while(Date.now()<deadline) {
      const result = await ensureControls(tab.id).catch(()=>null);
      if(result?.active) return {error:'DOT_VOICE_ALREADY_ACTIVE'};
      if(result?.ready) {
        await microphone(tab.id,'status','');
        return {ready:true,tab_id:tab.id,version};
      }
      await sleep(250);
    }
    return {error:'DOT_TAB_NOT_READY'};
  }
  if(message.action === 'start') {
    const tab = await chrome.tabs.get(message.tab_id);
    if(!tab.url?.startsWith('https://chatgpt.com/dots/')) return {error:'DOT_TAB_CHANGED'};
    await focusTab(tab);
    if((await ensureControls(tab.id)).active) return {error:'DOT_VOICE_ALREADY_ACTIVE'};
    if(canceled.has(message.job_id)) return {error:'CALL_CANCELED'};
    const leaseDeadline = Date.now()+(message.duration_seconds+100)*1000;
    await chrome.storage.local.set({owned:{jobId:message.job_id,tabId:tab.id,deadline:leaseDeadline}});
    // The MAIN-world hook must have loaded before Dot cached getUserMedia.
    // A newly upgraded extension needs one Dot page reload; do not silently
    // inject late and assume that changing the OS default changed Chrome.
    await microphone(tab.id,'arm',message.job_id,leaseDeadline,DOT_OUTPUT);
    if(canceled.has(message.job_id)) {
      await microphone(tab.id,'disarm',message.job_id);
      return {error:'CALL_CANCELED'};
    }
    const started = await messageTab(tab.id,'start');
    if(started.error) { await endOwned(message.job_id); return started; }
    const deadline=Date.now()+35000;
    let sawActive = false;
    while(Date.now()<deadline) {
      if(canceled.has(message.job_id)) return {error:'CALL_CANCELED'};
      const result = await inspect(tab.id);
      if(result.active) sawActive = true;
      const audio = await microphone(tab.id,'status',message.job_id);
      if(result.connected && audio.microphone_verified) {
        // Gate: Dot's voice must be routed to BlackHole 2ch, or it would play
        // on the speakers and never reach the phone.
        const routeDeadline=Date.now()+outputRouteTimeout();
        let routed=audio;
        while(!routed.output_verified && !routed.output?.errors?.length && Date.now()<routeDeadline) {
          if(canceled.has(message.job_id)) return {error:'CALL_CANCELED'};
          await sleep(250);
          routed=await microphone(tab.id,'status',message.job_id);
        }
        if(!routed.output_verified) return {error:'DOT_OUTPUT_NOT_ROUTED',diagnostics:{output:routed.output??null}};
        return {connected:true,tab_id:tab.id,version,...routed};
      }
      // Start call begins an asynchronous UI/network transition. Absence of
      // Hang up before it first appears is not evidence that voice ended.
      if(!result.active && sawActive) return {error:'DOT_VOICE_ENDED_BEFORE_CONNECT'};
      await sleep(250);
    }
    // The companion receives the failure immediately and then requests cleanup.
    const audio = await microphone(tab.id,'status',message.job_id);
    return {error:!sawActive ? 'DOT_VOICE_START_TIMEOUT'
      : audio.microphone_verified ? 'DOT_VOICE_CONNECT_TIMEOUT' : 'DOT_MICROPHONE_NOT_VERIFIED'};
  }
  return {error:'UNKNOWN_COMMAND'};
}
importScripts('offscreen-control.js','calls.js');
