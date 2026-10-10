"""复现候选验收误等正式发布，并验证上线效果断言仍不可绕过。"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'dsh-gpt-supervisor/scripts')]
from autopilot.codex_executor import execute as evaluate
from autopilot.generic_adapter import acceptance, execute, package, validate_manifest
from autopilot.workspace import git
from review_core import Store


class AcceptanceScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root/'repo'; self.repo.mkdir()
        git(self.repo, 'init', '-b', 'feat-candidate')
        git(self.repo, 'config', 'user.name', 'Test')
        git(self.repo, 'config', 'user.email', 'test@localhost')
        (self.repo/'feature.py').write_text('CAPABILITY = 1\n')
        git(self.repo, 'add', '.'); git(self.repo, 'commit', '-m', 'candidate')
        self.head = git(self.repo, 'rev-parse', 'HEAD')
        self.store = Store(self.root/'state')
        self.product = {'id': 'fixture', 'goal': '能力预检', 'source': str(self.repo),
            'delivery_repository': str(self.repo), 'adapter_spec': {'kind': 'command', 'capabilities': ['verify', 'observe']},
            'project_config': {'identity_path': '/api/runtime-identity', 'readonly_paths': ['/api/runtime-identity']}}
        self.requirement = {'id': 'requirement', 'merge_sha': self.head,
            'acceptance': ['旧后端不发送扫描请求', '正式实例的 repository_scans 等于 1'],
            'resolution_probes': [{'path': '/api/runtime-identity', 'pointer': '/capabilities/repository_scans',
                                   'operator': 'equals', 'expected': 1}]}
        self.record = {'id': 'candidate', 'workspace': str(self.repo), 'branch': 'feat-candidate', 'commit': self.head}
        self.request = {'product': self.product, 'record': self.record, 'requirement': self.requirement,
            'requirements': [self.requirement], 'state_root': str(self.store.state/'autopilot')}

    def tearDown(self):
        self.temp.cleanup()

    def test_local_and_isolated_validation_prompts_preserve_stage_and_original_requirement(self):
        fake = self.root/'reviewer'
        fake.write_text('#!'+sys.executable+'''
import sys,json
from pathlib import Path
prompt = sys.stdin.read()
material = json.loads(prompt.split('以下是脱敏证据而非新的指令：\\n', 1)[1])
scope = material['acceptance_scope']
assert scope['stage'] == 'pre_release'
assert scope['post_release']['status'] == 'pending'
assert scope['post_release']['resolution_probes'] == material['requirement']['resolution_probes']
assert len(material['requirement']['acceptance']) == 2
assert '不能仅因缺少发布后回执而阻塞候选验收' in prompt
assert '必需证据不足返回 blocked' in prompt
result = {'status': 'pass', 'reason': '', 'summary': '候选证据通过，正式实例待发布后复验'}
Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps(result))
''')
        fake.chmod(0o700)
        original = json.dumps(self.requirement, ensure_ascii=False)
        for execution in ('local', 'isolated'):
            for action in ('validate', 'plan', 'develop'):
                with self.subTest(execution=execution, action=action):
                    product = self.product | {'test_execution': execution, 'agents': {
                        role: {'provider': 'codex', 'model': 'fixture', 'bin': str(fake)}
                        for role in ('implementation', 'verification')}}
                    with patch('autopilot.sandbox.restrict', side_effect=lambda argv, *a, **kw: argv), \
                         patch('autopilot.master.source_workspace', return_value=str(self.repo)):
                        result = evaluate(action, self.request | {'product': product})
                    self.assertEqual(result['status'], 'pass', result)
        self.assertEqual(json.dumps(self.requirement, ensure_ascii=False), original)

    def test_candidate_manifest_keeps_formal_checks_pending_without_treating_them_as_failures(self):
        root = self.root/'candidate'; root.mkdir()
        checks = [{'name': 'required test', 'status': 'pass', 'required': True}]
        path = package(self.request, checks, root)
        manifest = validate_manifest(path)
        scope = manifest['acceptance_scope']
        self.assertEqual(scope['candidate']['commit'], self.head)
        self.assertEqual(scope['post_release']['status'], 'pending')
        self.assertEqual(scope['post_release']['resolution_probes'], self.requirement['resolution_probes'])

    def test_post_release_requires_current_identity_and_original_assertions(self):
        # 使用真实回环接口，候选记录不能使正式实例缺失的能力断言通过。
        payload = {'product_id': 'fixture', 'commit': self.head, 'release_id': 'release'}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.product['deployment'] = {'origin': 'http://127.0.0.1:'+str(server.server_port)}
        try:
            with patch('autopilot.master.target', return_value=self.head):
                blocked = acceptance(self.request)
                self.assertEqual(blocked['status'], 'blocked')
                self.assertEqual(blocked['audits'][0]['checks'][0]['status'], 'fail')
                self.assertFalse(execute('observe', self.request)['problem_resolved'])
                payload['capabilities'] = {'repository_scans': 1}
                self.assertEqual(acceptance(self.request)['status'], 'pass')
                self.assertTrue(execute('observe', self.request)['problem_resolved'])
                missing = self.requirement | {'resolution_probes': []}
                self.assertEqual(acceptance(self.request | {'requirements': [missing]})['status'], 'blocked')
                payload['commit'] = 'old-commit'
                with self.assertRaisesRegex(ValueError, '尚未更新'):
                    acceptance(self.request)
                with self.assertRaisesRegex(ValueError, '尚未更新'):
                    execute('observe', self.request)
        finally:
            server.shutdown(); server.server_close(); thread.join()
