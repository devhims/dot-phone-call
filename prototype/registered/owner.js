import {OpusTransport} from './transport.js';
const report = {events: [], microphone: 'BlackHole 2ch', phone_requested: false};
let transport, socket, stream, context, stopped = false;
let finish;
const ended = new Promise(resolve => { finish = resolve; });
const show = text => {
  if (text) document.getElementById('status').textContent = text;
  document.getElementById('report').textContent = JSON.stringify(report, null, 2);
};
async function post(path, body={}) {
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.status === 204 ? null : r.json();
}
async function bounded(promise, label, ms=15000) {
  let timer;
  try { return await Promise.race([promise,new Promise((_,reject) => {
    timer=setTimeout(()=>reject(new Error(label + ' timed out')),ms);
  })]); } finally {clearTimeout(timer);}
}
class OwnerTransport extends OpusTransport {
  async prepare() {
    this.outputContext = new AudioContext({sampleRate:48000});
    await this.outputContext.setSinkId({type:'none'});
    await bounded(this.outputContext.resume(),'Audio startup');
  }
  async routeRemote() {
    await super.routeRemote(); this.outputSource.disconnect();
    await this.outputContext.audioWorklet.addModule('/pcm-worklet.js');
    this.worklet = new AudioWorkletNode(this.outputContext,'local-pcm-output');
    this.worklet.port.onmessage = ({data}) => {
      if (socket.readyState === WebSocket.OPEN && socket.bufferedAmount < 48000) socket.send(data);
    };
    this.outputSource.connect(this.worklet).connect(this.outputContext.destination);
    await post('/connected');
    show('Phone connected. Starting Dot…');
  }
  close() {this.worklet?.disconnect();super.close();}
}
async function cleanup() {
  stopped=true;
  transport?.close(); socket?.close();
  stream?.getTracks().forEach(t=>t.stop());
  await transport?.outputClosed;
  if(context?.state!=='closed') await context?.close();
  if(Twilio.Device.audioContext?.state!=='closed') await Twilio.Device.audioContext?.close();
  report.cleanup={tracks_stopped:!stream || stream.getTracks().every(t=>t.readyState==='ended'),
    device_destroyed:!transport || transport.device.state==='destroyed',
    peers_closed:!transport || transport.peers.every(p=>p.connectionState==='closed')};
}
async function run() {
  try {
    if((await navigator.permissions.query({name:'microphone'})).state!=='granted') throw new Error('Microphone permission required');
    const devices=await navigator.mediaDevices.enumerateDevices();
    const device=devices.find(d=>d.kind==='audioinput' && /^BlackHole\s+2ch/.test(d.label));
    if(!device) throw new Error('BlackHole 2ch unavailable');
    const config=await post('/tokens');
    stream=await bounded(navigator.mediaDevices.getUserMedia({audio:{deviceId:{exact:device.deviceId},
      channelCount:1, sampleRate:48000, echoCancellation:false, noiseSuppression:false, autoGainControl:false}}),'BlackHole capture');
    if(stream.getAudioTracks()[0].getSettings().deviceId!==device.deviceId) throw new Error('Wrong microphone');
    context=new AudioContext({sampleRate:48000}); await context.setSinkId({type:'none'}); await bounded(context.resume(),'Capture startup');
    const gain=context.createGain(); gain.gain.value=1.4;
    const destination=context.createMediaStreamDestination();
    context.createMediaStreamSource(stream).connect(gain).connect(destination);
    socket=new WebSocket(location.origin.replace('http:','ws:')+'/audio');
    await bounded(new Promise((resolve,reject)=>{
      socket.onopen=()=>socket.send(config.audio_capability);
      socket.onmessage=({data})=>data==='ready' && resolve();
      socket.onerror=()=>reject(new Error('Local audio connection failed'));
      socket.onclose=()=>reject(new Error('Local audio connection closed'));
    }),'Local audio connection');
    socket.onclose=()=>{if(!stopped){report.helper_disconnected=true;finish();}};
    transport=new OwnerTransport(config.token,{stream:destination.stream,sinkId:null,onEvent:(event,detail)=>{
      report.events.push({event,...detail,at:Date.now()});show();
      if(['disconnected','error','output_error'].includes(event)) finish();
    }});
    await transport.prepare(); await bounded(transport.register(),'Twilio registration');
    report.registered=true;show('Calling your saved number…');
    await post('/start');report.phone_requested=true;show();
    await bounded(ended,'Call duration', (config.duration_seconds+60)*1000);
    report.stats=await transport.stats();
  } catch(error){report.error=error.message;}
  finally {
    await cleanup();show('Call ended. Audio released.');
    await post('/result',report).catch(()=>{});
  }
}
document.getElementById('end').onclick=()=>finish();
window.addEventListener('pagehide',()=>{transport?.close();socket?.close();});
run();
