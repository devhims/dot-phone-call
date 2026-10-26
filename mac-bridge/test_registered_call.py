import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from registered_call import run_registered_call

ROUTED = {'output': {'device': 'BlackHole 2ch', 'routed_elements': 2, 'routed_contexts': 1, 'errors': []}, 'output_verified': True}
IDLE_DEFAULTS = json.dumps({'input': {'name': 'MacBook Microphone'}, 'output': {'name': 'MacBook Speakers'}}).encode()


def companion_with(commands, cloud):
    return SimpleNamespace(bridge_state=None, current={'id': 'job'}, cleanup_failed=False,
        command=AsyncMock(side_effect=commands), route=AsyncMock(), cloud=AsyncMock(side_effect=cloud), report=MagicMock())


def defaults(payload=IDLE_DEFAULTS):
    return SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(payload, None)))


class RegisteredCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_calls_never_change_mac_audio_devices_in_either_direction(self):
        for direction in ['inbound', 'outbound']:
            companion = companion_with(
                [{'tab_id': 1}, {'connected': True, 'microphone_verified': True, **ROUTED},
                 {'active': False, 'microphone_verified': True, **ROUTED}, {'ended': True}],
                [{'ready': True}, {'connected': True}, {'ended': True}])
            with patch('registered_call.asyncio.create_subprocess_exec', return_value=defaults()), \
                    patch('companion.SNAPSHOT') as snapshot:
                snapshot.exists.return_value = False
                await run_registered_call(companion, {'id': 'job', 'duration_seconds': 60, 'direction': direction})
            companion.route.assert_not_awaited()
            self.assertEqual(companion.cloud.await_args_list[0].args, ('registered-prepare', {'call_id': 'job'}))
            self.assertEqual([c.args[0] for c in companion.command.await_args_list], ['prepare', 'start', 'inspect_owned', 'end'])
            self.assertFalse(companion.cleanup_failed); self.assertIsNone(companion.current)
            companion.report.assert_called_with('completed', None)

    async def test_dot_player_that_refuses_blackhole_fails_the_call_and_ends_dot(self):
        refused = {'output': {**ROUTED['output'], 'errors': ['elements:NotAllowedError']}, 'output_verified': False}
        companion = companion_with(
            [{'tab_id': 1}, {'connected': True, 'microphone_verified': True, **ROUTED},
             {'active': True, 'microphone_verified': True, **refused}, {'ended': True}],
            [{'ready': True}, {'connected': True}, {'ended': True}])
        with patch('registered_call.asyncio.create_subprocess_exec', return_value=defaults()), patch('companion.SNAPSHOT') as snapshot:
            snapshot.exists.return_value = False
            await run_registered_call(companion, {'id': 'job', 'duration_seconds': 60})
        self.assertEqual(companion.command.await_args_list[-1].args[0], 'end')
        companion.report.assert_called_with('failed', 'DOT_OUTPUT_NOT_ROUTED')

    async def test_unrouted_dot_output_from_start_gate_fails_the_call(self):
        companion = companion_with([{'tab_id': 1}, RuntimeError('DOT_OUTPUT_NOT_ROUTED'), {'ended': True}],
            [{'ready': True}, {'connected': True}, {'ended': True}])
        with patch('registered_call.asyncio.create_subprocess_exec', return_value=defaults()), patch('companion.SNAPSHOT') as snapshot:
            snapshot.exists.return_value = False
            await run_registered_call(companion, {'id': 'job', 'duration_seconds': 60})
        companion.report.assert_called_with('failed', 'DOT_OUTPUT_NOT_ROUTED')
        self.assertNotIn('connected', [c.args[0] for c in companion.report.call_args_list])

    async def test_stop_request_ends_a_connected_call_promptly(self):
        async def command(action, **fields):
            return {'prepare': {'tab_id': 1}, 'start': {'connected': True, 'microphone_verified': True, **ROUTED},
                    'inspect_owned': {'active': True, 'microphone_verified': True, **ROUTED}, 'end': {'ended': True}}[action]
        async def cloud(action, data):
            return {'registered-prepare': {'ready': True}, 'registered-state': {'connected': True},
                    'registered-finish': {'ended': True}}[action]
        companion = companion_with(None, None)
        companion.command.side_effect, companion.cloud.side_effect = command, cloud
        with patch('registered_call.asyncio.create_subprocess_exec', return_value=defaults()), patch('companion.SNAPSHOT') as snapshot:
            snapshot.exists.return_value = False
            task = asyncio.create_task(run_registered_call(companion, {'id': 'job', 'duration_seconds': 60}))
            while companion.report.call_count == 0:
                await asyncio.sleep(.01)
            started = asyncio.get_running_loop().time()
            companion.bridge_state.done.set()
            await asyncio.wait_for(task, 3)
        self.assertLess(asyncio.get_running_loop().time() - started, 1.5)
        companion.report.assert_called_with('completed', None)

    async def test_snapshot_left_by_an_older_version_is_restored_at_the_end(self):
        companion = companion_with(
            [{'tab_id': 1}, {'connected': True, 'microphone_verified': True, **ROUTED},
             {'active': False, 'microphone_verified': True, **ROUTED}, {'ended': True}],
            [{'ready': True}, {'connected': True}, {'ended': True}])
        with patch('registered_call.asyncio.create_subprocess_exec', return_value=defaults()), patch('companion.SNAPSHOT') as snapshot:
            snapshot.exists.side_effect = [False, True]
            await run_registered_call(companion, {'id': 'job', 'duration_seconds': 60})
        companion.route.assert_awaited_once_with('restore')

    async def test_an_existing_blackhole_route_is_not_taken_over(self):
        companion = companion_with([{'ended': True}], [{'ended': True}])
        with patch('registered_call.asyncio.create_subprocess_exec', return_value=defaults(b'{"input":{"name":"BlackHole 16ch"}}')), \
                patch('companion.SNAPSHOT') as snapshot:
            snapshot.exists.return_value = False
            await run_registered_call(companion, {'id': 'job', 'duration_seconds': 60})
        companion.route.assert_not_awaited()
        companion.report.assert_called_with('failed', 'AUDIO_ROUTE_ALREADY_IN_USE')

    async def test_idle_blackhole_defaults_are_recovered_before_browser_setup(self):
        companion = companion_with([RuntimeError('STOP_AFTER_PREFLIGHT'), {'ended': True}], [{'ended': True}])
        state = {'input': {'name': 'BlackHole 16ch', 'running': False}, 'output': {'name': 'BlackHole 2ch', 'running': False}}
        with patch('registered_call.asyncio.create_subprocess_exec', return_value=defaults(json.dumps(state).encode())), \
                patch('companion.SNAPSHOT') as snapshot:
            snapshot.exists.return_value = False
            await run_registered_call(companion, {'id': 'job', 'duration_seconds': 60})
        companion.route.assert_awaited_once_with('recover-idle')
        companion.report.assert_called_with('failed', 'STOP_AFTER_PREFLIGHT')
        self.assertEqual(companion.cloud.await_args_list[0].args[0], 'registered-finish')

    async def test_running_blackhole_defaults_remain_protected(self):
        companion = companion_with([{'ended': True}], [{'ended': True}])
        with patch('registered_call.asyncio.create_subprocess_exec', return_value=defaults(b'{"input":{"name":"BlackHole 16ch","running":true}}')), \
                patch('companion.SNAPSHOT') as snapshot:
            snapshot.exists.return_value = False
            await run_registered_call(companion, {'id': 'job', 'duration_seconds': 60})
        companion.route.assert_not_awaited()
        companion.report.assert_called_with('failed', 'AUDIO_ROUTE_ALREADY_IN_USE')


if __name__ == '__main__':
    unittest.main()
