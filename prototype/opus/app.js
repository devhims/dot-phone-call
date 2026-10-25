import {OpusTransport} from './transport.js';
import {assessProbe} from './verdict.js';

const report = {phone_dialed: false, verified: false, events: []};
const resources = {streams: [], contexts: [], transports: [], peers: [], audio: []};
const el = id => document.getElementById(id);
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
let stopped = false;
const assertRunning = () => { if (stopped) throw new Error('Audio test stopped'); };
const show = status => {
  if (status) { el('status').textContent = status; report.stage = status; }
  el('report').textContent = JSON.stringify(report,
    (key, value) => key === 'packets' && Array.isArray(value) ? `${value.length} generated-audio packets saved locally` : value, 2);
  if (report.caller_to_receiver && report.reference) {
    el('summary').textContent = `6 kHz tone: local Opus ${report.reference.local_opus[6000]} dB; Twilio ${report.caller_to_receiver[6000]} dB. Phone dialed: ${report.phone_dialed}.`;
  }
};
const event = (side, name, detail = {}) => {
  report.events.push({side, event: name, ...detail}); show();
};
async function post(path, body = {}) {
  const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)});
  if (!response.ok) throw new Error(`${path}: ${response.status} ${await response.text()}`);
  return response.status === 204 ? null : response.json();
}
async function context(sinkId) {
  assertRunning();
  const ctx = new AudioContext({sampleRate: 48000});
  resources.contexts.push(ctx);
  await ctx.setSinkId(sinkId ?? {type: 'none'});
  await ctx.resume();
  if (stopped) { await ctx.close(); assertRunning(); }
  return ctx;
}
function tone(ctx, frequencies, destination) {
  for (const frequency of frequencies) {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain(); gain.gain.value = .06;
    osc.frequency.value = frequency; osc.connect(gain).connect(destination); osc.start();
  }
}
function analyser(ctx, stream) {
  const node = ctx.createAnalyser(); node.fftSize = 8192; node.smoothingTimeConstant = 0;
  const source = ctx.createMediaStreamSource(stream); source.connect(node);
  // A silent destination keeps the graph processing without audible playback.
  const mute = ctx.createGain(); mute.gain.value = 0; node.connect(mute).connect(ctx.destination);
  return node;
}
function measure(node, frequencies, rate) {
  const bins = new Float32Array(node.frequencyBinCount); node.getFloatFrequencyData(bins);
  return Object.fromEntries(frequencies.map(f => {
    const index = Math.round(f / rate * node.fftSize);
    const db = Math.max(...bins.slice(index - 2, index + 3));
    return [f, Number.isFinite(db) ? Number(db.toFixed(1)) : -200];
  }));
}
async function virtualDevices() {
  let devices = await navigator.mediaDevices.enumerateDevices();
  if (!devices.some(d => /BlackHole/.test(d.label))) {
    const permission = await navigator.mediaDevices.getUserMedia({audio: true});
    permission.getTracks().forEach(track => track.stop());
    devices = await navigator.mediaDevices.enumerateDevices();
  }
  const find = (kind, channels) => {
    const device = devices.find(d => d.kind === kind && new RegExp(`^BlackHole\\s+${channels}ch`).test(d.label));
    if (!device) throw new Error(`BlackHole ${channels}ch ${kind} unavailable`);
    return device.deviceId;
  };
  return {input2: find('audioinput', 2), output2: find('audiooutput', 2),
    input16: find('audioinput', 16), output16: find('audiooutput', 16)};
}
async function capture(id) {
  assertRunning();
  const stream = await navigator.mediaDevices.getUserMedia({audio: {deviceId: {exact: id},
    channelCount: 1, sampleRate: 48000, echoCancellation: false,
    noiseSuppression: false, autoGainControl: false}});
  resources.streams.push(stream);
  if (stopped) { stream.getTracks().forEach(t => t.stop()); assertRunning(); }
  if (stream.getAudioTracks()[0].getSettings().deviceId !== id) throw new Error('Wrong microphone');
  return stream;
}
async function cleanup() {
  stopped = true;
  resources.transports.forEach(t => t.close());
  await Promise.all(resources.transports.map(t => t.outputClosed));
  resources.peers.forEach(p => p.close());
  resources.audio.forEach(a => { a.pause(); a.srcObject = null; });
  resources.streams.forEach(s => s.getTracks().forEach(t => t.stop()));
  await Promise.all(resources.contexts.filter(c => c.state !== 'closed').map(c => c.close()));
  if (Twilio.Device.audioContext?.state !== 'closed') await Twilio.Device.audioContext?.close();
  report.cleanup = {tracks_stopped: [...resources.streams, ...resources.transports.flatMap(t => t.streams)]
      .every(s => s.getTracks().every(t => t.readyState === 'ended')),
    contexts_closed: [...resources.contexts, ...resources.transports.map(t => t.outputContext).filter(Boolean)]
      .every(c => c.state === 'closed'),
    sdk_context_closed: !Twilio.Device.audioContext || Twilio.Device.audioContext.state === 'closed',
    peers_closed: [...resources.peers, ...resources.transports.flatMap(t => t.peers)]
      .every(p => p.connectionState === 'closed')};
  show();
}
async function localReference(ctx, source, frequencies, sinkId) {
  const left = new RTCPeerConnection(), right = new RTCPeerConnection();
  resources.peers.push(left, right);
  left.onicecandidate = e => e.candidate && right.addIceCandidate(e.candidate);
  right.onicecandidate = e => e.candidate && left.addIceCandidate(e.candidate);
  let received;
  right.ontrack = e => { received = e.streams[0]; };
  source.getTracks().forEach(track => left.addTrack(track, source));
  const codecs = RTCRtpSender.getCapabilities('audio').codecs.filter(c => c.mimeType.toLowerCase() === 'audio/opus');
  left.getTransceivers().forEach(t => t.setCodecPreferences(codecs));
  await left.setLocalDescription(await left.createOffer());
  await right.setRemoteDescription(left.localDescription);
  await right.setLocalDescription(await right.createAnswer());
  await left.setRemoteDescription(right.localDescription);
  await waitFor(() => received && left.connectionState === 'connected');
  // Chrome starts remote WebRTC playout through an HTML media element.
  const audio = new Audio(); resources.audio.push(audio);
  audio.srcObject = received; await audio.setSinkId(sinkId); await audio.play();
  const direct = analyser(ctx, source), encoded = analyser(ctx, received);
  await sleep(2000);
  const result = {source: measure(direct, frequencies, ctx.sampleRate),
    local_opus: measure(encoded, frequencies, ctx.sampleRate)};
  left.close(); right.close();
  return result;
}
async function waitFor(test, timeout = 20000) {
  const end = Date.now() + timeout;
  while (!test()) { if (stopped || Date.now() > end) throw new Error('Connection timed out or stopped'); await sleep(100); }
}

async function run(mode) {
  el('local').disabled = el('route').disabled = el('probe').disabled = el('owner').disabled = true;
  stopped = false;
  const bitrate = Number(el('bitrate').value);
  report.bitrate = bitrate;
  try {
    show('Preparing virtual devices');
    const devices = await virtualDevices();
    assertRunning();
    const tokens = await post('/tokens', {mode});
    assertRunning();
    const callerInput = await capture(devices.input2);
    if (mode === 'owner') {
      const caller = new OpusTransport(tokens.caller, {stream: callerInput, sinkId: devices.output16,
        onEvent: (name, detail) => event('caller', name, detail)});
      resources.transports.push(caller);
      await caller.prepare();
      // Explicit opt-in server flag required. No arbitrary destination parameter.
      await caller.connect({Mode: 'owner', Ticket: tokens.ticket});
      report.phone_dialed = true;
      show('Calling saved number. Start Dot voice after answer using the existing virtual route.');
      return;
    }
    const ctx = await context();
    const receiverSource = ctx.createMediaStreamDestination();
    tone(ctx, [733, 6500], receiverSource);
    resources.streams.push(receiverSource.stream);
    const receiver = new OpusTransport(tokens.receiver, {stream: receiverSource.stream, diagnostic: true, bitrate,
      // The SDK receiver is observed via its remote stream. Its speaker goes
      // to a virtual sink, never the physical speakers.
      sinkId: devices.output2, onEvent: (name, detail) => event('receiver', name, detail)});
    resources.transports.push(receiver);
    await receiver.prepare();
    // Use an independent sender fixture to avoid an acoustic feedback loop.
    // Capture the receiver's BlackHole output independently below.
    const senderSource = ctx.createMediaStreamDestination();
    tone(ctx, [997, 6000], senderSource);
    resources.streams.push(senderSource.stream);
    show('Checking generated tones through local Opus before Twilio');
    report.reference = await localReference(ctx, senderSource.stream, [997, 6000], devices.output16);
    const sender = new OpusTransport(tokens.caller, {stream: senderSource.stream, diagnostic: true, bitrate,
      sinkId: devices.output16, onEvent: (name, detail) => event('caller', name, detail)});
    resources.transports.push(sender);
    await sender.prepare();
    await receiver.register();
    show('Connecting two Twilio clients. Phone dialing disabled.');
    await sender.connect({Mode: 'probe', Ticket: tokens.ticket});
    await waitFor(() => sender.call?.status() === 'open' && receiver.call?.status() === 'open');
    await waitFor(() => sender.call.getRemoteStream() && receiver.call.getRemoteStream());
    await waitFor(() => sender.outputReady && receiver.outputReady);
    await Promise.all([sender.outputReady, receiver.outputReady]);
    const outgoing = analyser(ctx, receiver.call.getRemoteStream());
    const incoming = analyser(ctx, sender.call.getRemoteStream());
    const physicalForward = analyser(ctx, callerInput);
    show('Measuring wideband audio in both directions');
    await sleep(5000);
    report.caller_to_receiver = measure(outgoing, [997, 6000], ctx.sampleRate);
    report.receiver_to_caller = measure(incoming, [733, 6500], ctx.sampleRate);
    report.blackhole2_return = measure(physicalForward, [997, 6000], ctx.sampleRate);
    report.blackhole2_track = {muted: callerInput.getAudioTracks()[0].muted,
      state: callerInput.getAudioTracks()[0].readyState};
    const virtualReturn = await capture(devices.input16);
    const physical = analyser(ctx, virtualReturn);
    await sleep(1500);
    report.blackhole16_return = measure(physical, [733, 6500], ctx.sampleRate);
    report.blackhole2_simultaneous = measure(physicalForward, [997, 6000], ctx.sampleRate);
    report.caller_stats = await sender.stats();
    report.receiver_stats = await receiver.stats();
    report.caller_diagnostics = sender.diagnostics();
    report.receiver_diagnostics = receiver.diagnostics();
    Object.assign(report, assessProbe(report));
    show(report.verified ? 'Opus and two-way wideband audio verified. No phone called.' : 'Test finished with a failed audio check');
  } catch (error) {
    report.error = error.message;
    show('Test failed: ' + error.message);
  } finally {
    if (mode !== 'owner' || report.error) {
      await cleanup();
      await post('/result', report);
    }
  }
}
el('probe').onclick = () => run('probe');
el('route').onclick = async () => {
  stopped = false;
  el('local').disabled = el('route').disabled = el('probe').disabled = true;
  try {
    const devices = await virtualDevices(); report.virtual_devices = {};
    for (const count of [2, 16]) {
      show(`Checking BlackHole ${count}ch without Twilio`);
      const ctx = await context(devices['output' + count]);
      tone(ctx, [997, 6000], ctx.destination);
      const input = await capture(devices['input' + count]);
      const node = analyser(ctx, input);
      await sleep(2000);
      report.virtual_devices[count] = {levels: measure(node, [997, 6000], ctx.sampleRate),
        settings: input.getAudioTracks()[0].getSettings()};
      input.getTracks().forEach(t => t.stop()); await ctx.close();
    }
    show('Virtual device test completed. No phone called.');
  } catch (error) { report.error = error.message; show('Virtual device test failed'); }
  finally { await cleanup(); await post('/result', report); }
};
el('local').onclick = async () => {
  stopped = false;
  el('local').disabled = el('route').disabled = el('probe').disabled = true;
  try {
    const devices = await virtualDevices(), ctx = await context();
    const source = ctx.createMediaStreamDestination();
    resources.streams.push(source.stream); tone(ctx, [997, 6000], source);
    show('Testing local Opus without Twilio');
    report.reference = await localReference(ctx, source.stream, [997, 6000], devices.output16);
    show('Local reference completed. No phone called.');
  } catch (error) { report.error = error.message; show('Local reference failed'); }
  finally { await cleanup(); await post('/result', report); }
};
el('owner').onclick = () => run('owner');
el('stop').onclick = async () => { await cleanup(); await post('/result', report); show('Audio stopped'); };
el('shutdown').onclick = async () => {
  await cleanup(); await post('/result', report);
  await fetch('/shutdown', {method: 'POST'}); show('Prototype shut down');
};
window.addEventListener('pagehide', () => resources.transports.forEach(t => t.close()));
const config = await (await fetch('/config')).json();
report.sdk_version = config.sdk_version;
report.answer_on_bridge = config.answer_on_bridge;
el('owner').disabled = !config.owner_enabled;
show();
