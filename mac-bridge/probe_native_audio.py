"""Supervised native Dot audio test. No browser control, cloud connection or dialer.

Waits for explicit local start and hangup confirmations. If interrupted before
hangup confirmation, leaves virtual routing and its recovery snapshot in place.
"""
import asyncio
import fcntl
import json
import time
import uuid
from pathlib import Path

from audio import AudioBridge
from browser_audio_check import verify_browser_audio, command

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.runtime'
SNAPSHOT = RUNTIME / 'mac-audio-original.json'
CONTROL = RUNTIME / 'native-audio-probe-control.json'
RESULT = RUNTIME / 'native-audio-probe-result.json'


def save(result, stage):
    result.update(stage=stage, updated_at=time.time())
    RESULT.write_text(json.dumps(result, indent=2) + '\n')
    RESULT.chmod(0o600)
    print(json.dumps({'stage':stage, 'test_id':result['test_id']}), flush=True)


async def wait_for_action(test_id, action, timeout=600):
    async with asyncio.timeout(timeout):
        while True:
            if CONTROL.exists():
                try:
                    data = json.loads(CONTROL.read_text())
                except (ValueError, OSError):
                    data = {}
                if data.get('test_id') == test_id and data.get('action') == action:
                    CONTROL.unlink()
                    return
            await asyncio.sleep(.25)


async def main():
    lock = (RUNTIME / 'mac-companion.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if SNAPSHOT.exists():
        raise RuntimeError('PREVIOUS_AUDIO_RESTORE_REQUIRED')
    result = {'test_id':str(uuid.uuid4()), 'client':'ChatGPT macOS app',
              'dialed':False, 'verified':False, 'checks':[], 'audio_restored':False}
    audio = None
    try:
        await command(RUNTIME / 'audio-routing', 'enable', SNAPSHOT)
        audio = AudioBridge()
        audio.start()
        save(result, 'awaiting_native_call')
        await wait_for_action(result['test_id'], 'start')
        save(result, 'checking_audio')
        for _ in range(2):
            details = {}
            result['checks'].append(details)
            proof = await verify_browser_audio(audio, diagnostics=details)
            details.update(proof)
            save(result, 'checking_audio')
        result['verified'] = True
    except Exception as error:
        result['error'] = str(error) or type(error).__name__
        if getattr(error, 'diagnostics', None):
            result['error_diagnostics'] = error.diagnostics
    finally:
        if audio is not None:
            audio.close()
        save(result, 'awaiting_native_hangup' if SNAPSHOT.exists() else 'finished')
    if SNAPSHOT.exists():
        try:
            await wait_for_action(result['test_id'], 'hangup_confirmed')
            await command(RUNTIME / 'audio-routing', 'restore', SNAPSHOT)
            result['audio_restored'] = not SNAPSHOT.exists()
            save(result, 'finished')
        except BaseException:
            save(result, 'manual_audio_restore_required')
            raise


if __name__ == '__main__':
    asyncio.run(main())
