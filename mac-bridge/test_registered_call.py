import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from aiohttp.test_utils import TestClient, TestServer
from registered_call import LocalAudioPipe, run_registered_call

class AudioPipeTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_pcm_requires_loopback_host_origin_and_single_use_capability(self):
        pipe=LocalAudioPipe()
        async with TestClient(TestServer(pipe.app())) as client:
            for origin in ['https://evil.test','http://127.0.0.1:8893']:
                response=await client.get('/audio',headers={'Host':'127.0.0.1:8893','Origin':origin})
                self.assertEqual(response.status,403)
            headers={'Host':'127.0.0.1:8893','Origin':'http://127.0.0.1:8894'}
            ws=await client.ws_connect('/audio',headers=headers)
            await ws.send_str('wrong');await ws.receive();self.assertFalse(pipe.audio_claimed)
            ws=await client.ws_connect('/audio',headers=headers)
            await ws.send_str(pipe.audio_capability)
            self.assertEqual((await ws.receive()).data,'ready')
            rejected=await client.get('/audio',headers={'Host':'127.0.0.1:8893','Origin':'https://evil.test'})
            self.assertEqual(rejected.status,403)
            self.assertFalse(pipe.done.is_set())
            import numpy as np
            await ws.send_bytes(np.full(960,.1,dtype='<f4').tobytes())
            await ws.send_bytes(b'invalid frame')
            await ws.receive()
            self.assertEqual(pipe.audio_stats['received_frames'],960)
            await asyncio.wait_for(pipe.done.wait(),1)
        await pipe.close()

    async def test_helper_closes_audio_and_restores_after_outgoing_or_incoming(self):
        for direction in ['inbound','outbound']:
            companion=SimpleNamespace(bridge_state=None,current={'id':'job'},cleanup_failed=False,
                command=AsyncMock(side_effect=[{'tab_id':1},{'connected':True,'microphone_verified':True},{'active':False,'microphone_verified':True},{'ended':True}]),
                route=AsyncMock(),cloud=AsyncMock(side_effect=[{'ready':True},{'connected':True},{'ended':True}]),report=MagicMock())
            pipe=MagicMock();pipe.done=asyncio.Event();pipe.close=AsyncMock();pipe.audio_capability='x'*43
            process=SimpleNamespace(returncode=0,communicate=AsyncMock(return_value=(json.dumps({'input':{'name':'MacBook Microphone'},'output':{'name':'MacBook Speakers'}}).encode(),None)))
            runner=SimpleNamespace(setup=AsyncMock(),cleanup=AsyncMock())
            snapshot=MagicMock();snapshot.exists.side_effect=[False,True]
            with patch('registered_call.LocalAudioPipe',return_value=pipe),patch('registered_call.web.AppRunner',return_value=runner),patch('registered_call.web.TCPSite') as site,patch('registered_call.asyncio.create_subprocess_exec',return_value=process),patch('companion.verify_local_audio',new=AsyncMock()),patch('companion.SNAPSHOT',snapshot):
                site.return_value.start=AsyncMock()
                await run_registered_call(companion,{'id':'job','duration_seconds':60,'direction':direction})
            pipe.start_native_audio.assert_called_once_with(fixture=False)
            pipe.close.assert_awaited_once();runner.cleanup.assert_awaited_once()
            self.assertEqual([c.args[0] for c in companion.route.await_args_list],['enable','restore'])
            self.assertFalse(companion.cleanup_failed);self.assertIsNone(companion.current)
            companion.report.assert_called_with('completed',None)

    async def test_an_existing_blackhole_route_is_not_taken_over(self):
        companion=SimpleNamespace(bridge_state=None,current={'id':'job'},cleanup_failed=False,command=AsyncMock(return_value={'ended':True}),route=AsyncMock(),cloud=AsyncMock(return_value={'ended':True}),report=MagicMock())
        process=SimpleNamespace(returncode=0,communicate=AsyncMock(return_value=(b'{"input":{"name":"BlackHole 16ch"}}',None)))
        with patch('registered_call.asyncio.create_subprocess_exec',return_value=process),patch('companion.SNAPSHOT') as snapshot,patch('companion.verify_local_audio',new=AsyncMock()) as check:
            snapshot.exists.return_value=False
            await run_registered_call(companion,{'id':'job','duration_seconds':60})
        check.assert_not_awaited();companion.route.assert_not_awaited()
        companion.report.assert_called_with('failed','AUDIO_ROUTE_ALREADY_IN_USE')

    async def test_audio_pipe_accepts_only_the_paired_extension_origin(self):
        pipe=LocalAudioPipe(extension_id='a'*32)
        async with TestClient(TestServer(pipe.app())) as client:
            bad=await client.get('/audio',headers={'Host':'127.0.0.1:8893','Origin':'chrome-extension://'+'b'*32})
            self.assertEqual(bad.status,403)
            ws=await client.ws_connect('/audio',headers={'Host':'127.0.0.1:8893','Origin':'chrome-extension://'+'a'*32})
            await ws.send_str(pipe.audio_capability);self.assertEqual((await ws.receive()).data,'ready');await ws.close()
        await pipe.close()
