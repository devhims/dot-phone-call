"""Temporary owner-call test, with routing restoration and process cleanup."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT/'.runtime'
ROUTING = RUNTIME/'audio-routing'
SNAPSHOT = RUNTIME/'mac-audio-original.json'
READY = RUNTIME/'dot-audio-ready'
TUNNEL_LOG = RUNTIME/'mac-tunnel.log'

def routing(action):
    subprocess.run([str(ROUTING), action, str(SNAPSHOT)], check=True)

def stop(process):
    if process and process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

def main():
    if SNAPSHOT.exists():
        raise RuntimeError('Previous routing snapshot exists. Restore it before a new test.')
    READY.unlink(missing_ok=True)
    tunnel = call = None
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        with TUNNEL_LOG.open('w') as logfile:
            tunnel = subprocess.Popen([str(RUNTIME/'bin/cloudflared'), 'tunnel', '--url',
                'http://127.0.0.1:8891', '--no-autoupdate'], stdout=logfile, stderr=logfile)
            deadline = time.monotonic()+40
            origin = None
            while time.monotonic() < deadline:
                if tunnel.poll() is not None:
                    raise RuntimeError('Tunnel exited before it became ready')
                match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', TUNNEL_LOG.read_text())
                if match:
                    origin = match.group(0)
                    break
                time.sleep(.2)
            if not origin:
                raise RuntimeError('Tunnel did not become ready')
            routing('enable')
            print(json.dumps({'event':'awaiting_dot_voice', 'ready_file':str(READY),
                'timeout_seconds':180}), flush=True)
            deadline = time.monotonic()+180
            while not READY.exists():
                if tunnel.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('Dot was not ready within the test window')
                time.sleep(.2)
            READY.unlink()
            call = subprocess.Popen([sys.executable, str(ROOT/'mac-bridge/bridge.py'),
                '--public-url', origin, '--dot-ready', '--duration', '120'])
            try:
                code = call.wait(timeout=190)
            except subprocess.TimeoutExpired:
                raise RuntimeError('Local call exceeded the test deadline')
            if code:
                raise RuntimeError('Bridge failed; inspect its sanitized log')
    finally:
        stop(call)
        try:
            if SNAPSHOT.exists():
                routing('restore')
        finally:
            stop(tunnel)
            READY.unlink(missing_ok=True)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Test stopped; cleanup attempted.', flush=True)
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
