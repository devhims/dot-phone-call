"""Per-call audio only. Twilio signaling and durable ownership live in the local backend."""
import asyncio
import contextlib
import json
import secrets
import numpy as np
from aiohttp import WSMsgType
from types import SimpleNamespace
from aiohttp import web
from registered_probe import RegisteredProbe

class LocalAudioPipe(RegisteredProbe):
    def __init__(self, extension_id=None):
        super().__init__('AC'+'0'*32, 'unused', native_audio=True)
        self.origin = 'http://127.0.0.1:8894'
        self.allowed_origins = {self.origin}
        import re
        if extension_id is not None:
            if not re.fullmatch('[a-p]{32}',extension_id):raise ValueError('INVALID_EXTENSION_ID')
            self.allowed_origins.add('chrome-extension://'+extension_id)
        self.issued = True
    def check(self, request):
        if request.host != '127.0.0.1:8893' or request.headers.get('Origin') not in self.allowed_origins or self.stopping:
            raise web.HTTPForbidden()
    async def audio(self, request):
        self.check(request)
        if self.audio_claimed: raise web.HTTPConflict()
        ws = web.WebSocketResponse(max_msg_size=4096, heartbeat=10)
        await ws.prepare(request)
        authenticated = False
        try:
            first = await asyncio.wait_for(ws.receive(),5)
            if first.type != WSMsgType.TEXT or not secrets.compare_digest(first.data,self.audio_capability) or self.audio_claimed:
                await ws.close(code=1008)
                return ws
            self.audio_claimed = authenticated = True
            self.sockets.add(ws)
            await ws.send_str('ready')
            async for message in ws:
                if message.type != WSMsgType.BINARY: continue
                if len(message.data) != 960*4:
                    await ws.close(code=1008)
                    break
                samples = np.frombuffer(message.data,dtype='<f4').copy()
                if not np.isfinite(samples).all() or np.max(np.abs(samples))>1:
                    await ws.close(code=1008)
                    break
                self.audio_stats['received_peak'] = max(self.audio_stats.get('received_peak',0.0),float(np.max(np.abs(samples))))
                self.queue_audio(samples)
            return ws
        finally:
            self.sockets.discard(ws)
            if authenticated: self.done.set()
    def app(self):
        app = web.Application(client_max_size=4096)
        app.router.add_get('/audio', self.audio)
        return app
    async def close(self):
        self.stopping = True
        await asyncio.gather(*(ws.close() for ws in list(self.sockets)))
        for stream in self.outputs:
            try: stream.stop()
            finally: stream.close()

async def run_registered_call(companion, job):
    from companion import RUNTIME, SNAPSHOT, verify_local_audio
    pipe = LocalAudioPipe(extension_id=getattr(companion,'config',{}).get('extension_id'))
    runner = web.AppRunner(pipe.app(), access_log=None)
    state = SimpleNamespace(done=pipe.done, closed=False)
    companion.bridge_state = state
    code, started = None, False
    last_output = None
    try:
        if SNAPSHOT.exists(): raise RuntimeError('PREVIOUS_AUDIO_RESTORE_REQUIRED')
        # Never take over another project's shared BlackHole audio routing.
        proc = await asyncio.create_subprocess_exec(str(RUNTIME/'audio-routing'), 'status', stdout=asyncio.subprocess.PIPE)
        output, _ = await proc.communicate()
        if proc.returncode: raise RuntimeError('AUDIO_ROUTING_FAILED')
        defaults = json.loads(output)
        virtual = [d for d in defaults.values() if d['name'].startswith('BlackHole ')]
        if virtual:
            # A selected default is not proof of an active client. Headset loss
            # can leave idle BlackHole defaults behind after successful cleanup.
            if any(d.get('running') is not False for d in virtual):
                raise RuntimeError('AUDIO_ROUTE_ALREADY_IN_USE')
            await companion.route('recover-idle')
        prepared = await companion.command('prepare')
        await verify_local_audio()
        direct = getattr(companion, 'direct_audio', False)
        if direct:
            # Experiment: Dot's tab and the hidden page select BlackHole devices
            # themselves, so the Mac's default devices stay untouched.
            print(json.dumps({'event':'direct_audio_experiment','system_route_changed':False}),flush=True)
        else:
            await companion.route('enable')
            pipe.start_native_audio(fixture=False)
        await runner.setup()
        await web.TCPSite(runner, '127.0.0.1', 8893).start()
        await companion.cloud('registered-prepare', {'call_id':job['id'],'audio_capability':pipe.audio_capability,'direct_audio':direct})
        deadline = asyncio.get_running_loop().time() + 85
        while not pipe.done.is_set():
            status = await companion.cloud('registered-state', {'call_id':job['id']})
            if status.get('ended'): break
            if status.get('connected') and not started:
                result = await companion.command('start',job_id=job['id'],tab_id=prepared['tab_id'],duration_seconds=job['duration_seconds']+30)
                if not result.get('connected') or not result.get('microphone_verified'): raise RuntimeError('DOT_MICROPHONE_NOT_VERIFIED')
                started = True
                deadline = asyncio.get_running_loop().time()+job['duration_seconds']
                companion.report('connected')
            if started:
                inspected = await companion.command('inspect_owned',job_id=job['id'])
                if inspected.get('output') is not None: last_output = {**inspected['output'], 'verified': inspected.get('output_verified')}
                if not inspected.get('active') or not inspected.get('microphone_verified'): break
            if asyncio.get_running_loop().time()>deadline: raise RuntimeError('REGISTERED_CALL_TIMEOUT')
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        code = 'CALL_CANCELED'
    except Exception as error:
        import re
        code = str(error) if re.fullmatch('[A-Z_]{1,60}',str(error)) else 'REGISTERED_CALL_FAILED'
    finally:
        state.closed = True
        try:
            for _ in range(15):
                result = await companion.cloud('registered-finish',{'call_id':job['id']})
                if result.get('ended'): break
                await asyncio.sleep(.5)
            else: raise RuntimeError('BROWSER_AUDIO_RELEASE_UNCONFIRMED')
        except Exception:
            companion.cleanup_failed = True
            code = 'REGISTERED_HANGUP_UNCONFIRMED'
        try:
            await pipe.close()
            await runner.cleanup()
        except Exception:
            companion.cleanup_failed = True
        # Phone-to-Dot playout health: no audio content, only counters.
        print(json.dumps({'event':'registered_audio_closed',**pipe.audio_metrics()}),flush=True)
        if getattr(companion, 'direct_audio', False):
            print(json.dumps({'event':'direct_audio_dot_output','report':last_output}),flush=True)
        try:
            result = await companion.command('end',job_id=job['id'])
            if not result.get('ended'): raise RuntimeError('DOT_HANGUP_NOT_CONFIRMED')
            if SNAPSHOT.exists(): await companion.route('restore')
        except Exception:
            companion.cleanup_failed = True
            code = 'REGISTERED_AUDIO_RESTORE_REQUIRED'
        companion.report('failed' if code or companion.cleanup_failed else 'completed',code)
        companion.bridge_state = None
        companion.current = None
