// One call's BlackHole tracks and PCM playout. Registration has no open microphone.
export class AudioSession {
  constructor({capability,WebSocketClass=WebSocket}) {this.capability=capability;this.Socket=WebSocketClass;this.closed=false;}
  async prepare() {
    if((await navigator.permissions.query({name:'microphone'})).state!=='granted')throw Error('MICROPHONE_PERMISSION_REQUIRED');
    const devices=await navigator.mediaDevices.enumerateDevices();
    const input=devices.find(d=>d.kind==='audioinput'&&/^BlackHole\s+2ch(?:\b|\s)/i.test(d.label));
    if(!input)throw Error('BLACKHOLE_2CH_NOT_FOUND');
    this.stream=await navigator.mediaDevices.getUserMedia({audio:{deviceId:{exact:input.deviceId},sampleRate:48000,channelCount:1,echoCancellation:false,noiseSuppression:false,autoGainControl:false}});
    if(this.closed){this.stream.getTracks().forEach(t=>t.stop());throw Error('CALL_CANCELED');}
    if(this.stream.getAudioTracks()[0].getSettings().deviceId!==input.deviceId)throw Error('WRONG_MICROPHONE');
    this.context=new AudioContext({sampleRate:48000});await this.context.setSinkId({type:'none'});await this.context.resume();
    if(this.context.state!=='running')throw Error('AUDIO_CONTEXT_SUSPENDED');
    // Telephone voice shaping (band limit, bass cut, consonant lift, leveling, limiter).
    await this.context.audioWorklet.addModule(new URL('./phone-voice-worklet.js',import.meta.url).href);
    if(this.closed)throw Error('CALL_CANCELED');
    this.voice=new AudioWorkletNode(this.context,'phone-voice',{numberOfInputs:1,numberOfOutputs:1,outputChannelCount:[1],channelCount:1,channelCountMode:'explicit'});
    this.destination=this.context.createMediaStreamDestination();
    this.context.createMediaStreamSource(this.stream).connect(this.voice).connect(this.destination);
    this.socket=new this.Socket('ws://127.0.0.1:8893/audio');
    await new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>reject(Error('AUDIO_SOCKET_TIMEOUT')),5000);
      this.socket.onopen=()=>this.socket.send(this.capability);
      this.socket.onmessage=({data})=>{if(data==='ready'){clearTimeout(timer);resolve();}};
      this.socket.onerror=()=>{clearTimeout(timer);reject(Error('AUDIO_SOCKET_FAILED'));};
      this.socket.onclose=()=>{clearTimeout(timer);reject(Error('AUDIO_SOCKET_CLOSED'));};
    });
    await this.context.audioWorklet.addModule(new URL('./pcm-worklet.js',import.meta.url).href);
    if(this.closed)throw Error('CALL_CANCELED');
  }
  async remote(call) {
    for(let n=0;n<50&&!call.getRemoteStream();n++){if(this.closed)throw Error('CALL_CANCELED');await new Promise(r=>setTimeout(r,100));}
    if(this.closed||!call.getRemoteStream())throw Error('REMOTE_AUDIO_UNAVAILABLE');
    this.source=this.context.createMediaStreamSource(call.getRemoteStream());
    this.worklet=new AudioWorkletNode(this.context,'local-pcm-output');
    this.worklet.port.onmessage=({data})=>{if(this.socket.readyState===1&&this.socket.bufferedAmount<48000)this.socket.send(data);};
    this.source.connect(this.worklet).connect(this.context.destination);
  }
  async close() {
    this.closed=true;this.source?.disconnect();this.worklet?.disconnect();this.voice?.disconnect();this.socket?.close();
    for(const stream of [this.stream,this.destination?.stream])stream?.getTracks().forEach(t=>t.stop());
    if(this.context&&this.context.state!=='closed')await this.context.close();
  }
}
