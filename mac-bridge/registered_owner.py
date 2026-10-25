"""Explicit, one-job live test. Keeps the production transport as the default."""
import asyncio
import contextlib
import json
import re
import time
from types import SimpleNamespace

from aiohttp import web
from twilio.twiml.voice_response import VoiceResponse
from registered_probe import RegisteredProbe, ROOT, RUNTIME, TERMINAL

ARM = RUNTIME / 'registered-owner-arm.json'


async def consume_arm(job):
    """Only the exact cloud job assigned to a short-lived local test can opt in."""
    if not ARM.exists():
        return False
    for _ in range(50):
        try:
            value = json.loads(ARM.read_text())
        except (FileNotFoundError, ValueError):
            return False
        if not time.time() < value.get('expires_at', 0) <= time.time() + 180:
            return False
        if value.get('call_id') == job.get('id') and job.get('direction', 'outbound') == 'outbound':
            ARM.unlink()
            return True
        if value.get('call_id') != 'pending':
            return False
        await asyncio.sleep(.1)
    raise RuntimeError('REGISTERED_TEST_ARM_NOT_RESOLVED')


class OwnerTest(RegisteredProbe):
    def __init__(self, config, companion, job):
        super().__init__(config['TWILIO_ACCOUNT_SID'], config['TWILIO_AUTH_TOKEN'], native_audio=True)
        self.owner = config['INBOUND_OWNER_NUMBER']
        self.caller = config['INBOUND_TWILIO_NUMBER']
        if not all(re.fullmatch(r'\+[1-9][0-9]{7,14}', n) for n in [self.owner, self.caller]):
            raise ValueError('INVALID_SAVED_PHONE_CONFIGURATION')
        self.companion, self.job = companion, job
        self.identities = self.identities[:1]
        self.voice_ready = asyncio.Event()
        self.report.update(mode='saved_owner_live_test', job_id=job['id'],
                           control_path='local_queue', audio_path='registered_twilio_client')
        self.report.pop('cloudflare_used')
        self.report['cloudflare_audio_tunnel_used'] = False

    def save(self):
        path = RUNTIME / 'registered-owner-resources.json'
        path.write_text(json.dumps({'key_sid': self.key_sid, 'parent': self.parent,
                                   'identities': self.identities}) + '\n')
        path.chmod(0o600)

    def save_report(self):
        path = RUNTIME / 'registered-owner-result.json'
        path.write_text(json.dumps(self.report, indent=2) + '\n')
        path.chmod(0o600)

    async def tokens(self, request):
        self.check(request)
        if self.issued:
            raise web.HTTPConflict()
        # Native Messaging's parent already holds mac-companion.lock.
        self.issued = True
        self.start_native_audio(fixture=False)
        self.event('tokens_issued')
        return web.json_response({'token': self.mint(self.identities[0]),
            'audio_capability': self.audio_capability, 'duration_seconds': self.job['duration_seconds']})

    def call_options(self):
        response = VoiceResponse()
        dial = response.dial(timeout=15, time_limit=self.job['duration_seconds'],
                             answer_on_bridge=True, record='do-not-record')
        dial.client(self.identities[0])
        response.hangup()
        return {'to': self.owner, 'from_': self.caller, 'twiml': str(response),
                'timeout': 25, 'time_limit': self.job['duration_seconds'] + 15, 'record': False}

    async def start(self, request):
        self.check(request)
        if not self.issued or not self.audio_claimed or self.started:
            raise web.HTTPConflict()
        self.started = True
        self.save()
        try:
            call = await asyncio.to_thread(self.client.calls.create, **self.call_options())
        except Exception:
            self.report['creation_outcome_unknown'] = True
            self.save_report()
            self.done.set()
            raise web.HTTPBadGateway(text='Call creation failed; no automatic retry') from None
        self.parent = call.sid
        self.report['phone_dialed'] = True
        self.save()
        self.event('phone_requested')
        self.companion.report('ringing')
        return web.json_response({'started': True})

    async def connected(self, request):
        self.check(request)
        if not self.parent or not self.started:
            raise web.HTTPConflict()
        self.voice_ready.set()
        self.event('registered_client_audio_ready')
        return web.Response(status=204)

    async def asset(self, request):
        self.check(request)
        name = request.match_info.get('name') or 'index.html'
        if name in {'index.html', 'owner.js'}:
            path = ROOT / 'prototype/registered' / ('owner.html' if name == 'index.html' else name)
            return web.FileResponse(path, headers={'Cache-Control': 'no-store',
                'Content-Security-Policy': "frame-ancestors 'none'", 'Referrer-Policy': 'no-referrer'})
        return await super().asset(request)

    def app(self):
        app = super().app()
        app.router.add_post('/connected', self.connected)
        return app

    async def cleanup(self):
        # End the saved-number parent too. The probe's client-only audit must
        # never be widened to allow a phone number.
        errors = []
        if self.parent:
            try:
                call = await asyncio.to_thread(self.client.calls(self.parent).fetch)
                if call.to != self.owner:
                    raise ValueError('UNEXPECTED_PHONE_DESTINATION')
                if call.status not in TERMINAL:
                    await asyncio.to_thread(self.client.calls(self.parent).update, status='completed')
                call = await asyncio.to_thread(self.client.calls(self.parent).fetch)
                self.report['phone_call'] = {'sid': call.sid, 'status': call.status,
                                             'duration': call.duration, 'destination': 'saved_owner'}
                if call.status not in TERMINAL:
                    raise ValueError('PHONE_NOT_TERMINAL')
            except Exception as error:
                errors.append(type(error).__name__)
        elif self.report.get('creation_outcome_unknown'):
            errors.append('PHONE_CREATION_OUTCOME_UNKNOWN')
        await super().cleanup(preserve_manifest=True)
        self.report['cleanup_errors'].extend(errors)
        self.report['cleanup_ok'] = not self.report['cleanup_errors']
        self.save_report()
        if self.report['cleanup_ok']:
            (RUNTIME / 'registered-owner-resources.json').unlink(missing_ok=True)


async def run_owner_test(companion, job):
    from companion import SNAPSHOT, verify_local_audio
    if (RUNTIME / 'registered-owner-resources.json').exists():
        raise RuntimeError('PREVIOUS_REGISTERED_TEST_NEEDS_CLEANUP')
    config = json.loads((ROOT / '.secrets/inbound-worker-secrets.json').read_text())
    probe = OwnerTest(config, companion, job)
    runner = web.AppRunner(probe.app(), access_log=None)
    state = SimpleNamespace(done=probe.done, closed=False, reason='completed')
    companion.bridge_state = state
    watchdog = None
    voice_task = None
    terminal, code = 'failed', None
    try:
        if SNAPSHOT.exists():
            raise RuntimeError('PREVIOUS_AUDIO_RESTORE_REQUIRED')
        prepared = await companion.command('prepare')
        await verify_local_audio()
        await companion.route('enable')
        await runner.setup()
        await web.TCPSite(runner, '127.0.0.1', 8893).start()
        await probe.provision()
        probe.event('local_browser_bridge_ready')
        async def wait_ready():
            while not probe.voice_ready.is_set():
                if probe.done.is_set():
                    raise RuntimeError('REGISTERED_CALL_ENDED_BEFORE_ANSWER')
                await asyncio.sleep(.1)
        await asyncio.wait_for(wait_ready(), 90)
        voice_task = asyncio.create_task(companion.command('start', job_id=job['id'],
            tab_id=prepared['tab_id'], duration_seconds=job['duration_seconds'] + 30))
        while not voice_task.done():
            if probe.done.is_set():
                voice_task.cancel()
                raise RuntimeError('REGISTERED_CALL_ENDED_DURING_DOT_START')
            await asyncio.sleep(.1)
        connected = await voice_task
        if not connected.get('connected') or not connected.get('microphone_verified'):
            raise RuntimeError('DOT_MICROPHONE_NOT_VERIFIED')
        probe.event('dot_voice_connected')
        companion.report('connected')
        async def watch():
            while not probe.done.is_set():
                await asyncio.sleep(2)
                result = await companion.command('inspect_owned', job_id=job['id'])
                if not result.get('active') or not result.get('microphone_verified'):
                    probe.done.set()
        watchdog = asyncio.create_task(watch())
        watchdog.add_done_callback(lambda t: probe.done.set() if not t.cancelled() and t.exception() else None)
        await asyncio.wait_for(probe.done.wait(), job['duration_seconds'] + 20)
        terminal = 'completed'
    except asyncio.CancelledError:
        terminal = 'canceled'
    except Exception as error:
        code = str(error) if re.fullmatch(r'[A-Z_]{1,80}', str(error)) else 'REGISTERED_CALL_FAILED'
        probe.report['error'] = code
        companion.report('failed', code)
    finally:
        state.closed = True
        for task in [voice_task, watchdog]:
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        await probe.cleanup()
        try:
            result = await companion.command('end', job_id=job['id'])
            if not result.get('ended'):
                raise RuntimeError('DOT_HANGUP_NOT_CONFIRMED')
            if SNAPSHOT.exists():
                await companion.route('restore')
        except Exception:
            companion.cleanup_failed = True
            code = 'REGISTERED_AUDIO_RESTORE_REQUIRED'
        await runner.cleanup()
        probe.report['audio_restored'] = not SNAPSHOT.exists()
        probe.save_report()
        companion.report('failed' if companion.cleanup_failed or not probe.report.get('cleanup_ok') else terminal, code)
        companion.bridge_state = None
        companion.current = None
