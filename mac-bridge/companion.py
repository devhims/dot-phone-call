"""Paired Mac companion: fixed browser controls, owner-only calls, automatic cleanup."""
import asyncio
import contextlib
import fcntl
import hmac
import json
import os
from pathlib import Path
import re
import signal
import ssl
import sys
import time
from types import SimpleNamespace
import uuid

import aiohttp
from aiohttp import web
import certifi
from bridge import run as run_bridge, log
from public_tunnel import open_verified_tunnel
from audio import AudioBridge
from audio_check import check_bridge_audio
from browser_audio_check import verify_browser_audio

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT/'.runtime'
SNAPSHOT = RUNTIME/'mac-audio-original.json'


async def verify_local_audio():
    # Cancellation must wait for the tone peers to close before restoration or exit.
    task = asyncio.create_task(asyncio.to_thread(check_bridge_audio))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await task
        raise


class Companion:
    def __init__(self, config):
        self.config = config
        self.instance = str(uuid.uuid4())
        self.browser = None
        self.browser_ready = False
        self.browser_version = None
        self.pending = {}
        self.current = None
        self.task = None
        self.bridge_state = None
        self.reports = []
        self.shutdown = asyncio.Event()
        self.last_cloud_ok = 0
        self.cleanup_failed = False
        self.http = None
        self.recovery_task = None
        self.next_recovery = 0

    async def browser_socket(self, request):
        if request.headers.get('Origin') != 'chrome-extension://'+self.config['extension_id']:
            raise web.HTTPForbidden()
        if request.host != '127.0.0.1:8892':
            raise web.HTTPForbidden()
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=8192)
        await ws.prepare(request)
        try:
            data = await asyncio.wait_for(ws.receive_json(), 5)
            token = data.get('token')
            if data.get('type') != 'authenticate' or not isinstance(token, str) or not hmac.compare_digest(token, self.config['browser_token']):
                await ws.close(code=1008)
                return ws
            if self.browser is not None and not self.browser.closed:
                await ws.close(code=1008)
                return ws
            self.browser = ws
            await ws.send_json({'type':'authenticated'})
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    break
                data = json.loads(message.data)
                if data.get('type') == 'ready':
                    self.browser_version = str(data.get('version','unknown'))[:20]
                    self.browser_ready = data.get('ok') is True and self.browser_version in ('0.1.7', '0.1.8', '0.1.9', '0.2.0') and not self.cleanup_failed
                    log('browser_paired', ready=self.browser_ready, version=str(data.get('version','unknown'))[:20])
                if data.get('type') == 'result':
                    future = self.pending.get(data.get('id'))
                    if future and not future.done(): future.set_result(data)
        except (asyncio.TimeoutError, ValueError, aiohttp.ClientError):
            pass
        finally:
            if self.browser is ws:
                self.browser = None
                self.browser_ready = False
                self.browser_version = None
                for future in self.pending.values():
                    if not future.done(): future.set_exception(RuntimeError('BROWSER_DISCONNECTED'))
                self.stop_call()
            await ws.close()
        return ws

    async def command(self, action, **fields):
        if self.browser is None or self.browser.closed:
            raise RuntimeError('BROWSER_OFFLINE')
        ident = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await self.browser.send_json({'type':'command','id':ident,'action':action,**fields})
            timeout = {'start':45, 'prepare':30, 'hidden_audio_test':20}.get(action, 10)
            try:
                result = await asyncio.wait_for(future, timeout)
            except asyncio.TimeoutError:
                raise RuntimeError('BROWSER_COMMAND_TIMEOUT') from None
            if result.get('error'):
                error=RuntimeError(result['error'])
                error.diagnostics=result.get('diagnostics')
                log('browser_command_failed',action=action,code=result['error'],diagnostics=error.diagnostics)
                raise error
            if action == 'prepare' and result.get('version') not in ('0.1.7', '0.1.8', '0.1.9', '0.2.0'):
                raise RuntimeError('BROWSER_UPDATE_REQUIRED')
            if action in ('prepare','start','end'):
                log('browser_command_finished', action=action, version=result.get('version'),
                    tab_id=result.get('tab_id'), connected=result.get('connected'), ended=result.get('ended'),
                    microphone=result.get('microphone'), microphone_verified=result.get('microphone_verified'))
            return result
        finally:
            self.pending.pop(ident,None)

    def stop_call(self, reason='control_lost'):
        if self.bridge_state:
            if not getattr(self.bridge_state, 'closed', False):
                self.bridge_state.reason = reason
            self.bridge_state.done.set()
        elif self.task and not self.task.done():
            self.task.cancel()

    async def route(self, action):
        process = await asyncio.create_subprocess_exec(str(RUNTIME/'audio-routing'),action,str(SNAPSHOT))
        if await process.wait(): raise RuntimeError('AUDIO_ROUTING_FAILED')

    async def cloud(self, action, data):
        async with self.http.post(self.config['origin']+'/companion/'+action,
                headers={'Authorization':'Bearer '+self.config['cloud_token']},
                json={**data,'instance':self.instance}, allow_redirects=False) as response:
            if response.status != 200: raise RuntimeError('COMPANION_CLOUD_REQUEST_FAILED')
            result = await response.json()
            if result.get('error'): raise RuntimeError(result['error'])
            self.last_cloud_ok = time.monotonic()
            return result

    def report(self, status, code=None, diagnostics=None):
        if self.current:
            item={'call_id':self.current['id'],'status':status}
            if code: item['code']=code
            if diagnostics: item['diagnostics']=diagnostics
            self.reports.append(item)
            log('call_progress',call_id=self.current['id'],status=status,code=code,diagnostics=diagnostics)

    async def prepare_call(self):
        """Overlap public setup with browser readiness and the local audio check."""
        started = time.monotonic()
        async def browser_and_audio():
            prepared = await self.command('prepare')
            log('call_setup_phase', phase='browser_prepare', seconds=round(time.monotonic()-started, 3))
            # Prepare rejects an existing voice session before any test tones.
            audio_started = time.monotonic()
            levels = await verify_local_audio()
            log('bridge_audio_check_passed', **levels)
            log('call_setup_phase', phase='local_audio', seconds=round(time.monotonic()-audio_started, 3))
            return prepared
        tasks = [asyncio.create_task(browser_and_audio()),
                 asyncio.create_task(open_verified_tunnel())]
        try:
            prepared, tunnel = await asyncio.gather(*tasks)
        except BaseException:
            # gather does not cancel siblings on failure. Wait for audio peers
            # and tunnel startup to unwind before restoring devices or exiting.
            for task in tasks:
                if not task.cancelling():
                    task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            if not isinstance(results[1], BaseException):
                await results[1].close()
            raise
        log('call_setup_phase', phase='parallel_preflight', seconds=round(time.monotonic()-started, 3))
        return prepared, tunnel

    async def call(self, job):
        from registered_call import run_registered_call
        return await run_registered_call(self, job)

    async def legacy_call(self, job):
        from registered_owner import consume_arm, run_owner_test
        if await consume_arm(job):
            return await run_owner_test(self, job)
        setup_started = time.monotonic()
        tunnel = None
        watchdog = None
        terminal = 'failed'
        code = None
        audio = None
        try:
            if SNAPSHOT.exists(): raise RuntimeError('PREVIOUS_AUDIO_RESTORE_REQUIRED')
            prepared, tunnel = await self.prepare_call()
            origin=tunnel.origin
            await self.route('enable')
            audio=AudioBridge()
            audio.start()
            voice_connected = False
            async def start_dot():
                nonlocal voice_connected
                connected = await self.command('start', job_id=job['id'],tab_id=prepared['tab_id'],duration_seconds=job['duration_seconds']+60)
                if not connected.get('connected'): raise RuntimeError('DOT_NOT_CONNECTED')
                if connected.get('microphone_verified') is not True:
                    raise RuntimeError('DOT_MICROPHONE_NOT_VERIFIED')
                voice_connected = True
            if time.monotonic()-self.last_cloud_ok > 15: raise RuntimeError('CLOUD_CONTROL_LOST')
            def set_state(state): self.bridge_state=state
            async def watch():
                while True:
                    await asyncio.sleep(2)
                    if tunnel.process.returncode is not None or not self.browser_ready or time.monotonic()-self.last_cloud_ok > 20:
                        self.stop_call(); return
                    if voice_connected:
                        state = await self.command('inspect_owned',job_id=job['id'])
                        if not state.get('active') or state.get('microphone_verified') is not True:
                            self.stop_call(); return
            watchdog = asyncio.create_task(watch())
            watchdog.add_done_callback(lambda task:self.stop_call() if not task.cancelled() and task.exception() else None)
            async def inbound_ready(stream_url):
                result = await self.cloud('inbound-ready', {'call_id':job['id'],'stream_url':stream_url})
                if result.get('accepted') is not True:
                    raise RuntimeError('INBOUND_CALL_NO_LONGER_WAITING')
            result = await run_bridge(SimpleNamespace(public_url=origin, port=8891, duration=job['duration_seconds'],
                dot_ready=False,start_dot=start_dot,local_audio_verified=True,
                audio=audio,on_state=set_state,observer=self.report,manage_signals=False,
                setup_started=setup_started,
                inbound_call_sid=job.get('inbound_call_sid') if job.get('direction') == 'inbound' else None,
                inbound_ready=inbound_ready))
            terminal = result['status']
            code = result.get('code')
        except asyncio.CancelledError:
            terminal = 'canceled'
            self.report(terminal)
        except Exception as error:
            message = str(error)
            code = message if re.fullmatch(r'[A-Z_]{1,60}',message) else 'CALL_SETUP_FAILED'
            log('companion_call_failed',code=code,error_type=type(error).__name__)
            # Report failure before browser cleanup, which can itself time out.
            self.report('failed',code,getattr(error,'diagnostics',None))
        finally:
            if audio is not None:
                # Audio cleanup must not prevent browser hangup/restoration.
                with contextlib.suppress(Exception): audio.close()
            if watchdog:
                watchdog.cancel()
                with contextlib.suppress(asyncio.CancelledError,Exception): await watchdog
            # End the owned browser session before restoring the physical microphone.
            try:
                ended = await self.command('end',job_id=job['id'])
                if ended.get('ended') is not True:
                    raise RuntimeError('BROWSER_HANGUP_NOT_CONFIRMED')
            except Exception:
                # Extension has its own disconnect watchdog; do not expose the
                # physical mic while its owned voice session may still be live.
                self.cleanup_failed=True
                self.browser_ready=False
                code='BROWSER_HANGUP_NOT_CONFIRMED'
                log('cleanup_needs_attention',code=code)
            if SNAPSHOT.exists():
                if not self.cleanup_failed:
                    try: await self.route('restore')
                    except Exception:
                        self.cleanup_failed=True; self.browser_ready=False
                        code='AUDIO_RESTORE_FAILED'
            if tunnel is not None:
                await tunnel.close()
            self.report('failed' if self.cleanup_failed else terminal,code)
            self.bridge_state=None
            self.current=None
            log('call_cleanup_finished',audio_restored=not SNAPSHOT.exists())

    async def recover_audio(self):
        try:
            ended=await self.command('end')
            if ended.get('ended'):
                if SNAPSHOT.exists(): await self.route('restore')
                self.cleanup_failed=False
                self.browser_ready=self.browser_version in ('0.1.7', '0.1.8', '0.1.9', '0.2.0')
        except Exception:
            # Keep virtual audio until browser hangup is confirmed. Never block
            # the cloud heartbeat or pending failure reports on this recovery.
            pass
        finally:
            self.next_recovery=time.monotonic()+5

    async def test_browser_audio(self, job_id):
        """Local diagnostic using the permission-owning app. Never invokes Twilio.

        An explicit local request retains the challenge text and recognition
        result, but no recording. Normal call preflights retain neither.
        """
        result={'test_id':job_id,'verified':False,'dialed':False}
        audio=None
        try:
            if SNAPSHOT.exists(): raise RuntimeError('PREVIOUS_AUDIO_RESTORE_REQUIRED')
            prepared=await self.command('prepare')
            await self.route('enable')
            audio=AudioBridge()
            audio.start()
            started=await self.command('start',job_id=job_id,tab_id=prepared['tab_id'],duration_seconds=120)
            if not started.get('connected'): raise RuntimeError('DOT_NOT_CONNECTED')
            if started.get('microphone_verified') is not True:
                raise RuntimeError('DOT_MICROPHONE_NOT_VERIFIED')
            result['microphone']=started.get('microphone')
            result['microphone_verified']=True
            result['diagnostics']={}
            proof=await verify_browser_audio(audio,diagnostics=result['diagnostics'])
            state=await self.command('inspect_owned',job_id=job_id)
            if not state.get('active') or state.get('microphone_verified') is not True:
                raise RuntimeError('DOT_MICROPHONE_NOT_VERIFIED')
            result.update(proof)
        except asyncio.CancelledError:
            result['code']='AUDIO_TEST_CANCELED'
        except Exception as error:
            result['code']=str(error) if re.fullmatch(r'[A-Z_]{1,60}',str(error)) else 'AUDIO_TEST_FAILED'
            if getattr(error,'diagnostics',None):
                result.setdefault('diagnostics',{}).update(error.diagnostics)
        finally:
            if audio is not None:
                with contextlib.suppress(Exception): audio.close()
            try:
                ended=await self.command('end',job_id=job_id)
                if ended.get('ended') is not True: raise RuntimeError('BROWSER_HANGUP_NOT_CONFIRMED')
                if SNAPSHOT.exists(): await self.route('restore')
            except Exception:
                self.cleanup_failed=True
                self.browser_ready=False
                result.update(verified=False,code='AUDIO_TEST_CLEANUP_FAILED')
            result['audio_restored']=not SNAPSHOT.exists()
            result['completed_at']=time.time()
            output=RUNTIME/'browser-audio-test-result.json'
            output.write_text(json.dumps(result))
            output.chmod(0o600)
            self.current=None
            log('browser_audio_test_finished',verified=result['verified'],code=result.get('code'),
                audio_restored=result['audio_restored'])

    async def tick(self):
        if self.cleanup_failed and self.browser is not None and not self.current:
            if (self.recovery_task is None or self.recovery_task.done()) and time.monotonic()>=self.next_recovery:
                self.recovery_task=asyncio.create_task(self.recover_audio())
        while self.reports:
            await self.cloud('update',self.reports[0])
            self.reports.pop(0)
        # Private local diagnostic trigger, not a cloud command or dialing path.
        request=RUNTIME/'browser-audio-test-request.json'
        if request.exists() and self.current is None and self.browser_ready and not self.cleanup_failed:
            fresh=time.time()-request.stat().st_mtime < 60
            request.unlink()
            if fresh:
                job_id='audio-test-'+str(uuid.uuid4())
                self.current={'id':job_id}
                self.task=asyncio.create_task(self.test_browser_audio(job_id))
        data=await self.cloud('pulse',{'ready':self.browser_ready and not self.cleanup_failed,
            'busy':self.current is not None,'audio_guard_version':1})
        if data.get('cancel_call_id') and self.current and data['cancel_call_id']==self.current['id']:
            self.stop_call('phone_canceled')
        if data.get('job') and self.current is None:
            self.current=data['job']
            self.task=asyncio.create_task(self.call(self.current))

    async def run(self):
        app=web.Application(client_max_size=8192)
        app.router.add_get('/browser',self.browser_socket)
        runner=web.AppRunner(app,access_log=None)
        await runner.setup()
        await web.TCPSite(runner,'127.0.0.1',8892).start()
        ssl_context=ssl.create_default_context(cafile=certifi.where())
        self.http=aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context),timeout=aiohttp.ClientTimeout(total=8))
        log('companion_started',extension_id=self.config['extension_id'])
        try:
            while not self.shutdown.is_set():
                try:
                    await self.tick()
                except Exception:
                    if self.current and time.monotonic()-self.last_cloud_ok>20: self.stop_call()
                status={'browser_ready':self.browser_ready,'busy':self.current is not None,'cleanup_required':self.cleanup_failed,'audio_guard_version':1,
                    'browser_version':self.browser_version,'cloud_online':time.monotonic()-self.last_cloud_ok<20,'updated_at':time.time()}
                (RUNTIME/'mac-companion-status.json').write_text(json.dumps(status))
                try: await asyncio.wait_for(self.shutdown.wait(),2)
                except asyncio.TimeoutError: pass
        finally:
            if self.recovery_task:
                self.recovery_task.cancel()
                with contextlib.suppress(asyncio.CancelledError,Exception): await self.recovery_task
            self.stop_call()
            if self.task:
                with contextlib.suppress(asyncio.CancelledError,Exception): await self.task
            for item in self.reports:
                with contextlib.suppress(Exception): await self.cloud('update',item)
            with contextlib.suppress(Exception): await self.cloud('pulse',{'ready':False,'busy':False})
            if self.browser is not None: await self.browser.close()
            await self.http.close()
            await runner.cleanup()

async def main():
    config=json.loads((ROOT/'.secrets/mac-companion.json').read_text())
    if config['origin']!='http://127.0.0.1:8894': raise RuntimeError('Unexpected cloud origin')
    lock=(RUNTIME/'mac-companion.lock').open('w')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    companion=Companion(config)
    # A crash leaves a snapshot. Wait for the extension to close its owned call
    # before restoring devices and reporting ready.
    companion.cleanup_failed=SNAPSHOT.exists()
    if not companion.cleanup_failed:
        from audio_check import check_bridge_audio
        levels=await asyncio.to_thread(check_bridge_audio)
        log('bridge_audio_check_passed',**levels)
    loop=asyncio.get_running_loop()
    for sig in (signal.SIGINT,signal.SIGTERM): loop.add_signal_handler(sig,companion.shutdown.set)
    await companion.run()

if __name__=='__main__':
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
    except Exception as error:
        log('companion_failed',error_type=type(error).__name__)
        sys.exit(1)
