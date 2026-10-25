import asyncio
import base64
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace
import numpy as np
import soxr
from aiohttp.test_utils import TestClient, TestServer
from twilio.request_validator import RequestValidator
from audio import SampleQueue, encode, decode
from bridge import Session, run, check_no_active_calls


class ProviderReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_busy_state_blocks_dial_readiness(self):
        for busy in ('queued', 'ringing', 'in-progress'):
            with self.subTest(busy=busy):
                async def request(method, path, **kwargs):
                    self.assertEqual((method,path), ('GET','/Calls.json'))
                    return {'calls':[{}] if kwargs['params']['Status']==busy else []}
                with self.assertRaisesRegex(RuntimeError, 'Another Twilio call is active'):
                    await check_no_active_calls(request)

    async def test_request_failure_cancels_sibling_checks(self):
        started, closed = set(), set()
        ready = asyncio.Event()
        async def request(method, path, **kwargs):
            status = kwargs['params']['Status']
            started.add(status)
            if len(started)==3: ready.set()
            try:
                await ready.wait()
                if status=='queued': raise RuntimeError('PROVIDER_UNREACHABLE')
                await asyncio.Event().wait()
            finally: closed.add(status)
        with self.assertRaisesRegex(RuntimeError, 'PROVIDER_UNREACHABLE'):
            await check_no_active_calls(request)
        self.assertEqual(closed, {'queued','ringing','in-progress'})


class AudioTests(unittest.TestCase):
    def test_codec(self):
        tone = (.2*np.sin(2*np.pi*1000*np.arange(8000)/8000)).astype('float32')
        restored = decode(encode(tone))
        self.assertLess(float(np.sqrt(np.mean((tone-restored)**2))), .005)
        self.assertEqual(encode(np.zeros(160)), b'\xff'*160)

    def test_resample_continuity(self):
        signal = (.2*np.sin(2*np.pi*997*np.arange(48000)/48000)).astype('float32')
        resampler = soxr.ResampleStream(48000, 8000, 1, dtype='float32', quality='LQ')
        chunks = [resampler.resample_chunk(signal[i:i+960], last=i+960==len(signal))
                  for i in range(0, len(signal), 960)]
        result = np.concatenate(chunks)
        self.assertEqual(len(result), 8000)
        self.assertEqual(int(np.argmax(abs(np.fft.rfft(result)))), 997)
        self.assertLess(float(np.max(abs(np.diff(result)))), .17)

    def test_queue_discards_oldest_and_fills_silence(self):
        queue = SampleQueue(capacity=4)
        queue.put(np.arange(6, dtype='float32'))
        self.assertEqual(queue.dropped, 2)
        np.testing.assert_array_equal(queue.take(6), [2, 3, 4, 5, 0, 0])


class FakeAudio:
    def __init__(self):
        self.started = self.closed = False
        self.received = self.sent = self.discarded = 0
    def start(self): self.started = True
    def close(self): self.closed = True
    def metrics(self): return {}
    def receive(self, payload):
        self.received += len(payload)
        return .1
    def next_packet(self):
        self.sent += 1
        return b'\xff'*160
    def discard_reply(self): self.discarded += 1
    def reset_for_call(self): self.discard_reply()


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.audio = FakeAudio()
        self.state = Session('https://example.test', 'ACtest', 'test-token', self.audio)
        self.state.call_sid = 'CAtest'
        self.state.call_ready.set()
        self.client = TestClient(TestServer(self.state.app()))
        await self.client.start_server()
        self.media = '/media/'+self.state.nonce
        self.status = '/status/'+self.state.nonce
    async def asyncTearDown(self):
        await self.client.close()
    def headers(self, path, params=None):
        return {'X-Twilio-Signature': RequestValidator('test-token').compute_signature(
            'https://example.test'+path, params or {})}
    async def connect(self, call='CAtest'):
        ws = await self.client.ws_connect(self.media, headers=self.headers(self.media))
        await ws.send_json({'event': 'start', 'start': {
            'callSid': call, 'accountSid': 'ACtest', 'streamSid': 'MZtest',
            'mediaFormat': {'encoding': 'audio/x-mulaw', 'sampleRate': 8000, 'channels': 1}}})
        return ws
    async def test_unsigned_request_rejected(self):
        response = await self.client.get(self.media)
        self.assertEqual(response.status, 403)
        self.assertFalse(self.audio.started)
    async def test_public_probe_does_not_claim_media_or_start_audio(self):
        self.state.origin = str(self.client.make_url('')).rstrip('/')
        import aiohttp
        async with aiohttp.ClientSession() as client:
            await self.state.verify_public_connection(client, timeout=.5)
        self.assertFalse(self.state.claimed)
        self.assertFalse(self.audio.started)
        self.assertFalse(self.state.closed)
    async def test_public_502_fails_preflight(self):
        import aiohttp
        from aiohttp import web
        app = web.Application()
        async def unavailable(request):
            return web.Response(status=502)
        app.router.add_get('/{path:.*}', unavailable)
        async with TestServer(app) as server, aiohttp.ClientSession() as client:
            self.state.origin = str(server.make_url('')).rstrip('/')
            with self.assertRaisesRegex(RuntimeError, 'PUBLIC_BRIDGE_UNREACHABLE') as failed:
                await self.state.verify_public_connection(client, timeout=.05)
            self.assertEqual(failed.exception.diagnostics['reason'],'http_error')
            self.assertEqual(failed.exception.diagnostics['http_status'],502)
            self.assertFalse(failed.exception.diagnostics['phone_dialed'])
        self.assertFalse(self.state.claimed)
    async def test_unreachable_bridge_never_dials(self):
        import json
        client = MagicMock()
        client.close = AsyncMock()
        def config(path, *args, **kwargs):
            if path.name == 'twilio.json':
                return json.dumps({'TWILIO_ACCOUNT_SID':'ACtest','TWILIO_AUTH_TOKEN':'test-token'})
            return json.dumps({'phone':'+15555550101','from':'+15555550102'})
        with patch('bridge.Path.read_text', config), patch('bridge.AudioBridge', FakeAudio), \
                patch('bridge.aiohttp.ClientSession', return_value=client), \
                patch.object(Session, 'verify_public_connection', create=True,
                    new=AsyncMock(side_effect=RuntimeError('PUBLIC_BRIDGE_UNREACHABLE'))):
            with self.assertRaisesRegex(RuntimeError, 'PUBLIC_BRIDGE_UNREACHABLE'):
                await run(SimpleNamespace(public_url='https://example.test',dot_ready=True,
                    duration=120,port=0,manage_signals=False))
        client.request.assert_not_called()
        client.close.assert_awaited_once()

    async def test_silent_browser_never_reaches_twilio(self):
        import json
        client = MagicMock()
        client.close = AsyncMock()
        def config(path, *args, **kwargs):
            if path.name == 'twilio.json':
                return json.dumps({'TWILIO_ACCOUNT_SID':'ACtest','TWILIO_AUTH_TOKEN':'test-token'})
            return json.dumps({'phone':'+15555550101','from':'+15555550102'})
        with patch('bridge.Path.read_text', config), patch('bridge.AudioBridge', FakeAudio), \
                patch('bridge.aiohttp.ClientSession', return_value=client), \
                patch.object(Session, 'verify_public_connection', new=AsyncMock()), \
                patch('bridge.verify_browser_audio', create=True,
                    new=AsyncMock(side_effect=RuntimeError('DOT_AUDIO_SILENT'))):
            with self.assertRaisesRegex(RuntimeError, 'DOT_AUDIO_SILENT'):
                await run(SimpleNamespace(public_url='https://example.test',dot_ready=True,
                    duration=120,port=0,manage_signals=False))
        client.request.assert_not_called()
        client.close.assert_awaited_once()

    async def test_fast_path_dials_without_spoken_check_or_starting_dot_early(self):
        import json
        client = MagicMock()
        client.close = AsyncMock()
        state = None
        calls = []
        checks_started = set()
        all_checks_started = asyncio.Event()
        def set_state(value):
            nonlocal state
            state = value
        def config(path, *args, **kwargs):
            if path.name == 'twilio.json':
                return json.dumps({'TWILIO_ACCOUNT_SID':'ACtest','TWILIO_AUTH_TOKEN':'test-token'})
            return json.dumps({'phone':'+15555550101','from':'+15555550102'})
        def request(method, url, **kwargs):
            calls.append((method, url))
            response = MagicMock(status=200)
            if method == 'POST':
                # An unanswered call ends; Dot must never have spoken into it.
                state.done.set()
                data = {'sid':'CAtest'}
            elif url.endswith('/Calls/CAtest.json'):
                data = {'status':'completed'}
            else:
                data = {'calls':[]}
            async def read_response():
                if method == 'GET' and url.endswith('/Calls.json'):
                    checks_started.add(kwargs['params']['Status'])
                    if len(checks_started) == 3:
                        all_checks_started.set()
                    await asyncio.wait_for(all_checks_started.wait(), .25)
                return data
            response.json = AsyncMock(side_effect=read_response)
            context = MagicMock()
            context.__aenter__ = AsyncMock(return_value=response)
            context.__aexit__ = AsyncMock(return_value=False)
            return context
        client.request.side_effect = request
        start_dot = AsyncMock()
        with patch('bridge.Path.read_text', config), patch('bridge.Path.write_text'), \
                patch('bridge.os.chmod'), patch('bridge.aiohttp.ClientSession',return_value=client), \
                patch.object(Session,'verify_public_connection',new=AsyncMock()), \
                patch('bridge.verify_browser_audio',new=AsyncMock()) as spoken:
            await run(SimpleNamespace(public_url='https://example.test',dot_ready=False,
                start_dot=start_dot,local_audio_verified=True,audio=FakeAudio(),
                duration=20,port=0,manage_signals=False,on_state=set_state))
        spoken.assert_not_awaited()
        start_dot.assert_not_awaited()
        self.assertEqual(sum(method == 'POST' for method, url in calls), 1)

    async def test_fast_path_requires_local_audio_verification(self):
        import json
        client=MagicMock()
        client.close=AsyncMock()
        def config(path, *args, **kwargs):
            return json.dumps({'TWILIO_ACCOUNT_SID':'ACtest','TWILIO_AUTH_TOKEN':'test-token'}
                if path.name=='twilio.json' else {'phone':'+15555550101','from':'+15555550102'})
        with patch('bridge.Path.read_text',config),patch('bridge.aiohttp.ClientSession',return_value=client), \
                patch.object(Session,'verify_public_connection',new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError,'LOCAL_AUDIO_NOT_VERIFIED'):
                await run(SimpleNamespace(public_url='https://example.test',dot_ready=False,
                    start_dot=AsyncMock(),audio=FakeAudio(),duration=20,port=0,manage_signals=False))
        client.request.assert_not_called()

    async def test_no_media_is_not_a_completed_conversation(self):
        await self.state.finish('phone_completed')
        self.assertEqual(self.state.outcome(), {'status':'failed','code':'PHONE_MEDIA_NOT_CONNECTED'})

    async def test_failed_provider_status_is_not_completion(self):
        await self.state.finish('phone_failed')
        self.assertEqual(self.state.outcome(), {'status':'failed','code':'PHONE_FAILED'})
    async def test_wrong_call_never_starts_audio(self):
        ws = await self.connect('CAwrong')
        await ws.receive(timeout=2)
        self.assertFalse(self.audio.started)
        self.assertTrue(self.state.closed)
    async def test_answer_starts_dot_once_and_preserves_first_audio(self):
        starting = asyncio.Event()
        connected = asyncio.Event()
        async def start_dot():
            starting.set()
            await connected.wait()
        self.state.start_dot = start_dot
        self.state.dot_connected = False
        self.assertFalse(starting.is_set())
        ws = await self.connect()
        await asyncio.wait_for(starting.wait(), .2)
        # Caller hears a connecting tone before Dot speaks.
        tone = base64.b64decode((await ws.receive_json(timeout=1))['media']['payload'])
        self.assertGreater(float(np.max(abs(decode(tone)))), .01)
        # Dot may speak just before the browser's connected check returns.
        greeting = encode(np.full(160, .1, dtype='float32'))
        self.audio.next_packet = lambda: greeting
        while base64.b64decode((await ws.receive_json(timeout=1))['media']['payload']) != greeting:
            pass
        connected.set()
        await ws.send_json({'event':'stop'})
        await asyncio.wait_for(self.state.done.wait(), 1)
        await ws.close()

    async def test_hangup_during_dot_start_cancels_start(self):
        started = asyncio.Event()
        canceled = asyncio.Event()
        async def start_dot():
            started.set()
            try: await asyncio.Event().wait()
            finally: canceled.set()
        self.state.start_dot = start_dot
        self.state.dot_connected = False
        ws = await self.connect()
        await asyncio.wait_for(started.wait(), .2)
        await ws.send_json({'event':'stop'})
        await asyncio.wait_for(canceled.wait(), 1)
        await ws.close()

    async def test_dot_start_failure_ends_answered_call(self):
        self.state.start_dot = AsyncMock(side_effect=RuntimeError('DOT_MICROPHONE_NOT_VERIFIED'))
        self.state.dot_connected = False
        ws = await self.connect()
        await asyncio.wait_for(self.state.done.wait(), .2)
        self.assertEqual(self.state.outcome()['code'], 'DOT_MICROPHONE_NOT_VERIFIED')
        await ws.close()
    async def test_round_trip_noise_and_stop(self):
        ws = await self.connect()
        message = await ws.receive_json(timeout=2)
        self.assertEqual(len(base64.b64decode(message['media']['payload'])), 160)
        duplicate = await self.client.get(self.media, headers=self.headers(self.media))
        self.assertEqual(duplicate.status, 409)
        for _ in range(3):
            await ws.send_json({'event':'media', 'streamSid':'MZtest', 'media': {
                'track':'inbound', 'payload':base64.b64encode(b'\xff'*160).decode()}})
        # The bridge forwards caller audio to Dot, which owns turn detection.
        # It no longer treats the fake input level as permission to erase speech.
        while self.audio.received < 480:
            await asyncio.sleep(.001)
        self.assertEqual(self.audio.received, 480)
        self.assertEqual(self.audio.discarded, 1)
        await ws.send_json({'event':'stop'})
        await asyncio.wait_for(self.state.done.wait(), 2)
        sent = self.audio.sent
        await asyncio.sleep(.06)
        self.assertTrue(self.audio.closed)
        self.assertEqual(self.audio.sent, sent)
        retry = await self.client.get(self.media, headers=self.headers(self.media))
        self.assertEqual(retry.status, 409)
        await ws.close()

    async def test_playback_marks_acknowledged_and_unknown_marks_ignored(self):
        ws = await self.connect()
        message = await ws.receive_json(timeout=2)
        while message['event'] != 'mark':
            message = await ws.receive_json(timeout=2)
        await ws.send_json({'event': 'mark', 'streamSid': 'MZtest', 'mark': {'name': 'unknown'}})
        await ws.send_json(message)
        for _ in range(100):
            if self.state.marks_acked:
                break
            await asyncio.sleep(.001)
        self.assertEqual(self.state.marks_acked, 1)
        self.assertGreater(self.state.mark_max_round_trip_ms, 0)
        self.assertNotIn(message['mark']['name'], self.state.pending_marks)
        await ws.send_json({'event': 'stop'})
        await ws.close()
    async def test_status_requires_signature_and_matching_call(self):
        params = {'CallSid': 'CAtest', 'CallStatus': 'completed'}
        bad = await self.client.post(self.status, data=params)
        self.assertEqual(bad.status, 403)
        other = dict(params, CallSid='CAother')
        await self.client.post(self.status, data=other, headers=self.headers(self.status, other))
        self.assertFalse(self.state.closed)
        good = await self.client.post(self.status, data=params, headers=self.headers(self.status, params))
        self.assertEqual(good.status, 200)
        self.assertTrue(self.state.closed)


if __name__ == '__main__': unittest.main()
