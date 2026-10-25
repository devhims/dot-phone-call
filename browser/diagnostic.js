import {AudioSession} from './session.js';
// Local Opus loopback. No Twilio Device.connect(), phone API, or Dot voice.
export async function testHiddenAudio(capability) {
 if(!/^[a-zA-Z0-9_-]{32,100}$/.test(capability||''))throw Error('INVALID_AUDIO_CAPABILITY');
 const audio=new AudioSession({capability}),a=new RTCPeerConnection({iceServers:[]}),b=new RTCPeerConnection({iceServers:[]});
 const pendingA=[],pendingB=[];
 a.onicecandidate=e=>{if(e.candidate){if(b.remoteDescription)void b.addIceCandidate(e.candidate);else pendingB.push(e.candidate);}};
 b.onicecandidate=e=>{if(e.candidate){if(a.remoteDescription)void a.addIceCandidate(e.candidate);else pendingA.push(e.candidate);}};
 let timer,playback;
 try {
  await audio.prepare();
  const remote=new Promise((resolve,reject)=>{b.ontrack=e=>resolve(e.streams[0]);timer=setTimeout(()=>reject(Error('LOCAL_WEBRTC_TIMEOUT')),5000);});
  for(const track of audio.destination.stream.getTracks())a.addTrack(track,audio.destination.stream);
  const opus=RTCRtpSender.getCapabilities('audio').codecs.filter(c=>c.mimeType.toLowerCase()==='audio/opus');
  for(const t of a.getTransceivers())t.setCodecPreferences(opus);
  await a.setLocalDescription(await a.createOffer());await b.setRemoteDescription(a.localDescription);
  for(const c of pendingB)await b.addIceCandidate(c);
  await b.setLocalDescription(await b.createAnswer());await a.setRemoteDescription(b.localDescription);
  for(const c of pendingA)await a.addIceCandidate(c);
  const stream=await remote;clearTimeout(timer);
  // Twilio starts a muted HTML audio element as well as exposing the remote
  // stream. Match that decoder/playout lifecycle in the local test.
  playback=new Audio();playback.muted=true;playback.srcObject=stream;await playback.play();
  await audio.remote({getRemoteStream:()=>stream});
  await new Promise(resolve=>setTimeout(resolve,2500));
  const stats=await b.getStats(),sent=await a.getStats();let received=0,codec,receivedEnergy=0,sourceEnergy=0;
  sent.forEach(r=>{if(r.type==='media-source'&&r.kind==='audio')sourceEnergy=r.totalAudioEnergy||0;});
  stats.forEach(report=>{if(report.type==='inbound-rtp'&&report.kind==='audio'){received+=report.packetsReceived||0;receivedEnergy=report.totalAudioEnergy||0;codec=stats.get(report.codecId)?.mimeType;}});
  if(received<20||codec?.toLowerCase()!=='audio/opus')throw Error('LOCAL_OPUS_AUDIO_MISSING');
  return {verified:true,codec,packets_received:received,source_energy:sourceEnergy,received_energy:receivedEnergy,context:audio.context.state,phone_dialed:false};
 } finally {clearTimeout(timer);if(playback){playback.pause();playback.srcObject=null;}a.close();b.close();await audio.close();}
}
