"""技术栈识别与本机运行时安装检测，不执行项目代码。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'dsh-gpt-supervisor/scripts')]
from autopilot import project_environment as env
from autopilot.api import Control
from review_core import Store


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root/'repo'; self.repo.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def package(self, folder, dependency):
        folder.mkdir(parents=True, exist_ok=True)
        (folder/'package.json').write_text(json.dumps({'dependencies': {dependency: '*'}}))

    def test_mixed_modules_and_explicit_selection(self):
        (self.repo/'backend').mkdir(); (self.repo/'backend/pom.xml').write_text('<project/>')
        self.package(self.repo/'web', 'react')
        self.package(self.repo/'admin', 'vue')
        self.package(self.repo/'node_modules/ignored', 'vue')
        value = env.describe(self.repo, {'modules':[{'id':'ui','path':'web','stack':'vue','commands':{}}]})
        self.assertEqual(value['stacks'], ['java','react','vue'])
        self.assertEqual(len(value['modules']), 3)
        web = next(m for m in value['modules'] if m['path']=='web')
        self.assertEqual(web['workflow_stack'], 'vue')
        self.assertEqual(web['selection'], 'configured')
        self.assertEqual(web['tools'], ['node','npm'])

    def test_missing_then_installed_and_probe_contract(self):
        self.package(self.repo, 'react')
        with patch.object(env.shutil, 'which', return_value=None), patch.object(env.subprocess, 'run') as run:
            result = env.check(self.repo)
        self.assertEqual(result['status'], 'blocked'); run.assert_not_called()
        executable = self.root/'node'; executable.write_text('local fixture')
        with patch.object(env.shutil, 'which', return_value=str(executable)), patch.object(env.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'v22.0.0', '')) as run:
            result = env.check(self.repo)
        self.assertEqual(result['status'], 'ready')
        for call in run.call_args_list:
            self.assertEqual(call.args[0][1:], ['--version'])
            self.assertEqual(call.kwargs['env']['COREPACK_ENABLE_NETWORK'], '0')
            self.assertNotEqual(call.kwargs['cwd'], str(self.repo))
            self.assertNotIn('shell', call.kwargs)

    def test_project_scripts_and_symlinks_are_never_executed(self):
        self.package(self.repo, 'vue')
        binary = self.repo/'npm'; binary.write_text('malicious')
        link = self.root/'npm'; link.symlink_to(binary)
        with patch.object(env.shutil, 'which', return_value=str(link)), patch.object(env.subprocess, 'run') as run:
            result = env.check(self.repo)
        run.assert_not_called()
        self.assertTrue(all(c['status']=='error' for c in result['checks']))

    def test_timeout_and_nonzero_are_not_installed(self):
        self.package(self.repo, 'react')
        executable = self.root/'node'; executable.write_text('fixture')
        with patch.object(env.shutil, 'which', return_value=str(executable)), patch.object(env.subprocess, 'run', side_effect=[subprocess.TimeoutExpired('node',12),subprocess.CompletedProcess([],1,'','broken')]):
            result = env.check(self.repo)
        self.assertEqual(result['status'], 'blocked')
        self.assertTrue(all(c['status']=='error' for c in result['checks']))

    def test_java_and_gradle_missing_jdk_no_tool_download(self):
        (self.repo/'build.gradle.kts').write_text('plugins { java }')
        with patch.object(env.jvm, 'java_home', side_effect=env.jvm.MissingRuntime('JDK','请安装 JDK')), patch.object(env.subprocess,'run') as run:
            result = env.check(self.repo)
        run.assert_not_called()
        self.assertEqual({c['tool'] for c in result['checks']}, {'java','javac','gradle'})
        self.assertTrue(all('安装' in c['install_hint'] for c in result['checks']))

    def test_api_checks_preserve_product_and_scan_history(self):
        control = Control(Store(self.root/'state'))
        self.package(self.repo, 'react')
        product = control.ledger.create('products', {'source':str(self.repo),'name':'fixture'}, 'paused')
        value = control.get(f"/products/{product['id']}/environment")
        self.assertEqual(value['stacks'], ['react'])
        with patch.object(env.shutil,'which',return_value=None):
            self.assertEqual(control.mutate(f"/products/{product['id']}/runtime-check",{})['status'], 'blocked')
        self.assertEqual(control.ledger.get('products',product['id'])['version'], product['version'])
        scan = control.ledger.create('scans', {'source':str(self.repo)}, 'running')
        root = control.ledger.store.state/'autopilot/scans'/scan['id']; root.mkdir(parents=True)
        (root/'workspace').mkdir(); self.package(root/'workspace','vue')
        (root/'environment.json').write_text(json.dumps({'configuration':{}}))
        self.assertEqual(control.get(f"/scans/{scan['id']}/environment")['stacks'], ['vue'])
        with patch.object(env.shutil,'which',return_value=None):
            self.assertEqual(control.mutate(f"/scans/{scan['id']}/runtime-check",{})['status'],'blocked')
        self.assertEqual(control.ledger.get('scans',scan['id'])['status'],'running')

    def test_scan_stops_before_build_and_keeps_detection_when_runtime_missing(self):
        from autopilot import onboarding
        from autopilot.workspace import git
        self.package(self.repo, 'react')
        git(self.repo, 'init'); git(self.repo, 'config', 'user.name', 'Test')
        git(self.repo, 'config', 'user.email', 'test@localhost')
        git(self.repo, 'add', '.'); git(self.repo, 'commit', '-m', 'fixture')
        state = self.root/'state/autopilot'
        with patch.object(onboarding, 'independent_runtime', return_value=None), patch.object(env.shutil, 'which', return_value=None), patch.object(onboarding, 'verify') as verify:
            result = onboarding.execute('scan', {'record':{'id':'fixture','source':str(self.repo)}, 'state_root':str(state)})
        verify.assert_not_called()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['configuration']['stack'], 'react')
        self.assertEqual(result['runtime_check']['stacks'], ['react'])
        self.assertTrue((state/'scans/fixture/environment.json').is_file())

    def test_real_installed_node_and_npm_versions(self):
        if not env.shutil.which('node') or not env.shutil.which('npm'):
            self.skipTest('本机未安装 Node/npm')
        self.package(self.repo, 'react')
        result = env.check(self.repo)
        self.assertEqual(result['status'], 'ready', result)
        self.assertTrue(all(c['version'] and c['exit_code']==0 for c in result['checks']))


if __name__ == '__main__':
    unittest.main()
