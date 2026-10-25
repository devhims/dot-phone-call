"""Create independent local configuration. Does not enable calling or install services."""
import json
import secrets
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
private=ROOT/'.secrets';private.mkdir(exist_ok=True,mode=0o700)
path=private/'local-backend.json'
if not path.exists():
    path.write_text(json.dumps({'mcp_token':secrets.token_hex(32),'calling_enabled':False,'allow_phone_dialing':False},indent=2)+'\n');path.chmod(0o600)
print('Local configuration ready. Calling and phone dialing remain disabled.')
