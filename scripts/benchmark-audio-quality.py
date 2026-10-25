"""Offline bridge comparison. Never opens devices, sockets, or phone APIs.

Example:
  .venv/bin/python scripts/benchmark-audio-quality.py \
    --baseline .runtime/audio-quality/before/audio.py \
    --speech .runtime/audio-quality/source.wav --out .runtime/audio-quality

Speech must be a mono 48 kHz PCM16 WAV. The baseline is a saved pre-change
audio.py, not today's git HEAD, which may predate unrelated call fixes.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch
import wave

import numpy as np
import soxr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'mac-bridge'))
import audio
from resampling import PhoneResampler


def load_baseline(path):
    spec = importlib.util.spec_from_file_location('baseline_audio', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bridge(module):
    with patch.object(module, 'find_device', side_effect=[0, 1]):
        return module.AudioBridge()


def write_wav(path, samples, rate):
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes((np.clip(samples, -1, 1) * 32767).astype('<i2').tobytes())


def converter(name, rate_in, rate_out):
    if name == 'FIR481':
        return PhoneResampler(rate_in, rate_out)
    return soxr.ResampleStream(rate_in, rate_out, 1, dtype='float32', quality=name)


def resampling_options():
    results = {}
    for name in ['LQ', 'MQ', 'HQ', 'VHQ', 'FIR481']:
        result = {}
        for a, b in [(48000, 8000), (8000, 48000)]:
            stream = converter(name, a, b)
            lengths, pending, costs = [], [], []
            for i in range(200):
                source = (.2 * np.sin(2*np.pi*997*(np.arange(a//50)+i*a//50)/a)).astype('float32')
                started = time.perf_counter()
                out = stream.resample_chunk(source)
                costs.append((time.perf_counter() - started)*1000)
                lengths.append(len(out))
                pending.append(stream.delay()/b*1000 if name != 'FIR481' else 5)
            result[f'{a}_to_{b}'] = {
                'first_output_block_ms': next(i for i, n in enumerate(lengths) if n) * 20,
                'max_internal_delay_ms': round(max(pending), 3),
                'output_sizes': sorted(set(lengths)),
                'processing_p95_ms_per_20ms_block': round(float(np.percentile(costs, 95)), 4),
            }
        for frequency in [1000, 3000, 3400, 4200, 6000]:
            source = (.2*np.sin(2*np.pi*frequency*np.arange(48000)/48000)).astype('float32')
            stream = converter(name, 48000, 8000)
            output = stream.resample_chunk(source)[1000:-1000]
            gain = 20*np.log10(np.sqrt(np.mean(output.astype('float64')**2))/(.2/np.sqrt(2))+1e-15)
            result[f'gain_{frequency}hz_db'] = round(float(gain), 3)
        results[name] = result
    return results


def continuity(module, jitter):
    state = bridge(module)
    packets = 250
    times = np.arange(packets)
    if jitter:
        times += np.where(np.isin(times % 50, [20, 21]), 2, 0)
    times = np.maximum.accumulate(times)
    cursor, holes = 0, 0
    for tick in range(packets):
        while cursor < packets and times[cursor] <= tick:
            state.receive(audio.encode(np.full(160, .1, dtype='float32')))
            cursor += 1
        samples = state.playback.take(960)
        if tick >= 10:
            holes += int(np.count_nonzero(abs(samples) < .001))
    return {'silence_after_first_200ms_ms': round(holes/48, 3),
            'overflow_dropped_ms': round(state.playback.dropped/48, 3)}


def backlog(module):
    state = bridge(module)
    for _ in range(12):
        state.capture.append(np.full(960, .1, dtype='float32'))
    state.encoded.extend(audio.encode(np.full(800, .1, dtype='float32')))
    packets = [state.next_packet() for _ in range(12)]
    retained = sum(np.count_nonzero(abs(audio.decode(p)) > .01) for p in packets)
    return {'audible_audio_retained_ms': round(float(retained)/8, 3)}


def speech_comparison(module, source, out_dir, name):
    state = bridge(module)
    # Include trailing silence so all filter/queue output is collected.
    padded = np.pad(source, (0, 48000))
    padded = np.pad(padded, (0, -len(padded) % 960))
    outgoing = []
    for i in range(0, len(padded), 960):
        state.capture.append(padded[i:i+960])
        outgoing.append(audio.decode(state.next_packet()))
    write_wav(out_dir/f'{name}-dot-to-phone.wav', np.concatenate(outgoing), 8000)
    # Both bridges receive exactly the same ideal telephony input.
    incoming = audio.decode(audio.encode(soxr.resample(padded, 48000, 8000, quality='VHQ')))
    received = []
    for i in range(0, len(incoming), 160):
        state.receive(audio.encode(incoming[i:i+160]))
        received.append(state.playback.take(960))
    write_wav(out_dir/f'{name}-phone-to-dot.wav', np.concatenate(received), 48000)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--speech', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    old = load_baseline(args.baseline)
    with wave.open(str(args.speech), 'rb') as source_file:
        assert (source_file.getnchannels(), source_file.getsampwidth(), source_file.getframerate()) == (1, 2, 48000)
        source = np.frombuffer(source_file.readframes(source_file.getnframes()), dtype='<i2').astype('float32')/32768
    if len(source) < 48000 or float(np.max(abs(source), initial=0)) < .01:
        raise ValueError('Speech fixture must contain at least one second of audible audio')
    result = {'scope': 'offline synthetic fixtures; no phone/network/perceptual score',
              'speech_seconds': round(len(source)/48000, 3), 'resampling': resampling_options()}
    for name, module in [('before', old), ('after', audio)]:
        result[name] = {'steady_input': continuity(module, False),
                        '40ms_arrival_jitter': continuity(module, True),
                        '340ms_backlog': backlog(module)}
        speech_comparison(module, source, args.out, name)
    (args.out/'measurements.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
