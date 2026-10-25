import asyncio
import unittest
from unittest.mock import AsyncMock,MagicMock,patch
from public_tunnel import open_verified_tunnel

def dns_failure(reason='dns_lookup_failed'):
    error=RuntimeError('PUBLIC_BRIDGE_UNREACHABLE')
    error.diagnostics={'reason':reason}
    return error

def process():
    item=MagicMock(returncode=None)
    item.stdout.readline=AsyncMock(side_effect=[b'https://fresh.trycloudflare.com\n',b'Registered tunnel connection\n',b''])
    async def wait():item.returncode=0
    item.wait=AsyncMock(side_effect=wait)
    return item

class TunnelTests(unittest.IsolatedAsyncioTestCase):
    async def test_dns_failure_closes_first_tunnel_before_one_replacement(self):
        first,second=process(),process()
        calls=0
        async def launch(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==2:self.assertEqual(first.returncode,0)
            return first if calls==1 else second
        with patch('public_tunnel.asyncio.create_subprocess_exec',side_effect=launch),patch('public_tunnel.verify_public_bridge',new=AsyncMock(side_effect=[dns_failure(),None])) as verify:
            tunnel=await open_verified_tunnel()
            self.assertEqual(tunnel.attempt,2)
            self.assertEqual(verify.await_count,2)
            await tunnel.close()
        first.terminate.assert_called_once();second.terminate.assert_called_once()

    async def test_second_dns_failure_is_terminal(self):
        processes=[process(),process()]
        with patch('public_tunnel.asyncio.create_subprocess_exec',new=AsyncMock(side_effect=processes)) as launch,patch('public_tunnel.verify_public_bridge',new=AsyncMock(side_effect=dns_failure())):
            with self.assertRaisesRegex(RuntimeError,'PUBLIC_BRIDGE_UNREACHABLE'):await open_verified_tunnel()
            self.assertEqual(launch.await_count,2)
        for item in processes:item.terminate.assert_called_once()

    async def test_non_dns_failure_does_not_replace_tunnel(self):
        item=process()
        with patch('public_tunnel.asyncio.create_subprocess_exec',new=AsyncMock(return_value=item)) as launch,patch('public_tunnel.verify_public_bridge',new=AsyncMock(side_effect=dns_failure('tls_failed'))):
            with self.assertRaisesRegex(RuntimeError,'PUBLIC_BRIDGE_UNREACHABLE'):await open_verified_tunnel()
            self.assertEqual(launch.await_count,1)
        item.terminate.assert_called_once()

    async def test_cancellation_closes_tunnel_without_replacement(self):
        item=process();started=asyncio.Event()
        async def blocked(*args):started.set();await asyncio.Event().wait()
        with patch('public_tunnel.asyncio.create_subprocess_exec',new=AsyncMock(return_value=item)) as launch,patch('public_tunnel.verify_public_bridge',side_effect=blocked):
            task=asyncio.create_task(open_verified_tunnel());await started.wait();task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
            self.assertEqual(launch.await_count,1)
        item.terminate.assert_called_once()
