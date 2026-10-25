"""Create private per-developer configuration without installing services or dialing."""
import argparse
import getpass
import hashlib
import json
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def private_write(path, text):
    with open(path, 'x', opener=lambda name, flags: __import__('os').open(name, flags, 0o600)) as handle:
        handle.write(text)


def checked(value, pattern, label):
    if not re.fullmatch(pattern, value):
        raise ValueError('Invalid ' + label)
    return value


def configure(root=ROOT, interactive=False, read=input, secret=getpass.getpass):
    folder = root / '.secrets'
    folder.mkdir(exist_ok=True, mode=0o700)
    folder.chmod(0o700)
    pending = {}
    backend = folder / 'local-backend.json'
    if not backend.exists():
        pending[backend] = json.dumps({'mcp_token': secrets.token_hex(32),
            'calling_enabled': False, 'allow_phone_dialing': False}, indent=2) + '\n'
    if interactive:
        twilio = folder / 'inbound-worker-secrets.json'
        if not twilio.exists():
            account = checked(read('Twilio account SID (AC...): ').strip(), r'AC[0-9a-f]{32}', 'Twilio account SID')
            auth = checked(secret('Twilio account auth token (hidden): ').strip(), r'[^\s]{16,}', 'Twilio auth token')
            key = checked(read('Twilio Standard API key SID (SK...): ').strip(), r'SK[0-9a-f]{32}', 'Twilio API key SID')
            key_secret = checked(secret('Twilio API key secret (hidden): ').strip(), r'[^\s]{16,}', 'Twilio API key secret')
            owner = checked(read('Your phone number (+countrycode...): ').strip(), r'\+[1-9][0-9]{7,14}', 'owner phone number')
            number = checked(read('Your Twilio voice number (+countrycode...): ').strip(), r'\+[1-9][0-9]{7,14}', 'Twilio phone number')
            if owner == number:
                raise ValueError('Owner and Twilio numbers must differ')
            pin = checked(secret('Choose a six-digit incoming-call PIN (hidden): ').strip(), r'[0-9]{6}', 'PIN')
            if secret('Repeat the PIN (hidden): ').strip() != pin:
                raise ValueError('PINs did not match')
            salt = secrets.token_hex(32)
            pending[twilio] = json.dumps({'TWILIO_ACCOUNT_SID': account, 'TWILIO_AUTH_TOKEN': auth,
                'TWILIO_API_KEY_SID': key, 'TWILIO_API_KEY_SECRET': key_secret,
                'INBOUND_OWNER_NUMBER': owner, 'INBOUND_TWILIO_NUMBER': number,
                'INBOUND_PIN_SALT': salt, 'INBOUND_PIN_HASH': hashlib.sha256((salt + ':' + pin).encode()).hexdigest(),
                'CLIENT_IDENTITY': 'dot_phone_' + secrets.token_hex(12)}, indent=2) + '\n'
        tunnel = folder / 'tunnel.json'
        if not tunnel.exists():
            ident = checked(read('OpenAI tunnel ID (tunnel_...): ').strip(), r'tunnel_[0-9a-f]{32}', 'tunnel ID')
            pending[tunnel] = json.dumps({'tunnel_id': ident}, indent=2) + '\n'
        runtime_key = folder / 'tunnel-runtime-key.txt'
        if not runtime_key.exists():
            value = checked(secret('OpenAI runtime API key with Tunnels Read + Use (hidden): ').strip(), r'[^\s]{20,}', 'runtime API key')
            pending[runtime_key] = value + '\n'
    # Validate all input before persisting any credentials. Existing files stay intact.
    for path, text in pending.items():
        private_write(path, text)
    return [path.name for path in pending]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interactive', action='store_true', help='collect Twilio and tunnel settings with hidden secret prompts')
    args = parser.parse_args()
    if args.interactive and not sys.stdin.isatty():
        raise SystemExit('Run --interactive in a terminal so credential entry stays hidden')
    try:
        created = configure(interactive=args.interactive)
    except (ValueError, EOFError, KeyboardInterrupt) as error:
        raise SystemExit(str(error) or 'Setup canceled')
    print('Created: ' + (', '.join(created) or 'nothing; existing configuration preserved'))
    print('No calling flags were enabled, no services installed, and no phone calls placed.')
