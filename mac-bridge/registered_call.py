"""Per-call sequencing only. Twilio signaling and durable ownership live in the local backend.

Audio never passes through this helper. Dot's tab routes its playback to
BlackHole 2ch and the hidden Twilio page plays phone audio to BlackHole 16ch,
so the Mac's default devices are left unchanged.
"""
import asyncio
import json
import re
from types import SimpleNamespace


def output_report(inspected):
    output = inspected.get('output')
    return None if output is None else {**output, 'verified': inspected.get('output_verified')}


async def run_registered_call(companion, job):
    from companion import RUNTIME, SNAPSHOT
    state = SimpleNamespace(done=asyncio.Event(), closed=False)
    companion.bridge_state = state
    code, started = None, False
    last_output = None
    try:
        if SNAPSHOT.exists(): raise RuntimeError('PREVIOUS_AUDIO_RESTORE_REQUIRED')
        # Never take over an active BlackHole default: whatever plays into it
        # would be heard on the call. Idle defaults left by a headset change recover.
        proc = await asyncio.create_subprocess_exec(str(RUNTIME/'audio-routing'), 'status', stdout=asyncio.subprocess.PIPE)
        output, _ = await proc.communicate()
        if proc.returncode: raise RuntimeError('AUDIO_ROUTING_FAILED')
        defaults = json.loads(output)
        virtual = [d for d in defaults.values() if d['name'].startswith('BlackHole ')]
        if virtual:
            if any(d.get('running') is not False for d in virtual):
                raise RuntimeError('AUDIO_ROUTE_ALREADY_IN_USE')
            await companion.route('recover-idle')
        prepared = await companion.command('prepare')
        await companion.cloud('registered-prepare', {'call_id':job['id']})
        deadline = asyncio.get_running_loop().time() + 85
        while not state.done.is_set():
            status = await companion.cloud('registered-state', {'call_id':job['id']})
            if status.get('ended'): break
            if status.get('connected') and not started:
                result = await companion.command('start',job_id=job['id'],tab_id=prepared['tab_id'],duration_seconds=job['duration_seconds']+30)
                if not result.get('connected') or not result.get('microphone_verified'): raise RuntimeError('DOT_MICROPHONE_NOT_VERIFIED')
                last_output = output_report(result)
                started = True
                deadline = asyncio.get_running_loop().time()+job['duration_seconds']
                companion.report('connected')
            if started:
                inspected = await companion.command('inspect_owned',job_id=job['id'])
                last_output = output_report(inspected) or last_output
                # A Dot player that refused BlackHole 2ch would play on the speakers.
                if (last_output or {}).get('errors'): raise RuntimeError('DOT_OUTPUT_NOT_ROUTED')
                if not inspected.get('active') or not inspected.get('microphone_verified'): break
            if asyncio.get_running_loop().time()>deadline: raise RuntimeError('REGISTERED_CALL_TIMEOUT')
            try:
                await asyncio.wait_for(state.done.wait(), 1)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        code = 'CALL_CANCELED'
    except Exception as error:
        code = str(error) if re.fullmatch('[A-Z_]{1,60}',str(error)) else 'REGISTERED_CALL_FAILED'
    finally:
        state.closed = True
        try:
            for _ in range(15):
                result = await companion.cloud('registered-finish',{'call_id':job['id']})
                if result.get('ended'): break
                await asyncio.sleep(.5)
            else: raise RuntimeError('BROWSER_AUDIO_RELEASE_UNCONFIRMED')
        except Exception:
            companion.cleanup_failed = True
            code = 'REGISTERED_HANGUP_UNCONFIRMED'
        # How Dot's playback was routed: counts and error names only, no audio.
        print(json.dumps({'event':'dot_output_routing','report':last_output}),flush=True)
        try:
            result = await companion.command('end',job_id=job['id'])
            if not result.get('ended'): raise RuntimeError('DOT_HANGUP_NOT_CONFIRMED')
            # A snapshot can only exist from an older version that switched devices.
            if SNAPSHOT.exists(): await companion.route('restore')
        except Exception:
            companion.cleanup_failed = True
            code = 'REGISTERED_AUDIO_RESTORE_REQUIRED'
        companion.report('failed' if code or companion.cleanup_failed else 'completed',code)
        companion.bridge_state = None
        companion.current = None
