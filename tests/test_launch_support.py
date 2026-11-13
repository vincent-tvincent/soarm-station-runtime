import os
import tempfile
import unittest
from pathlib import Path

from soarm.launch_support import child_environment,discover_root,read_secret_environment,resolve_root


class LaunchSupportTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.write('python/soarm/config.py','')
        (self.root/'ros2_ws').mkdir()
        for relative in ('.venv/bin/python','.runtime-venv/bin/python'):
            self.write(relative,'#!/bin/sh\n');(self.root/relative).chmod(0o700)
        for name in ('cert.pem','key.pem','identity_ca.cert.pem','permissions_ca.cert.pem','permissions.p7s','governance.p7s'):
            self.write('local/security/enclaves/'+name,'test')
        self.write('local/tls/cert.pem','test');self.write('local/tls/key.pem','test')
        self.write('local/secrets.env','SOARM_TOKEN='+('t'*32)+'\nSOARM_COLLECTOR_TOKEN='+('c'*32)+'\n')
        self.write('config/mock.yaml','')
        self.config={'_path':str(self.root/'config/mock.yaml'),
            'web':{'cert':'../local/tls/cert.pem','key':'../local/tls/key.pem'}}

    def write(self,relative,text):
        path=self.root/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
        return path

    def test_source_and_installed_launch_discover_own_checkout(self):
        for relative in ('ros2_ws/src/soarm_system/launch/system.launch.py',
                         'ros2_ws/install/soarm_system/share/soarm_system/launch/system.launch.py'):
            origin=self.write(relative,'')
            self.assertEqual(discover_root(origin,{'SOARM_ROOT':'/stale/checkout'}),self.root)
        self.assertEqual(resolve_root(str(self.root),'/unrelated/launch.py'),self.root)
        self.assertEqual(discover_root('/unrelated/launch.py',{'SOARM_ROOT':str(self.root)}),self.root)
        with self.assertRaisesRegex(ValueError,'root:='):
            resolve_root('/missing/root','/unrelated/launch.py',{})

    def test_secret_file_is_data_and_only_selected_names_are_loaded(self):
        file=self.write('local/test.env',"# private\nexport SOARM_TOKEN='a quoted token' # comment\nIGNORED=other\nSOARM_COLLECTOR_TOKEN=$(literal-command)\n")
        self.assertEqual(read_secret_environment(file,{'SOARM_TOKEN','SOARM_COLLECTOR_TOKEN'}),
            {'SOARM_TOKEN':'a quoted token','SOARM_COLLECTOR_TOKEN':'$(literal-command)'})
        file.write_text('SOARM_TOKEN="private value\n')
        with self.assertRaises(ValueError) as caught:read_secret_environment(file,{'SOARM_TOKEN'})
        self.assertNotIn('private value',str(caught.exception))

    def test_child_paths_security_and_file_credentials(self):
        env=child_environment(self.root,self.config,{'PATH':'/usr/bin','PYTHONPATH':'/ros/python'})
        self.assertEqual(env['SOARM_TOKEN'],'t'*32)
        self.assertEqual(env['SOARM_CONFIG'],self.config['_path'])
        self.assertEqual(env['TZ'],'UTC')
        self.assertEqual(env['ROS_DOMAIN_ID'],'71')
        self.assertEqual(env['ROS_SECURITY_STRATEGY'],'Enforce')
        self.assertEqual(env['ROS_SECURITY_KEYSTORE'],str(self.root/'local/security'))
        self.assertEqual(env['PYTHONPATH'],'/ros/python'+os.pathsep+str(self.root/'python'))
        self.assertTrue(env['PATH'].startswith(str(self.root/'.venv/bin')+os.pathsep))

    def test_native_interpreter_retains_virtualenv_symlink(self):
        interpreter=self.write('base-python','#!/bin/sh\n');interpreter.chmod(0o700)
        native=self.root/'.runtime-venv/bin/python';native.unlink();native.symlink_to(interpreter)
        env=child_environment(self.root,self.config,{})
        self.assertEqual(env['SOARM_RUNTIME_PYTHON'],str(native))
        self.assertNotEqual(env['SOARM_RUNTIME_PYTHON'],str(interpreter))

    def test_exported_credentials_take_precedence(self):
        env=child_environment(self.root,self.config,{'SOARM_TOKEN':'e'*32,'SOARM_COLLECTOR_TOKEN':'f'*32})
        self.assertNotIn('SOARM_TOKEN',env)
        self.assertNotIn('SOARM_COLLECTOR_TOKEN',env)

    def test_custom_token_names_and_relative_secret_file(self):
        self.config['web']['token_env']='CUSTOM_TOKEN'
        self.write('local/custom.env','CUSTOM_TOKEN='+('x'*32)+'\nUNRELATED=skip\n')
        env=child_environment(self.root,self.config,{},secrets_file='local/custom.env')
        self.assertEqual(env['CUSTOM_TOKEN'],'x'*32)
        self.assertNotIn('UNRELATED',env)

    def test_missing_dependencies_fail_before_starting_nodes(self):
        cases=[('.venv/bin/python','scripts/bootstrap'),
               ('local/security/enclaves/permissions.p7s','scripts/init-security'),
               ('local/tls/key.pem','TLS key'),('local/secrets.env','SOARM_TOKEN')]
        for relative,message in cases:
            with self.subTest(relative=relative):
                path=self.root/relative;content=path.read_bytes();mode=path.stat().st_mode;path.unlink()
                try:
                    with self.assertRaisesRegex(ValueError,message):child_environment(self.root,self.config,{})
                finally:path.write_bytes(content);path.chmod(mode)


if __name__=='__main__':unittest.main()
