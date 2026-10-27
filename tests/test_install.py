import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class InstallTests(unittest.TestCase):
    def test_install_update_uninstall_and_purge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'; source.mkdir()
            for name in ('collector.py', '.env.example', 'hyperhive-agent.service'):
                shutil.copy(ROOT / name, source / name)
            for path in ('run/systemd/system', 'etc/systemd/system', 'bin'):
                (root / path).mkdir(parents=True)
            for name, text in {'id':'#!/bin/sh\necho 0\n', 'systemctl':'#!/bin/sh\nexit 0\n', 'chown':'#!/bin/sh\nexit 0\n'}.items():
                p = root / 'bin' / name; p.write_text(text); p.chmod(0o755)
            for name in ('install.sh','uninstall.sh'):
                text = (ROOT / name).read_text()
                for path in ('/run/systemd/system','/etc/systemd/system','/opt/hyperhive-agent','/etc/hyperhive-agent','/var/lib/hyperhive-agent'):
                    text = text.replace(path, str(root) + path)
                (source / name).write_text(text)
            env = {**os.environ, 'PATH': str(root / 'bin') + ':' + os.environ['PATH']}
            def run(name, *args):
                return subprocess.run(['sh',str(source / name),*args],env=env,check=True,capture_output=True,text=True)
            run('install.sh')
            config = root / 'etc/hyperhive-agent/.env'
            self.assertTrue(config.exists())
            self.assertFalse((root / 'etc/systemd/system/hyperhive-agent.service').exists())
            config.write_text('SUPPORT_INGEST_URL=https://example.test/ingest\nSUPPORT_APP_ID=id\nSUPPORT_APP_SECRET=secret\n')
            run('install.sh'); run('install.sh')
            self.assertIn('SUPPORT_APP_SECRET=secret', config.read_text())
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            state = root / 'var/lib/hyperhive-agent'; state.mkdir(parents=True)
            (state / 'state.json').write_text('{}')
            run('uninstall.sh')
            self.assertTrue(config.exists()); self.assertTrue((state / 'state.json').exists())
            self.assertFalse((root / 'etc/systemd/system/hyperhive-agent.service').exists())
            run('uninstall.sh','--purge')
            self.assertFalse(config.exists()); self.assertFalse((state / 'state.json').exists())
