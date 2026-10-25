"""Install the local MCP tunnel. Does not change the hosted fallback or Twilio routing."""
import argparse,json,os,plistlib,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if (Path.home()/'Library/LaunchAgents/dev.dot-phone-call.app.plist').exists():
    raise SystemExit('Dot Phone.app manages these services. Use its Restart services menu, or restore the separate services with install-background-app.py --restore first.')
p=argparse.ArgumentParser();p.add_argument('--tunnel-id',required=True);p.add_argument('--install',action='store_true');args=p.parse_args()
import re
if not re.fullmatch('tunnel_[a-f0-9]{32}',args.tunnel_id):raise SystemExit('Invalid tunnel ID')
config=json.loads((ROOT/'.secrets/local-backend.json').read_text())
header=ROOT/'.secrets/local-mcp-authorization.txt'
header.write_text('Bearer '+config['mcp_token']);header.chmod(0o600)
command=[str(ROOT/'.runtime/tunnel-client'),'run','--control-plane.tunnel-id',args.tunnel_id,'--control-plane.api-key','file:'+str(ROOT/'.secrets/tunnel-runtime-key.txt'),'--mcp.server-url','http://127.0.0.1:8894/mcp','--mcp.extra-headers','Authorization: file:'+str(header),'--mcp.discovery-extra-headers','Authorization: file:'+str(header),'--mcp.startup-wait-timeout','30s','--health.listen-addr','127.0.0.1:8896','--health.url-file',str(ROOT/'.runtime/mcp-health.url'),'--log.format','json','--log.level','warn']
subprocess.run([command[0],'doctor',*command[2:]],check=True)
label='dev.dot-phone-call.mcp-tunnel';log=ROOT/'.runtime/mcp-tunnel.log'
if not log.exists():log.touch(mode=0o600)
manifest={'Label':label,'ProgramArguments':command,'WorkingDirectory':str(ROOT),'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':10,'StandardOutPath':str(log),'StandardErrorPath':str(log)}
path=ROOT/'.runtime/mcp-tunnel.plist';path.write_bytes(plistlib.dumps(manifest));path.chmod(0o600)
if args.install:
    target=Path.home()/'Library/LaunchAgents'/(label+'.plist')
    if target.exists() and plistlib.loads(target.read_bytes()).get('ProgramArguments')!=command:raise SystemExit('Existing tunnel service differs. Nothing replaced.')
    target.write_bytes(path.read_bytes());target.chmod(0o600)
    domain=f'gui/{os.getuid()}'
    if subprocess.run(['launchctl','print',domain+'/'+label],capture_output=True).returncode:
        subprocess.run(['launchctl','bootstrap',domain,str(target)],check=True)
print(json.dumps({'tunnel_id':args.tunnel_id,'installed':args.install,'phone_dialed':False}))
