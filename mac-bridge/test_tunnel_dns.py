import socket
import unittest
from unittest.mock import AsyncMock,patch
from tunnel_dns import TunnelResolver

class ResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_local_lookup_uses_public_answer_for_exact_tunnel_only(self):
        resolver=TunnelResolver('fresh.trycloudflare.com')
        resolver.system.resolve=AsyncMock(side_effect=socket.gaierror(8,'not found'))
        resolver.lookup=AsyncMock(return_value={'Status':0,'Answer':[{'name':'fresh.trycloudflare.com.','type':1,'data':'104.16.230.132'}]})
        result=await resolver.resolve('fresh.trycloudflare.com',443)
        self.assertEqual(result[0]['host'],'104.16.230.132')
        self.assertEqual(result[0]['hostname'],'fresh.trycloudflare.com')
        self.assertEqual(result[0]['port'],443)
        resolver.lookup.reset_mock()
        with self.assertRaises(socket.gaierror):await resolver.resolve('api.twilio.com',443)
        resolver.lookup.assert_not_called()
        await resolver.close()

    async def test_valid_system_result_does_not_use_fallback(self):
        resolver=TunnelResolver('fresh.trycloudflare.com')
        resolver.system.resolve=AsyncMock(return_value=[{'host':'104.16.230.132'}])
        resolver.lookup=AsyncMock()
        self.assertEqual(await resolver.resolve('fresh.trycloudflare.com',443),[{'host':'104.16.230.132'}])
        resolver.lookup.assert_not_called()
        await resolver.close()

    async def test_unrelated_private_and_failed_dns_answers_are_rejected(self):
        resolver=TunnelResolver('fresh.trycloudflare.com')
        resolver.system.resolve=AsyncMock(side_effect=socket.gaierror(8,'not found'))
        for response in ({'Status':3}, {'Status':0,'Answer':[{'name':'other.example','type':1,'data':'104.16.230.132'}]}, {'Status':0,'Answer':[{'name':'fresh.trycloudflare.com','type':1,'data':'127.0.0.1'}]}):
            resolver.lookup=AsyncMock(return_value=response)
            with self.assertRaises(socket.gaierror):await resolver.resolve('fresh.trycloudflare.com',443)
        await resolver.close()
