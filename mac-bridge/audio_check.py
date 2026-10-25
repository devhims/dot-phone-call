"""Test AudioBridge against separate virtual-device peers, like browser audio."""
import json
from pathlib import Path
import subprocess
import sys
import time
import numpy as np
import sounddevice as sd
from audio import AudioBridge, BLOCK, RATE, decode, encode, find_device

# Signal-present bounds. Phone-bound audio is leveled toward telephone
# loudness, so the -25 dBFS test tone arrives near -13 dBFS.
DOT_TO_PHONE_RMS = (.02, .45)
PHONE_TO_DOT_RMS = (.02, .10)


def peer(mode):
    captured=[]
    cursor=0
    def source(outdata,frames,timing,status):
        nonlocal cursor
        phase=(np.arange(frames)+cursor)/RATE
        cursor+=frames
        outdata[:]=(0.08*np.sin(2*np.pi*997*phase))[:,None]
    def sink(indata,frames,timing,status):
        captured.append(indata[:,0].copy())
    if mode=='source':
        stream=sd.OutputStream(device=find_device('BlackHole 2ch','output'),channels=2,
                               samplerate=RATE,blocksize=BLOCK,callback=source)
    else:
        stream=sd.InputStream(device=find_device('BlackHole 16ch','input'),channels=2,
                              samplerate=RATE,blocksize=BLOCK,callback=sink)
    with stream:
        print('ready',flush=True)
        time.sleep(3)
    samples=np.concatenate(captured) if captured else np.array([])
    rms=float(np.sqrt(np.mean(samples**2))) if samples.size else 0.0
    print(json.dumps({'rms':rms,'frames':int(samples.size)}),flush=True)


def check_bridge_audio():
    bridge=AudioBridge()
    peers=[]
    outgoing=[]
    try:
        bridge.start()
        for mode in ('sink','source'):
            proc=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),mode],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            peers.append(proc)
            if proc.stdout.readline().strip()!='ready':
                raise RuntimeError('BRIDGE_AUDIO_PEER_FAILED')
        for i in range(75):
            t=(np.arange(160)+i*160)/8000
            bridge.receive(encode(0.08*np.sin(2*np.pi*733*t)))
            outgoing.append(decode(bridge.next_packet()))
            time.sleep(.02)
        data=decode(b'') if not outgoing else np.concatenate(outgoing)[1600:]
        sink_out,sink_error=peers[0].communicate(timeout=5)
        if peers[0].returncode: raise RuntimeError('BRIDGE_AUDIO_PEER_FAILED')
        sink=json.loads(sink_out)
        values={'dot_to_phone_rms':float(np.sqrt(np.mean(data**2))) if data.size else 0.0,
                'phone_to_dot_rms':sink['rms']}
        bounds={'dot_to_phone_rms':DOT_TO_PHONE_RMS,'phone_to_dot_rms':PHONE_TO_DOT_RMS}
        if any(not bounds[key][0]<value<bounds[key][1] for key,value in values.items()):
            raise RuntimeError('BRIDGE_AUDIO_CHECK_FAILED: '+json.dumps(values))
        return {key:round(value,4) for key,value in values.items()}
    finally:
        for proc in peers:
            if proc.poll() is None: proc.terminate()
            try: proc.communicate(timeout=3)
            except subprocess.TimeoutExpired: proc.kill();proc.communicate()
        bridge.close()


if __name__=='__main__':
    if len(sys.argv)>1: peer(sys.argv[1])
    else: print(json.dumps(check_bridge_audio()))
