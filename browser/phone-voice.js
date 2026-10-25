// Telephone voice conditioning for Dot-to-phone audio in the browser call path.
// A port of mac-bridge/phone_voice.py, designed at the AudioContext rate.
//
// Synthesized speech puts most of its energy below 300 Hz, which handsets and
// carrier codecs discard, while consonants (2-3.4 kHz) stay quiet. This stage
// band-limits to the phone band, high-passes below it, lifts consonants,
// levels toward a firm telephone loudness, and soft-limits peaks. It is causal,
// sample-continuous across render quanta, and adds no buffering.
export const SETTINGS = Object.freeze({
  lowpassHz: 4400,       // Twilio's PSTN leg is 8 kHz; flat through 3.4 kHz with the shelf.
  highpassHz: 260,
  presenceHz: 2000,      // At 48 kHz this shelf reaches +4.8 dB at 3 kHz, +4.9 at 3.4 kHz,
  presenceDb: 6,         // matching the 8 kHz bridge's +5 dB shelf in the phone band.
  targetDbfs: -12,       // Leveler envelope target; active speech lands near -15 dBFS.
  maxGainDb: 15,
  minGainDb: -10,
  initialLevelDbfs: -20, // Typical Dot envelope, so first words start near final gain.
  gateDbfs: -50,         // Never raise gain on silence or noise below this level.
  stepMs: 128 / 48,      // One render quantum at 48 kHz.
  attackMs: 10,
  releaseMs: 400,
  knee: .75,
  ceiling: .95,
});

function normalize(b, a) {return {b: b.map(x => x / a[0]), a: a.slice(1).map(x => x / a[0])};}

function lowpass(rate, frequency, q) {
  const w = 2 * Math.PI * frequency / rate, alpha = Math.sin(w) / (2 * q), cos = Math.cos(w);
  return normalize([(1 - cos) / 2, 1 - cos, (1 - cos) / 2], [1 + alpha, -2 * cos, 1 - alpha]);
}

function highpass(rate, frequency, q) {
  const w = 2 * Math.PI * frequency / rate, alpha = Math.sin(w) / (2 * q), cos = Math.cos(w);
  return normalize([(1 + cos) / 2, -(1 + cos), (1 + cos) / 2], [1 + alpha, -2 * cos, 1 - alpha]);
}

function highShelf(rate, frequency, gainDb) {
  // RBJ cookbook shelf with slope 1.
  const w = 2 * Math.PI * frequency / rate, A = 10 ** (gainDb / 40), cos = Math.cos(w);
  const root = 2 * Math.sqrt(A) * Math.sin(w) / 2 * Math.SQRT2;
  return normalize(
    [A * ((A + 1) + (A - 1) * cos + root), -2 * A * ((A - 1) + (A + 1) * cos), A * ((A + 1) + (A - 1) * cos - root)],
    [(A + 1) - (A - 1) * cos + root, 2 * ((A - 1) - (A + 1) * cos), (A + 1) - (A - 1) * cos - root]);
}

export function softLimit(x, {knee, ceiling} = SETTINGS) {
  const magnitude = Math.abs(x);
  if (magnitude <= knee) return x;
  return Math.sign(x) * (knee + (ceiling - knee) * Math.tanh((magnitude - knee) / (ceiling - knee)));
}

export class PhoneVoice {
  constructor(rate = 48000, settings = SETTINGS) {
    this.rate = rate;
    this.settings = settings;
    // Fourth-order Butterworth low-pass as two sections, then the voice shaping.
    this.sections = [lowpass(rate, settings.lowpassHz, .5412), lowpass(rate, settings.lowpassHz, 1.3066),
      highpass(rate, settings.highpassHz, Math.SQRT1_2), highShelf(rate, settings.presenceHz, settings.presenceDb)];
    this.step = Math.max(1, Math.round(rate * settings.stepMs / 1000));
    const stepMs = this.step / rate * 1000;
    this.attack = 1 - Math.exp(-stepMs / settings.attackMs);
    this.release = 1 - Math.exp(-stepMs / settings.releaseMs);
    this.clear();
  }

  clear() {
    this.state = this.sections.map(() => [0, 0]);
    this.level = this.settings.initialLevelDbfs;
    this.gainDb = this.settings.targetDbfs - this.settings.initialLevelDbfs;
  }

  // Processes input into output (may be the same array). Lengths must match.
  process(input, output = new Float32Array(input.length)) {
    const s = this.settings;
    for (let i = 0; i < input.length; i++) {
      let y = input[i];
      for (let k = 0; k < this.sections.length; k++) {
        const {b, a} = this.sections[k], z = this.state[k], x = y;
        // Transposed direct form II.
        y = b[0] * x + z[0];
        z[0] = b[1] * x - a[0] * y + z[1];
        z[1] = b[2] * x - a[1] * y;
      }
      output[i] = y;
    }
    for (let start = 0; start < output.length; start += this.step) {
      const end = Math.min(start + this.step, output.length);
      let power = 0;
      for (let i = start; i < end; i++) power += output[i] * output[i];
      const level = 10 * Math.log10(power / (end - start) + 1e-20);
      if (level > s.gateDbfs) this.level += (level - this.level) * (level > this.level ? this.attack : this.release);
      const target = Math.min(s.maxGainDb, Math.max(s.minGainDb, s.targetDbfs - this.level));
      // Ramp across the block from the previous gain, so steps never click.
      const from = this.gainDb, count = end - start;
      for (let i = start; i < end; i++) {
        const gain = 10 ** ((from + (target - from) * (i - start + 1) / count) / 20);
        output[i] = softLimit(output[i] * gain, s);
      }
      this.gainDb = target;
    }
    return output;
  }
}
