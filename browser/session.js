// One call's BlackHole audio. Registration has no open microphone.
// Dot's voice: BlackHole 2ch -> voice shaping -> Twilio. Phone audio: Twilio ->
// BlackHole 16ch, selected per context, so the Mac's default devices never change.
export class AudioSession {
  constructor() {this.closed=false;}
  async prepare() {
    if((await navigator.permissions.query({name:'microphone'})).state!=='granted')throw Error('MICROPHONE_PERMISSION_REQUIRED');
    const devices=await navigator.mediaDevices.enumerateDevices();
    const input=devices.find(d=>d.kind==='audioinput'&&/^BlackHole\s+2ch(?:\b|\s)/i.test(d.label));
    if(!input)throw Error('BLACKHOLE_2CH_NOT_FOUND');
    const output=devices.find(d=>d.kind==='audiooutput'&&/^BlackHole\s+16ch(?:\b|\s)/i.test(d.label));
    if(!output)throw Error('BLACKHOLE_16CH_OUTPUT_NOT_FOUND');
    // If the Mac's default output is BlackHole, every app's sound would join the call.
    if(devices.some(d=>d.kind==='audiooutput'&&d.deviceId==='default'&&/BlackHole/i.test(d.label)))throw Error('MAC_DEFAULT_OUTPUT_IS_BLACKHOLE');
    this.stream=await navigator.mediaDevices.getUserMedia({audio:{deviceId:{exact:input.deviceId},sampleRate:48000,channelCount:1,echoCancellation:false,noiseSuppression:false,autoGainControl:false}});
    if(this.closed){this.stream.getTracks().forEach(t=>t.stop());throw Error('CALL_CANCELED');}
    if(this.stream.getAudioTracks()[0].getSettings().deviceId!==input.deviceId)throw Error('WRONG_MICROPHONE');
    this.context=new AudioContext({sampleRate:48000});await this.context.setSinkId(output.deviceId);await this.context.resume();
    if(this.context.sinkId!==output.deviceId)throw Error('PHONE_OUTPUT_NOT_SELECTED');
    if(this.context.state!=='running')throw Error('AUDIO_CONTEXT_SUSPENDED');
    // Telephone voice shaping (band limit, bass cut, consonant lift, leveling, limiter).
    await this.context.audioWorklet.addModule(new URL('./phone-voice-worklet.js',import.meta.url).href);
    if(this.closed)throw Error('CALL_CANCELED');
    this.voice=new AudioWorkletNode(this.context,'phone-voice',{numberOfInputs:1,numberOfOutputs:1,outputChannelCount:[1],channelCount:1,channelCountMode:'explicit'});
    this.destination=this.context.createMediaStreamDestination();
    this.context.createMediaStreamSource(this.stream).connect(this.voice).connect(this.destination);
  }
  async remote(call) {
    for(let n=0;n<50&&!call.getRemoteStream();n++){if(this.closed)throw Error('CALL_CANCELED');await new Promise(r=>setTimeout(r,100));}
    if(this.closed||!call.getRemoteStream())throw Error('REMOTE_AUDIO_UNAVAILABLE');
    // WebRTC's own jitter buffer times this playout; the context sink is BlackHole 16ch.
    this.source=this.context.createMediaStreamSource(call.getRemoteStream());
    this.source.connect(this.context.destination);
  }
  async close() {
    this.closed=true;this.source?.disconnect();this.voice?.disconnect();
    for(const stream of [this.stream,this.destination?.stream])stream?.getTracks().forEach(t=>t.stop());
    if(this.context&&this.context.state!=='closed')await this.context.close();
  }
}
