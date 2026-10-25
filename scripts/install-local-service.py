"""Install only dot-phone-call's local backend login service; never modify other agents."""
import os
import plistlib
import subprocess
from pathlib import Path
import shutil
ROOT=Path(__file__).resolve().parents[1]
if (Path.home()/'Library/LaunchAgents/dev.dot-phone-call.app.plist').exists():
    raise SystemExit('Dot Phone.app manages these services. Use its Restart services menu, or restore the separate services with install-background-app.py --restore first.')
LABEL='dev.dot-phone-call.backend'
node=shutil.which('node')
if not node: raise SystemExit('Node 24+ must be on PATH.')
if not (ROOT/'.secrets/local-backend.json').exists(): raise SystemExit('Configure the local backend first.')
folder=Path.home()/'Library/LaunchAgents';folder.mkdir(parents=True,exist_ok=True)
path=folder/(LABEL+'.plist')
program=[node,str(ROOT/'src/local-main.mjs')]
if path.exists() and plistlib.loads(path.read_bytes()).get('ProgramArguments')!=program:
    raise SystemExit('Another installation owns this service. Nothing changed.')
(ROOT/'.runtime').mkdir(exist_ok=True,mode=0o700)
data={'Label':LABEL,'ProgramArguments':program,'WorkingDirectory':str(ROOT),'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':10,'StandardOutPath':str(ROOT/'.runtime/backend.log'),'StandardErrorPath':str(ROOT/'.runtime/backend-errors.log')}
path.write_bytes(plistlib.dumps(data));path.chmod(0o600)
subprocess.run(['launchctl','bootout',f'gui/{os.getuid()}',str(path)],capture_output=True)
subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(path)],check=True)
print('Installed',LABEL)
