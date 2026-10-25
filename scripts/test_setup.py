"""Fresh-install checks use temporary directories and never install host services."""
import hashlib
import importlib.util
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent

def load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

configure = load('configure_local', 'configure-local.py')
installer = load('install_background_app', 'install-background-app.py')
verify = load('verify_setup', 'verify-setup.py')
from service_config import fresh_commands

class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inputs = ['AC'+'1'*32, 'SK'+'2'*32, '+15555550101', '+15555550102', 'tunnel_'+'3'*32]
        self.secrets = ['a'*32, 'b'*32, '123456', '123456', 'sk-'+'c'*40]

    def configured(self):
        reads, secrets = iter(self.inputs), iter(self.secrets)
        configure.configure(self.root, True, lambda _: next(reads), lambda _: next(secrets))
        (self.root/'.secrets/mac-companion.json').write_text('{}')
        (self.root/'.runtime').mkdir()
        binary = self.root/'.runtime/tunnel-client';binary.write_text('test binary, never executed');binary.chmod(0o700)

    def test_fresh_configuration_is_private_independent_and_disabled(self):
        self.configured()
        folder = self.root/'.secrets'
        config = json.loads((folder/'local-backend.json').read_text())
        twilio = json.loads((folder/'inbound-worker-secrets.json').read_text())
        self.assertFalse(config['calling_enabled']);self.assertFalse(config['allow_phone_dialing'])
        self.assertEqual(twilio['INBOUND_PIN_HASH'], hashlib.sha256((twilio['INBOUND_PIN_SALT']+':123456').encode()).hexdigest())
        self.assertNotIn('123456', (folder/'inbound-worker-secrets.json').read_text())
        self.assertEqual((folder/'local-backend.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        self.assertNotIn(self.secrets[-1], (folder/'tunnel.json').read_text())

    def test_repeat_setup_preserves_existing_credentials_and_calling_flags(self):
        self.configured()
        path=self.root/'.secrets/local-backend.json';value=json.loads(path.read_text());value['calling_enabled']=True;path.write_text(json.dumps(value))
        before={p.name:p.read_bytes() for p in path.parent.iterdir()}
        configure.configure(self.root, True, lambda _: self.fail('unexpected prompt'), lambda _: self.fail('unexpected secret prompt'))
        self.assertEqual(before,{p.name:p.read_bytes() for p in path.parent.iterdir()})

    def test_invalid_pin_does_not_partially_write_credentials(self):
        reads,secrets=iter(self.inputs),iter(['a'*32,'b'*32,'123456','654321'])
        with self.assertRaisesRegex(ValueError,'PINs'):
            configure.configure(self.root,True,lambda _:next(reads),lambda _:next(secrets))
        self.assertEqual(list((self.root/'.secrets').iterdir()),[])

    def test_commands_need_no_legacy_installation_or_literal_secrets(self):
        self.configured();commands=fresh_commands(self.root,node='/usr/local/bin/node')
        self.assertIn(str(self.root/'src/local-main.mjs'),commands['backend'])
        text=' '.join(commands['tunnel'])
        self.assertIn('file:'+str(self.root/'.secrets/tunnel-runtime-key.txt'),text)
        for secret in self.secrets[:2]+self.secrets[4:]:self.assertNotIn(secret,text)
        self.assertTrue((self.root/'.secrets/local-mcp-authorization.txt').read_text().startswith('Bearer '))
        self.assertEqual(commands,fresh_commands(self.root,node='/usr/local/bin/node'))

    def patch_installer(self):
        runtime=self.root/'.runtime';agents=self.root/'fake-login-items';agents.mkdir()
        for name,value in {'ROOT':self.root,'RUNTIME':runtime,'AGENTS':agents,'BACKUP':runtime/'login-items-before-app','APP':runtime/'Dot Phone.app/Contents/MacOS/DotPhone'}.items():
            p=patch.object(installer,name,value);p.start();self.addCleanup(p.stop)
        return agents

    def test_fresh_prepare_and_restore_do_not_require_old_login_items(self):
        self.configured();agents=self.patch_installer()
        with patch.object(installer,'loaded',return_value=False),patch('service_config.shutil.which',return_value='/usr/local/bin/node'):installer.prepare(fresh=True)
        manifest=plistlib.loads((self.root/'.runtime/menu-bar-app.plist').read_bytes())
        self.assertEqual(manifest['Label'],'dev.dot-phone-call.app')
        self.assertEqual(list(agents.iterdir()),[])
        target=agents/'dev.dot-phone-call.app.plist';target.write_bytes((self.root/'.runtime/menu-bar-app.plist').read_bytes())
        with patch.object(installer,'assert_idle'),patch.object(installer,'bootout'),patch.object(installer,'wait_ports_closed'),patch.object(installer,'launch') as launch:
            installer.restore();launch.assert_not_called()
        self.assertFalse(target.exists())

    def test_fresh_install_refuses_existing_legacy_service(self):
        self.configured();agents=self.patch_installer()
        (agents/'dev.dot-phone-call.backend.plist').write_text('unrelated')
        with patch.object(installer,'loaded',return_value=False),self.assertRaisesRegex(RuntimeError,'Existing separate'):
            installer.prepare(fresh=True)
        self.assertEqual((agents/'dev.dot-phone-call.backend.plist').read_text(),'unrelated')

    def test_fresh_install_writes_only_the_app_login_item(self):
        import time
        self.configured();agents=self.patch_installer()
        installer.APP.parent.mkdir(parents=True);installer.APP.write_text('test app')
        (self.root/'.runtime/app-status.json').write_text(json.dumps({'updated_at':time.time(),'backend_ready':True,'tunnel_ready':True,'status':'Outgoing calls disabled'}))
        with patch.object(installer,'loaded',return_value=False),patch('service_config.shutil.which',return_value='/usr/local/bin/node'),patch.object(installer,'assert_idle'),patch.object(installer,'bootout'),patch.object(installer,'wait_ports_closed'),patch.object(installer,'launch') as launch:
            installer.install(fresh=True)
        launch.assert_called_once_with('dev.dot-phone-call.app')
        self.assertEqual([p.name for p in agents.iterdir()],['dev.dot-phone-call.app.plist'])

    def test_failed_fresh_install_rolls_back_without_inventing_legacy_services(self):
        self.configured();agents=self.patch_installer()
        installer.APP.parent.mkdir(parents=True);installer.APP.write_text('test app')
        with patch.object(installer,'loaded',return_value=False),patch('service_config.shutil.which',return_value='/usr/local/bin/node'),patch.object(installer,'assert_idle'),patch.object(installer,'bootout'),patch.object(installer,'wait_ports_closed'),patch.object(installer,'launch',side_effect=RuntimeError('test launch failed')):
            with self.assertRaisesRegex(RuntimeError,'test launch failed'):installer.install(fresh=True)
        self.assertEqual(list(agents.iterdir()),[])
        self.assertTrue((self.root/'.secrets/local-backend.json').exists())

    def test_existing_service_migration_preserves_commands_and_backups(self):
        self.configured();agents=self.patch_installer()
        commands=fresh_commands(self.root,node='/usr/local/bin/node')
        originals={}
        for label,kind in zip(installer.LEGACY,['backend','tunnel']):
            data=plistlib.dumps({'Label':label,'ProgramArguments':commands[kind]})
            (agents/(label+'.plist')).write_bytes(data);originals[label]=data
        with patch.object(installer,'loaded',return_value=True):installer.prepare()
        self.assertEqual(json.loads((self.root/'.runtime/background-services.json').read_text()),commands)
        for label,data in originals.items():
            self.assertEqual((self.root/'.runtime/login-items-before-app'/(label+'.plist')).read_bytes(),data)
            self.assertEqual((agents/(label+'.plist')).read_bytes(),data)

    def test_audio_check_collects_only_a_new_verified_no_phone_result(self):
        self.configured()
        status={'mac_online':True,'active_call':None,'registered_client':{'registered':True,'microphone_ready':True,'browser_context':'offscreen','phone_dialing_enabled':False}}
        def complete(_):
            import os,time
            path=self.root/'.runtime/native-helper-result.json'
            path.write_text(json.dumps({'mode':'hidden-audio-test','verified':True,'cleanup_ok':True,'audio_restored':True,'dialed':False,'browser':{'codec':'audio/opus'},'active_frames':100000,'peak_hz':997.0,'received_frames':120000}))
            os.utime(path,(time.time()+1,time.time()+1))
        with patch.object(verify.time,'sleep',side_effect=complete):
            result=verify.check_audio(self.root,status_reader=lambda _:status,timeout=2)
        self.assertTrue(result['verified']);self.assertFalse(result['dialed'])
        self.assertFalse((self.root/'.runtime/hidden-audio-test-request.json').exists())

    def test_audio_check_refuses_enabled_calling_without_issuing_request(self):
        self.configured();p=self.root/'.secrets/local-backend.json';v=json.loads(p.read_text());v['calling_enabled']=True;p.write_text(json.dumps(v))
        with self.assertRaisesRegex(RuntimeError,'Disable both'):
            verify.check_audio(self.root,status_reader=lambda _:self.fail('must reject before network'))
        self.assertFalse((self.root/'.runtime/hidden-audio-test-request.json').exists())

    def test_audio_check_rejects_busy_or_nonhidden_bridge(self):
        self.configured()
        for active,context in [(True,'offscreen'),(False,'tab')]:
            status={'mac_online':True,'active_call':active,'registered_client':{'registered':True,'microphone_ready':True,'browser_context':context}}
            with self.assertRaisesRegex(RuntimeError,'ready, idle'):
                verify.check_audio(self.root,status_reader=lambda _:status)
        self.assertFalse((self.root/'.runtime/hidden-audio-test-request.json').exists())

if __name__ == '__main__':unittest.main()
