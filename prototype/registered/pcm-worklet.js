// Local playout only. No microphone or phone-number access.
class PCMOutput extends AudioWorkletProcessor {
  constructor() { super(); this.buffer = new Float32Array(960); this.offset = 0; }
  process(inputs) {
    const input = inputs[0]?.[0];
    if (input) for (const sample of input) {
      this.buffer[this.offset++] = sample;
      if (this.offset === this.buffer.length) {
        this.port.postMessage(this.buffer.buffer, [this.buffer.buffer]);
        this.buffer = new Float32Array(960); this.offset = 0;
      }
    }
    return true;
  }
}
registerProcessor('local-pcm-output', PCMOutput);
