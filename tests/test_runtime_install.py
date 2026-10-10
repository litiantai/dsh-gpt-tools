"""验证运行时安装失败、中断和竞争时不会暴露半成品。"""
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from autopilot.runtime import PACKAGES, install, manifest, ready


def fixture(path, version='1.2.3'):
    path = Path(path); path.mkdir(parents=True, exist_ok=True)
    data = manifest(version)
    (path/'package.json').write_text(json.dumps(data))
    packages = {'':{'dependencies':data['dependencies']}}
    for name in PACKAGES:
        folder = path/'node_modules'/name; folder.mkdir(parents=True, exist_ok=True)
        meta = {'version':version, 'bin':{'dsh':'entry.js'}} if name == PACKAGES[0] else {'version':version, 'main':'entry.js'}
        (folder/'package.json').write_text(json.dumps(meta))
        (folder/'entry.js').write_text('// deterministic test fixture')
        packages['node_modules/'+name] = {'version':version}
    (path/'package-lock.json').write_text(json.dumps({'lockfileVersion':3, 'packages':packages}))


class RuntimeInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.state = Path(self.temp.name)
        self.runtime = self.state/'autopilot/runtime'

    def tearDown(self):
        self.temp.cleanup()

    def test_partial_runtime_is_never_ready(self):
        fixture(self.runtime)
        self.assertTrue(ready(self.runtime))
        (self.runtime/'package-lock.json').unlink()
        self.assertFalse(ready(self.runtime))
        fixture(self.runtime)
        (self.runtime/'node_modules'/PACKAGES[0]/'entry.js').unlink()
        self.assertFalse(ready(self.runtime))

    def test_success_publishes_only_after_install_and_reuses_complete_runtime(self):
        def npm(*args, cwd, **kwargs):
            self.assertFalse(self.runtime.exists())
            fixture(cwd)
        with patch('autopilot.runtime.subprocess.run',side_effect=npm) as run:
            self.assertFalse(install(self.state,'1.2.3')['reused'])
            self.assertTrue(ready(self.runtime))
            self.assertTrue(install(self.state,'1.2.3')['reused'])
            self.assertEqual(run.call_count,1)

    def test_failed_install_keeps_existing_partial_directory_and_failure_log(self):
        self.runtime.mkdir(parents=True)
        (self.runtime/'package.json').write_text(json.dumps(manifest('1.2.3')))
        (self.runtime/'original-evidence').write_text('preserve')
        with patch('autopilot.runtime.subprocess.run',side_effect=subprocess.CalledProcessError(1,['npm'])):
            with self.assertRaises(RuntimeError):install(self.state,'1.2.3')
        self.assertEqual((self.runtime/'original-evidence').read_text(),'preserve')
        self.assertFalse(ready(self.runtime))
        self.assertEqual(len(list(self.runtime.parent.glob('.runtime-install-*/failure.json'))),1)

    def test_invalid_success_receipt_does_not_publish(self):
        with patch('autopilot.runtime.subprocess.run'):
            with self.assertRaises(RuntimeError):install(self.state,'1.2.3')
        self.assertFalse(self.runtime.exists())

    def test_interrupted_atomic_switch_can_retry_without_losing_evidence(self):
        self.runtime.mkdir(parents=True)
        (self.runtime/'package.json').write_text(json.dumps(manifest('1.2.3')))
        (self.runtime/'original-evidence').write_text('preserve')
        with patch('autopilot.runtime.subprocess.run',side_effect=lambda *a,cwd,**kw:fixture(cwd)), patch('autopilot.runtime.os.replace',side_effect=OSError('simulated interruption')):
            with self.assertRaises(RuntimeError):install(self.state,'1.2.3')
        self.assertFalse(ready(self.runtime))
        self.assertEqual(len(list(self.runtime.parent.glob('.runtime-incomplete-*/original-evidence'))),1)
        with patch('autopilot.runtime.subprocess.run',side_effect=lambda *a,cwd,**kw:fixture(cwd)):
            install(self.state,'1.2.3')
        self.assertTrue(ready(self.runtime))

    def test_competing_installers_share_one_completed_install(self):
        entered=threading.Event(); release=threading.Event(); results=[]; failures=[]
        def npm(*args,cwd,**kwargs):
            entered.set()
            if not release.wait(5):raise TimeoutError('test lock wait')
            fixture(cwd)
        def worker():
            try:results.append(install(self.state,'1.2.3'))
            except Exception as exc:failures.append(exc)
        with patch('autopilot.runtime.subprocess.run',side_effect=npm) as run:
            first=threading.Thread(target=worker); second=threading.Thread(target=worker)
            first.start(); self.assertTrue(entered.wait(5)); second.start()
            self.assertFalse(ready(self.runtime)); release.set()
            first.join(5); second.join(5)
            self.assertFalse(first.is_alive() or second.is_alive())
            self.assertEqual(run.call_count,1)
        self.assertFalse(failures)
        self.assertEqual(sorted(r['reused'] for r in results),[False,True])

    def test_different_version_never_replaces_running_runtime(self):
        fixture(self.runtime)
        with patch('autopilot.runtime.subprocess.run') as run:
            with self.assertRaises(ValueError):install(self.state,'2.0.0')
            run.assert_not_called()
        self.assertTrue(ready(self.runtime))
