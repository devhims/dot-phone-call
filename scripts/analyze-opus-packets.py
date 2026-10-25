"""Decode generated probe packets locally with FFmpeg, without network or audio I/O."""
import argparse
import base64
import json
from pathlib import Path
import shutil
import struct
import subprocess

import numpy as np


def packet_format(packet):
    if not packet:
        raise ValueError('Empty packet')
    config, code = packet[0] >> 3, packet[0] & 3
    band = ([4, 6, 8][config//4] if config < 12 else
            [12, 20][(config-12)//2] if config < 16 else
            [4, 8, 12, 20][(config-16)//4])
    duration = ([10, 20, 40, 60][config % 4] if config < 12 else
                [10, 20][config % 2] if config < 16 else
                [2.5, 5, 10, 20][config % 4])
    if code == 3 and len(packet) < 2:
        raise ValueError('Missing frame count')
    frames = 1 if code == 0 else 2 if code < 3 else packet[1] & 63
    if not frames or duration * frames > 120:
        raise ValueError('Invalid packet duration')
    return band, int(duration * frames * 48)


def ogg_page(packet, sequence, granule=0, flags=0):
    segments = bytes([255]*(len(packet)//255) + [len(packet)%255])
    page = bytearray(b'OggS' + struct.pack('<BBQIII', 0, flags, granule, 1, sequence, 0)
                     + bytes([len(segments)]) + segments + packet)
    crc = 0
    for byte in page:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ (0x04c11db7 if crc & 0x80000000 else 0)) & 0xffffffff
    page[22:26] = struct.pack('<I', crc)
    return page


def decode(packets):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError('FFmpeg is required for packet diagnostics')
    if not packets:
        raise ValueError('No encoded packets were captured')
    # Mono output, no preskip because the bounded capture starts mid-stream.
    ogg = ogg_page(b'OpusHead' + struct.pack('<BBHIhB', 1, 1, 0, 48000, 0, 0), 0, flags=2)
    ogg += ogg_page(b'OpusTags' + struct.pack('<II', 0, 0), 1)
    granule, bandwidths = 0, {}
    for index, encoded in enumerate(packets):
        packet = base64.b64decode(encoded, validate=True)
        bandwidth, samples = packet_format(packet)
        bandwidths[bandwidth] = bandwidths.get(bandwidth, 0) + 1
        granule += samples
        ogg += ogg_page(packet, index+2, granule, flags=4 if index == len(packets)-1 else 0)
    result = subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-i', 'pipe:0',
        '-f', 'f32le', '-ac', '1', '-ar', '48000', 'pipe:1'], input=ogg, capture_output=True, check=True)
    return np.frombuffer(result.stdout, dtype='<f4'), bandwidths


def levels(samples, frequencies):
    # Discard decoder startup because capture starts in the middle of a stream.
    samples = samples[4800:]
    if len(samples) < 8192:
        raise ValueError('Insufficient steady audio')
    window = np.hanning(len(samples))
    spectrum = np.abs(np.fft.rfft(samples * window)) * 2 / window.sum()
    bins = np.fft.rfftfreq(len(samples), 1/48000)
    return {str(f): round(float(20*np.log10(max(spectrum[np.abs(bins-f)<10].max(),1e-10))), 2)
            for f in frequencies}


def analyze(report):
    result = {}
    for side in ['caller', 'receiver']:
        encoded = report[side + '_diagnostics']['encoded']
        for direction in ['outbound', 'inbound']:
            entry = encoded[direction]
            data, bands = decode(entry['packets'])
            frequencies = [997, 6000] if (side == 'caller') == (direction == 'outbound') else [733, 6500]
            result[side + '_' + direction] = {
                'sampled_packets': len(entry['packets']),
                'bandwidths': {str(k)+'kHz':v for k,v in bands.items()},
                'decoded_levels_dbfs': levels(data, frequencies)}
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = analyze(json.loads(args.report.read_text())['browser'])
    rendered = json.dumps(result, indent=2)+'\n'
    if args.output:
        args.output.write_text(rendered)
    print(rendered)
