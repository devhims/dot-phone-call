import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import time
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from companion import Companion


class PreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_audio_failure_closes_already_open_tunnel(self):
        companion = Companion({})
        companion.command = AsyncMock(return_value={'tab_id':7})
        opened = asyncio.Event()
        tunnel = MagicMock(close=AsyncMock())
        async def open_tunnel():
            opened.set()
            return tunnel
        async def fail_audio():
            await opened.wait()
            raise RuntimeError('LOCAL_AUDIO_FAILED')
        with patch('companion.open_verified_tunnel', side_effect=open_tunnel), \
                patch('companion.verify_local_audio', side_effect=fail_audio):
            with self.assertRaisesRegex(RuntimeError, 'LOCAL_AUDIO_FAILED'):
                await companion.prepare_call()
        tunnel.close.assert_awaited_once()

    async def test_browser_failure_cancels_tunnel_and_never_tests_audio(self):
        companion = Companion({})
        started, closed = asyncio.Event(), asyncio.Event()
        async def prepare(*args):
            await started.wait()
            raise RuntimeError('DOT_VOICE_ALREADY_ACTIVE')
        companion.command = AsyncMock(side_effect=prepare)
        async def open_tunnel():
            started.set()
            try: await asyncio.Event().wait()
            finally: closed.set()
        with patch('companion.open_verified_tunnel', side_effect=open_tunnel), \
                patch('companion.verify_local_audio', new=AsyncMock()) as audio:
            with self.assertRaisesRegex(RuntimeError, 'DOT_VOICE_ALREADY_ACTIVE'):
                await companion.prepare_call()
        self.assertTrue(closed.is_set())
        audio.assert_not_awaited()

    async def test_cancellation_waits_for_audio_shutdown_and_closes_tunnel(self):
        companion = Companion({})
        companion.command = AsyncMock(return_value={'tab_id':7})
        started, closed = asyncio.Event(), asyncio.Event()
        tunnel = MagicMock(close=AsyncMock())
        async def audio():
            started.set()
            try: await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                closed.set()
        with patch('companion.open_verified_tunnel', new=AsyncMock(return_value=tunnel)), \
                patch('companion.verify_local_audio', side_effect=audio):
            task = asyncio.create_task(companion.prepare_call())
            await started.wait()
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(closed.is_set())
        tunnel.close.assert_awaited_once()


class CompanionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.c = Companion({'extension_id':'test-extension','browser_token':'paired-token'})
        app = web.Application()
        app.router.add_get('/browser',self.c.browser_socket)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.headers={'Origin':'chrome-extension://test-extension','Host':'127.0.0.1:8892'}
    async def asyncTearDown(self):
        await self.client.close()
    async def test_page_cannot_connect(self):
        response = await self.client.get('/browser',headers={**self.headers,'Origin':'https://chatgpt.com'})
        self.assertEqual(response.status,403)
    async def test_wrong_token_cannot_pair(self):
        ws=await self.client.ws_connect('/browser',headers=self.headers)
        await ws.send_json({'type':'authenticate','token':'wrong'})
        await ws.receive(timeout=2)
        self.assertIsNone(self.c.browser)
        self.assertFalse(self.c.browser_ready)
    async def test_commands_require_pairing_and_correlate_results(self):
        ws=await self.client.ws_connect('/browser',headers=self.headers)
        await ws.send_json({'type':'authenticate','token':'paired-token'})
        self.assertEqual((await ws.receive_json())['type'],'authenticated')
        await ws.send_json({'type':'ready','ok':True,'version':'0.1.7'})
        command=asyncio.create_task(self.c.command('prepare'))
        message=await ws.receive_json(timeout=2)
        self.assertEqual(message['action'],'prepare')
        await ws.send_json({'type':'result','id':message['id'],'ready':True,'tab_id':7,'version':'0.1.7'})
        self.assertEqual((await command)['tab_id'],7)
        self.assertTrue(self.c.browser_ready)
        await ws.close()
        await asyncio.sleep(.01)
        self.assertFalse(self.c.browser_ready)
    async def test_old_extension_cannot_report_ready_for_another_call(self):
        ws=await self.client.ws_connect('/browser',headers=self.headers)
        await ws.send_json({'type':'authenticate','token':'paired-token'})
        await ws.receive_json()
        await ws.send_json({'type':'ready','ok':True})
        await asyncio.sleep(.01)
        self.assertFalse(self.c.browser_ready)
        await ws.close()
    async def test_browser_disconnect_stops_current_call(self):
        class State:
            done=asyncio.Event()
        self.c.bridge_state=State()
        ws=await self.client.ws_connect('/browser',headers=self.headers)
        await ws.send_json({'type':'authenticate','token':'paired-token'})
        await ws.receive_json()
        await ws.close()
        await asyncio.wait_for(self.c.bridge_state.done.wait(),2)

    async def test_stalled_cleanup_does_not_hide_failure_or_stop_cloud_heartbeat(self):
        self.c.cleanup_failed = True
        self.c.browser = object()
        self.c.reports = [{'call_id':'failed-call','status':'failed','code':'DOT_VOICE_CONNECT_TIMEOUT'}]
        stuck = asyncio.Event()
        self.c.command = AsyncMock(side_effect=lambda *a, **kw: None)
        async def blocked(*a, **kw):
            await stuck.wait()
        self.c.command.side_effect = blocked
        self.c.cloud = AsyncMock(return_value={})
        try:
            await asyncio.wait_for(self.c.tick(), .1)
            self.assertEqual([call.args[0] for call in self.c.cloud.call_args_list], ['update','pulse'])
            self.assertFalse(self.c.cloud.call_args_list[-1].args[1]['ready'])
        finally:
            task = getattr(self.c, 'recovery_task', None)
            if task:
                task.cancel()
                try: await task
                except asyncio.CancelledError: pass

    async def test_call_keeps_bridge_failure_and_closes_audio(self):
        job={'id':'local-job','duration_seconds':120}
        self.c.current=job
        self.c.last_cloud_ok=time.monotonic()
        self.c.route=AsyncMock()
        self.c.command=AsyncMock(side_effect=lambda action, **kw: {
            'prepare':{'tab_id':7},'start':{'connected':True,'microphone_verified':True},'end':{'ended':True}
        }[action])
        tunnel=MagicMock(returncode=0)
        tunnel.stdout.readline=AsyncMock(side_effect=[b'https://test.trycloudflare.com\n',b'Registered tunnel connection\n',b''])
        audio=MagicMock()
        result={'status':'failed','code':'PHONE_MEDIA_NOT_CONNECTED'}
        with patch('companion.SNAPSHOT') as snapshot, \
                patch('companion.verify_local_audio',new=AsyncMock(return_value={})), \
                patch('companion.AudioBridge',return_value=audio), \
                patch('companion.asyncio.create_subprocess_exec',new=AsyncMock(return_value=tunnel)), \
                patch('public_tunnel.verify_public_bridge',new=AsyncMock()), \
                patch('companion.run_bridge',new=AsyncMock(return_value=result)):
            snapshot.exists.return_value=False
            await self.c.legacy_call(job)
        self.assertEqual(self.c.reports[-1]['status'],'failed')
        self.assertEqual(self.c.reports[-1]['code'],'PHONE_MEDIA_NOT_CONNECTED')
        audio.close.assert_called_once()
        self.assertFalse(self.c.cleanup_failed)

    async def test_tunnel_setup_overlaps_local_audio_check_before_dial(self):
        job={'id':'parallel-setup','duration_seconds':120}
        self.c.current=job
        self.c.last_cloud_ok=time.monotonic()
        self.c.route=AsyncMock()
        self.c.command=AsyncMock(side_effect=lambda action, **kw:
            {'prepare':{'tab_id':7},'end':{'ended':True}}[action])
        tunnel_started, audio_started = asyncio.Event(), asyncio.Event()
        tunnel=MagicMock(origin='https://example.test', close=AsyncMock())
        async def open_tunnel():
            tunnel_started.set()
            await audio_started.wait()
            return tunnel
        async def check_audio():
            audio_started.set()
            await tunnel_started.wait()
            return {}
        with patch('companion.SNAPSHOT') as snapshot, \
                patch('companion.open_verified_tunnel',side_effect=open_tunnel), \
                patch('companion.verify_local_audio',side_effect=check_audio), \
                patch('companion.AudioBridge'), \
                patch('companion.run_bridge',new=AsyncMock(return_value={'status':'completed'})) as bridge:
            snapshot.exists.return_value=False
            await asyncio.wait_for(self.c.legacy_call(job), .25)
        bridge.assert_awaited_once()
        self.assertTrue(tunnel_started.is_set() and audio_started.is_set())
        tunnel.close.assert_awaited_once()

    async def test_public_failure_happens_before_voice_audio_or_dialing(self):
        job={'id':'unreachable-before-voice','duration_seconds':120}
        self.c.current=job
        self.c.route=AsyncMock()
        self.c.command=AsyncMock(side_effect=lambda action,**kw:{'prepare':{'tab_id':7},'end':{'ended':True}}[action])
        error=RuntimeError('PUBLIC_BRIDGE_UNREACHABLE')
        error.diagnostics={'stage':'public_bridge_preflight','phone_dialed':False,'reason':'dns_lookup_failed','attempts':39,'elapsed_seconds':20}
        with patch('companion.SNAPSHOT') as snapshot,patch('companion.open_verified_tunnel',new=AsyncMock(side_effect=error)),patch('companion.verify_local_audio',new=AsyncMock(return_value={})),patch('companion.AudioBridge') as audio,patch('companion.run_bridge',new=AsyncMock()) as phone:
            snapshot.exists.return_value=False
            await self.c.legacy_call(job)
            audio.assert_not_called();phone.assert_not_called();self.c.route.assert_not_called()
        self.assertEqual([x.args[0] for x in self.c.command.call_args_list],['prepare','end'])
        self.assertEqual(self.c.reports[0]['diagnostics']['reason'],'dns_lookup_failed')

    async def test_local_audio_test_never_invokes_phone_bridge(self):
        self.c.current={'id':'audio-test-local'}
        self.c.route=AsyncMock()
        self.c.command=AsyncMock(side_effect=lambda action, **kw: {
            'prepare':{'tab_id':7},
            'start':{'connected':True,'microphone_verified':True,'microphone':'BlackHole 16ch'},
            'inspect_owned':{'active':True,'microphone_verified':True},
            'end':{'ended':True},
        }[action])
        audio=MagicMock()
        with patch('companion.RUNTIME') as runtime, patch('companion.SNAPSHOT') as snapshot, \
                patch('companion.AudioBridge',return_value=audio), \
                patch('companion.verify_browser_audio',new=AsyncMock(return_value={'verified':True})), \
                patch('companion.run_bridge',new=AsyncMock()) as phone:
            snapshot.exists.return_value=False
            await self.c.test_browser_audio('audio-test-local')
            phone.assert_not_called()
            payload=__import__('json').loads((runtime/'browser-audio-test-result.json').write_text.call_args.args[0])
            self.assertTrue(payload['verified'])
            self.assertFalse(payload['dialed'])
        audio.close.assert_called_once()
        self.assertIsNone(self.c.current)
        self.assertEqual(self.c.reports,[])

    async def test_local_audio_test_rejects_unverified_microphone(self):
        self.c.route=AsyncMock()
        self.c.command=AsyncMock(side_effect=lambda action, **kw: {
            'prepare':{'tab_id':7},'start':{'connected':True},'end':{'ended':True},
        }[action])
        with patch('companion.RUNTIME') as runtime, patch('companion.SNAPSHOT') as snapshot, \
                patch('companion.AudioBridge'), \
                patch('companion.verify_browser_audio',new=AsyncMock()) as challenge, \
                patch('companion.run_bridge',new=AsyncMock()) as phone:
            snapshot.exists.return_value=False
            await self.c.test_browser_audio('audio-test-local')
            challenge.assert_not_called()
            phone.assert_not_called()
            payload=__import__('json').loads((runtime/'browser-audio-test-result.json').write_text.call_args.args[0])
            self.assertFalse(payload['verified'])
            self.assertEqual(payload['code'],'DOT_MICROPHONE_NOT_VERIFIED')

    async def test_audio_failure_diagnostics_survive_call_cleanup(self):
        from browser_audio_check import AudioCheckError
        job={'id':'local-failed-preflight','duration_seconds':120}
        self.c.current=job
        self.c.last_cloud_ok=time.monotonic()
        self.c.route=AsyncMock()
        self.c.command=AsyncMock(side_effect=lambda action, **kw: {
            'prepare':{'tab_id':7},'start':{'connected':True,'microphone_verified':True},'end':{'ended':True}
        }[action])
        tunnel=MagicMock(returncode=0)
        tunnel.stdout.readline=AsyncMock(side_effect=[b'https://test.trycloudflare.com\n',b'Registered tunnel connection\n',b''])
        details={'stage':'browser_audio_preflight','phone_dialed':False,
                 'expected_response':'dolphin river purple','heard_challenge_words':['dolphin'],
                 'reply_peak':.2,'active_seconds':1.2}
        with patch('companion.SNAPSHOT') as snapshot, patch('companion.AudioBridge'), \
                patch('companion.verify_local_audio',new=AsyncMock(return_value={})), \
                patch('companion.asyncio.create_subprocess_exec',new=AsyncMock(return_value=tunnel)), \
                patch('public_tunnel.verify_public_bridge',new=AsyncMock()), \
                patch('companion.run_bridge',new=AsyncMock(side_effect=AudioCheckError('DOT_AUDIO_REPLY_MISMATCH',details))):
            snapshot.exists.return_value=False
            await self.c.legacy_call(job)
        self.assertEqual(self.c.reports[0]['diagnostics'],details)
        self.assertEqual(self.c.reports[0]['code'],'DOT_AUDIO_REPLY_MISMATCH')
        self.assertFalse(self.c.cleanup_failed)
