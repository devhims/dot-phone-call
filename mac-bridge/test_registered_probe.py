import unittest
import numpy as np
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from xml.etree import ElementTree

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from registered_probe import RegisteredProbe


class RegisteredPolicyTests(unittest.TestCase):
    def setUp(self):
        self.probe = RegisteredProbe('AC' + '1' * 32, 'fake')

    def test_only_fixed_random_clients_without_webhooks(self):
        options = self.probe.call_options()
        self.assertEqual(options['to'], 'client:' + self.probe.identities[0])
        root = ElementTree.fromstring(options['twiml'])
        self.assertEqual(root.find('Dial/Client').text, self.probe.identities[1])
        self.assertIsNone(root.find('.//Number'))
        self.assertFalse(options['record'])
        self.assertLessEqual(options['time_limit'], 60)
        self.assertFalse(any('url' in k.lower() or 'callback' in k.lower() for k in options))
        self.assertFalse(any('url' in k.lower() or 'callback' in k.lower() for e in root.iter() for k in e.attrib))

    def test_origin_and_host_required(self):
        for host, origin in [('evil.test', self.probe.origin), ('127.0.0.1:8893', 'https://evil.test'),
                             ('127.0.0.1:8893', None), ('localhost:8893', self.probe.origin)]:
            with self.assertRaises(web.HTTPForbidden):
                self.probe.check(SimpleNamespace(host=host, method='POST', path='/start', headers={'Origin': origin}))


class RegisteredHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.probe = RegisteredProbe('AC' + '1' * 32, 'fake')
        self.probe.client = Mock()
        self.probe.client.calls.create.return_value = SimpleNamespace(sid='CA' + '2' * 32)
        self.probe.save = Mock()
        self.probe.event = Mock()
        self.client = TestClient(TestServer(self.probe.app()))
        await self.client.start_server()
        self.probe.origin = str(self.client.make_url('')).rstrip('/')

    async def asyncTearDown(self):
        await self.client.close()

    async def test_start_requires_tokens_and_is_one_shot_ignoring_destination(self):
        headers = {'Origin': self.probe.origin}
        response = await self.client.post('/start', headers=headers)
        self.assertEqual(response.status, 409)
        self.probe.issued = True
        with patch('registered_probe.Path.exists', return_value=False):
            response = await self.client.post('/start', headers=headers, json={'to': '+15555550100'})
            self.assertEqual(response.status, 200)
            response = await self.client.post('/start', headers=headers)
            self.assertEqual(response.status, 409)
        self.probe.client.calls.create.assert_called_once_with(**self.probe.call_options())

    async def test_reject_cross_origin_and_unexposed_routes(self):
        response = await self.client.post('/start', headers={'Origin': 'https://evil.test'})
        self.assertEqual(response.status, 403)
        response = await self.client.get('/secrets.json')
        self.assertEqual(response.status, 404)
        response = await self.client.post('/voice')
        self.assertEqual(response.status, 405)
        self.probe.client.calls.create.assert_not_called()

    async def test_uncertain_create_cannot_be_retried(self):
        self.probe.issued = True
        self.probe.client.calls.create.side_effect = TimeoutError('unknown result')
        with patch('registered_probe.Path.exists', return_value=False):
            response = await self.client.post('/start', headers={'Origin': self.probe.origin})
            self.assertEqual(response.status, 502)
            response = await self.client.post('/start', headers={'Origin': self.probe.origin})
            self.assertEqual(response.status, 409)
        self.probe.client.calls.create.assert_called_once()

    async def test_audio_socket_requires_origin_and_capability(self):
        self.probe.native_audio = self.probe.issued = True
        response = await self.client.get('/audio', headers={'Origin': 'https://evil.test'})
        self.assertEqual(response.status, 403)
        socket = await self.client.ws_connect('/audio', headers={'Origin': self.probe.origin})
        await socket.send_str('wrong capability')
        await socket.receive()
        self.assertEqual(socket.close_code, 1008)
        self.assertFalse(self.probe.audio_claimed)

    async def test_audio_socket_accepts_bounded_finite_pcm_only(self):
        self.probe.native_audio = self.probe.issued = True
        socket = await self.client.ws_connect('/audio', headers={'Origin': self.probe.origin})
        await socket.send_str(self.probe.audio_capability)
        self.assertEqual((await socket.receive()).data, 'ready')
        chunk = np.full(960, .05, dtype='<f4')
        await socket.send_bytes(chunk.tobytes())
        await socket.send_bytes(np.full(960, np.nan, dtype='<f4').tobytes())
        await socket.receive()
        self.assertEqual(socket.close_code, 1008)
        self.assertEqual(self.probe.audio_stats['received_frames'], 960)
        np.testing.assert_array_equal(self.probe.audio_queue.get_nowait(), chunk)


if __name__ == '__main__':
    unittest.main()
