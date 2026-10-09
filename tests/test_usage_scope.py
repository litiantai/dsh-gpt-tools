"""开发额度仅覆盖需求方案至业务验收，发现及交付独立记账。"""
import json
import datetime as dt
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'dsh-gpt-supervisor/scripts')]
from review_core import Store
from review_core import Conflict
from autopilot.scheduler import Scheduler
from autopilot.progress import investigate
from autopilot.usage import register, collect
from autopilot.quota import snapshot, ZONE


class UsageScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.scheduler = Scheduler(Store(self.root / 'state'))
        self.ledger = self.scheduler.ledger
        self.product = self.ledger.create('products', {
            'source': str(self.root), 'executor': ['fake'], 'adapter': ['fake'],
            'last_probe': time.time(), 'last_inspect': time.time(),
            'policy': {'tokens_per_day': 1, 'runs_per_day': 5}}, 'active')

    def tearDown(self):
        self.tmp.cleanup()

    def trace(self, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'type': 'turn.completed',
                                   'usage': {'input_tokens': 90, 'output_tokens': 10}}) + '\n')
        return path

    def test_only_business_workflow_charges_development(self):
        for action in ('plan', 'plan_review', 'develop', 'verify', 'acceptance_review', 'discover', 'investigate'):
            register(self.ledger, self.product['id'], self.trace(action), action=action)
        for action in ('repair_plan', 'repair', 'validate'):
            register(self.ledger, self.product['id'], self.trace(action), 'code_delivery_tokens', action=action)
        for action in ('review', 'review_feature', 'review_release'):
            register(self.ledger, self.product['id'], self.trace(action), 'code_delivery_tokens', action=action)
        collect(self.ledger)
        collect(self.ledger)
        self.assertEqual(self.ledger.budget_used(self.product['id'], 'tokens'), 500)
        self.assertEqual(self.ledger.budget_used(self.product['id'], 'discovery_tokens'), 200)
        self.assertEqual(self.ledger.budget_used(self.product['id'], 'code_delivery_tokens'), 600)
        with self.ledger.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM auto_token_sources').fetchone()[0], 13)

    def test_discovery_and_investigation_ignore_exhausted_token_quota(self):
        register(self.ledger, self.product['id'], self.trace('plan'), action='plan')
        collect(self.ledger)
        self.assertFalse(self.scheduler.tokens_available(self.product))
        self.ledger.signal(self.product['id'], {'source': 'test', 'code': 'test',
                                              'summary': 'test', 'evidence': 'test'})
        with patch.object(self.scheduler, 'start_call') as start:
            self.scheduler.monitor(self.product)
            self.assertEqual(start.call_args.args[3], 'discover')
        req = self.ledger.create('requirements', {'product_id': self.product['id'],
            'classification': 'investigation', 'in_scope': True}, 'pending')
        with patch('autopilot.scheduler.subprocess.Popen'):
            self.assertTrue(investigate(self.scheduler))
        req = self.ledger.get('requirements', req['id'])
        self.assertEqual(req['status'], 'investigating')
        self.assertEqual(req['call']['action'], 'investigate')
        run = self.ledger.create('runs', {'product_id': self.product['id']}, 'verifying')
        with patch('autopilot.scheduler.subprocess.Popen') as launch:
            self.scheduler.start_call('runs', run, self.product, 'verify', ['fake'])
            launch.assert_not_called()
        self.assertIn('Token', self.ledger.get('runs', run['id'])['reason'])

    def test_today_limit_resumes_work_and_expires_at_shanghai_midnight(self):
        register(self.ledger, self.product['id'], self.trace('plan'), action='plan')
        collect(self.ledger)
        original = snapshot(self.ledger, self.product)
        self.assertTrue(original['tokens_exhausted'])
        endpoint = '/products/' + self.product['id'] + '/today-token-limit'
        body = {'day': original['day'], 'revision': 0, 'tokens_limit': 200}
        updated = self.scheduler.control.mutate(endpoint, body)
        self.assertFalse(updated['tokens_exhausted'])
        self.assertEqual(updated['tokens_used'], 100)
        product = self.ledger.get('products', self.product['id'])
        self.assertEqual(product['policy']['tokens_per_day'], 1)
        self.assertTrue(self.scheduler.tokens_available(product))
        run = self.ledger.create('runs', {'product_id': product['id'],
            'reason': '每日 Token 额度已用尽，等待次日或调整额度'}, 'verifying')
        with patch('autopilot.scheduler.subprocess.Popen') as launch:
            self.scheduler.start_call('runs', run, product, 'verify', ['fake'])
            launch.assert_called_once()
        self.assertEqual(self.ledger.get('runs', run['id'])['reason'], '')
        with self.assertRaises(Conflict):
            self.scheduler.control.mutate(endpoint, body)
        midnight = dt.datetime.fromtimestamp(updated['tokens_reset_at'], ZONE)
        self.assertEqual(midnight.hour, 0)
        tomorrow = snapshot(self.ledger, product, midnight)
        self.assertEqual(tomorrow['tokens_limit'], 1)
        self.assertEqual(tomorrow['tokens_used'], 0)
        self.assertFalse(tomorrow['tokens_override'])
        with patch('autopilot.quota.dt.datetime') as clock:
            clock.now.return_value = midnight
            with self.assertRaises(Conflict):
                self.scheduler.control.mutate(endpoint, body | {'revision': 1})

    def test_today_limit_validates_values_and_allows_unlimited_without_resetting_usage(self):
        original = snapshot(self.ledger, self.product)
        endpoint = '/products/' + self.product['id'] + '/today-token-limit'
        body = {'day': original['day'], 'revision': 0}
        for value in (None, -1, 1.5, True, '200', 9007199254740992):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.scheduler.control.mutate(endpoint, body | {'tokens_limit': value})
        # Runtime updates do not conflict with an independent quota edit.
        self.ledger.update('products', self.product['id'], self.product['version'], {'last_probe': time.time()})
        updated = self.scheduler.control.mutate(endpoint, body | {'tokens_limit': 0})
        self.assertEqual(updated['tokens_limit'], 0)
        self.assertFalse(updated['tokens_exhausted'])
        automation = self.scheduler.control.get('/products/' + self.product['id'] + '/automation')
        self.assertTrue(automation['tokens_override'])
        self.assertEqual(automation['tokens_default_limit'], 1)


if __name__ == '__main__':
    unittest.main()
