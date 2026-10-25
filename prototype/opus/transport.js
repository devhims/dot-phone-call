// Uses public Twilio SDK APIs. No 8 kHz PCM or mu-law conversion in this path.
export class OpusTransport {
  constructor(token, {stream, sinkId, diagnostic = false, bitrate = 40000, onEvent = () => {}}) {
    this.peers = [];
    this.streams = [];
    this.packetStats = {};
    if (diagnostic) {
      if (typeof RTCRtpScriptTransform === 'undefined') throw new Error('Encoded audio diagnostics unsupported');
      this.packetWorker = new Worker('/packet-probe.js');
      this.packetWorker.onmessage = ({data}) => { this.packetStats[data.direction || 'error'] = data; };
    }
    const observe = (endpoint, direction) => {
      if (this.packetWorker && !endpoint.transform) {
        endpoint.transform = new RTCRtpScriptTransform(this.packetWorker, {direction});
      }
    };
    const peers = this.peers;
    class ObservedPeer extends RTCPeerConnection {
      constructor(...args) {
        super(...args); peers.push(this);
        this.addEventListener('track', e => observe(e.receiver, 'inbound'));
      }
      addTrack(...args) {
        const sender = super.addTrack(...args); observe(sender, 'outbound');
        this.getReceivers().forEach(receiver => observe(receiver, 'inbound'));
        return sender;
      }
    }
    this.device = new Twilio.Device(token, {
      codecPreferences: ['opus'], maxAverageBitrate: bitrate,
      edge: 'singapore', logLevel: 'error',
      getUserMedia: async () => {
        const clone = stream.clone(); this.streams.push(clone); return clone;
      },
      RTCPeerConnection: ObservedPeer,
      appName: 'dot-opus-prototype', appVersion: '0.1.0',
    });
    this.device.audio.incoming(false);
    this.device.audio.outgoing(false);
    this.device.audio.disconnect(false);
    this.sinkId = sinkId;
    this.onEvent = onEvent;
    this.device.on('error', error => onEvent('error', {code: error.code, message: error.message}));
    this.device.on('incoming', call => {
      this.track(call);
      call.accept({rtcConstraints: {audio: {echoCancellation: false,
        noiseSuppression: false, autoGainControl: false}}});
    });
  }
  async prepare() {
    // Enumeration in AudioHelper is asynchronous. Poll only the known sink.
    for (let i = 0; i < 50 && !this.device.audio.availableOutputDevices.has(this.sinkId); i++) {
      if (this.closed) throw new Error('Transport stopped');
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    await this.device.audio.speakerDevices.set(this.sinkId);
    if (this.closed) throw new Error('Transport stopped');
    this.outputContext = new AudioContext({sampleRate: 48000});
    await this.outputContext.setSinkId(this.sinkId);
    await this.outputContext.resume();
    if (this.closed) throw new Error('Transport stopped');
  }
  track(call) {
    this.call = call;
    this.outputs = [];
    call.on('audio', audio => {
      // Keep SDK playout alive for WebRTC decoding, but route audible output
      // ourselves. This avoids the SDK's silent secondary-sink path on this Mac.
      audio.muted = true; this.outputs.push(audio);
    });
    call.on('accept', () => {
      this.outputReady = this.routeRemote();
      this.outputReady.then(() => this.onEvent('connected', {codec: call.codec}))
        .catch(error => this.onEvent('output_error', {message: error.message}));
    });
    call.on('disconnect', () => this.onEvent('disconnected'));
    call.on('error', error => this.onEvent('error', {code: error.code, message: error.message}));
    call.on('warning', warning => this.onEvent('warning', {warning}));
    return call;
  }
  async register() {
    if (this.closed) throw new Error('Transport stopped');
    await this.device.register();
  }
  async connect(params) {
    if (this.closed) throw new Error('Transport stopped');
    return this.track(await this.device.connect({params}));
  }
  async routeRemote() {
    for (let i = 0; i < 50 && !this.call.getRemoteStream(); i++) {
      if (this.closed) throw new Error('Transport stopped');
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    if (this.closed || !this.call.getRemoteStream()) throw new Error('Remote audio unavailable');
    this.outputSource = this.outputContext.createMediaStreamSource(this.call.getRemoteStream());
    this.outputSource.connect(this.outputContext.destination);
  }
  async stats() {
    const result = [];
    for (const peer of this.peers) {
      const report = await peer.getStats();
      for (const stat of report.values()) {
        if (!['inbound-rtp', 'outbound-rtp'].includes(stat.type) || stat.kind !== 'audio') continue;
        const codec = report.get(stat.codecId);
        result.push({direction: stat.type, codec: codec?.mimeType, clockRate: codec?.clockRate,
          packets: stat.packetsReceived ?? stat.packetsSent, lost: stat.packetsLost,
          jitter: stat.jitter, bytes: stat.bytesReceived ?? stat.bytesSent,
          concealedSamples: stat.concealedSamples, totalSamples: stat.totalSamplesReceived});
      }
    }
    return result;
  }
  diagnostics() {
    // Only codec parameters are retained. SDP also contains network credentials.
    const formats = description => description?.sdp.split(/\r?\n/)
      .filter(line => /^a=(rtpmap|fmtp|ptime|maxptime):/.test(line));
    return {encoded: this.packetStats,
      output: {expected_sink: this.outputContext?.sinkId === this.sinkId,
      state: this.outputContext?.state, sampleRate: this.outputContext?.sampleRate},
      sdp: this.peers.map(p => ({local: formats(p.localDescription), remote: formats(p.remoteDescription)})),
      sinks: this.outputs.map(audio => ({expected: audio.sinkId === this.sinkId,
        paused: audio.paused, muted: audio.muted, volume: audio.volume})),
      input: this.peers.flatMap(p => p.getSenders().map(s => s.track?.getSettings()))};
  }
  close() {
    if (this.closed) return;
    this.closed = true;
    this.call?.disconnect();
    this.device.disconnectAll();
    this.device.destroy();
    this.streams.forEach(s => s.getTracks().forEach(t => t.stop()));
    this.outputSource?.disconnect();
    this.outputClosed = this.outputContext?.close();
    this.packetWorker?.terminate();
  }
}
