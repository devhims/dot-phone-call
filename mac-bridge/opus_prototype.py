"""Isolated Voice SDK prototype. Default mode cannot generate PSTN Dial TwiML.

Run with .venv/bin/python mac-bridge/opus_prototype.py. Open the printed local
URL. The public tunnel serves only signed Twilio callbacks, never tokens/UI.
Temporary API key/application are deleted on shutdown. Production is unchanged.
"""
import argparse
import asyncio
import contextlib
import json
from pathlib import Path
import secrets
import signal
import time

from aiohttp import web
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse

from public_tunnel import open_verified_tunnel

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'prototype/opus'
RUNTIME = ROOT / '.runtime'
RESOURCES = RUNTIME / 'opus-prototype-resources.json'


class Prototype:
    def __init__(self, account, token, owner, caller, *, allow_owner=False, port=8893, answer_on_bridge=True):
        self.account, self.auth_token = account, token
        self.owner, self.caller = owner, caller
        self.allow_owner = allow_owner
        self.answer_on_bridge = answer_on_bridge
        self.local_origin = f'http://127.0.0.1:{port}'
        self.origin = None
        self.nonce = secrets.token_hex(24)
        self.identity = 'opus_sender_' + secrets.token_hex(8)
        self.receiver = 'opus_receiver_' + secrets.token_hex(8)
        self.ticket = None
        self.mode = None
        self.parent = None
        self.calls = set()
        self.events = []
        self.validator = RequestValidator(token)
        self.client = Client(account, token)
        self.key_sid = self.key_secret = self.app_sid = None
        self.done = asyncio.Event()
        self.result = {'phone_dialed': False, 'verified': False}

    def local_request(self, request, mutation=False):
        if request.host != self.local_origin.removeprefix('http://'):
            raise web.HTTPForbidden()
        if mutation and request.headers.get('Origin') != self.local_origin:
            raise web.HTTPForbidden()
        navigation = request.method == 'GET' and request.path == '/' and request.headers.get('Sec-Fetch-Mode') == 'navigate'
        if request.headers.get('Sec-Fetch-Site') == 'cross-site' and not navigation:
            raise web.HTTPForbidden()

    def twiml(self, params):
        """Policy is server-side; browser parameters cannot choose a number."""
        if (params.get('AccountSid') != self.account
                or params.get('From') != 'client:' + self.identity
                or not self.ticket or params.get('Ticket') != self.ticket
                or params.get('Mode') != self.mode
                or not params.get('CallSid', '').startswith('CA')):
            raise web.HTTPForbidden()
        sid = params['CallSid']
        if self.parent and self.parent != sid:
            raise web.HTTPConflict(text='A probe has already been started')
        response = VoiceResponse()
        if self.mode == 'probe':
            dial = response.dial(timeout=10, time_limit=40, answer_on_bridge=self.answer_on_bridge,
                                 record='do-not-record')
            dial.client(self.receiver)
        elif self.mode == 'owner' and self.allow_owner:
            dial = response.dial(caller_id=self.caller, timeout=20, time_limit=120,
                                 answer_on_bridge=True, record='do-not-record')
            dial.number(self.owner)
            self.result['phone_dialed'] = True
        else:
            raise web.HTTPForbidden(text='Phone dialing is disabled')
        response.hangup()
        self.parent = sid
        self.calls.add(sid)
        self.events.append({'event': 'twiml', 'mode': self.mode, 'at': time.time()})
        return str(response)

    async def signed_params(self, request):
        params = dict(await request.post())
        if not self.origin or request.query_string or not self.validator.validate(
                self.origin + request.path, params, request.headers.get('X-Twilio-Signature', '')):
            raise web.HTTPForbidden()
        return params

    async def voice(self, request):
        xml = self.twiml(await self.signed_params(request))
        self.save_resources()
        return web.Response(text=xml, content_type='text/xml')

    async def status(self, request):
        params = await self.signed_params(request)
        if params.get('AccountSid') != self.account or params.get('CallSid') not in self.calls:
            raise web.HTTPForbidden()
        self.events.append({'event': 'call_status', 'status': params.get('CallStatus'), 'at': time.time()})
        return web.Response(status=204)

    def public_app(self):
        app = web.Application(client_max_size=16384)
        app.router.add_post('/voice/' + self.nonce, self.voice)
        app.router.add_post('/status/' + self.nonce, self.status)
        return app

    def mint(self, identity, incoming=False):
        token = AccessToken(self.account, self.key_sid, self.key_secret, identity=identity, ttl=600)
        token.add_grant(VoiceGrant(incoming_allow=incoming,
            outgoing_application_sid=None if incoming else self.app_sid))
        return token.to_jwt()

    async def tokens(self, request):
        self.local_request(request, True)
        mode = (await request.json()).get('mode')
        if mode not in {'probe', 'owner'} or (mode == 'owner' and not self.allow_owner):
            raise web.HTTPForbidden(text='Phone dialing is disabled')
        if self.ticket:
            raise web.HTTPConflict(text='One test per prototype session; restart for another')
        if (RUNTIME / 'mac-audio-original.json').exists():
            raise web.HTTPConflict(text='An existing Dot call owns the audio route')
        self.ticket, self.mode = secrets.token_urlsafe(32), mode
        return web.json_response({'caller': self.mint(self.identity),
            'receiver': self.mint(self.receiver, incoming=True) if mode == 'probe' else None,
            'ticket': self.ticket, 'mode': mode})

    async def config(self, request):
        self.local_request(request)
        return web.json_response({'owner_enabled': self.allow_owner, 'answer_on_bridge': self.answer_on_bridge, 'sdk_version':
            json.loads((ROOT/'node_modules/@twilio/voice-sdk/package.json').read_text())['version']})

    async def result_handler(self, request):
        self.local_request(request, True)
        report = await request.json()
        # Keep browser evidence under its own key; it cannot overwrite dial policy.
        self.result['browser'] = report
        self.result['verified'] = report.get('verified') is True and not self.result['phone_dialed']
        self.save_result()
        return web.Response(status=204)

    def save_result(self):
        (RUNTIME/'opus-prototype-result.json').write_text(json.dumps(
            {**self.result, 'events': self.events}, indent=2)+'\n')

    async def shutdown(self, request):
        self.local_request(request, True)
        self.done.set()
        return web.Response(text='Stopping prototype')

    async def asset(self, request):
        self.local_request(request)
        name = request.match_info.get('name') or 'index.html'
        if name == 'twilio.min.js':
            path = ROOT/'node_modules/@twilio/voice-sdk/dist/twilio.min.js'
        elif name in {'index.html', 'app.js', 'transport.js', 'verdict.js', 'packet-probe.js'}:
            path = ASSETS/name
        else:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={'Cache-Control': 'no-store',
            'Content-Security-Policy': "frame-ancestors 'none'", 'Referrer-Policy': 'no-referrer'})

    def local_app(self):
        app = web.Application(client_max_size=131072)
        app.router.add_get('/config', self.config)
        app.router.add_post('/tokens', self.tokens)
        app.router.add_post('/result', self.result_handler)
        app.router.add_post('/shutdown', self.shutdown)
        app.router.add_get('/', self.asset)
        app.router.add_get('/{name}', self.asset)
        return app

    def save_resources(self):
        RESOURCES.write_text(json.dumps({'app_sid': self.app_sid, 'key_sid': self.key_sid,
                                        'calls': sorted(self.calls)}, indent=2)+'\n')
        RESOURCES.chmod(0o600)

    async def provision(self):
        app = await asyncio.to_thread(self.client.applications.create,
            friendly_name='Dot Opus isolated prototype', voice_url=self.origin+'/voice/'+self.nonce,
            voice_method='POST', status_callback=self.origin+'/status/'+self.nonce,
            status_callback_method='POST')
        self.app_sid = app.sid
        self.save_resources()
        key = await asyncio.to_thread(self.client.new_keys.create, friendly_name='Dot Opus temporary SDK key')
        self.key_sid, self.key_secret = key.sid, key.secret
        self.save_resources()

    async def cleanup(self):
        errors = []
        for sid in self.calls:
            try:
                call = await asyncio.to_thread(self.client.calls(sid).fetch)
                if call.status not in {'completed', 'canceled', 'failed', 'busy', 'no-answer'}:
                    await asyncio.to_thread(self.client.calls(sid).update, status='completed')
            except Exception as error:
                errors.append('call_cleanup:' + type(error).__name__)
        for kind, sid in [('key', self.key_sid), ('application', self.app_sid)]:
            if sid:
                try:
                    resource = self.client.keys(sid) if kind == 'key' else self.client.applications(sid)
                    await asyncio.to_thread(resource.delete)
                except Exception as error:
                    errors.append(kind + '_cleanup:' + type(error).__name__)
        self.result['cleanup_ok'] = not errors
        self.result['cleanup_errors'] = errors
        self.save_result()
        if not errors:
            RESOURCES.unlink(missing_ok=True)


async def main(args):
    if RESOURCES.exists():
        raise RuntimeError('Previous prototype resources need cleanup before another run')
    credentials = json.loads((ROOT/'.secrets/twilio.json').read_text())
    defaults = json.loads((ROOT/'.secrets/setup-defaults.json').read_text())
    state = Prototype(credentials['TWILIO_ACCOUNT_SID'], credentials['TWILIO_AUTH_TOKEN'],
                      defaults['phone'], defaults['from'], allow_owner=args.enable_owner_dial,
                      answer_on_bridge=not args.answer_immediately)
    tunnel = None
    runners = []
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, state.done.set)
    try:
        tunnel = await open_verified_tunnel(port=8894)
        state.origin = tunnel.origin
        for app, port in [(state.public_app(), 8894), (state.local_app(), 8893)]:
            runner = web.AppRunner(app, access_log=None)
            runners.append(runner)
            await runner.setup()
            await web.TCPSite(runner, '127.0.0.1', port).start()
        await state.provision()
        print(json.dumps({'ready': True, 'url': state.local_origin,
                          'owner_dial_enabled': state.allow_owner}), flush=True)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(state.done.wait(), 900)
    finally:
        await state.cleanup()
        for runner in runners:
            await runner.cleanup()
        if tunnel:
            await tunnel.close()
        print(json.dumps({'finished': True, 'cleanup_ok': state.result.get('cleanup_ok'),
                          'phone_dialed': state.result['phone_dialed']}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--enable-owner-dial', action='store_true',
                        help='Explicitly enable the separate saved-number test. Never needed for the probe.')
    parser.add_argument('--answer-immediately', action='store_true',
                        help='Diagnostic: answer the probe before connecting its client leg.')
    asyncio.run(main(parser.parse_args()))
