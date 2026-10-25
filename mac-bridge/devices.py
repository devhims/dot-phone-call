"""List devices or prove virtual loopback with generated audio, never a physical mic."""
import argparse
import json
import time
import numpy as np
import sounddevice as sd
from audio import RATE, BLOCK, find_device

parser = argparse.ArgumentParser()
parser.add_argument('--test-loopback', action='store_true')
args = parser.parse_args()
if not args.test_loopback:
    print(json.dumps([{'id': i, 'name': d['name'], 'inputs': d['max_input_channels'],
                      'outputs': d['max_output_channels']} for i, d in enumerate(sd.query_devices())], indent=2))
else:
    for name in ('BlackHole 2ch', 'BlackHole 16ch'):
        source, sink = find_device(name, 'input'), find_device(name, 'output')
        captured = []
        cursor = [0]
        def loopback(indata, outdata, frames, timing, status):
            captured.append(indata[:, 0].copy())
            t = (np.arange(frames)+cursor[0])/RATE
            cursor[0] += frames
            outdata[:] = (0.08*np.sin(2*np.pi*997*t))[:, None]
        with sd.Stream(device=(source, sink), channels=2, samplerate=RATE, blocksize=BLOCK, callback=loopback):
            time.sleep(1)
        if not captured:
            raise RuntimeError(f'{name}: CoreAudio delivered no callbacks')
        data = np.concatenate(captured)[RATE//10:]
        rms = float(np.sqrt(np.mean(data*data)))
        if not .02 < rms < .10:
            raise RuntimeError(f'{name}: loopback failed, RMS {rms:.5f}')
        spectrum = np.abs(np.fft.rfft(data))
        peak = float(np.fft.rfftfreq(len(data), 1/RATE)[np.argmax(spectrum)])
        if abs(peak-997) > 5:
            raise RuntimeError(f'{name}: wrong tone frequency')
        print(json.dumps({'device': name, 'loopback': 'passed', 'rms': round(rms, 4), 'peak_hz': round(peak, 1)}))
