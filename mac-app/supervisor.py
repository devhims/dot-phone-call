"""Own the local backend and tunnel for the Dot Phone menu-bar app.

No dialing, credential provisioning, or browser automation occurs here.
Only processes created by this supervisor are signalled.
"""
import fcntl
import json
import os
from pathlib import Path
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def write_private(path, value):
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(value, handle)
    os.replace(temporary, path)


class Child:
    def __init__(self, name, command, root):
        self.name, self.command, self.root = name, command, root
        self.process = None
        self.started = 0
        self.failures = 0
        self.retry_at = 0

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None

    def start(self):
        if self.running:
            return
        path = self.root / '.runtime' / ('app-' + self.name + '.log')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'ab', buffering=0) as log:
            self.process = subprocess.Popen(self.command, cwd=self.root, stdin=subprocess.DEVNULL,
                                            stdout=log, stderr=log)
        self.started = time.monotonic()

    def stop(self, timeout=30):
        if not self.running:
            self.process = None
            return
        # Signal only the direct child represented by this live Popen object.
        self.process.terminate()
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process = None

    def crashed(self):
        if self.process is None or self.running:
            return False
        self.failures = 1 if time.monotonic() - self.started > 60 else self.failures + 1
        self.retry_at = time.monotonic() + min(30, 2 ** min(self.failures, 5))
        self.process = None
        return True


class Supervisor:
    def __init__(self, root=ROOT, backend_port=8894, tunnel_port=8896):
        self.root = root
        self.backend_port, self.tunnel_port = backend_port, tunnel_port
        self.runtime = root / '.runtime'
        config = json.loads((self.runtime / 'background-services.json').read_text())
        self.backend = Child('backend', config['backend'], root)
        self.tunnel = Child('tunnel', config['tunnel'], root)
        self.companion_token = json.loads((root / '.secrets/mac-companion.json').read_text())['cloud_token']
        self.mcp_token = json.loads((root / '.secrets/local-backend.json').read_text())['mcp_token']
        self.commands = queue.Queue()
        self.parent = os.getppid()
        self.quitting = False
        self.message = ''
        self.last_status = None
        self.next_probe = 0
        self.backend_healthy = False
        self.tunnel_healthy = False
        self.lock = None

    def acquire(self):
        self.lock = open(self.runtime / 'app-supervisor.lock', 'a')
        os.chmod(self.runtime / 'app-supervisor.lock', 0o600)
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Refuse to compete with legacy login services or unrelated listeners.
        for port in (self.backend_port, self.tunnel_port):
            with socket.socket() as sock:
                sock.settimeout(.3)
                if sock.connect_ex(('127.0.0.1', port)) == 0:
                    raise RuntimeError('Existing service uses port ' + str(port))

    def request(self, path, data, token):
        request = urllib.request.Request(f'http://127.0.0.1:{self.backend_port}' + path,
            data=json.dumps(data).encode(), headers={'Content-Type':'application/json',
            'Accept':'application/json, text/event-stream', 'Authorization':'Bearer ' + token})
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.load(response)

    def health(self):
        try:
            self.last_status = self.request('/companion/service-status', {}, self.companion_token)
            self.backend_healthy = True
        except Exception:
            self.backend_healthy = False
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{self.tunnel_port}/readyz', timeout=1) as response:
                self.tunnel_healthy = response.status == 200
        except Exception:
            self.tunnel_healthy = False

    def busy(self):
        return bool((self.last_status or {}).get('active_call')) or (self.runtime / 'mac-audio-original.json').exists()

    def publish(self, phase=None):
        status = self.last_status or {}
        if phase is None:
            if not self.backend_healthy:
                phase = 'Starting local service' if self.backend.running else 'Local service stopped; retrying'
            elif not self.tunnel_healthy:
                phase = 'Connecting to ChatGPT'
            elif self.busy():
                phase = 'Call in progress'
            elif status.get('registered_client', {}).get('recovery_required'):
                phase = 'Finishing call recovery'
            elif not status.get('mac_online') or not status.get('registered_client', {}).get('registered'):
                phase = 'Open Chrome to connect'
            elif not status.get('registered_client', {}).get('phone_dialing_enabled'):
                phase = 'Outgoing calls disabled'
            else:
                phase = 'Ready for calls'
        write_private(self.runtime / 'app-status.json', {
            'status':phase, 'message':self.message, 'updated_at':time.time(),
            'supervisor_pid':os.getpid(), 'backend_pid':self.backend.process.pid if self.backend.running else None,
            'tunnel_pid':self.tunnel.process.pid if self.tunnel.running else None,
            'backend_ready':self.backend_healthy, 'tunnel_ready':self.tunnel_healthy,
            'active_call':self.busy(), 'chrome_connected':bool(status.get('registered_client', {}).get('registered'))})

    def drain(self):
        self.request('/mcp', {'jsonrpc':'2.0','id':1,'method':'tools/call',
            'params':{'name':'end_owner_call','arguments':{}}}, self.mcp_token)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            self.health()
            if self.backend_healthy and not self.busy():
                return
            time.sleep(.5)
        raise RuntimeError('Call cleanup has not finished; services remain running')

    def handle(self, command):
        action = command.get('action')
        if action not in ('restart', 'quit'):
            return
        self.health()
        if self.busy():
            if command.get('end_call') is not True:
                self.message = 'End the current call before restarting or quitting.'
                return
            try:
                self.publish('Ending call and restoring audio')
                self.drain()
            except Exception:
                self.message = 'Call cleanup is not confirmed. Services remain running.'
                return
        self.message = ''
        self.publish('Stopping services')
        self.tunnel.stop()
        self.backend.stop()
        self.last_status = None
        self.backend_healthy = self.tunnel_healthy = False
        if action == 'quit':
            self.quitting = True
        else:
            self.backend.retry_at = self.tunnel.retry_at = 0
            self.next_probe = 0

    def read_commands(self):
        try:
            for line in sys.stdin:
                if len(line) > 2048:
                    continue
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        self.commands.put(value)
                except ValueError:
                    pass
        finally:
            # App exited or crashed. Never leave its services orphaned.
            self.quitting = True

    def run(self):
        # The app can reap this dedicated group if the supervisor crashes.
        if os.getpgrp() != os.getpid():
            os.setsid()
        self.acquire()
        threading.Thread(target=self.read_commands, daemon=True).start()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: setattr(self, 'quitting', True))
        try:
            while not self.quitting and os.getppid() == self.parent:
                try:
                    self.handle(self.commands.get_nowait())
                except queue.Empty:
                    pass
                if self.quitting:
                    break
                if self.backend.crashed():
                    self.tunnel.stop()
                    self.backend_healthy = self.tunnel_healthy = False
                self.tunnel.crashed()
                now = time.monotonic()
                for child, allowed in ((self.backend, True), (self.tunnel, self.backend_healthy)):
                    if allowed and not child.running and now >= child.retry_at:
                        try:
                            child.start()
                        except OSError:
                            child.retry_at = now + 10
                            self.message = 'Could not start ' + child.name + '. Check the service log.'
                if now >= self.next_probe:
                    self.health()
                    self.next_probe = now + 2
                self.publish()
                time.sleep(.2)
        finally:
            # SIGTERM, logout, or app crash still attempts owned-call cleanup.
            if self.busy():
                try:
                    self.drain()
                except Exception:
                    pass
            self.tunnel.stop()
            self.backend.stop()
            self.last_status = None
            self.backend_healthy = self.tunnel_healthy = False
            self.publish('Stopped')
            if self.lock:
                self.lock.close()


if __name__ == '__main__':
    try:
        Supervisor().run()
    except BlockingIOError:
        sys.exit(0)  # A second app instance must not start duplicate services.
    except Exception as error:
        # Do not print configuration or request headers in the app log.
        print('Dot Phone supervisor stopped: ' + type(error).__name__, file=sys.stderr)
        sys.exit(1)
