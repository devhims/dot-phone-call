import {OpusTransport} from './transport.js';
const report = {verified: false, phone_dialed: false, events: [],
  initial_visibility: document.visibilityState,
  startup_activation: {active: navigator.userActivation.isActive, ever_active: navigator.userActivation.hasBeenActive}};
const resources = {contexts: [], streams: [], transports: [], sockets: []};
const started = performance.now();
const sleep = ms => new Promise(r => setTimeout(r, ms));
let stopped = false;
function show(stage) {
  if (stage) document.getElementById('status').textContent = stage;
  document.getElementById('report').textContent = JSON.stringify(report, null, 2);
}
async function post(path, body = {}) {
  const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.status === 204 ? null : response.json();
}
async function stage(name) {
  report.stage = name; show(name); await post('/progress', {stage: name});
}
async function bound(promise, label, ms = 15000) {
  let timer;
  try { return await Promise.race([promise, new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} timed out`)), ms);
  })]); } finally { clearTimeout(timer); }
}
async function waitFor(test, label, ms = 20000) {
  const end = performance.now() + ms;
  while (!test()) {
    if (stopped || performance.now() > end) throw new Error(`${label} timed out`);
    await sleep(100);
  }
}
async function context(sink) {
  const ctx = new AudioContext({sampleRate: 48000}); resources.contexts.push(ctx);
  await ctx.setSinkId(sink ?? {type: 'none'});
  await bound(ctx.resume(), 'AudioContext resume', 5000);
  return ctx;
}
function tone(ctx, frequency, destination) {
  const oscillator = ctx.createOscillator(), gain = ctx.createGain();
  oscillator.frequency.value = frequency; gain.gain.value = .06;
  oscillator.connect(gain).connect(destination); oscillator.start();
}
function analyser(ctx, stream) {
  const analyser = ctx.createAnalyser(); analyser.fftSize = 8192; analyser.smoothingTimeConstant = 0;
  ctx.createMediaStreamSource(stream).connect(analyser);
  const mute = ctx.createGain(); mute.gain.value = 0; analyser.connect(mute).connect(ctx.destination);
  return analyser;
}
function level(node, hz) {
  const bins = new Float32Array(node.frequencyBinCount); node.getFloatFrequencyData(bins);
  const index = Math.round(hz / 48000 * node.fftSize);
  const value = Math.max(...bins.slice(index - 2, index + 3));
  return Number.isFinite(value) ? Number(value.toFixed(1)) : -200;
}
async function capture(id) {
  const stream = await bound(navigator.mediaDevices.getUserMedia({audio: {deviceId: {exact: id},
    channelCount: 1, sampleRate: 48000, echoCancellation: false, noiseSuppression: false, autoGainControl: false}}), 'BlackHole capture');
  resources.streams.push(stream);
  if (stream.getAudioTracks()[0].getSettings().deviceId !== id) throw new Error('Wrong capture device');
  return stream;
}
// The remote test client must not feed received audio back into the source's
// BlackHole device. Use a silent sink while measuring its received stream.
class SilentTransport extends OpusTransport {
  async prepare() { this.outputContext = await context(); }
}
class NativeOutputTransport extends SilentTransport {
  async routeRemote() {
    await super.routeRemote();
    this.outputSource.disconnect();
    const socket = this.nativeSocket;
    await this.outputContext.audioWorklet.addModule('/pcm-worklet.js');
    this.worklet = new AudioWorkletNode(this.outputContext, 'local-pcm-output');
    this.worklet.port.onmessage = ({data}) => {
      if (socket.readyState === WebSocket.OPEN && socket.bufferedAmount < 48000 * 4 / 4) socket.send(data);
    };
    this.outputSource.connect(this.worklet).connect(this.outputContext.destination);
  }
  close() { this.worklet?.disconnect(); super.close(); }
}
async function cleanup() {
  stopped = true;
  resources.transports.forEach(t => t.close());
  resources.sockets.forEach(s => s.close());
  await Promise.all(resources.transports.map(t => t.outputClosed));
  resources.streams.forEach(s => s.getTracks().forEach(t => t.stop()));
  await Promise.all(resources.contexts.filter(c => c.state !== 'closed').map(c => c.close()));
  if (Twilio.Device.audioContext?.state !== 'closed') await Twilio.Device.audioContext?.close();
  report.cleanup = {
    tracks_stopped: [...resources.streams, ...resources.transports.flatMap(t => t.streams)]
      .every(s => s.getTracks().every(t => t.readyState === 'ended')),
    contexts_closed: [...resources.contexts, ...resources.transports.map(t => t.outputContext).filter(Boolean)]
      .every(c => c.state === 'closed'),
    peers_closed: resources.transports.flatMap(t => t.peers).every(p => p.connectionState === 'closed'),
    devices_destroyed: resources.transports.every(t => t.device.state === 'destroyed')};
}
async function run() {
  try {
    await stage('Checking existing microphone permission');
    report.microphone_permission = (await navigator.permissions.query({name: 'microphone'})).state;
    // An unattended test must not manufacture a click or accept a new prompt.
    if (report.microphone_permission !== 'granted') throw new Error('Microphone permission is not already granted');
    const devices = await navigator.mediaDevices.enumerateDevices();
    function find(kind, channels) {
      const d = devices.find(d => d.kind === kind && new RegExp(`^BlackHole\\s+${channels}ch`).test(d.label));
      if (!d) throw new Error(`Missing BlackHole ${channels}ch ${kind}`);
      return d.deviceId;
    }
    const config = await post('/tokens'), tokens = config.tokens;
    const input2 = await capture(find('audioinput', 2));
    await stage('Starting audio without user activation');
    const ctx = await context();
    const dotOutput = await context(find('audiooutput', 2));
    report.source_fixture = config.native_audio || new URLSearchParams(location.search).get('source') === 'native' ? 'native' : 'browser';
    report.playout = config.native_audio ? 'local_python' : 'chrome';
    if (report.source_fixture === 'browser') tone(dotOutput, 997, dotOutput.destination);
    const source = analyser(ctx, input2);
    await sleep(2000);
    report.local_preflight = {single_input997: level(source, 997),
      source_context_state: dotOutput.state, source_context_time: dotOutput.currentTime,
      analysis_context_state: ctx.state, analysis_context_time: ctx.currentTime,
      source_sink_matches: dotOutput.sinkId === find('audiooutput', 2),
      capture2_muted: input2.getAudioTracks()[0].muted,
      capture2_state: input2.getAudioTracks()[0].readyState};
    const input16 = await capture(find('audioinput', 16));
    await sleep(2000);
    report.local_preflight.two_inputs997 = level(source, 997);
    report.local_preflight.capture2_muted_after_second = input2.getAudioTracks()[0].muted;
    report.local_preflight.capture16_muted = input16.getAudioTracks()[0].muted;
    show();
    if (report.local_preflight.two_inputs997 < -65) throw new Error('Local BlackHole loopback is silent before Twilio');
    const remoteSource = ctx.createMediaStreamDestination(); resources.streams.push(remoteSource.stream);
    tone(ctx, 733, remoteSource);
    const event = side => (name, detail) => {
      report.events.push({side, event: name, ...detail, ms: Math.round(performance.now() - started)}); show();
    };
    const LocalTransport = config.native_audio ? NativeOutputTransport : OpusTransport;
    const local = new LocalTransport(tokens[0], {stream: input2, sinkId: find('audiooutput', 16), onEvent: event('local')});
    const remote = new SilentTransport(tokens[1], {stream: remoteSource.stream, sinkId: null, onEvent: event('remote')});
    resources.transports.push(local, remote);
    if (config.native_audio) {
      const socket = new WebSocket(location.origin.replace('http:', 'ws:') + '/audio');
      resources.sockets.push(socket);
      await bound(new Promise((resolve, reject) => {
        socket.onopen = () => socket.send(config.audio_capability);
        socket.onmessage = ({data}) => data === 'ready' && resolve();
        socket.onerror = () => reject(new Error('Local audio socket failed'));
        socket.onclose = () => reject(new Error('Local audio socket closed before ready'));
      }), 'Local audio socket');
      socket.onclose = () => {
        if (!stopped) {
          report.helper_disconnected = true; stopped = true;
          resources.transports.forEach(t => t.close());
        }
      };
      local.nativeSocket = socket;
    }
    await bound(Promise.all([local.prepare(), remote.prepare()]), 'Output preparation');
    await stage('Registering both clients without user activation');
    await bound(Promise.all([local.register(), remote.register()]), 'Client registration');
    report.registration_ms = Math.round(performance.now() - started);
    report.registered = resources.transports.every(t => t.device.state === 'registered');
    // Exercise token replacement plus signaling re-registration before calling.
    const refreshed = (await post('/refresh')).tokens;
    resources.transports.forEach((t, i) => t.device.updateToken(refreshed[i]));
    await remote.device.unregister();
    await bound(remote.register(), 'Re-registration');
    report.reregistered = remote.device.state === 'registered';
    await stage('Connecting only temporary clients with inline call instructions');
    await post('/start');
    await waitFor(() => resources.transports.every(t => t.call?.status() === 'open' && t.outputReady), 'Call connection');
    await Promise.all(resources.transports.map(t => t.outputReady));
    report.connected_ms = Math.round(performance.now() - started);
    const forward = analyser(ctx, remote.call.getRemoteStream());
    const reverse = analyser(ctx, input16);
    const directReverse = analyser(ctx, local.call.getRemoteStream());
    await stage('Measuring both audio directions through BlackHole');
    report.samples = [];
    for (let i = 0; i < 4; i++) {
      await sleep(2000);
      if (stopped) throw new Error('Local audio helper disconnected during the test');
      report.samples.push({source997: level(source, 997), received997: level(forward, 997),
        remote733: level(directReverse, 733), blackhole16_733: level(reverse, 733)});
    }
    report.local_stats = await local.stats(); report.remote_stats = await remote.stats();
    report.activation_at_end = {active: navigator.userActivation.isActive, ever_active: navigator.userActivation.hasBeenActive};
    report.audio_both_ways = report.samples.every(s => Object.values(s).every(db => db > -65));
    report.opus_both_ways = [report.local_stats, report.remote_stats].every(stats =>
      ['inbound-rtp', 'outbound-rtp'].every(d => stats.some(s => s.direction === d && s.codec?.toLowerCase() === 'audio/opus' && s.packets > 0)));
    await stage('Checking remote hangup propagation');
    remote.call.disconnect();
    await waitFor(() => local.call.status() === 'closed', 'Remote hangup', 10000);
    report.remote_hangup = true;
    report.verified = report.audio_both_ways && report.opus_both_ways && report.registered && report.reregistered &&
      !report.startup_activation.ever_active && !report.activation_at_end.ever_active;
  } catch (error) { report.error = error.message; }
  finally {
    try { await cleanup(); } catch (error) { report.cleanup_error = error.message; report.verified = false; }
    report.elapsed_ms = Math.round(performance.now() - started);
    report.verified &&= Object.values(report.cleanup ?? {}).every(Boolean);
    show(report.verified ? 'Passed without a click. Both audio directions and hangup verified.' : 'Test failed. See report.');
    await post('/result', report);
  }
}
window.addEventListener('pagehide', () => resources.transports.forEach(t => t.close()));
if (new URLSearchParams(location.search).get('manual') === '1') {
  show('Interactive control only. This does not prove unattended startup.');
  const button = document.createElement('button'); button.textContent = 'Run interactive control';
  button.onclick = () => {
    report.startup_activation = {active: navigator.userActivation.isActive, ever_active: navigator.userActivation.hasBeenActive};
    button.disabled = true; run();
  };
  document.body.append(button);
} else run();
