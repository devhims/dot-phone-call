import {AudioSession} from './session.js';
// No-phone audio check. A 997 Hz tone stands in for Dot on BlackHole 2ch, passes
// through the real call session (voice shaping, local Opus WebRTC) and its
// playback to BlackHole 16ch, where it is recorded. No Twilio Device, phone API, or Dot.
const TONE_HZ = 997, SECONDS = 2.5;

function device(devices, kind, name) {
  return devices.find(d => d.kind === kind && new RegExp('^BlackHole\\s+' + name + '(?:\\b|\\s)', 'i').test(d.label));
}

async function measure(stream, context) {
  const analyser = new AnalyserNode(context, {fftSize: 16384});
  context.createMediaStreamSource(stream).connect(analyser);
  const frame = new Float32Array(2048);
  let sum = 0, count = 0, activeMs = 0;
  for (let elapsed = 0; elapsed < SECONDS * 1000; elapsed += 100) {
    await new Promise(resolve => setTimeout(resolve, 100));
    analyser.getFloatTimeDomainData(frame);
    let power = 0;
    for (const x of frame) power += x * x;
    if (Math.sqrt(power / frame.length) > .003) {activeMs += 100; sum += power; count += frame.length;}
  }
  const spectrum = new Float32Array(analyser.frequencyBinCount);
  analyser.getFloatFrequencyData(spectrum);
  let peak = 0;
  spectrum.forEach((value, index) => {if (value > spectrum[peak]) peak = index;});
  return {rms: count ? Math.sqrt(sum / count) : 0, peak_hz: peak * context.sampleRate / analyser.fftSize, active_ms: activeMs};
}

export async function testHiddenAudio() {
  const devices = await navigator.mediaDevices.enumerateDevices();
  const dotOutput = device(devices, 'audiooutput', '2ch'), phoneInput = device(devices, 'audioinput', '16ch');
  if (!dotOutput) throw Error('BLACKHOLE_2CH_OUTPUT_NOT_FOUND');
  if (!phoneInput) throw Error('BLACKHOLE_16CH_INPUT_NOT_FOUND');
  const audio = new AudioSession(), a = new RTCPeerConnection({iceServers: []}), b = new RTCPeerConnection({iceServers: []});
  const pendingA = [], pendingB = [];
  a.onicecandidate = e => {if (e.candidate) {if (b.remoteDescription) void b.addIceCandidate(e.candidate); else pendingB.push(e.candidate);}};
  b.onicecandidate = e => {if (e.candidate) {if (a.remoteDescription) void a.addIceCandidate(e.candidate); else pendingA.push(e.candidate);}};
  let timer, playback, tone, recorder, recording;
  try {
    // Stand-in for Dot: a tone played into BlackHole 2ch.
    tone = new AudioContext({sampleRate: 48000, sinkId: dotOutput.deviceId});
    const oscillator = new OscillatorNode(tone, {frequency: TONE_HZ}), level = new GainNode(tone, {gain: .06});
    oscillator.connect(level).connect(tone.destination); oscillator.start();
    await tone.resume();
    await audio.prepare();
    const remote = new Promise((resolve, reject) => {b.ontrack = e => resolve(e.streams[0]); timer = setTimeout(() => reject(Error('LOCAL_WEBRTC_TIMEOUT')), 5000);});
    for (const track of audio.destination.stream.getTracks()) a.addTrack(track, audio.destination.stream);
    const opus = RTCRtpSender.getCapabilities('audio').codecs.filter(c => c.mimeType.toLowerCase() === 'audio/opus');
    for (const t of a.getTransceivers()) t.setCodecPreferences(opus);
    await a.setLocalDescription(await a.createOffer()); await b.setRemoteDescription(a.localDescription);
    for (const c of pendingB) await b.addIceCandidate(c);
    await b.setLocalDescription(await b.createAnswer()); await a.setRemoteDescription(b.localDescription);
    for (const c of pendingA) await a.addIceCandidate(c);
    const stream = await remote; clearTimeout(timer);
    // Twilio starts a muted HTML audio element as well as exposing the remote
    // stream. Match that decoder/playout lifecycle in the local test.
    playback = new Audio(); playback.muted = true; playback.srcObject = stream; await playback.play();
    await audio.remote({getRemoteStream: () => stream});
    // What Dot would hear: BlackHole 16ch, recorded without browser processing.
    recording = await navigator.mediaDevices.getUserMedia({audio: {deviceId: {exact: phoneInput.deviceId},
      echoCancellation: false, noiseSuppression: false, autoGainControl: false}});
    recorder = new AudioContext({sampleRate: 48000}); await recorder.setSinkId({type: 'none'});
    const heard = await measure(recording, recorder);
    const stats = await b.getStats(); let received = 0, codec;
    stats.forEach(report => {if (report.type === 'inbound-rtp' && report.kind === 'audio') {received += report.packetsReceived || 0; codec = stats.get(report.codecId)?.mimeType;}});
    if (received < 20 || codec?.toLowerCase() !== 'audio/opus') throw Error('LOCAL_OPUS_AUDIO_MISSING');
    const rms = Math.round(heard.rms * 10000) / 10000, peakHz = Math.round(heard.peak_hz * 10) / 10;
    // Voice shaping levels the -27 dBFS tone toward telephone loudness (~-13 dBFS).
    const verified = heard.active_ms >= 1000 && rms > .015 && rms < .45 && Math.abs(peakHz - TONE_HZ) <= 10;
    return {verified, ...(verified ? {} : {code: 'HIDDEN_AUDIO_TONE_MISMATCH'}), sink: 'blackhole-16ch', codec,
      packets_received: received, peak_hz: peakHz, rms, active_ms: heard.active_ms, phone_dialed: false};
  } finally {
    clearTimeout(timer);
    if (playback) {playback.pause(); playback.srcObject = null;}
    recording?.getTracks().forEach(t => t.stop());
    for (const context of [recorder, tone]) if (context && context.state !== 'closed') await context.close();
    a.close(); b.close(); await audio.close();
  }
}
