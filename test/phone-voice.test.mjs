import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {PhoneVoice, SETTINGS} from '../browser/phone-voice.js';

const RATE = 48000;
const tone = (frequency, amplitude, seconds) =>
  Float32Array.from({length: Math.round(RATE * seconds)}, (_, i) => amplitude * Math.sin(2 * Math.PI * frequency * i / RATE));
const db = samples => {let power = 0; for (const x of samples) power += x * x; return 10 * Math.log10(power / samples.length + 1e-20);};
const shaped = (source, quantum = 128) => {
  const voice = new PhoneVoice(RATE), out = new Float32Array(source.length);
  for (let i = 0; i < source.length; i += quantum) voice.process(source.subarray(i, i + quantum), out.subarray(i, i + quantum));
  return out;
};
// Filters only; the leveler would otherwise normalize every tone.
const equalizerDb = frequency => {
  const voice = new PhoneVoice(RATE), source = tone(frequency, .05, 1), out = new Float32Array(source.length);
  source.forEach((x, i) => {
    let y = x;
    voice.sections.forEach(({b, a}, k) => {
      const z = voice.state[k], u = y;
      y = b[0] * u + z[0]; z[0] = b[1] * u - a[0] * y + z[1]; z[1] = b[2] * u - a[1] * y;
    });
    out[i] = y;
  });
  return db(out.subarray(RATE / 2)) - db(source.subarray(RATE / 2));
};

test('bass below the phone band is cut and the consonant band is lifted', () => {
  const reference = equalizerDb(1000);
  assert.ok(equalizerDb(120) - reference < -10);
  assert.ok(equalizerDb(200) - reference < -4);
  assert.ok(Math.abs(equalizerDb(500) - reference) < 1);
  assert.ok(equalizerDb(3000) - reference > 4);
  assert.ok(equalizerDb(3400) - reference > 4);
  assert.ok(equalizerDb(8000) < -12, 'audio above the 8 kHz telephone band is attenuated');
});

test('phone-band response matches the earlier native bridge within 1 dB', () => {
  // The removed 8 kHz bridge: 260 Hz high-pass and +5 dB shelf at 2 kHz, phone-tested.
  const bridge = {200: -5.9, 300: -1.9, 500: -.3, 1000: .1, 3000: 4.8, 3400: 5.0};
  for (const [frequency, expected] of Object.entries(bridge)) {
    assert.ok(Math.abs(equalizerDb(Number(frequency)) - expected) < 1, `${frequency} Hz: ${equalizerDb(Number(frequency)).toFixed(2)} dB`);
  }
});

test('quiet Dot speech is raised and levels converge across inputs', () => {
  const quiet = tone(997, .08, 2);
  assert.ok(db(shaped(quiet).subarray(RATE)) - db(quiet) > 9);
  const levels = [.04, .1, .3].map(amplitude => db(shaped(tone(997, amplitude, 3)).subarray(2 * RATE)));
  // A 17.5 dB input range lands within 6 dB at the phone.
  assert.ok(Math.max(...levels) - Math.min(...levels) < 6, levels.join(', '));
  assert.ok(Math.min(...levels) > -24);
});

test('peaks are bounded and silence stays silent after the filters ring out', () => {
  const voice = new PhoneVoice(RATE), loud = tone(997, .9, 1), out = new Float32Array(loud.length);
  for (let i = 0; i < loud.length; i += 128) voice.process(loud.subarray(i, i + 128), out.subarray(i, i + 128));
  assert.ok(Math.max(...out.map(Math.abs)) <= SETTINGS.ceiling + 1e-6);
  for (let i = 0; i < 40; i++) voice.process(new Float32Array(128));
  for (let i = 0; i < 100; i++) assert.ok(voice.process(new Float32Array(128)).every(x => Math.abs(x) < 1e-6));
});

test('quantum-aligned chunking matches unbroken processing', () => {
  const source = Float32Array.from({length: 128 * 300}, (_, i) => .1 * Math.sin(i * .37) * Math.sin(i * .0011));
  const expected = new PhoneVoice(RATE).process(source);
  const voice = new PhoneVoice(RATE), result = new Float32Array(source.length);
  let cursor = 0;
  for (const size of [128, 256, 128, 3840, 128 * 100, 128 * 166]) {
    voice.process(source.subarray(cursor, cursor + size), result.subarray(cursor, cursor + size));
    cursor += size;
  }
  assert.equal(cursor, source.length);
  result.forEach((x, i) => assert.ok(Math.abs(x - expected[i]) < 1e-6));
});

test('a steady tone has no gain steps at render-quantum boundaries', () => {
  const out = shaped(tone(997, .1, 2)).subarray(RATE / 2);
  const slope = .1 * 10 ** (SETTINGS.maxGainDb / 20) * 2 * Math.PI * 997 / RATE * 1.6;
  for (let i = 1; i < out.length; i++) assert.ok(Math.abs(out[i] - out[i - 1]) < slope);
});

test('the worklet registers phone-voice, shapes input and outputs silence without input', async () => {
  let registered;
  Object.assign(globalThis, {AudioWorkletProcessor: class {}, sampleRate: RATE,
    registerProcessor: (name, Processor) => {registered = {name, Processor};}});
  await import('../browser/phone-voice-worklet.js');
  assert.equal(registered.name, 'phone-voice');
  const processor = new registered.Processor(), input = tone(150, .3, 128 / RATE), output = new Float32Array(128);
  assert.equal(processor.process([[input]], [[output]]), true);
  assert.ok(output.some(x => x !== 0));
  assert.ok(db(output) < db(input), '150 Hz is attenuated');
  output.fill(1);
  assert.equal(processor.process([[]], [[output]]), true);
  assert.ok(output.every(x => x === 0));
});

test('browser modules loaded by the call session are served and packaged', () => {
  const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');
  const session = read('../browser/session.js'), worklet = read('../browser/phone-voice-worklet.js');
  const files = [...session.matchAll(/new URL\('\.\/([\w.-]+\.js)'/g), ...worklet.matchAll(/from '\.\/([\w.-]+\.js)'/g)].map(m => m[1]);
  assert.ok(files.includes('phone-voice-worklet.js') && files.includes('phone-voice.js'));
  assert.ok(!session.includes('8893') && !session.includes('pcm-worklet'), 'no helper audio socket remains');
  const backend = read('../src/local-backend.mjs'), build = read('../scripts/build-mac-extension.mjs');
  for (const file of files) {
    assert.ok(backend.includes(`'/bridge/${file}'`), `${file} is served by the local backend`);
    assert.ok(build.includes(file), `${file} is copied into the extension`);
  }
});

test('the call session shapes Dot through phone-voice and plays phone audio to BlackHole 16ch', async () => {
  const modules = [], connections = [];
  const node = name => ({name, connect(target) {connections.push([name, target.name]); return target;}, disconnect() {}});
  class AudioContext {
    constructor(options) {this.options = options; this.state = 'suspended'; this.sinkId = '';
      this.destination = node('phone-output');
      this.audioWorklet = {addModule: async url => {modules.push(url.split('/').at(-1));}};}
    async setSinkId(id) {this.sinkId = id;} async resume() {this.state = 'running';} async close() {this.state = 'closed';}
    createGain() {throw Error('flat gain must not be used');}
    createMediaStreamDestination() {return {...node('to-twilio'), stream: {getTracks: () => []}};}
    createMediaStreamSource(stream) {return node(stream.name);}
    // The level meter reads Dot's raw voice; a quiet half-scale tone peaks at -6 dBFS.
    createAnalyser() {return {...node('dot-level'), getFloatTimeDomainData(data) {data.fill(0); data[3] = -.5;}};}
  }
  class AudioWorkletNode {constructor(context, name, options) {Object.assign(this, node(name), {options});}}
  const track = {getSettings: () => ({deviceId: 'bh2-in'}), stop() {}};
  Object.assign(globalThis, {AudioContext, AudioWorkletNode,
    WebSocket: class {constructor() {throw Error('no audio socket may be opened');}}});
  // Node 24 defines a read-only navigator; replace it for this browser module.
  Object.defineProperty(globalThis, 'navigator', {configurable: true, value: {
    permissions: {query: async () => ({state: 'granted'})},
    mediaDevices: {enumerateDevices: async () => [
        {kind: 'audioinput', label: 'BlackHole 2ch', deviceId: 'bh2-in'},
        {kind: 'audiooutput', label: 'BlackHole 16ch (Virtual)', deviceId: 'bh16-out'},
        {kind: 'audiooutput', label: 'MacBook Pro Speakers', deviceId: 'speakers'}],
      getUserMedia: async () => ({name: 'blackhole-2ch', getAudioTracks: () => [track], getTracks: () => [track]})}}});
  const {AudioSession} = await import('../browser/session.js');
  const session = new AudioSession();
  await session.prepare();
  assert.equal(session.context.sinkId, 'bh16-out');
  assert.deepEqual(modules, ['phone-voice-worklet.js']);
  assert.deepEqual(connections, [['blackhole-2ch', 'phone-voice'], ['phone-voice', 'to-twilio'], ['blackhole-2ch', 'dot-level']]);
  assert.equal(session.dotPeakDb(), -6);
  await session.remote({getRemoteStream: () => ({name: 'twilio-remote'})});
  assert.deepEqual(connections.at(-1), ['twilio-remote', 'phone-output']);
  assert.equal(session.voice.options.outputChannelCount[0], 1);
  await session.close();
  assert.equal(session.dotPeakDb(), null);
});

test('a call refuses to prepare when the Mac default output is BlackHole', async () => {
  // Every app's sound would otherwise be captured into the phone call.
  Object.assign(globalThis, {AudioContext: class {constructor() {throw Error('no audio context may open');}}});
  Object.defineProperty(globalThis, 'navigator', {configurable: true, value: {
    permissions: {query: async () => ({state: 'granted'})},
    mediaDevices: {enumerateDevices: async () => [
        {kind: 'audioinput', label: 'BlackHole 2ch', deviceId: 'bh2-in'},
        {kind: 'audiooutput', label: 'BlackHole 16ch (Virtual)', deviceId: 'bh16-out'},
        {kind: 'audiooutput', label: 'Default - BlackHole 2ch (Virtual)', deviceId: 'default'}],
      getUserMedia: async () => {throw Error('no microphone may open');}}}});
  const {AudioSession} = await import('../browser/session.js');
  await assert.rejects(new AudioSession().prepare(), /MAC_DEFAULT_OUTPUT_IS_BLACKHOLE/);
});
