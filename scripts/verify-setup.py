"""Check local readiness, optionally run the hidden audio diagnostic. Never dial."""
import argparse
import json
from pathlib import Path
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]


def service_status(root=ROOT):
    paired = json.loads((root / '.secrets/mac-companion.json').read_text())
    request = urllib.request.Request('http://127.0.0.1:8894/companion/service-status', data=b'{}',
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + paired['cloud_token']})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def ready(status):
    browser = status.get('registered_client', {})
    return (status.get('mac_online') and browser.get('registered') and
        browser.get('browser_context') == 'offscreen' and browser.get('microphone_ready') and
        not browser.get('recovery_required'))


def check_audio(root=ROOT, status_reader=service_status, timeout=90):
    config = json.loads((root / '.secrets/local-backend.json').read_text())
    if config.get('calling_enabled') or config.get('allow_phone_dialing'):
        raise RuntimeError('Disable both calling flags and restart services before the audio diagnostic')
    status = status_reader(root)
    if not ready(status) or status.get('active_call') or status['registered_client'].get('phone_dialing_enabled'):
        raise RuntimeError('The hidden browser must be ready, idle, and have dialing disabled')
    request = root / '.runtime/hidden-audio-test-request.json'
    if request.exists():
        previous = json.loads(request.read_text())
        if previous.get('expires_at', 0) > time.time() * 1000:
            raise RuntimeError('Another audio diagnostic request is pending')
    ident = str(uuid.uuid4())
    started = time.time()
    request.write_text(json.dumps({'id': ident, 'expires_at': int((started + timeout + 30) * 1000)}))
    request.chmod(0o600)
    result_file = root / '.runtime/native-helper-result.json'
    try:
        while time.time() - started < timeout:
            if result_file.exists() and result_file.stat().st_mtime >= started:
                try:
                    result = json.loads(result_file.read_text())
                except json.JSONDecodeError:
                    time.sleep(.2)
                    continue
                if result.get('mode') == 'hidden-audio-test':
                    if (result.get('verified') and result.get('cleanup_ok') and result.get('audio_restored')
                        and result.get('dialed') is False and result.get('browser', {}).get('codec') == 'audio/opus'
                        and result.get('active_frames', 0) >= 48000):
                        return {key: result[key] for key in ['verified', 'dialed', 'cleanup_ok', 'peak_hz', 'received_frames']}
                    raise RuntimeError(result.get('code', 'Audio verification or cleanup failed'))
            time.sleep(.5)
        raise RuntimeError('Audio diagnostic timed out; check .runtime/native-helper.log and extension Errors')
    finally:
        if request.exists() and json.loads(request.read_text()).get('id') == ident:
            request.unlink()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio', action='store_true')
    args = parser.parse_args()
    try:
        status = service_status()
        if not ready(status):
            raise RuntimeError('Not ready: open Chrome, load the extension, grant its microphone permission, and check the Dot Phone menu')
        print('Hidden browser registered; microphone permission granted; local helper available.')
        if args.audio:
            print(json.dumps(check_audio()))
        print('No phone call was placed. Verify the ChatGPT plugin separately with get_phone_service_status.')
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        # Avoid raw network exception bodies or credential-bearing request data.
        message = str(error) if isinstance(error, RuntimeError) else 'Local setup unavailable; check configuration files and the Dot Phone menu/logs'
        raise SystemExit(message)
