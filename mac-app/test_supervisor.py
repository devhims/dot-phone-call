"""Real subprocess lifecycle tests with fake local services, never Twilio."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

SOURCE=Path(__file__).with_name('supervisor.py')
spec=importlib.util.spec_from_file_location('supervisor',SOURCE)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
FAKE='''
from http.server import BaseHTTPRequestHandler,HTTPServer
import json,sys
from pathlib import Path
root=Path(sys.argv[2])
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_GET(self):self.send_response(200);self.end_headers();self.wfile.write(b'{}')
 def do_POST(self):
  self.rfile.read(int(self.headers.get('Content-Length',0)))
  if self.path=='/mcp':
   state=json.loads((root/'mock.json').read_text());state['active_call']=None;(root/'mock.json').write_text(json.dumps(state))
  data=(root/'mock.json').read_bytes()
  self.send_response(200);self.end_headers();self.wfile.write(data)
HTTPServer(('127.0.0.1',int(sys.argv[1])),Handler).serve_forever()
'''
def port():
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));return sock.getsockname()[1]

def alive(pid):
    try:os.kill(pid,0);return True
    except ProcessLookupError:return False

class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        for name in ('.runtime','.secrets'):(self.root/name).mkdir()
        (self.root/'.secrets/mac-companion.json').write_text(json.dumps({'cloud_token':'test'}))
        (self.root/'.secrets/local-backend.json').write_text(json.dumps({'mcp_token':'test'}))
        self.backend_port,self.tunnel_port=port(),port()
        fake=self.root/'fake.py';fake.write_text(FAKE)
        (self.root/'.runtime/background-services.json').write_text(json.dumps({name:[sys.executable,str(fake),str(p),str(self.root)] for name,p in [('backend',self.backend_port),('tunnel',self.tunnel_port)]}))
        self.mock={'mac_online':True,'active_call':None,'registered_client':{'registered':True,'phone_dialing_enabled':True}}
        self.write_mock()
        self.process=None
    def write_mock(self):
        p=self.root/'mock.tmp';p.write_text(json.dumps(self.mock));p.replace(self.root/'mock.json')
    def start(self):
        code=f"import importlib.util;from pathlib import Path;s=importlib.util.spec_from_file_location('supervisor',{str(SOURCE)!r});m=importlib.util.module_from_spec(s);s.loader.exec_module(m);m.Supervisor(Path({str(self.root)!r}),{self.backend_port},{self.tunnel_port}).run()"
        self.process=subprocess.Popen([sys.executable,'-c',code],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        return self.wait(lambda s:s['status']=='Ready for calls')
    def state(self):
        try:return json.loads((self.root/'.runtime/app-status.json').read_text())
        except (OSError,ValueError):return {}
    def wait(self,predicate,timeout=12):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            value=self.state()
            if value and predicate(value):return value
            if self.process and self.process.poll() is not None:self.fail('Supervisor exited: '+self.process.stderr.read().decode())
            time.sleep(.1)
        self.fail('State did not converge: '+str(self.state()))
    def send(self,action,**extra):
        self.process.stdin.write((json.dumps({'action':action,**extra})+'\n').encode());self.process.stdin.flush()
    def tearDown(self):
        if self.process:
            if self.process.poll() is None:self.process.terminate()
            self.process.wait(timeout=15)
            self.process.stdin.close();self.process.stderr.close()
        self.temp.cleanup()
    def test_start_restart_and_quit_release_owned_services(self):
        first=self.start();self.send('restart')
        second=self.wait(lambda s:s['status']=='Ready for calls' and s['backend_pid']!=first['backend_pid'])
        self.assertFalse(alive(first['backend_pid']));self.assertFalse(alive(first['tunnel_pid']))
        self.send('quit');self.process.wait(timeout=12)
        self.assertEqual(self.process.returncode,0)
        self.assertFalse(alive(second['backend_pid']));self.assertFalse(alive(second['tunnel_pid']))
    def test_crashed_backend_recovers_with_new_tunnel(self):
        first=self.start();os.kill(first['backend_pid'],signal.SIGKILL)
        second=self.wait(lambda s:s['status']=='Ready for calls' and s['backend_pid']!=first['backend_pid'])
        self.assertNotEqual(first['tunnel_pid'],second['tunnel_pid'])
    def test_active_call_blocks_restart_then_explicit_quit_drains(self):
        first=self.start();self.mock['active_call']={'id':'test'};self.write_mock()
        self.wait(lambda s:s['active_call']);self.send('restart')
        blocked=self.wait(lambda s:bool(s['message']))
        self.assertEqual(blocked['backend_pid'],first['backend_pid'])
        self.send('quit',end_call=True);self.process.wait(timeout=12)
        self.assertIsNone(json.loads((self.root/'mock.json').read_text())['active_call'])
    def test_closed_app_control_pipe_does_not_orphan_services(self):
        first=self.start();self.process.stdin.close();self.process.wait(timeout=12)
        self.assertFalse(alive(first['backend_pid']));self.assertFalse(alive(first['tunnel_pid']))
    def test_second_supervisor_cannot_own_same_runtime(self):
        first=self.start();other=module.Supervisor(self.root,self.backend_port,self.tunnel_port)
        try:
            with self.assertRaises(BlockingIOError):other.acquire()
        finally:other.lock.close()
        self.assertTrue(alive(first['backend_pid']))
    def test_occupied_port_is_not_adopted_or_killed(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',self.backend_port));sock.listen()
            other=module.Supervisor(self.root,self.backend_port,self.tunnel_port)
            try:
                with self.assertRaises(RuntimeError):other.acquire()
            finally:other.lock.close()
            self.assertIsNone(other.backend.process)

if __name__=='__main__':unittest.main()
