// A packaged hidden page owns WebRTC. Only the paired worker creates it.
let creatingHiddenBridge=null;
let microphoneSetupShown=false;
async function hiddenBridgeStatus() {
  return bounded(chrome.runtime.sendMessage({target:'dot-phone-offscreen',action:'status'}),3000);
}
async function ensureHiddenBridge(session) {
  if(creatingHiddenBridge)return creatingHiddenBridge;
  creatingHiddenBridge=(async()=>{
    const base=chrome.runtime.getURL('offscreen.html');
    const url=base+'#'+session.url.split('#')[1];
    const contexts=await chrome.runtime.getContexts({contextTypes:['OFFSCREEN_DOCUMENT']});
    const current=contexts.find(c=>c.documentUrl?.startsWith(base));
    if(current?.documentUrl!==url) {
      if(session.busy)throw Error('BRIDGE_MIGRATION_WAITING_FOR_CALL');
      if(current)await chrome.offscreen.closeDocument();
      const oldTabs=await chrome.tabs.query({url:DOT_PHONE_CONFIG.origin+'/bridge/*'});
      // Close only the known bridge, never Dot or unrelated tabs. Its old
      // registration lease expires before the hidden page takes ownership.
      if(oldTabs.length)await chrome.tabs.remove(oldTabs.map(t=>t.id));
      await chrome.offscreen.createDocument({url,reasons:['USER_MEDIA','WEB_RTC'],
        justification:'Maintain the phone connection and route Dot call audio without a visible browser tab.'});
    }
    const status=await hiddenBridgeStatus();
    await chrome.storage.local.set({hiddenBridgeStatus:{...status,at:Date.now()}});
    if(status?.microphone_ready===false&&!microphoneSetupShown) {
      microphoneSetupShown=true;
      await chrome.runtime.openOptionsPage();
    }
    return status;
  })();
  try{return await creatingHiddenBridge;}finally{creatingHiddenBridge=null;}
}
