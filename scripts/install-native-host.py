"""Register the fixed Chrome native host. Keep the login agent until validation."""
import json
from pathlib import Path
root=Path(__file__).resolve().parents[1]
config=json.loads((root/'.secrets/mac-companion.json').read_text())
binary=root/'.runtime/Dot Phone Call Helper.app/Contents/MacOS/DotPhone'
if not binary.is_file(): raise SystemExit('Build the app first.')
path=Path.home()/'Library/Application Support/Google/Chrome/NativeMessagingHosts/dev.dot_phone_call.helper.json'
manifest={'name':'dev.dot_phone_call.helper','description':'On-demand Dot Phone audio helper',
    'path':str(binary),'type':'stdio','allowed_origins':['chrome-extension://'+config['extension_id']+'/']}
if path.exists() and json.loads(path.read_text()).get('path')!=str(binary):
    raise SystemExit('An unrelated host uses this name; installation stopped.')
path.parent.mkdir(parents=True,exist_ok=True)
path.write_text(json.dumps(manifest,indent=2)+'\n');path.chmod(0o600)
print('Registered native helper:',path)
