// Dot-to-phone voice conditioning on the audio rendering thread.
import {PhoneVoice} from './phone-voice.js';

class PhoneVoiceProcessor extends AudioWorkletProcessor {
  constructor() {super();this.voice = new PhoneVoice(sampleRate);}
  process(inputs, outputs) {
    const input = inputs[0]?.[0], output = outputs[0][0];
    if (input) this.voice.process(input, output);
    else output.fill(0);
    return true;
  }
}
registerProcessor('phone-voice', PhoneVoiceProcessor);
