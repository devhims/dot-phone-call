"""Prove hidden-page audio through BlackHole and local Opus, without any phone API."""
import asyncio
import numpy as np
import sounddevice as sd
from aiohttp import web
from registered_call import LocalAudioPipe
from audio import find_device

async def test_offscreen_audio(companion):
    from companion import SNAPSHOT
    if SNAPSHOT.exists():raise RuntimeError('AUDIO_ROUTE_ALREADY_IN_USE')
    pipe=LocalAudioPipe(extension_id=companion.config['extension_id'])
    runner=web.AppRunner(pipe.app(),access_log=None)
    captured=[]
    capture=None
    try:
        # One duplex stream keeps playback and recording on the same CoreAudio
        # device clock. Separate AUHAL streams can invalidate each other.
        import queue
        def duplex(indata,outdata,frames,timing,status):
            captured.append(indata[:,0].copy())
            outdata.fill(0)
            offset=0
            while offset<frames:
                if not len(pipe.pending_audio):
                    try:pipe.pending_audio=pipe.audio_queue.get_nowait()
                    except queue.Empty:break
                count=min(frames-offset,len(pipe.pending_audio))
                outdata[offset:offset+count]=pipe.pending_audio[:count,None]
                pipe.pending_audio=pipe.pending_audio[count:]
                offset+=count;pipe.audio_stats['played_frames']+=count
        device=find_device('BlackHole 16ch','input')
        capture=sd.Stream(device=(device,device),channels=(2,2),samplerate=48000,
            blocksize=960,dtype='float32',callback=duplex)
        capture.start()
        phase=0
        def tone(data,frames,timing,status):
            nonlocal phase
            data[:]=(.06*np.sin(2*np.pi*997*(np.arange(frames)+phase)/48000))[:,None]
            phase+=frames
        fixture=sd.OutputStream(device=find_device('BlackHole 2ch','output'),channels=2,
            samplerate=48000,blocksize=960,dtype='float32',callback=tone)
        pipe.outputs.append(fixture);fixture.start()
        await runner.setup();await web.TCPSite(runner,'127.0.0.1',8893).start()
        browser=await companion.command('hidden_audio_test',audio_capability=pipe.audio_capability)
        if not browser.get('verified'):raise RuntimeError('OFFSCREEN_AUDIO_FAILED')
        data=np.concatenate(captured) if captured else np.empty(0)
        # Exclude startup silence; require sustained received tone, not one impulse.
        active=data[np.abs(data)>.001]
        metrics={'browser':browser,'captured_frames':len(data),'active_frames':len(active),'native_audio':dict(pipe.audio_stats),'capture_active':capture.active,'output_active':[stream.active for stream in pipe.outputs]}
        print(__import__('json').dumps({'event':'offscreen_audio_metrics',**metrics}),flush=True)
        if not capture.active:
            return {'verified':False,'dialed':False,'code':'OFFSCREEN_RECORDER_STOPPED',**metrics}
        if len(active)<48000 or pipe.audio_stats['received_frames']<48000:
            return {'verified':False,'dialed':False,'code':'OFFSCREEN_AUDIO_SILENT',**metrics}
        start=np.flatnonzero(np.abs(data)>.001)[0]
        signal=data[start:]
        rms=float(np.sqrt(np.mean(signal**2)))
        peak=float(np.fft.rfftfreq(len(signal),1/48000)[np.argmax(np.abs(np.fft.rfft(signal)))])
        if not .015<rms<.15 or abs(peak-997)>5:raise RuntimeError('OFFSCREEN_AUDIO_TONE_MISMATCH')
        return {'verified':True,'dialed':False,'browser':browser,'rms':round(rms,4),
                'peak_hz':round(peak,1),'received_frames':pipe.audio_stats['received_frames'],**metrics}
    finally:
        if capture is not None:capture.stop();capture.close()
        await pipe.close();await runner.cleanup()
