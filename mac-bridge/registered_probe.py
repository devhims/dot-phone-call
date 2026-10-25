"""No-webhook Twilio probe. Only two random client identities can be called.

No phone numbers, Cloudflare, TwiML applications, or public listeners are used.
The browser starts on navigation without a button. Temporary resources expire
and are cleaned up on result, timeout, or SIGTERM. A leftover manifest blocks
another run until its resources have been inspected and cleaned up.
"""
import argparse
import asyncio
import json
from pathlib import Path
import secrets
import signal
import time
import os
import queue
import fcntl
import numpy as np
import sounddevice as sd

from aiohttp import web
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from twilio.rest import Client
from twilio.http.http_client import TwilioHttpClient
from twilio.twiml.voice_response import VoiceResponse

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.runtime'
TERMINAL = {'completed', 'canceled', 'failed', 'busy', 'no-answer'}


class RegisteredProbe:
    def __init__(self, account, token, port=8893, native_audio=False):
        self.client = Client(account, token, http_client=TwilioHttpClient(timeout=15))
        self.account = account
        self.origin = f'http://127.0.0.1:{port}'
        self.identities = ['registered_' + secrets.token_hex(10) for _ in range(2)]
        self.key_sid = self.key_secret = self.parent = None
        self.issued = self.started = False
        self.native_audio = native_audio
        self.outputs = []
        self.audio_queue = queue.Queue(maxsize=12)
        self.pending_audio = np.empty(0, dtype=np.float32)
        self.audio_stats = {'received_frames': 0, 'played_frames': 0, 'dropped_chunks': 0}
        self.audio_capability = secrets.token_urlsafe(32)
        self.audio_claimed = False
        self.audio_lock = None
        self.sockets = set()
        self.stopping = False
        self.done = asyncio.Event()
        self.report = {'phone_dialed': False, 'public_listener': False,
                       'cloudflare_used': False, 'events': [], 'verified': False}

    def check(self, request):
        if request.host != self.origin.removeprefix('http://'):
            raise web.HTTPForbidden()
        if request.method == 'POST' and request.headers.get('Origin') != self.origin:
            raise web.HTTPForbidden()
        if self.stopping and request.path != '/result':
            raise web.HTTPServiceUnavailable(text='Probe is stopping')
        navigation = request.method == 'GET' and request.path == '/' and request.headers.get('Sec-Fetch-Mode') == 'navigate'
        if request.headers.get('Sec-Fetch-Site') == 'cross-site' and not navigation:
            raise web.HTTPForbidden()

    def save(self):
        path = RUNTIME / 'registered-probe-resources.json'
        path.write_text(json.dumps({'key_sid': self.key_sid, 'parent': self.parent,
                                   'identities': self.identities}) + '\n')
        path.chmod(0o600)

    def event(self, name):
        self.report['events'].append({'event': name, 'at': time.time()})
        self.save_report()

    def save_report(self):
        path = RUNTIME / 'registered-probe-result.json'
        path.write_text(json.dumps(self.report, indent=2) + '\n')
        path.chmod(0o600)

    async def provision(self):
        key = await asyncio.to_thread(self.client.new_keys.create,
                                     friendly_name='Dot registered client temporary probe')
        self.key_sid, self.key_secret = key.sid, key.secret
        self.save()
        self.save_report()

    def mint(self, identity):
        token = AccessToken(self.account, self.key_sid, self.key_secret, identity=identity, ttl=600)
        token.add_grant(VoiceGrant(incoming_allow=True))
        return token.to_jwt()

    async def tokens(self, request):
        self.check(request)
        if self.issued or (RUNTIME / 'mac-audio-original.json').exists():
            raise web.HTTPConflict(text='Test already claimed or production audio is busy')
        if self.audio_lock is None:
            self.audio_lock = (RUNTIME / 'mac-companion.lock').open('a')
            try:
                fcntl.flock(self.audio_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.audio_lock.close()
                self.audio_lock = None
                raise web.HTTPConflict(text='Native helper is already using audio') from None
        self.issued = True
        if self.native_audio:
            self.start_native_audio()
        self.event('tokens_issued')
        return web.json_response({'tokens': [self.mint(i) for i in self.identities],
            'native_audio': self.native_audio,
            'audio_capability': self.audio_capability if self.native_audio else None})

    def start_native_audio(self, *, fixture=True):
        def device(name):
            return next(i for i, d in enumerate(sd.query_devices())
                        if d['name'] == name and d['max_output_channels'] >= 2)
        phase = 0
        def fixture_output(data, frames, timing, status):
            nonlocal phase
            data[:] = (.06 * np.sin(2*np.pi*997*(np.arange(frames)+phase)/48000))[:, None]
            phase += frames
        def playout(data, frames, timing, status):
            data.fill(0)
            offset = 0
            while offset < frames:
                if not len(self.pending_audio):
                    try:
                        self.pending_audio = self.audio_queue.get_nowait()
                    except queue.Empty:
                        break
                count = min(frames-offset, len(self.pending_audio))
                data[offset:offset+count] = self.pending_audio[:count, None]
                self.pending_audio = self.pending_audio[count:]
                offset += count
                self.audio_stats['played_frames'] += count
        outputs = [('BlackHole 16ch', playout)]
        if fixture:
            outputs.insert(0, ('BlackHole 2ch', fixture_output))
        for name, callback in outputs:
            stream = sd.OutputStream(device=device(name), channels=2, samplerate=48000,
                                     blocksize=960, dtype='float32', callback=callback)
            self.outputs.append(stream)
            stream.start()

    async def audio(self, request):
        self.check(request)
        if (not self.native_audio or not self.issued or self.audio_claimed
                or request.headers.get('Origin') != self.origin):
            raise web.HTTPForbidden()
        ws = web.WebSocketResponse(max_msg_size=4096, heartbeat=10)
        await ws.prepare(request)
        first = await asyncio.wait_for(ws.receive(), 5)
        if first.type != web.WSMsgType.TEXT or not secrets.compare_digest(first.data, self.audio_capability):
            await ws.close(code=1008)
            return ws
        self.audio_claimed = True
        self.sockets.add(ws)
        await ws.send_str('ready')
        async for message in ws:
            if message.type == web.WSMsgType.BINARY:
                if len(message.data) != 960 * 4:
                    await ws.close(code=1008)
                    break
                samples = np.frombuffer(message.data, dtype='<f4').copy()
                if not np.isfinite(samples).all() or np.max(np.abs(samples)) > 1:
                    await ws.close(code=1008)
                    break
                self.audio_stats['received_frames'] += len(samples)
                if self.audio_queue.full():
                    try:
                        self.audio_queue.get_nowait()
                        self.audio_stats['dropped_chunks'] += 1
                    except queue.Empty:
                        pass
                self.audio_queue.put_nowait(samples)
        self.sockets.discard(ws)
        return ws

    def call_options(self):
        response = VoiceResponse()
        dial = response.dial(timeout=15, time_limit=45, answer_on_bridge=True, record='do-not-record')
        dial.client(self.identities[1])
        response.hangup()
        return {'to': 'client:' + self.identities[0],
                'from_': 'client:registered_probe', 'twiml': str(response),
                'timeout': 15, 'time_limit': 60, 'record': False}

    async def start(self, request):
        self.check(request)
        # Commit the one-shot guard before awaiting the external request.
        if not self.issued or self.started or (RUNTIME / 'mac-audio-original.json').exists():
            raise web.HTTPConflict()
        self.started = True
        self.save()
        try:
            call = await asyncio.to_thread(self.client.calls.create, **self.call_options())
        except Exception:
            raise web.HTTPBadGateway(text='Client creation failed; no retry is permitted') from None
        self.parent = call.sid
        self.save()
        self.event('client_call_created')
        return web.json_response({'started': True})

    async def refresh(self, request):
        self.check(request)
        if not self.issued:
            raise web.HTTPConflict()
        self.event('tokens_refreshed')
        return web.json_response({'tokens': [self.mint(i) for i in self.identities]})

    async def progress(self, request):
        self.check(request)
        body = await request.json()
        stage = str(body.get('stage', ''))[:100]
        self.event('browser:' + stage)
        return web.Response(status=204)

    async def result(self, request):
        self.check(request)
        body = await request.json()
        self.report['browser'] = body
        self.report['verified'] = body.get('verified') is True
        self.save_report()
        self.done.set()
        return web.Response(status=204)

    async def asset(self, request):
        self.check(request)
        name = request.match_info.get('name') or 'index.html'
        paths = {'index.html': ROOT/'prototype/registered/index.html',
                 'app.js': ROOT/'prototype/registered/app.js',
                 'pcm-worklet.js': ROOT/'prototype/registered/pcm-worklet.js',
                 'transport.js': ROOT/'prototype/opus/transport.js',
                 'twilio.min.js': ROOT/'node_modules/@twilio/voice-sdk/dist/twilio.min.js'}
        if name not in paths:
            raise web.HTTPNotFound()
        return web.FileResponse(paths[name], headers={'Cache-Control': 'no-store',
            'Content-Security-Policy': "frame-ancestors 'none'", 'Referrer-Policy': 'no-referrer'})

    def app(self):
        app = web.Application(client_max_size=65536)
        for route in ['tokens', 'start', 'refresh', 'progress', 'result']:
            app.router.add_post('/' + route, getattr(self, route))
        app.router.add_get('/audio', self.audio)
        app.router.add_get('/', self.asset)
        app.router.add_get('/{name}', self.asset)
        return app

    async def cleanup(self, *, preserve_manifest=False):
        self.stopping = True
        errors, calls = [], {}
        await asyncio.gather(*(ws.close(code=1001, message=b'Local helper stopped') for ws in list(self.sockets)))
        for stream in self.outputs:
            try:
                stream.stop()
                stream.close()
            except Exception as error:
                errors.append('audio:' + type(error).__name__)
        if self.audio_lock is not None:
            self.audio_lock.close()
            self.audio_lock = None
        self.report['native_audio'] = {**self.audio_stats, 'streams_closed': all(s.closed for s in self.outputs)}
        try:
            # Also discover a call whose creation response was lost.
            for identity in self.identities:
                found = await asyncio.to_thread(self.client.calls.list, to='client:' + identity, limit=20)
                calls.update({c.sid: c for c in found})
            for sid, call in calls.items():
                if call.status not in TERMINAL:
                    await asyncio.to_thread(self.client.calls(sid).update, status='completed')
            await asyncio.sleep(1)
            self.report['call_audit'] = []
            for sid in calls:
                call = await asyncio.to_thread(self.client.calls(sid).fetch)
                self.report['call_audit'].append({'sid': sid, 'parent': call.parent_call_sid,
                    'to': call.to, 'status': call.status, 'duration': call.duration})
                if call.to not in ['client:' + i for i in self.identities] or call.status not in TERMINAL:
                    errors.append('unexpected_destination_or_nonterminal_call')
        except Exception as error:
            errors.append('calls:' + type(error).__name__)
        if self.key_sid:
            try:
                await asyncio.to_thread(self.client.keys(self.key_sid).delete)
            except Exception as error:
                errors.append('key:' + type(error).__name__)
        self.report['cleanup_ok'] = not errors
        self.report['cleanup_errors'] = errors
        self.save_report()
        if not errors and not preserve_manifest:
            (RUNTIME / 'registered-probe-resources.json').unlink(missing_ok=True)


async def main(port, native_audio=False):
    RUNTIME.mkdir(exist_ok=True)
    if (RUNTIME / 'registered-probe-resources.json').exists():
        raise RuntimeError('Previous registered probe needs cleanup')
    config = json.loads((ROOT / '.secrets/inbound-worker-secrets.json').read_text())
    probe = RegisteredProbe(config['TWILIO_ACCOUNT_SID'], config['TWILIO_AUTH_TOKEN'], port, native_audio)
    runner = web.AppRunner(probe.app(), access_log=None)
    loop = asyncio.get_running_loop()
    def stop():
        probe.event('signal_shutdown')
        probe.done.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop)
    try:
        await runner.setup()
        await web.TCPSite(runner, '127.0.0.1', port).start()
        await probe.provision()
        print(json.dumps({'ready': True, 'pid': os.getpid(), 'url': probe.origin, 'phone_dialing': 'impossible'}), flush=True)
        try:
            await asyncio.wait_for(probe.done.wait(), timeout=300)
        except asyncio.TimeoutError:
            probe.event('server_timeout')
    finally:
        await probe.cleanup()
        await runner.cleanup()
        print(json.dumps({'finished': True, 'verified': probe.report['verified'],
                          'cleanup_ok': probe.report['cleanup_ok']}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8893)
    parser.add_argument('--native-audio', action='store_true')
    args = parser.parse_args()
    asyncio.run(main(args.port, args.native_audio))
