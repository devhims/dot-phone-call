"""Prove hidden-page audio through BlackHole and local Opus, without any phone API.

A tone plays into BlackHole 2ch (standing in for Dot). The hidden page captures
it, applies the voice shaping, sends it over a local Opus loop, and plays the
received audio to BlackHole 16ch itself. This records BlackHole 16ch.
"""
import json
import numpy as np
import sounddevice as sd
from audio import find_device

async def test_offscreen_audio(companion):
    from companion import SNAPSHOT
    if SNAPSHOT.exists():raise RuntimeError('AUDIO_ROUTE_ALREADY_IN_USE')
    captured=[]
    streams=[]
    try:
        def record(indata,frames,timing,status):
            captured.append(indata[:,0].copy())
        capture=sd.InputStream(device=find_device('BlackHole 16ch','input'),channels=2,samplerate=48000,
            blocksize=960,dtype='float32',callback=record)
        streams.append(capture);capture.start()
        phase=0
        def tone(data,frames,timing,status):
            nonlocal phase
            data[:]=(.06*np.sin(2*np.pi*997*(np.arange(frames)+phase)/48000))[:,None]
            phase+=frames
        fixture=sd.OutputStream(device=find_device('BlackHole 2ch','output'),channels=2,
            samplerate=48000,blocksize=960,dtype='float32',callback=tone)
        streams.append(fixture);fixture.start()
        browser=await companion.command('hidden_audio_test')
        if not browser.get('verified'):raise RuntimeError('OFFSCREEN_AUDIO_FAILED')
        data=np.concatenate(captured) if captured else np.empty(0)
        # Exclude startup silence; require sustained received tone, not one impulse.
        active=data[np.abs(data)>.001]
        metrics={'browser':browser,'captured_frames':len(data),'active_frames':len(active),
                 'capture_active':capture.active,'output_active':fixture.active}
        print(json.dumps({'event':'offscreen_audio_metrics',**metrics}),flush=True)
        if not capture.active:
            return {'verified':False,'dialed':False,'code':'OFFSCREEN_RECORDER_STOPPED',**metrics}
        if len(active)<48000:
            return {'verified':False,'dialed':False,'code':'OFFSCREEN_AUDIO_SILENT',**metrics}
        start=np.flatnonzero(np.abs(data)>.001)[0]
        signal=data[start:]
        rms=float(np.sqrt(np.mean(signal**2)))
        peak=float(np.fft.rfftfreq(len(signal),1/48000)[np.argmax(np.abs(np.fft.rfft(signal)))])
        # The tone passes through the phone-bound voice shaping, which levels
        # it toward telephone loudness: -27 dBFS in arrives near -13 dBFS.
        if not .015<rms<.45 or abs(peak-997)>5:raise RuntimeError('OFFSCREEN_AUDIO_TONE_MISMATCH')
        return {'verified':True,'dialed':False,'browser':browser,'rms':round(rms,4),
                'peak_hz':round(peak,1),'received_frames':len(active),**metrics}
    finally:
        for stream in streams:
            stream.stop();stream.close()
