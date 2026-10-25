import asyncio
import base64
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp.test_utils import TestClient, TestServer
from twilio.request_validator import RequestValidator
from bridge import Session, run
from test_bridge import FakeAudio

SID = 'CA'+'1'*32
ACCOUNT = 'AC'+'0'*32
OWNER, NUMBER = '+15555550101', '+15555550102'


class InboundBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def execute(self, overrides=None, ready_action=None, other_call=False):
        self.requests=[]
        self.state=None
        self.audio=FakeAudio()
        self.dot=AsyncMock()
        self.ready=AsyncMock()
        client=MagicMock(close=AsyncMock())
        def set_state(value): self.state=value
        def config(path, *args, **kwargs):
            return json.dumps({'TWILIO_ACCOUNT_SID':ACCOUNT,'TWILIO_AUTH_TOKEN':'test-token'}
                if path.name=='twilio.json' else {'phone':OWNER,'from':NUMBER})
        def request(method, url, **kwargs):
            self.requests.append((method,url,kwargs))
            if url.endswith('/Calls.json'):
                if method != 'GET': raise AssertionError('Inbound path attempted an outgoing call')
                calls=([{'sid':SID}]+([{'sid':'CA'+'2'*32}] if other_call else [])) if kwargs['params']['Status']=='in-progress' else []
                data={'calls':calls}
            else:
                self.assertTrue(url.endswith('/Calls/'+SID+'.json'))
                data={'sid':SID,'account_sid':ACCOUNT,'direction':'inbound','from':OWNER,'to':NUMBER,
                      'status':'completed' if self.state.closed else 'in-progress',**(overrides or {})}
            response=MagicMock(status=200,json=AsyncMock(return_value=data))
            return MagicMock(__aenter__=AsyncMock(return_value=response),__aexit__=AsyncMock(return_value=False))
        client.request.side_effect=request
        async def ready(url):
            await self.ready(url)
            self.dot.assert_not_awaited()
            self.assertEqual(self.state.call_sid,SID)
            self.assertTrue(self.state.call_ready.is_set())
            if ready_action: await ready_action(self.state,url)
            else: self.state.done.set()
        with patch('bridge.Path.read_text',config),patch('bridge.Path.write_text'),patch('bridge.os.chmod'), \
                patch('bridge.aiohttp.ClientSession',return_value=client), \
                patch.object(Session,'verify_public_connection',new=AsyncMock()):
            return await run(SimpleNamespace(public_url='https://example.test',dot_ready=False,
                start_dot=self.dot,local_audio_verified=True,audio=self.audio,duration=600,port=0,
                manage_signals=False,on_state=set_state,inbound_call_sid=SID,inbound_ready=ready))

    async def test_adopts_existing_inbound_and_never_creates_an_outbound_call(self):
        await self.execute()
        self.ready.assert_awaited_once()
        self.dot.assert_not_awaited()
        posts=[(url,kw['data']) for method,url,kw in self.requests if method=='POST']
        self.assertEqual(len(posts),1)
        self.assertEqual(posts[0][1],{'TimeLimit':'600'})
        self.assertTrue(self.audio.closed)

    async def test_provider_identity_mismatch_cannot_attach_or_hang_up_unrelated_call(self):
        for field,value in [('from','+15555559999'),('to','+15555559999'),('direction','outbound-api'),
                            ('account_sid','AC'+'9'*32),('sid','CA'+'9'*32)]:
            with self.subTest(field=field):
                with self.assertRaisesRegex(RuntimeError,'INBOUND_CALL_IDENTITY_MISMATCH'):
                    await self.execute({field:value})
                self.ready.assert_not_awaited()
                self.assertFalse(any(method=='POST' for method,_,_ in self.requests))

    async def test_caller_already_hung_up_never_starts_dot_or_attaches(self):
        with self.assertRaisesRegex(RuntimeError,'INBOUND_CALL_ALREADY_ENDED'):
            await self.execute({'status':'completed'})
        self.ready.assert_not_awaited();self.dot.assert_not_awaited()
        self.assertFalse(any(method=='POST' for method,_,_ in self.requests))

    async def test_another_active_call_blocks_inbound_bridge(self):
        with self.assertRaisesRegex(RuntimeError,'Another Twilio call is active'):
            await self.execute(other_call=True)
        self.ready.assert_not_awaited()

    async def test_inbound_signed_media_starts_dot_and_carries_audio_both_ways(self):
        async def ready(state,url):
            path='/media/'+state.nonce
            signature=RequestValidator('test-token').compute_signature('https://example.test'+path,{})
            async with TestClient(TestServer(state.app())) as client:
                ws=await client.ws_connect(path,headers={'X-Twilio-Signature':signature})
                await ws.send_json({'event':'start','start':{'callSid':SID,'accountSid':ACCOUNT,'streamSid':'MZtest',
                    'mediaFormat':{'encoding':'audio/x-mulaw','sampleRate':8000,'channels':1}}})
                async with asyncio.timeout(2):
                    while not state.dot_connected: await asyncio.sleep(.001)
                    message=await ws.receive_json()
                    self.assertEqual(len(base64.b64decode(message['media']['payload'])),160)
                    await ws.send_json({'event':'media','streamSid':'MZtest','media':{
                        'track':'inbound','payload':base64.b64encode(b'\xff'*160).decode()}})
                    while not self.audio.received: await asyncio.sleep(.001)
                    await ws.send_json({'event':'stop'})
                    await state.done.wait()
                await ws.close()
        await self.execute(ready_action=ready)
        self.dot.assert_awaited_once()
        self.assertEqual(self.audio.received,160)
        self.assertTrue(self.audio.closed)
