"""人工阻塞重试的单轮授权、返修额度隔离与持久化记录。"""
import concurrent.futures
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'dsh-gpt-supervisor/scripts')]
from review_core import Store, Conflict
from autopilot.scheduler import Scheduler


class ManualRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root/'state')
        self.scheduler = Scheduler(self.store)
        self.ledger = self.scheduler.ledger
        self.product = self.ledger.create('products', {'source': str(self.root),
            'executor': ['test-executor'], 'policy': {'max_revisions': 3, 'deepseek_off_peak_only': False}}, 'active')
        self.run = self.ledger.create('runs', {'product_id': self.product['id'], 'revisions': 3,
            'extra_revisions': 0, 'execution_seconds': 123, 'resume_status': 'acceptance_review',
            'reason': '返修次数已用尽：修复部分拒收', 'feedback': '修复部分拒收',
            'review_id': 'old-review', 'review_packet': {'request_id': 'old-review'},
            'receipts': [{'call_id': 'old-call', 'action': 'verify', 'result': {'status': 'fail'}}]}, 'blocked')

    def retry(self, run):
        return self.scheduler.control.mutate(f'/runs/{run["id"]}/retry', {'version': run['version']})

    def dispatch(self, run, action='develop'):
        with patch('autopilot.scheduler.subprocess.Popen') as spawn:
            spawn.return_value.pid = 123456
            return self.scheduler.start_call('runs', run, self.product, action, self.product['executor'])

    def test_repeated_manual_repairs_exceed_three_without_charging_or_erasing_history(self):
        run = self.run
        for count in range(1, 8):
            run = self.retry(run)
            self.assertEqual(run['status'], 'developing')
            self.assertEqual(run['manual_retry_count'], count)
            self.assertIsNone(run['review_id'])
            run = self.dispatch(run)
            self.assertIsNone(run['manual_retry_pending'])
            run = self.scheduler.consume('runs', run, {'status': 'fail', 'reason': '仍未修复'})
            run = self.scheduler.complete_action(run, self.product, 'develop', {'status': 'fail', 'reason': '仍未修复'})
            self.assertEqual(run['status'], 'blocked')
            self.assertEqual((run['revisions'], run['extra_revisions'], run['execution_seconds']), (3, 0, 123))
        self.assertEqual(run['receipts'][0]['call_id'], 'old-call')
        restarted = Scheduler(self.store)
        records = restarted.ledger.scoped('evidence', self.product['id'], phase='manual_retry')
        self.assertEqual(len(records), 7)
        self.assertEqual(records[0]['details']['review_id'], 'old-review')
        self.assertEqual(records[0]['details']['reason'], self.run['reason'])
        self.assertTrue(all(r['details']['counts_toward_revisions'] is False for r in records))
        context = restarted.control.get(f'/runs/{run["id"]}/context')
        self.assertEqual(sum(item['record'].get('phase') == 'manual_retry' for item in context['related']), 7)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='autopilot_manual_retry'").fetchone()[0], 7)

    def test_manual_validation_can_request_one_free_repair_below_or_above_limit(self):
        for revisions in (1, 3, 5):
            run = self.ledger.get('runs', self.run['id'])
            run = self.ledger.update('runs', run['id'], run['version'], {'revisions': revisions,
                'reason': '网络中断', 'resume_status': 'verifying', 'revision_exhausted': False}, 'blocked')
            run = self.retry(run)
            self.assertEqual(run['status'], 'verifying')
            run = self.dispatch(run, 'verify')
            run = self.scheduler.consume('runs', run, {'status': 'fail'})
            run = self.scheduler.complete_action(run, self.product, 'verify', {'status': 'fail', 'reason': '功能失败'})
            self.assertEqual(run['status'], 'developing')
            self.assertEqual(run['revisions'], revisions)
            run = self.dispatch(run)
            run = self.scheduler.consume('runs', run, {'status': 'fail'})
            run = self.scheduler.complete_action(run, self.product, 'develop', {'status': 'fail', 'reason': '下一轮失败'})
            self.assertEqual(run['revisions'], revisions + int(revisions < 3))
            self.assertEqual(run['status'], 'developing' if revisions < 3 else 'blocked')

    def test_plan_retry_preserves_diagnostics_and_supersedes_coordinator_grant(self):
        run = self.ledger.update('runs', self.run['id'], self.run['version'], {'resume_status': 'plan_review',
            'last_revision_diagnostic': {'error': 'compiler failure'},
            'coordination_pending': {'action': 'repair', 'decision_id': 'coordinator-old'}})
        run = self.retry(run)
        self.assertEqual(run['status'], 'planning')
        self.assertIn('compiler failure', run['feedback'])
        self.assertIsNone(run['coordination_pending'])
        run = self.dispatch(run, 'plan')
        self.assertEqual(run['revisions'], 3)
        evidence = self.ledger.get('evidence', run['manual_retry_evidence_id'])
        self.assertEqual(evidence['details']['coordination_pending']['decision_id'], 'coordinator-old')

    def test_busy_uncertain_and_pending_results_cannot_retry(self):
        for field, value in [('call', {'id': 'running'}), ('uncertain', True), ('pending_result', {'status': 'pass'})]:
            run = self.ledger.get('runs', self.run['id'])
            run = self.ledger.update('runs', run['id'], run['version'], {'call': None, 'uncertain': False, 'pending_result': None, field: value})
            with self.assertRaises(Conflict):
                self.retry(run)
        self.assertEqual(self.ledger.scoped('evidence', self.product['id']), [])

    def test_concurrent_clicks_create_only_one_authorization_and_record(self):
        def attempt(_):
            try:
                return self.retry(self.run)
            except Conflict:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(len(self.ledger.scoped('evidence', self.product['id'])), 1)

    def test_review_must_finish_before_manual_retry(self):
        for review in ({'status': 'running'}, {'status': 'blocked', 'execution_done': False}):
            with patch.object(self.store, 'get', return_value=review), self.assertRaises(Conflict):
                self.retry(self.run)
        self.assertEqual(self.ledger.scoped('evidence', self.product['id']), [])

    def test_automatic_failure_still_stops_at_limit(self):
        run = self.ledger.update('runs', self.run['id'], self.run['version'], {'revisions': 2}, 'developing')
        run = self.scheduler.revise(run, self.product, '第一次失败')
        self.assertEqual(run['revisions'], 3)
        run = self.scheduler.revise(run, self.product, '再次失败')
        self.assertEqual(run['status'], 'blocked')
        self.assertEqual(run['revisions'], 3)
        self.assertNotIn('manual_retry_count', run)

    def test_unused_authorization_expires_on_new_block_or_completion(self):
        run = self.retry(self.run)
        run = self.scheduler.block(run, '网络中断')
        self.assertIsNone(run['manual_retry_pending'])
        run = self.retry(run)
        run = self.scheduler.change('runs', run, {}, 'accepted')
        self.assertIsNone(run['manual_retry_pending'])

    def test_resource_wait_keeps_authorization_and_other_budgets(self):
        run = self.retry(self.run)
        with patch.object(self.scheduler, 'tokens_available', return_value=False), patch('autopilot.scheduler.subprocess.Popen') as spawn:
            run = self.scheduler.start_call('runs', run, self.product, 'develop', self.product['executor'])
            spawn.assert_not_called()
        self.assertTrue(run['manual_retry_pending'])
        self.assertEqual((run['revisions'], run['execution_seconds']), (3, 123))


if __name__ == '__main__':
    unittest.main()
