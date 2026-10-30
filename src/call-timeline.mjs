// Reads the call log (see call-log.mjs) and explains a call in plain language:
// whether Dot answered, why the call ended, and where the audio went quiet.
const SILENT_DB=-45;

export function parseLog(text) {
  const events=[];
  for(const line of text.split('\n')) {
    if(!line.trim())continue;
    try {
      const event=JSON.parse(line),at=Date.parse(event.t);
      if(event.ev&&Number.isFinite(at))events.push({...event,at});
    } catch {}
  }
  return events.sort((a,b)=>a.at-b.at);
}

// Bridge events seen before the backend knows the job carry only Twilio SIDs.
// The backend's events link those SIDs to the job, so every event finds its call.
export function groupCalls(events) {
  const bySid=new Map();
  for(const e of events) if(e.call) for(const sid of [e.child,e.parent]) if(sid)bySid.set(sid,e.call);
  const calls=new Map();
  for(const e of events) {
    const call=e.call??bySid.get(e.child)??bySid.get(e.parent);
    if(!call)continue;
    if(!calls.has(call))calls.set(call,[]);
    calls.get(call).push({...e,call});
  }
  return calls;
}

// Service events (registration, restarts) that happened around a call.
export function contextEvents(events,callEvents) {
  if(!callEvents.length)return [];
  const from=callEvents[0].at-60000,to=callEvents.at(-1).at+10000;
  return events.filter(e=>!e.call&&!e.child&&!e.parent&&e.at>=from&&e.at<=to);
}

const first=(events,...names)=>events.find(e=>names.includes(e.ev));
const seconds=ms=>`${Math.round(ms/1000)} s`;
const clock=(ms,start)=>{const s=Math.max(0,Math.round((ms-start)/1000));return `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`;};

const START_FAILURES={
  DOT_VOICE_ENDED_BEFORE_CONNECT:"Dot's voice session opened and then closed before it connected.",
  DOT_VOICE_START_TIMEOUT:'Dot never started a voice session after the extension pressed Start call.',
  DOT_VOICE_CONNECT_TIMEOUT:"Dot's voice session started but did not connect within 35 seconds.",
  DOT_MICROPHONE_NOT_VERIFIED:'Dot started, but never took the phone audio as its microphone.',
  DOT_OUTPUT_NOT_ROUTED:"Dot's voice could not be routed into the call (BlackHole 2ch).",
  DOT_TAB_NOT_READY:'The Dot tab did not show Start call within 15 seconds.',
  DOT_TAB_LOAD_TIMEOUT:'The Dot tab did not finish loading within 15 seconds.',
  DOT_VOICE_ALREADY_ACTIVE:'Dot was already in a voice session in that tab.',
  DOT_VOICE_ALREADY_OWNED:'Another call still owned the Dot tab.',
  DOT_MICROPHONE_RELOAD_REQUIRED:"The Dot tab needed a reload before the extension could route its microphone.",
  REGISTERED_CLIENT_UNAVAILABLE:"Chrome's Twilio client was not registered.",
  REGISTERED_CALL_TIMEOUT:'The call reached its time limit.',
  EXTENSION_RESTARTED:'The extension restarted during the call.',
};
const REJECTIONS={
  INBOUND_AUTHORIZATION_FAILED:'The PIN was rejected.',
  REGISTERED_CLIENT_UNAVAILABLE:"Chrome's Twilio client was not registered when the call arrived.",
  CALL_ENDED:'The phone call had already ended when Chrome was asked to answer.',
  INBOUND_NOT_AVAILABLE:'The backend was not accepting incoming calls (calling disabled, or another call in progress).',
  REGISTERED_CALL_BUSY:'Another call was already in progress.',
};
// The backend's first reason wins: the extension often notices an ending only afterwards.
function endingReason(events) {
  const ending=first(events,'call_ending'),loop=first(events,'call_loop_ended'),dotTool=first(events,'dot_tool_end_call');
  const reason=ending?.reason;
  if(reason==='end_requested'||(!reason&&loop?.reason==='end_requested'))
    return dotTool?'Dot used its hang-up tool.':'A hang-up was requested (Dot, the extension or the menu bar).';
  // Twilio disconnects Chrome's leg when the phone side ends, and also when its time limit
  // for that leg runs out, so the connected time tells the two apart.
  if(reason==='twilio_client_disconnected') {
    const limit=first(events,'call_job_received','dot_tool_start_call','dialing_owner')?.time_limit_s;
    const connected=first(events,'call_disconnected')?.connected_s;
    if(Number.isFinite(limit)&&Number.isFinite(connected)&&connected>=limit-3)
      return `The call reached its time limit of ${limit} seconds${limit===120?' (the default when Dot does not ask for longer)':''}.`;
    return first(events,'call_error','call_reconnecting')
      ? "Chrome's connection to Twilio dropped."
      : 'The call ended from the phone side: you hung up, or the phone network dropped the call.';
  }
  if(reason==='extension_finished'||!reason) {
    if(loop?.reason==='dot_hung_up')return dotTool?'Dot used its hang-up tool.':"Dot's voice session ended (Dot hung up, or the Dot tab's Hang up was pressed).";
    if(loop?.reason==='dot_microphone_lost')return 'Dot lost the phone audio it uses as its microphone, so the extension ended the call.';
    if(loop?.reason==='backend_ended_call')return 'The backend ended the call.';
    if(loop?.code)return (START_FAILURES[loop.code]??`The extension stopped the call with ${loop.code}.`);
    return reason?'The extension finished the call.':null;
  }
  return {
    phone_completed:'The phone leg ended: you hung up, or the phone network dropped the call.',
    phone_canceled:'The phone call was canceled before it connected.',
    'phone_no-answer':'The phone was not answered.',
    phone_busy:'The phone was busy.',
    phone_failed:'Twilio could not complete the phone call.',
    twilio_client_lost:"Chrome's Twilio client stopped reporting as registered, so the backend ended the call.",
    extension_unresponsive:'The extension sent no heartbeat for 15 seconds (Chrome may have paused it), so the backend ended the call.',
    time_limit:'The call reached its time limit.',
    bridge_failed:`Chrome's call audio failed${ending.code?` (${ending.code})`:''}.`,
    backend_stopping:'The Dot Phone backend stopped during the call.',
    backend_recovering:'The Dot Phone backend restarted during the call.',
  }[reason]??`The backend ended the call (${reason}).`;
}

// Runs of consecutive 5-second audio windows that match a condition.
function runs(windows,test,minSeconds) {
  const found=[];let current=null;
  for(const w of windows) {
    if(test(w)){current??={from:w.from,to:w.to,windows:[]};current.to=w.to;current.windows.push(w);}
    else {if(current)found.push(current);current=null;}
  }
  if(current)found.push(current);
  return found.filter(r=>(r.to-r.from)/1000>=minSeconds);
}
export function audioWindows(events) {
  return events.filter(e=>e.ev==='audio'&&Number.isFinite(e.seconds)).map(e=>({...e,from:e.at-e.seconds*1000,to:e.at}));
}
function audioFindings(events,start) {
  const windows=audioWindows(events),findings=[];
  if(!windows.length)return findings;
  const span=r=>`${clock(r.from,start)} to ${clock(r.to,start)}`;
  // Dot's raw level decides: voice shaping lifts background hiss in the sent audio to about -43 dB.
  const dotQuiet=w=>Number.isFinite(w.dot_peak_db)?!(w.dot_active_s>0):!(w.sent_active_s>0);
  for(const r of runs(windows,w=>dotQuiet(w)&&w.caller_active_s>0,15))
    findings.push(`Dot made no sound from ${span(r)} while you were speaking. If Dot's chat shows what you said, Dot heard you but its reply never played; if not, your audio did not reach Dot.`);
  for(const r of runs(windows,w=>w.dot_active_s>0&&!(w.sent_active_s>0),10))
    findings.push(`Dot spoke from ${span(r)}, but nothing was sent to the phone. The fault is between the Mac bridge and Twilio.`);
  for(const r of runs(windows,w=>!(w.caller_active_s>0),30))
    findings.push(`No sound from your phone reached the Mac from ${span(r)}. Check the phone's mute button or the network.`);
  const worst=(key,pick=Math.max)=>{const v=windows.map(w=>w[key]).filter(Number.isFinite);return v.length?pick(...v):null;};
  const loss=worst('loss_pct'),jitter=worst('jitter_ms'),rtt=worst('rtt_ms'),mos=worst('mos_min',Math.min);
  const network=[loss>3&&`packet loss up to ${loss}%`,jitter>30&&`jitter up to ${jitter} ms`,rtt>400&&`round trip up to ${rtt} ms`,mos!==null&&mos<3.5&&`call quality score down to ${mos}`].filter(Boolean);
  if(network.length)findings.push(`Network trouble on the Chrome-Twilio connection: ${network.join(', ')}.`);
  if(windows.some(w=>w.audio_context&&w.audio_context!=='running'))findings.push("Chrome's call audio was paused (its audio context was not running) for part of the call.");
  return findings;
}
function dotStateFindings(events,start) {
  const findings=[];let players=null;
  for(const e of events.filter(e=>e.ev==='dot_state')) {
    // A track that ends with Dot's session is normal cleanup; only a live session matters.
    if(e.dot_active&&(e.microphone_track==='muted'||e.microphone_track==='ended'))
      findings.push(`At ${clock(e.at,start)} Dot's phone-audio microphone track was ${e.microphone_track} while Dot was still in the call, so Dot could not hear you.`);
    if(Number.isFinite(e.dot_players_seen)) {
      if(players!==null&&e.dot_players_seen>players)
        findings.push(`At ${clock(e.at,start)} Dot created another audio player (${e.dot_players_seen} in all). Dot's page usually does this when its voice connection restarts.`);
      players=e.dot_players_seen;
    }
  }
  return [...new Set(findings)];
}

export function diagnose(events,context=[]) {
  const start=events[0]?.at??0,end=events.at(-1)?.at??0;
  // Calls Dot places also ring Chrome as an incoming Twilio leg, so direction comes from the job.
  const direction=first(events,'call_job_received','extension_prepared')?.direction;
  const incoming=direction?direction==='inbound':!first(events,'dot_tool_start_call','dialing_owner')&&!!first(events,'pin_checked','incoming_ringing');
  const answered=first(events,'dot_connected');
  const findings=[];let outcome;
  if(answered) {
    const ended=endingReason(events);
    outcome=`Dot answered after ${seconds(answered.at-start)} and the call lasted ${seconds(end-answered.at)}.`;
    if(ended)findings.push(`How it ended: ${ended}`);
    const phone=first(events,'phone_leg_ended');
    if(phone?.duration_s)findings.push(`Twilio recorded the phone leg as ${phone.status}, ${phone.duration_s} s long.`);
    findings.push(...dotStateFindings(events,start),...audioFindings(events,start));
    for(const w of events.filter(e=>e.ev==='call_warning'))findings.push(`Twilio warned "${w.name}" at ${clock(w.at,start)}.`);
    for(const e of events.filter(e=>e.ev==='call_error'))findings.push(`Twilio reported error ${e.code??e.name} at ${clock(e.at,start)}.`);
    if(!audioWindows(events).length)findings.push('No audio levels were recorded for this call.');
  } else {
    outcome='Dot did not answer.';
    const rejected=first(events,'incoming_rejected'),canceled=first(events,'incoming_canceled'),failed=first(events,'call_failed');
    const bridge=first(events,'bridge_failure'),prepared=first(events,'dot_tab_prepared');
    if(rejected)findings.push(REJECTIONS[rejected.code]??`Chrome refused the incoming call (${rejected.code}).`);
    else if(failed) {
      findings.push(START_FAILURES[failed.code]??`The extension failed with ${failed.code}.`);
      const age=failed.page_age_ms??prepared?.page_age_ms;
      if(Number.isFinite(age)&&age<30000)findings.push(`The Dot tab had loaded only ${seconds(age)} before Start call. A page that new may not be ready to hold a voice session.`);
      if(prepared?.tab_created)findings.push('There was no Dot tab, so the extension opened one for this call.');
      if(prepared?.tab_reloaded)findings.push('The extension reloaded the Dot tab just before this call.');
      if(failed.active_after_ms!=null)findings.push(`Dot's voice controls appeared ${seconds(failed.active_after_ms)} after Start call; the extension gave up after ${seconds(failed.waited_ms)}.`);
      if(failed.microphone_error)findings.push(`Microphone routing reported ${failed.microphone_error}.`);
    }
    else if(bridge)findings.push(`Chrome's call audio failed to start (${bridge.code}).`);
    else if(canceled&&!first(events,'call_accepted'))findings.push('The ringing stopped before Chrome answered: you hung up, or Twilio stopped ringing Chrome.');
    else if(!events.some(e=>e.src==='extension'||e.src==='bridge'))findings.push('Nothing from Chrome was logged for this call. Check that Chrome is running with the Dot Phone extension enabled.');
    const ended=first(events,'call_ending');
    if(ended&&ended.reason!=='extension_finished')findings.push(`Then: ${endingReason(events)}`);
  }
  for(const e of context) {
    if(e.ev==='twilio_client_unregistered'||e.ev==='twilio_unregistered')findings.push(`Chrome's Twilio client unregistered at ${clock(e.at,start)}.`);
    if(e.ev==='backend_started')findings.push(`The backend started at ${new Date(e.at).toLocaleTimeString()}, shortly before or during this call.`);
    if(e.ev==='extension_started')findings.push(`The extension restarted at ${new Date(e.at).toLocaleTimeString()}, shortly before or during this call.`);
  }
  return {incoming,answered:!!answered,outcome,findings,start,end};
}

const SKIP=new Set(['t','at','src','ev','call']);
export function formatEvent(e,start) {
  const fields=Object.entries(e).filter(([k,v])=>!SKIP.has(k)&&v!==null).map(([k,v])=>`${k}=${v}`).join(' ');
  const offset=`+${((e.at-start)/1000).toFixed(1)}s`;
  return `${e.t.slice(11,23)}  ${offset.padStart(7)}  ${(e.src??'').padEnd(9)}  ${e.ev}${fields?'  '+fields:''}`;
}
export function formatAudio(events,start) {
  const windows=audioWindows(events);
  if(!windows.length)return [];
  const cell=(v,w=5)=>String(Number.isFinite(v)?Math.round(v):v??'-').padStart(w);
  return ['   time    you dB  dot dB  sent dB  rtt ms  loss %',
    ...windows.map(w=>`  ${clock(w.to,start).padStart(5)}   ${cell(w.caller_peak_db,6)}  ${cell(w.dot_peak_db,6)}  ${cell(w.sent_peak_db,7)}  ${cell(w.rtt_ms,6)}  ${cell(w.loss_pct,6)}`)];
}
