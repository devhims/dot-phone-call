import asyncio
import json
import struct
import unittest
import uuid
from unittest.mock import AsyncMock, patch
from native_host import encode_frame, read_frame, validate_request, run_operation
from companion import Companion

class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    def test_backend_is_limited_to_paired_local_queue(self):
        base={'type':'run','mode':'recover','instance':str(uuid.uuid4())}
        for origin in [None,'http://127.0.0.1:8894',
                'http://127.0.0.1:8894']:
            self.assertEqual(validate_request({**base,'backend_origin':origin})['backend_origin'],origin)
        for origin in ['http://127.0.0.1:9999','https://evil.test',
                'http://127.0.0.1:8894@evil.test','http://localhost:8894']:
            with self.assertRaises(ValueError): validate_request({**base,'backend_origin':origin})

    async def test_fragmented_frames(self):
        reader=asyncio.StreamReader()
        frame=encode_frame({'type':'heartbeat'})
        task=asyncio.create_task(read_frame(reader))
        for byte in frame:
            reader.feed_data(bytes([byte]));await asyncio.sleep(0)
        self.assertEqual(await task,{'type':'heartbeat'})
    async def test_rejects_oversized_or_non_object_frames(self):
        for frame in [struct.pack('=I',8193),struct.pack('=I',2)+b'[]']:
            reader=asyncio.StreamReader();reader.feed_data(frame)
            with self.assertRaises(ValueError): await read_frame(reader)
    async def test_truncated_pipe_is_not_a_command(self):
        reader=asyncio.StreamReader();reader.feed_data(b'\x04');reader.feed_eof()
        with self.assertRaises(asyncio.IncompleteReadError): await read_frame(reader)
    def test_no_arbitrary_native_commands_or_unbounded_calls(self):
        instance=str(uuid.uuid4())
        for mode in ['shell','exec','dial','']:
            with self.assertRaises(ValueError):validate_request({'type':'run','mode':mode,'instance':instance})
        for duration in [0,19,601,True,'60']:
            with self.assertRaises(ValueError):validate_request({'type':'run','mode':'call','instance':instance,
                'job':{'id':str(uuid.uuid4()),'duration_seconds':duration}})
    async def test_diagnostic_cannot_claim_or_dial_and_exits_after_cleanup(self):
        reader=asyncio.StreamReader();reader.feed_data(encode_frame({'type':'ready','ok':True,'version':'0.1.7'}))
        sent=[]
        async def test_audio(companion, job_id):
            companion.current=None
        with patch('native_host.RUNTIME') as runtime,patch('native_host.SNAPSHOT') as snapshot, \
            patch.object(Companion,'test_browser_audio',test_audio), \
            patch.object(Companion,'call',new=AsyncMock()) as call, \
            patch.object(Companion,'cloud',new=AsyncMock()) as cloud:
            snapshot.exists.return_value=False
            (runtime/'browser-audio-test-result.json').read_text.return_value=json.dumps({'verified':True,'dialed':False})
            result=await run_operation({}, {'mode':'audio-test','instance':str(uuid.uuid4())},reader,sent.append)
            call.assert_not_called();cloud.assert_not_called()
        self.assertTrue(result['verified']);self.assertFalse(result['dialed'])
        self.assertTrue(result['cleanup_ok']);self.assertEqual(sent[-1]['type'],'complete')

    def test_inbound_jobs_require_an_exact_provider_call_id(self):
        request={'type':'run','mode':'call','instance':str(uuid.uuid4()),
            'job':{'id':str(uuid.uuid4()),'duration_seconds':600,'direction':'inbound','inbound_call_sid':'CA'+'1'*32}}
        self.assertEqual(validate_request(request),request)
        for extra in [{'inbound_call_sid':'CAwrong'},{'inbound_call_sid':'../../Calls'},
                      {'direction':'outbound'},{'direction':'unknown'}]:
            with self.subTest(extra=extra),self.assertRaises(ValueError):
                validate_request({**request,'job':{**request['job'],**extra}})
    async def test_disconnect_before_start_does_not_open_audio(self):
        reader=asyncio.StreamReader();reader.feed_eof()
        with patch('native_host.RUNTIME'),patch('native_host.SNAPSHOT') as snapshot, \
            patch.object(Companion,'test_browser_audio',new=AsyncMock()) as audio:
            snapshot.exists.return_value=False
            result=await run_operation({}, {'mode':'audio-test','instance':str(uuid.uuid4())},reader,lambda _:None)
            audio.assert_not_called()
        self.assertEqual(result['code'],'NATIVE_BROWSER_NOT_READY')
    async def test_route_test_has_no_browser_voice_cloud_or_phone_operation(self):
        reader=asyncio.StreamReader();reader.feed_data(encode_frame({'type':'ready','ok':True,'version':'0.1.7'}))
        with patch('native_host.RUNTIME'),patch('native_host.SNAPSHOT') as snapshot, \
            patch('native_host.verify_local_audio',new=AsyncMock(return_value={'dot_to_phone_rms':.05,'phone_to_dot_rms':.03})), \
            patch.object(Companion,'call',new=AsyncMock()) as call, \
            patch.object(Companion,'command',new=AsyncMock()) as command, \
            patch.object(Companion,'cloud',new=AsyncMock()) as cloud:
            snapshot.exists.return_value=False
            result=await run_operation({}, {'mode':'route-test','instance':str(uuid.uuid4())},reader,lambda _:None)
            call.assert_not_called();command.assert_not_called();cloud.assert_not_called()
        self.assertTrue(result['verified']);self.assertFalse(result['dialed']);self.assertTrue(result['cleanup_ok'])
    async def test_recovery_restores_only_after_confirmed_browser_hangup(self):
        reader=asyncio.StreamReader();reader.feed_data(encode_frame({'type':'ready','ok':True,'version':'0.1.7'}))
        events=[]
        with patch('native_host.RUNTIME'),patch('native_host.SNAPSHOT') as snapshot, \
            patch('companion.SNAPSHOT',snapshot):
            snapshot.exists.return_value=True
            async def command(companion,action,**kw):
                events.append(action);return {'ended':True}
            async def route(companion,action):
                events.append(action);snapshot.exists.return_value=False
            with patch.object(Companion,'command',command),patch.object(Companion,'route',route):
                result=await run_operation({}, {'mode':'recover','instance':str(uuid.uuid4())},reader,lambda _:None)
        self.assertEqual(events,['end','restore']);self.assertTrue(result['cleanup_ok'])
    async def test_failed_hangup_retains_virtual_route_for_recovery(self):
        reader=asyncio.StreamReader();reader.feed_data(encode_frame({'type':'ready','ok':True,'version':'0.1.7'}))
        with patch('native_host.RUNTIME'),patch('native_host.SNAPSHOT') as snapshot, \
            patch('companion.SNAPSHOT',snapshot), \
            patch.object(Companion,'command',new=AsyncMock(side_effect=RuntimeError('BROWSER_OFFLINE'))), \
            patch.object(Companion,'route',new=AsyncMock()) as route:
            snapshot.exists.return_value=True
            result=await run_operation({}, {'mode':'recover','instance':str(uuid.uuid4())},reader,lambda _:None)
            route.assert_not_called()
        self.assertFalse(result['cleanup_ok']);self.assertFalse(result['verified'])

    async def test_prepare_diagnostic_only_prepares_a_fresh_tab_without_audio_or_phone(self):
        reader=asyncio.StreamReader();reader.feed_data(encode_frame({'type':'ready','ok':True,'version':'0.1.9'}))
        with patch('native_host.RUNTIME'),patch('native_host.SNAPSHOT') as snapshot, \
            patch.object(Companion,'command',new=AsyncMock(return_value={'ready':True,'tab_id':99,'version':'0.1.9'})) as command, \
            patch.object(Companion,'call',new=AsyncMock()) as call, \
            patch.object(Companion,'cloud',new=AsyncMock()) as cloud, \
            patch.object(Companion,'route',new=AsyncMock()) as route:
            snapshot.exists.return_value=False
            result=await run_operation({}, {'mode':'prepare-test','instance':str(uuid.uuid4())},reader,lambda _:None)
            command.assert_awaited_once_with('prepare',diagnostic_new_tab=True)
            call.assert_not_called();cloud.assert_not_called();route.assert_not_called()
        self.assertTrue(result['verified']);self.assertFalse(result['dialed']);self.assertTrue(result['cleanup_ok'])

    async def test_hidden_audio_diagnostic_cannot_enqueue_or_dial(self):
        reader=asyncio.StreamReader();reader.feed_data(encode_frame({'type':'ready','ok':True,'version':'0.2.0'}))
        with patch('native_host.RUNTIME'),patch('native_host.SNAPSHOT') as snapshot, \
            patch('offscreen_test.test_offscreen_audio',new=AsyncMock(return_value={'verified':True,'dialed':False})) as diagnostic, \
            patch.object(Companion,'call',new=AsyncMock()) as call, \
            patch.object(Companion,'cloud',new=AsyncMock()) as cloud:
            snapshot.exists.return_value=False
            result=await run_operation({}, {'mode':'hidden-audio-test','instance':str(uuid.uuid4())},reader,lambda _:None)
            diagnostic.assert_awaited_once();call.assert_not_called();cloud.assert_not_called()
        self.assertTrue(result['verified']);self.assertFalse(result['dialed'])
