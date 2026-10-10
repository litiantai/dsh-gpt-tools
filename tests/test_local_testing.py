"""本机分支测试的真实进程、版本绑定与失败回执回归。"""
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'dsh-gpt-supervisor/scripts')]
from autopilot.local_testing import verification, environment, run
from autopilot.workspace import git
from autopilot.master import source_workspace


@unittest.skipIf(os.environ.get('DSH_PROJECT_ISOLATED'), '真实本机控制器进程测试须在主机测试阶段执行')
class LocalTestingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root/'repo'; self.repo.mkdir()
        git(self.repo, 'init', '-b', 'master')
        git(self.repo, 'config', 'user.name', 'Test')
        git(self.repo, 'config', 'user.email', 'test@localhost')
        (self.repo/'index.html').write_text('master')
        git(self.repo, 'add', '.'); git(self.repo, 'commit', '-m', 'baseline')
        self.base = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, 'checkout', '-b', 'release-fixture')
        (self.repo/'index.html').write_text('release')
        git(self.repo, 'commit', '-am', 'release fixture')
        self.head = git(self.repo, 'rev-parse', 'HEAD')
        self.request = {'state_root': str(self.root/'auto'), 'product': {'id': 'fixture',
            'test_execution': 'local', 'delivery_repository': str(self.repo),
            'project_config': {'commands': {'start': [[sys.executable, '-m', 'http.server', '{port}', '--bind', '127.0.0.1']],
                'test': [[sys.executable, '-c', 'import os,urllib.request; assert not os.environ.get("DSH_PROJECT_ISOLATED"); assert urllib.request.urlopen(os.environ["DSH_TEST_ORIGIN"]).read()==b"release"']]},
                'startup_timeout': 5}},
            'record': {'workspace': str(self.repo), 'branch': 'release-fixture',
                       'commit': self.head, 'flow': 'review_before_release'}}

    def tearDown(self):
        self.temp.cleanup()

    def assert_stopped(self, instance):
        self.assertTrue(instance['stopped'])
        port = int(instance['origin'].rsplit(':', 1)[1])
        with socket.socket() as sock:
            self.assertNotEqual(sock.connect_ex(('127.0.0.1', port)), 0)

    def test_release_instance_lives_through_acceptance_then_stops(self):
        with verification(self.request, self.root/'checks') as result:
            self.assertEqual(result['status'], 'pass', result)
            self.assertEqual(result['instance']['branch'], 'release-fixture')
            self.assertEqual(result['instance']['commit'], self.head)
            with urlopen(result['instance']['origin']) as response:
                self.assertEqual(response.read(), b'release')
            self.assertEqual([c['name'] for c in result['checks']], ['startup', 'test-0'])
        self.assert_stopped(result['instance'])
        self.assertTrue(json.loads((self.root/'checks/instance.json').read_text())['stopped'])

    def test_feature_testing_uses_feature_instance(self):
        git(self.repo, 'checkout', '-b', 'feat-fixture')
        self.request['record'].update(branch='feat-fixture')
        self.request['record'].pop('flow')
        with verification(self.request, self.root/'feature') as result:
            self.assertEqual(result['status'], 'pass', result)
            self.assertEqual(result['instance']['instance_role'], 'feature')
        self.assert_stopped(result['instance'])

    def test_wrong_branch_or_commit_never_runs_tests(self):
        for change in ({'branch': 'release-wrong'}, {'commit': self.base}, {'flow': ''}):
            with self.subTest(change=change), patch('autopilot.local_testing.run') as spawn:
                request = self.request | {'record': self.request['record'] | change}
                with verification(request, self.root/'rejected') as result:
                    self.assertEqual(result['status'], 'blocked')
                    spawn.assert_not_called()

    def test_test_failure_is_not_accepted_and_instance_stops(self):
        self.request['product']['project_config']['commands']['test'] = [[sys.executable, '-c', 'raise SystemExit(7)']]
        with verification(self.request, self.root/'failed') as result:
            self.assertEqual(result['status'], 'fail')
            self.assertEqual(result['checks'][-1]['exit_code'], 7)
        self.assert_stopped(result['instance'])

    def test_acceptance_exception_still_stops_instance(self):
        with self.assertRaisesRegex(RuntimeError, 'judge'):
            with verification(self.request, self.root/'exception') as result:
                self.assertEqual(result['status'], 'pass')
                raise RuntimeError('judge failed')
        self.assert_stopped(result['instance'])

    def test_local_environment_retains_home_without_service_credentials(self):
        with patch.dict(os.environ, {'PRODUCTION_PASSWORD': 'fixture'}):
            env = environment(self.root/'env')
        self.assertEqual(env['HOME'], os.environ['HOME'])
        self.assertTrue(Path(env['HOME']).is_dir())
        self.assertNotIn('PRODUCTION_PASSWORD', env)
        self.assertNotIn('DSH_PROJECT_ISOLATED', env)
        with patch.dict(os.environ, {'DSH_PROJECT_ISOLATED': '1'}):
            with self.assertRaisesRegex(ValueError, '控制器'):
                environment(self.root/'nested')

    def test_timeout_kills_command_and_keeps_blocked_receipt(self):
        env = environment(self.root/'timeout')
        result = run([sys.executable, '-c', 'import time; time.sleep(20)'], self.repo,
                     self.root/'timeout', 'slow', env=env, timeout=.1)
        self.assertEqual(result['status'], 'blocked')
        self.assertIsNone(result['exit_code'])
        self.assertLess(result['elapsed'], 5)

    def test_other_workflows_read_master_not_release(self):
        with patch('autopilot.master.target', return_value=self.base):
            path = Path(source_workspace(self.request))
            self.assertEqual(git(path, 'rev-parse', 'HEAD'), self.base)
            self.assertEqual((path/'index.html').read_text(), 'master')
            self.assertEqual(source_workspace(self.request), str(path))
        self.assertEqual(git(self.repo, 'branch', '--show-current'), 'release-fixture')

    def test_adapter_passes_live_instance_to_judge_and_cleans_up(self):
        from autopilot.generic_adapter import execute
        self.request['record'].update(id='candidate', base_commit=self.base)
        self.request['product']['adapter_spec'] = {'capabilities': ['verify']}
        def judge(action, request):
            self.assertEqual(action, 'validate')
            instance = request['test_instance']
            self.assertFalse(instance['stopped'])
            with urlopen(instance['origin']) as response:
                self.assertEqual(response.read(), b'release')
            return {'status': 'pass'}
        with patch('autopilot.codex_executor.execute', side_effect=judge), patch('autopilot.generic_adapter.package', return_value='manifest'):
            result = execute('verify', self.request)
        self.assertEqual(result['status'], 'pass', result)
        self.assert_stopped(result['instance'])

    def test_adapter_does_not_judge_or_package_failed_tests(self):
        from autopilot.generic_adapter import execute
        self.request['record'].update(id='candidate', base_commit=self.base)
        self.request['product']['adapter_spec'] = {'capabilities': ['verify']}
        self.request['product']['project_config']['commands']['test'] = [[sys.executable, '-c', 'raise SystemExit(7)']]
        with patch('autopilot.codex_executor.execute') as judge, patch('autopilot.generic_adapter.package') as package:
            result = execute('verify', self.request)
        self.assertEqual(result['status'], 'fail')
        judge.assert_not_called(); package.assert_not_called()
        self.assert_stopped(result['instance'])

    def test_worker_cannot_launch_tests_in_local_controller_mode(self):
        import subprocess
        script = """
import {apply} from './scripts/autopilot-guard.mjs';
process.env.DSH_AUTOPILOT_WORKER='test';
process.env.DSH_AUTOPILOT_PHASE='develop';
process.env.DSH_AUTOPILOT_TEST_EXECUTION='local';
let guard;apply({tools:{guard:g=>guard=g}});
if(!guard({name:'bash',arguments:{command:'npm test'}})) throw Error('worker ran tests');
if(guard({name:'edit',arguments:{}})) throw Error('worker cannot edit code');
"""
        subprocess.run(['node', '--input-type=module', '-e', script], cwd=ROOT, check=True, capture_output=True)

    def test_functional_failure_becomes_one_high_priority_requirement(self):
        from autopilot.generic_adapter import execute
        from autopilot.store import Ledger
        from review_core import Store
        self.request['record'].update(id='candidate', base_commit=self.base)
        self.request['product'].update(adapter_spec={'capabilities': ['verify']}, functional_findings_policy='backlog')
        finding = {'status': 'fail', 'reason': '能力检查完成前允许提交扫描', 'evidence': 'fixture-log'}
        with patch('autopilot.codex_executor.execute', return_value=finding), patch('autopilot.generic_adapter.package', return_value='manifest'):
            first = execute('verify', self.request)
            second = execute('verify', self.request)
        self.assertEqual(first['status'], 'pass', first)
        check = first['checks'][-1]
        self.assertEqual(check['status'], 'fail')
        self.assertFalse(check['required'])
        self.assertEqual(check['disposition'], 'backlog')
        self.assertEqual(first['findings'], second['findings'])
        items = Ledger(Store(self.root)).list('requirements')
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['priority'], 0)
        self.assertTrue(items[0]['queue_first'])
        self.assertEqual(items[0]['verification_occurrences'][0]['commit'], self.head)

    def test_incomplete_verification_and_feature_findings_still_block(self):
        from autopilot.verification_findings import defer
        self.request['product']['functional_findings_policy'] = 'backlog'
        for judged in ({'status': 'blocked', 'reason': '模型无响应'},
                       {'status': 'fail', 'failure_kind': 'agent_execution', 'reason': '执行器退出'},
                       {'status': 'fail', 'reason': ''}):
            self.assertIsNone(defer(self.request, judged))
        feature = self.request | {'record': self.request['record'] | {'review_target': 'feature'}}
        self.assertIsNone(defer(feature, {'status': 'fail', 'reason': '功能缺陷'}))

    def test_logs_keep_full_output_and_failure_tail(self):
        result = run([sys.executable, '-c', 'print("line\\n"*6000); print("END-OF-TESTS"); raise SystemExit(1)'], self.repo,
                     self.root/'logs', 'long', env=environment(self.root/'logs'))
        self.assertGreater(Path(result['log']).stat().st_size, 20000)
        self.assertIn('END-OF-TESTS', result['log_tail'])
        self.assertEqual(result['status'], 'fail')

    def test_retry_failed_release_revalidates_without_rewriting_old_receipts(self):
        from autopilot.api import Control
        from review_core import Store, Conflict
        control = Control(Store(self.root/'control'))
        old = control.ledger.create('deliveries', {'product_id': 'fixture', 'flow': 'review_before_release',
            'receipts': [{'action': 'validate_release', 'result': {'status': 'fail'}}]}, 'release_failed')
        row = control.mutate('/deliveries/'+old['id']+'/retry', {'version': old['version']})
        self.assertEqual(row['status'], 'syncing_release')
        self.assertEqual(row['receipts'], old['receipts'])
        busy = control.ledger.create('deliveries', {'product_id': 'fixture', 'call': {'id': 'running'}}, 'release_failed')
        with self.assertRaises(Conflict):
            control.mutate('/deliveries/'+busy['id']+'/retry', {'version': busy['version']})
