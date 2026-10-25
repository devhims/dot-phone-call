"""Replace only this project's two login services with Dot Phone.app; reversible."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import socket
import subprocess
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
RUNTIME=ROOT/'.runtime'
AGENTS=Path.home()/'Library/LaunchAgents'
BACKUP=RUNTIME/'login-items-before-app'
LABEL='dev.dot-phone-call.app'
LEGACY=['dev.dot-phone-call.backend','dev.dot-phone-call.mcp-tunnel']
DOMAIN=f'gui/{os.getuid()}'
APP=ROOT/'.runtime/Dot Phone.app/Contents/MacOS/DotPhone'

def loaded(label):
    return subprocess.run(['launchctl','print',DOMAIN+'/'+label],capture_output=True).returncode==0

def launch(label):
    if not loaded(label):subprocess.run(['launchctl','bootstrap',DOMAIN,str(AGENTS/(label+'.plist'))],check=True)

def bootout(label):
    if loaded(label):subprocess.run(['launchctl','bootout',DOMAIN+'/'+label],check=True)

def listener(port):
    with socket.socket() as sock:
        sock.settimeout(.3)
        return sock.connect_ex(('127.0.0.1',port))==0

def assert_idle():
    if (RUNTIME/'mac-audio-original.json').exists():raise RuntimeError('Audio is active; finish the call first')
    if not listener(8894):return
    token=json.loads((ROOT/'.secrets/local-backend.json').read_text())['mcp_token']
    request=urllib.request.Request('http://127.0.0.1:8894/mcp',data=json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'get_phone_service_status','arguments':{}}}).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json','Accept':'application/json, text/event-stream'})
    with urllib.request.urlopen(request,timeout=10) as response: result=json.load(response)['result']
    status=result.get('structuredContent') or json.loads(result['content'][0]['text'])
    if status.get('active_call') or status.get('backend')!='local':raise RuntimeError('Local call is active or ownership could not be verified')

def wait_ports_closed():
    deadline=time.monotonic()+55
    while time.monotonic()<deadline:
        if not any(listener(p) for p in (8894,8896)):return
        time.sleep(.5)
    raise RuntimeError('Previous services did not stop; no replacement started')

def write(path,data):
    path.write_bytes(data);path.chmod(0o600)

def prepare():
    BACKUP.mkdir(mode=0o700,exist_ok=True)
    commands={}
    states={}
    for label in LEGACY:
        source=AGENTS/(label+'.plist');backup=BACKUP/source.name
        data=(source if source.exists() else backup).read_bytes()
        args=plistlib.loads(data)['ProgramArguments']
        if label.endswith('.backend'):
            if len(args)!=2 or args[1]!=str(ROOT/'src/local-main.mjs'):raise RuntimeError('Backend ownership mismatch')
            commands['backend']=args
        else:
            if args[0]!=str(ROOT/'.runtime/tunnel-client') or str(ROOT/'.secrets/tunnel-runtime-key.txt') not in ' '.join(args):raise RuntimeError('Tunnel ownership mismatch')
            commands['tunnel']=args
        if not backup.exists():write(backup,data)
        elif backup.read_bytes()!=data:raise RuntimeError('Legacy service differs from backup; refusing replacement')
        states[label]=loaded(label)
    statefile=BACKUP/'loaded.json'
    if not statefile.exists():write(statefile,json.dumps(states).encode())
    write(RUNTIME/'background-services.json',json.dumps(commands).encode())
    manifest={'Label':LABEL,'ProgramArguments':[str(APP)],'WorkingDirectory':str(ROOT),
      'AssociatedBundleIdentifiers':['dev.dot-phone-call.app'],'RunAtLoad':True,
      'KeepAlive':{'SuccessfulExit':False},'ThrottleInterval':10,'ExitTimeOut':50,
      'StandardOutPath':str(RUNTIME/'menu-bar-launch.log'),'StandardErrorPath':str(RUNTIME/'menu-bar-launch.log')}
    write(RUNTIME/'menu-bar-app.plist',plistlib.dumps(manifest))

def restore():
    assert_idle();bootout(LABEL)
    for label in LEGACY:bootout(label)
    wait_ports_closed()
    target=AGENTS/(LABEL+'.plist')
    if target.exists():
        if plistlib.loads(target.read_bytes())['ProgramArguments']!=[str(APP)]:raise RuntimeError('App ownership mismatch')
        target.rename(RUNTIME/'menu-bar-app.disabled.plist')
    states=json.loads((BACKUP/'loaded.json').read_text())
    for label in LEGACY:
        target=AGENTS/(label+'.plist');data=(BACKUP/target.name).read_bytes()
        if target.exists() and target.read_bytes()!=data:raise RuntimeError('Existing legacy service differs')
        write(target,data)
        if states.get(label):launch(label)

def install():
    if not APP.exists():raise RuntimeError('Build Dot Phone.app first')
    assert_idle();prepare()
    target=AGENTS/(LABEL+'.plist')
    if target.exists() and plistlib.loads(target.read_bytes())['ProgramArguments']!=[str(APP)]:raise RuntimeError('App ownership mismatch')
    try:
        bootout(LABEL)
        for label in LEGACY:bootout(label)
        wait_ports_closed()
        for label in LEGACY:
            old=AGENTS/(label+'.plist')
            if old.exists():old.unlink()  # Exact recoverable copy is verified in BACKUP.
        write(target,(RUNTIME/'menu-bar-app.plist').read_bytes());launch(LABEL)
        deadline=time.monotonic()+45
        while time.monotonic()<deadline:
            try:
                state=json.loads((RUNTIME/'app-status.json').read_text())
                if time.time()-state['updated_at']<5 and state['backend_ready'] and state['tunnel_ready']:
                    print(json.dumps({'installed':True,'status':state['status'],'phone_dialed':False}));return
            except (OSError,ValueError,KeyError):pass
            time.sleep(.5)
        raise RuntimeError('App did not become ready')
    except Exception:
        # Restore only owned, backed-up service files. Never modify the old project.
        restore()
        raise

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--install',action='store_true');parser.add_argument('--restore',action='store_true');args=parser.parse_args()
    if args.install and args.restore:parser.error('Choose install or restore')
    if args.restore:restore();print('Restored separate login services; no call placed.')
    elif args.install:install()
    else:prepare();print('Prepared app configuration and rollback backup. Login services unchanged.')
