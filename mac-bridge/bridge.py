"""Owner-only, single-call Mac audio bridge. No additional language/voice model."""
import argparse
import asyncio
import base64
import contextlib
import json
import os
import re
from pathlib import Path
import secrets
import signal
import ssl
import time
from urllib.parse import urlsplit
from xml.sax.saxutils import quoteattr

import aiohttp
from aiohttp import web
import certifi
import numpy as np
from twilio.request_validator import RequestValidator

from audio import AudioBridge, encode, decode
from browser_audio_check import verify_browser_audio
from tunnel_dns import TunnelResolver

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {'completed', 'failed', 'busy', 'no-answer', 'canceled'}


def log(event, **fields):
    print(json.dumps({'event': event, **fields}), flush=True)


class Session:
    def __init__(self, origin, account, token, audio, duration=120, observer=None, start_dot=None):
        self.observer = observer or (lambda status: None)
        self.origin = origin.rstrip('/')
        self.account = account
        self.validator = RequestValidator(token)
        self.audio = audio
        self.duration = duration
        self.nonce = secrets.token_hex(24)
        self.probe_response = secrets.token_hex(24)
        self.call_sid = None
        self.stream_sid = None
        self.call_ready = asyncio.Event()
        self.done = asyncio.Event()
        self.closed = False
        self.claimed = False
        self.ws = None
        self.sender_stalls = 0
        self.sender_max_late_ms = 0.0
        self.pending_marks = {}
        self.mark_sequence = self.marks_acked = self.marks_expired = 0
        self.mark_max_round_trip_ms = 0.0
        self.reason = None
        self.start_dot = start_dot
        self.dot_connected = start_dot is None
        self.dot_task = None
        self.dot_error = None
        self.dot_connected_at = None
        self.heard_dot = False
        self.tone_samples = 0

    def outcome(self):
        if self.dot_error:
            return {'status':'failed', 'code':self.dot_error}
        if self.reason in {'phone_failed','phone_busy','phone_no-answer','phone_canceled'}:
            return {'status':'failed', 'code':self.reason.upper().replace('-','_')}
        if self.reason in {'media_error','control_lost','poll_failed','sender_failed'}:
            return {'status':'failed', 'code':self.reason.upper()}
        if not self.stream_sid:
            return {'status':'failed','code':'PHONE_MEDIA_NOT_CONNECTED'}
        metrics = self.audio.metrics()
        if not metrics.get('phone_audio_seconds'):
            return {'status':'failed','code':'PHONE_AUDIO_NOT_RECEIVED'}
        if metrics.get('dot_peak',0) < .01:
            return {'status':'failed','code':'DOT_AUDIO_SILENT'}
        return {'status':'completed'}

    def valid_signature(self, request, params):
        signature = request.headers.get('X-Twilio-Signature', '')
        url = self.origin + request.path
        urls = [url]
        if request.path.startswith('/media/'):
            # Twilio's documented handshake normalization can include a slash.
            urls += [url + '/', url.replace('https://', 'wss://', 1), url.replace('https://', 'wss://', 1) + '/']
        return bool(signature) and not request.query_string and any(
            self.validator.validate(u, params, signature) for u in urls)

    async def finish(self, reason):
        if self.closed:
            return
        self.closed = True
        self.reason = reason
        if self.dot_task and self.dot_task is not asyncio.current_task():
            self.dot_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.dot_task
        self.audio.close()
        self.done.set()
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
        log('bridge_closed', reason=reason, **self.audio.metrics(),
            sender_stalls=self.sender_stalls,
            sender_max_late_ms=round(self.sender_max_late_ms, 2),
            playback_marks_acked=self.marks_acked, playback_marks_expired=self.marks_expired,
            playback_mark_max_round_trip_ms=round(self.mark_max_round_trip_ms, 2))

    async def connect_dot(self):
        try:
            await asyncio.wait_for(self.start_dot(), 45)
            if not self.closed:
                self.dot_connected = True
                self.dot_connected_at = time.monotonic()
                log('dot_connected_after_answer')
        except asyncio.CancelledError:
            raise
        except Exception as error:
            message = str(error)
            self.dot_error = message if re.fullmatch(r'[A-Z_]{1,60}', message) else 'DOT_START_FAILED'
            await self.finish('dot_start_failed')

    def connecting_packet(self):
        # A quiet ringback cadence, sent only to the handset. Never feed it to Dot.
        samples = np.arange(160) + self.tone_samples
        self.tone_samples += 160
        t = samples / 8000
        tone = .06 * (np.sin(2*np.pi*440*t) + np.sin(2*np.pi*480*t))
        tone *= (samples % 32000 < 8000)
        return encode(tone)

    async def status(self, request):
        params = await request.post()
        if not self.valid_signature(request, params):
            raise web.HTTPForbidden()
        if params.get('CallSid') == self.call_sid and params.get('CallStatus') in TERMINAL:
            await self.finish('phone_' + params['CallStatus'])
        return web.Response(text='ok')

    async def probe(self, request):
        # Connectivity only. Never claim the phone stream or open audio devices.
        ws = web.WebSocketResponse(max_msg_size=1024)
        await ws.prepare(request)
        await ws.send_str(self.probe_response)
        await ws.close()
        return ws

    async def verify_public_connection(self, client=None, timeout=20):
        if client is None:
            # Do not send the Twilio REST client's credentials to a tunnel.
            context = ssl.create_default_context(cafile=certifi.where())
            resolver=TunnelResolver(urlsplit(self.origin).hostname)
            try:
                async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=context,resolver=resolver)) as probe_client:
                    return await self.verify_public_connection(probe_client, timeout)
            finally:
                if resolver.fallback_count:log('public_bridge_dns_fallback',count=resolver.fallback_count)
                await resolver.close()
        started=time.monotonic()
        attempts=0
        reason='timeout'
        http_status=None
        try:
            async with asyncio.timeout(timeout):
                while not self.done.is_set():
                    attempts+=1
                    reason='timeout'
                    http_status=None
                    try:
                        async with asyncio.timeout(3):
                            async with client.ws_connect(self.origin+'/probe/'+self.nonce,
                                    max_msg_size=1024) as ws:
                                message = await ws.receive()
                                if message.type == aiohttp.WSMsgType.TEXT and message.data == self.probe_response:
                                    if self.done.is_set():raise RuntimeError('CALL_CANCELED_BEFORE_DIAL')
                                    log('public_bridge_verified',attempts=attempts,elapsed_seconds=round(time.monotonic()-started,2))
                                    return
                                reason='unexpected_response'
                    except aiohttp.ClientConnectorDNSError:
                        reason='dns_lookup_failed'
                    except (aiohttp.ClientConnectorCertificateError,aiohttp.ClientSSLError):
                        reason='tls_failed'
                    except aiohttp.WSServerHandshakeError as error:
                        reason='http_error';http_status=error.status
                    except asyncio.TimeoutError:
                        reason='timeout'
                    except aiohttp.ClientError:
                        reason='connection_failed'
                    await asyncio.sleep(.5)
        except asyncio.TimeoutError:
            pass
        if self.done.is_set():raise RuntimeError('CALL_CANCELED_BEFORE_DIAL')
        diagnostics={'stage':'public_bridge_preflight','phone_dialed':False,
            'reason':reason,'attempts':attempts,'elapsed_seconds':round(time.monotonic()-started,2)}
        if http_status is not None:diagnostics['http_status']=http_status
        log('public_bridge_failed',**diagnostics)
        error=RuntimeError('PUBLIC_BRIDGE_UNREACHABLE')
        error.diagnostics=diagnostics
        raise error

    async def media(self, request):
        if not self.valid_signature(request, {}):
            log('rejected_media_signature')
            raise web.HTTPForbidden()
        if self.claimed or self.closed or self.done.is_set():
            raise web.HTTPConflict()
        self.claimed = True
        ws = self.ws = web.WebSocketResponse(heartbeat=15, max_msg_size=32768)
        sender = None
        try:
            await ws.prepare(request)
            # REST response normally arrives before answer; tolerate a quick answer.
            await asyncio.wait_for(self.call_ready.wait(), timeout=10)
            async with asyncio.timeout(self.duration + 10):
                while not self.closed:
                    msg = await asyncio.wait_for(ws.receive(), timeout=15)
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        break
                    data = json.loads(msg.data)
                    event = data.get('event')
                    if event == 'connected':
                        continue
                    if event == 'start':
                        if self.done.is_set():
                            break
                        start = data.get('start', {})
                        fmt = start.get('mediaFormat', {})
                        if (self.stream_sid or start.get('accountSid') != self.account or
                                start.get('callSid') != self.call_sid or
                                fmt != {'encoding': 'audio/x-mulaw', 'sampleRate': 8000, 'channels': 1}):
                            raise ValueError('unexpected_stream')
                        self.stream_sid = start['streamSid']
                        # Ringing-time capture is not live call loss. Reset the
                        # filters and counters before Dot begins its greeting.
                        self.audio.reset_for_call()
                        self.audio.start()
                        log('audio_connected')
                        self.observer('connected')
                        sender = asyncio.create_task(self.send_audio())
                        sender.add_done_callback(lambda task: self.fail_task(task, 'sender_failed'))
                        if self.start_dot:
                            self.dot_task = asyncio.create_task(self.connect_dot())
                    elif event == 'media':
                        if not self.stream_sid or data.get('streamSid') != self.stream_sid:
                            raise ValueError('unexpected_media')
                        media = data.get('media', {})
                        if media.get('track') != 'inbound':
                            continue
                        payload = base64.b64decode(media['payload'], validate=True)
                        if not 0 < len(payload) <= 8000:
                            raise ValueError('invalid_audio_size')
                        if not self.dot_connected:
                            continue
                        # Dot already owns speech/turn detection. A level gate
                        # cannot distinguish a real interruption from noise or
                        # speakerphone echo and must not delete its reply.
                        self.audio.receive(payload)
                    elif event == 'mark':
                        if not self.stream_sid or data.get('streamSid') != self.stream_sid:
                            raise ValueError('unexpected_mark')
                        sent_at = self.pending_marks.pop(data.get('mark', {}).get('name'), None)
                        if sent_at is not None:
                            self.marks_acked += 1
                            self.mark_max_round_trip_ms = max(self.mark_max_round_trip_ms,
                                (time.monotonic() - sent_at) * 1000)
                    elif event == 'stop':
                        break
        except (ValueError, KeyError, asyncio.TimeoutError, aiohttp.ClientError):
            log('media_session_failed')
            await self.finish('media_error')
        finally:
            if sender:
                sender.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await sender
            await self.finish('media_disconnected')
        return ws

    def fail_task(self, task, reason):
        if not task.cancelled() and task.exception() and not self.closed:
            self.reason = reason
            self.done.set()

    async def send_audio(self):
        deadline = time.monotonic()
        next_mark = deadline + .5
        while not self.closed and not self.ws.closed:
            now = time.monotonic()
            late = now - deadline
            if late > .04:
                self.sender_stalls += 1
                self.sender_max_late_ms = max(self.sender_max_late_ms, late * 1000)
                # Resume from now without clearing audio already at Twilio.
                # The local FIFO bounds delay if a stall is unusually long.
                deadline = now
            packet = self.audio.next_packet()
            if not self.heard_dot and np.max(np.abs(decode(packet)), initial=0) > .01:
                self.heard_dot = True
            if self.start_dot and not self.heard_dot:
                if self.dot_connected_at and time.monotonic() - self.dot_connected_at > 15:
                    self.dot_error = 'DOT_AUDIO_SILENT'
                    self.reason = 'dot_audio_silent'
                    self.done.set()
                    return
                packet = self.connecting_packet()
            await asyncio.wait_for(self.ws.send_json({'event': 'media', 'streamSid': self.stream_sid,
                                    'media': {'payload': base64.b64encode(packet).decode()}}), 2)
            now = time.monotonic()
            if now >= next_mark:
                # Marks measure network round trip plus remote playback delay,
                # not pure queue length. Missing acknowledgments stay bounded.
                for name, sent_at in list(self.pending_marks.items()):
                    if now - sent_at >= 5:
                        del self.pending_marks[name]
                        self.marks_expired += 1
                self.mark_sequence += 1
                name = f'playback-{self.mark_sequence}'
                self.pending_marks[name] = now
                await asyncio.wait_for(self.ws.send_json({'event': 'mark',
                    'streamSid': self.stream_sid, 'mark': {'name': name}}), 2)
                next_mark = now + .5
            deadline += .02
            await asyncio.sleep(max(0, deadline - time.monotonic()))

    def app(self):
        app = web.Application(client_max_size=16384)
        app.router.add_get('/media/' + self.nonce, self.media)
        app.router.add_post('/status/' + self.nonce, self.status)
        app.router.add_get('/probe/' + self.nonce, self.probe)
        return app


async def check_no_active_calls(request, exclude_sid=None):
    """Read independent provider states together; all must pass before dialing."""
    started = time.monotonic()
    tasks = [asyncio.create_task(request('GET', '/Calls.json',
             params={'Status': status, 'PageSize': '2' if exclude_sid else '1'}))
             for status in ('queued', 'ringing', 'in-progress')]
    try:
        results = await asyncio.gather(*tasks)
        if any(exclude_sid is None or call.get('sid') != exclude_sid
               for result in results for call in result.get('calls', [])):
            raise RuntimeError('Another Twilio call is active; no call placed')
        log('call_setup_phase', phase='provider_idle_check', seconds=round(time.monotonic()-started, 3))
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run(args):
    setup_started = getattr(args, 'setup_started', time.monotonic())
    incoming_sid = getattr(args, 'inbound_call_sid', None)
    if incoming_sid is not None and not re.fullmatch(r'CA[0-9a-f]{32}', incoming_sid):
        raise ValueError('INVALID_INBOUND_CALL')
    origin = args.public_url.rstrip('/')
    url = urlsplit(origin)
    if url.scheme != 'https' or not url.hostname or url.path or url.query or url.fragment or url.username:
        raise ValueError('public-url must be an HTTPS origin')
    start_dot = getattr(args, 'start_dot', None)
    if not args.dot_ready and start_dot is None:
        raise ValueError('Start and verify the Dot voice session first, then use --dot-ready')
    creds = json.loads((ROOT/'.secrets/twilio.json').read_text())
    defaults = json.loads((ROOT/'.secrets/setup-defaults.json').read_text())
    # The prototype only permits the owner and caller already authorized for this project.
    if not all(re.fullmatch(r'\+[1-9][0-9]{7,14}', defaults.get(key, '')) for key in ('phone', 'from')):
        raise ValueError('Configure valid saved owner and Twilio numbers')
    audio = getattr(args, 'audio', None) or AudioBridge()
    state = Session(origin, creds['TWILIO_ACCOUNT_SID'], creds['TWILIO_AUTH_TOKEN'], audio, args.duration, getattr(args, 'observer', None), start_dot)
    if getattr(args, 'on_state', None):
        args.on_state(state)
    runner = web.AppRunner(state.app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', args.port).start()
    loop = asyncio.get_running_loop()
    if getattr(args, 'manage_signals', True):
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, state.done.set)
    base = f'https://api.twilio.com/2010-04-01/Accounts/{state.account}'
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    auth = aiohttp.BasicAuth(state.account, creds['TWILIO_AUTH_TOKEN'])
    session = aiohttp.ClientSession(auth=auth, connector=aiohttp.TCPConnector(ssl=ssl_context),
                                    timeout=aiohttp.ClientTimeout(total=15))
    poller = None
    async def request(method, path, **kwargs):
        async with session.request(method, base+path, allow_redirects=False, **kwargs) as response:
            if response.status >= 300:
                raise RuntimeError(f'Twilio request failed: HTTP {response.status}')
            return await response.json()
    async def poll():
        while not state.closed:
            await asyncio.sleep(2)
            data = await request('GET', f'/Calls/{state.call_sid}.json')
            if data.get('status') in TERMINAL:
                await state.finish('phone_'+data['status'])
                return
    try:
        await state.verify_public_connection()
        audio.start()
        if start_dot is None:
            # Legacy/manual bridge retains its explicit spoken preflight.
            proof = await verify_browser_audio(audio, canceled=state.done)
            if proof.get('verified') is not True:
                raise RuntimeError('DOT_AUDIO_NOT_VERIFIED')
            log('browser_audio_verified', reply_peak=proof['reply_peak'])
        elif getattr(args, 'local_audio_verified', False) is not True:
            raise RuntimeError('LOCAL_AUDIO_NOT_VERIFIED')
        if state.done.is_set():
            raise RuntimeError('CALL_CANCELED_BEFORE_DIAL')
        if incoming_sid:
            # Revalidate provider identity before attaching to or ending this call.
            incoming = await request('GET', f'/Calls/{incoming_sid}.json')
            if (incoming.get('sid') != incoming_sid or incoming.get('account_sid') != state.account
                    or incoming.get('direction') != 'inbound' or incoming.get('from') != defaults['phone']
                    or incoming.get('to') != defaults['from']):
                raise RuntimeError('INBOUND_CALL_IDENTITY_MISMATCH')
            if incoming.get('status') != 'in-progress':
                raise RuntimeError('INBOUND_CALL_ALREADY_ENDED')
            state.call_sid = incoming_sid
        # The authenticated incoming call itself is allowed; every other call blocks.
        await check_no_active_calls(request, exclude_sid=incoming_sid)
        stream_url = origin.replace('https://', 'wss://', 1) + '/media/' + state.nonce
        twiml = '<Response><Connect><Stream url='+quoteattr(stream_url)+'/></Connect><Hangup/></Response>'
        # Exactly one POST, deliberately no automatic retry if the outcome is unknown.
        if state.done.is_set():
            raise RuntimeError('CALL_CANCELED_BEFORE_DIAL')
        if incoming_sid:
            state.call_ready.set()
            # Bound the existing call before publishing the signed stream route.
            # This updates the incoming call; it never creates an outgoing call.
            await request('POST', f'/Calls/{incoming_sid}.json', data={'TimeLimit':str(args.duration)})
            if state.done.is_set(): raise RuntimeError('INBOUND_CALL_ALREADY_ENDED')
            await args.inbound_ready(stream_url)
            log('inbound_bridge_ready', setup_seconds=round(time.monotonic()-setup_started, 3))
        else:
            log('dialing_owner', maximum_seconds=args.duration,
                setup_seconds=round(time.monotonic()-setup_started, 3))
            created = await request('POST', '/Calls.json', data={
                'To': defaults['phone'], 'From': defaults['from'], 'Twiml': twiml,
                'Timeout': '25', 'TimeLimit': str(args.duration),
                'StatusCallback': origin+'/status/'+state.nonce,
                'StatusCallbackEvent': 'completed', 'StatusCallbackMethod': 'POST'})
            state.call_sid = created['sid']
            state.call_ready.set()
        path = ROOT/'.runtime/mac-bridge-call.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({'call_sid': state.call_sid, 'started_at': time.time()}))
        os.chmod(path, 0o600)
        log('inbound_call_attached' if incoming_sid else 'call_created', call_sid=state.call_sid,
            setup_seconds=round(time.monotonic()-setup_started, 3))
        state.observer('ringing')
        poller = asyncio.create_task(poll())
        poller.add_done_callback(lambda task: state.fail_task(task, 'poll_failed'))
        await asyncio.wait_for(state.done.wait(), timeout=args.duration + 35)
    finally:
        if poller:
            poller.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await poller
        await state.finish(state.reason or 'local_shutdown')
        if state.call_sid:
            try:
                status = await request('GET', f'/Calls/{state.call_sid}.json')
                if status.get('status') in {'failed','busy','no-answer','canceled'}:
                    state.reason = 'phone_' + status['status']
                if status.get('status') not in TERMINAL:
                    await request('POST', f'/Calls/{state.call_sid}.json', data={'Status': 'completed'})
                log('call_ended')
            except Exception:
                log('hangup_check_failed', maximum_seconds=args.duration)
        await session.close()
        await runner.cleanup()
    return state.outcome()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public-url', required=True)
    parser.add_argument('--port', type=int, default=8891)
    parser.add_argument('--duration', type=int, choices=range(20, 601), default=120)
    parser.add_argument('--dot-ready', action='store_true')
    try:
        result = asyncio.run(run(parser.parse_args()))
        if result['status'] != 'completed':
            log('bridge_failed', code=result.get('code', 'CALL_FAILED'))
            raise SystemExit(1)
    except KeyboardInterrupt:
        pass
    except Exception as error:
        # Do not echo exception bodies that can contain authenticated request data.
        log('bridge_failed', error_type=type(error).__name__)
        raise SystemExit(1)
