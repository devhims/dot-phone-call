"""Manual browser-only audio probe. Start Dot via its UI after routing BlackHole.

Feeds a local 8 kHz mono WAV through the real phone codec/playback path and
records only the BlackHole return channel. Never dials or controls the browser.
"""
import argparse
import json
import time
import wave

import numpy as np
from audio import AudioBridge, encode, decode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phrase')
    parser.add_argument('reply')
    parser.add_argument('--seconds', type=int, default=30)
    args = parser.parse_args()
    with wave.open(args.phrase, 'rb') as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (8000, 1, 2):
            raise ValueError('Phrase must be 8 kHz, mono, 16-bit PCM')
        speech = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2').astype('float32') / 32768
    bridge = AudioBridge()
    result = []
    try:
        bridge.start()
        print('probe_started', flush=True)
        start = time.monotonic()
        for i in range(args.seconds * 50):
            offset = (i - 100) * 160
            block = speech[offset:offset + 160] if 0 <= offset < len(speech) else np.zeros(160)
            bridge.receive(encode(block))
            result.append(decode(bridge.next_packet()))
            time.sleep(max(0, start + (i + 1) * .02 - time.monotonic()))
        print(json.dumps(bridge.metrics()), flush=True)
    finally:
        bridge.close()
    data = np.concatenate(result)
    with wave.open(args.reply, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes((data * 32767).astype('<i2').tobytes())
    print('captured_peak', float(np.max(np.abs(data))), flush=True)


if __name__ == '__main__':
    main()
