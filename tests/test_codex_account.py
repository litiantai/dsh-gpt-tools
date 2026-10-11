"""实际 CLI 控制协议的账户查询回归，不发送生成请求或读取认证凭据。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'dsh-gpt-supervisor/scripts')]
from codex_account import account_status
from dashboard_server import Dashboard
from review_core import Store
import reviewers


@unittest.skipUnless(shutil.which('node'), '需要 Node.js 运行 CLI 控制协议')
class CodexAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixture = self.root / 'response.json'
        self.calls = self.root / 'calls.jsonl'
        self.cli = self.root / 'fake-codex'
        self.cli.write_text(f'''#!{sys.executable}
import json, os, sys, time
from pathlib import Path
assert sys.argv[1:] == ['app-server']
Path({str(self.root / 'pid')!r}).write_text(str(os.getpid()))
for line in sys.stdin:
    request = json.loads(line)
    method = request['method']
    with Path({str(self.calls)!r}).open('a') as out:
        out.write(json.dumps({{'method': method, 'params': request.get('params'), 'home': os.environ.get('CODEX_HOME')}}) + '\\n')
    data = json.loads(Path({str(self.fixture)!r}).read_text())
    if data.get('exit'): sys.exit(2)
    if data.get('hang'): time.sleep(60)
    if method == 'initialized': continue
    assert method in ('initialize', 'account/read', 'account/rateLimits/read', 'model/list'), method
    if method == 'account/read': assert request['params']['refreshToken'] is False
    value = {{'initialize': {{}}, 'account/read': {{'account': data.get('account')}},
             'account/rateLimits/read': data.get('limits'),
             'model/list': {{'data': [{{'model': 'fixture', 'displayName': 'Fixture'}}], 'nextCursor': None}}}}[method]
    print(json.dumps({{'id': request['id'], 'result': value}}), flush=True)
''')
        self.cli.chmod(0o700)
        self.cfg = {'model': 'fixture', 'home': str(self.root), 'codex_bin': str(self.cli),
            'node_bin': shutil.which('node'), 'reviewer_helper': str(ROOT / 'dsh-gpt-supervisor/scripts/reviewer_cli.mjs')}
        self.response = {'account': {'type': 'chatgpt', 'planType': 'prolite', 'email': 'private-email'},
            'limits': {'accountId': 'account-fixture', 'ordinaryUsageAllowed': True,
                'rateLimitsByLimitId': {'codex': {'limitId': 'codex',
                    'primary': {'usedPercent': 7, 'windowDurationMins': 10080, 'resetsAt': 2000000000},
                    'secondary': None, 'credits': {'hasCredits': False, 'balance': '0'}}},
                'access_token': 'private-token'}}
        self.write()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self):
        self.fixture.write_text(json.dumps(self.response))

    def test_reads_selected_cli_and_inherited_home_without_generation_or_secrets(self):
        with patch.dict(os.environ, {'CODEX_HOME': str(self.root / 'auth-home')}):
            result = account_status(self.cfg | {'codex_bin': '/wrong-cli'}, {'bin': str(self.cli)}, self.root)
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['account_id'], 'account-fixture')
        self.assertEqual(result['rate_limits']['codex']['primary']['usedPercent'], 7)
        self.assertIsNone(result['rate_limits']['codex']['secondary'])
        self.assertIsNotNone(result['fetched_at'])
        self.assertNotIn('private-email', str(result))
        self.assertNotIn('private-token', str(result))
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual([r['method'] for r in calls], ['initialize', 'initialized', 'account/read', 'account/rateLimits/read'])
        self.assertTrue(all(r['home'] == str(self.root / 'auth-home') for r in calls))

    def test_new_query_observes_reset_and_failure_does_not_reuse_old_allowance(self):
        self.response['limits']['ordinaryUsageAllowed'] = False
        self.write()
        limited = account_status(self.cfg)
        self.assertEqual(limited['status'], 'limited')
        self.response['limits']['ordinaryUsageAllowed'] = True
        self.response['limits']['rateLimitsByLimitId']['codex']['primary']['resetsAt'] += 86400
        self.write()
        available = account_status(self.cfg)
        self.assertEqual(available['status'], 'available')
        self.assertGreater(available['rate_limits']['codex']['primary']['resetsAt'], limited['rate_limits']['codex']['primary']['resetsAt'])
        self.response['exit'] = True
        self.write()
        failed = account_status(self.cfg)
        self.assertEqual(failed['status'], 'unknown')
        self.assertTrue(failed['error'])
        self.assertIsNone(failed['fetched_at'])
        self.assertEqual(failed['rate_limits'], {})

    def test_legacy_limits_and_missing_permission_remain_unknown(self):
        bucket = self.response['limits']['rateLimitsByLimitId']['codex']
        bucket['primary']['usedPercent'] = 100
        self.response['limits'] = {'rateLimits': bucket}
        self.write()
        result = account_status(self.cfg)
        self.assertEqual(result['status'], 'unknown')
        self.assertIsNone(result['ordinary_usage_allowed'])
        self.assertEqual(result['rate_limits']['codex']['primary']['usedPercent'], 100)
        self.response['limits']['ordinaryUsageAllowed'] = True
        self.write()
        self.assertEqual(account_status(self.cfg)['status'], 'available')

    def test_all_buckets_preserved_without_inventing_missing_windows(self):
        self.response['limits']['rateLimitsByLimitId']['other-model'] = {'limitId': 'other-model', 'primary': None}
        self.write()
        result = account_status(self.cfg)
        self.assertEqual(set(result['rate_limits']), {'codex', 'other-model'})
        self.assertIsNone(result['rate_limits']['other-model']['primary'])

    def test_logged_out_does_not_query_limits(self):
        self.response['account'] = None
        self.write()
        self.assertEqual(account_status(self.cfg)['status'], 'unauthenticated')
        self.assertNotIn('account/rateLimits/read', self.calls.read_text())

    def test_timeout_stops_control_process(self):
        self.response['hang'] = True
        self.write()
        started = time.monotonic()
        result = account_status(self.cfg)
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('查询超时', result['error'])
        self.assertLess(time.monotonic() - started, 20)
        pid = int((self.root / 'pid').read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_model_catalog_protocol_still_works(self):
        result = reviewers.catalog(self.cfg, self.root, 'codex')
        self.assertIsNone(result['error'])
        self.assertEqual(result['models'], [{'id': 'fixture', 'name': 'Fixture'}])
        self.assertNotIn('account/read', self.calls.read_text())

    def test_readonly_dashboard_route_uses_current_config_without_quota_mutation(self):
        state = self.root / 'state'
        Store(state, self.cfg)
        app = Dashboard(state)
        try:
            result = app.get('/reviewers/codex/account')
            self.assertEqual(result['status'], 'available')
            self.assertFalse((state / 'quota.json').exists())
        finally:
            app.engine.stop()


if __name__ == '__main__':
    unittest.main()
