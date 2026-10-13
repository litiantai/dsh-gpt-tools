"""执行时间授权续额、重启幂等和单次超时的行为验证。"""
import unittest
from unittest.mock import patch

from tests import test_coordinator as fixtures
from autopilot import coordinator as c
from autopilot.execution_budget import snapshot, renew
from autopilot.retry import manual_run_retry, quota_reason_for, recover
from autopilot.scheduler import Scheduler
from review_core import Conflict


class ExecutionBudgetTests(unittest.TestCase):
    setUp = fixtures.CoordinatorTests.setUp
    tearDown = fixtures.CoordinatorTests.tearDown
    fresh = fixtures.CoordinatorTests.fresh
    decide = fixtures.CoordinatorTests.decide
    apply = fixtures.CoordinatorTests.apply

    def exhaust(self, seconds=3600.06):
        run = self.fresh()
        return self.ledger.update('runs', run['id'], run['version'],
            {'execution_seconds': seconds, 'resume_status': 'planning', 'review_id': None,
             'reason': '执行超时', 'receipts': [{'call_id': 'failed', 'action': 'plan',
                 'result': {'status': 'blocked', 'failure_kind': 'agent_execution'}}]})

    def test_manual_retry_restores_full_window_preserves_history_and_rejects_duplicate(self):
        original = self.exhaust()
        run = manual_run_retry(self.ledger, original['id'], original['version'])
        self.assertEqual(run['execution_seconds'], original['execution_seconds'])
        self.assertEqual(run['receipts'], original['receipts'])
        self.assertAlmostEqual(snapshot(run, self.product)['remaining_seconds'], 3600)
        self.assertEqual(run['revisions'], original['revisions'])
        with self.assertRaises(Conflict):
            manual_run_retry(self.ledger, original['id'], original['version'])
        restarted = Scheduler(self.store).ledger.get('runs', run['id'])
        self.assertEqual(len(restarted['execution_grants']), 1)
        evidence = self.ledger.get('evidence', run['manual_retry_evidence_id'])
        self.assertEqual(evidence['details']['execution_grant'], run['execution_grants'][0])

    def test_watch_approved_repair_resumes_legacy_exhausted_task_once_after_restart(self):
        original = self.exhaust()
        self.apply()
        pending = self.fresh()
        self.assertNotIn('extra_execution_seconds', pending)
        self.assertEqual(quota_reason_for(self.ledger, pending, self.product, 'runs', True), '')
        restarted = Scheduler(self.store)
        self.assertTrue(c.resume_pending(restarted))
        run = self.fresh()
        self.assertEqual(run['status'], 'planning')
        self.assertFalse(run['plan_approved'])
        self.assertAlmostEqual(snapshot(run, self.product)['remaining_seconds'], 3600)
        self.assertEqual(run['execution_seconds'], original['execution_seconds'])
        before = run['extra_execution_seconds']
        run = c.consume_grant(restarted, run)
        run = c.consume_grant(Scheduler(self.store), run)
        self.assertEqual(run['extra_execution_seconds'], before)
        self.assertEqual(len(run['execution_grants']), 1)
        self.assertEqual(run['revisions'], 4)

    def test_waiting_for_tokens_or_off_peak_does_not_save_or_consume_time_grant(self):
        self.exhaust()
        self.apply()
        with patch.object(self.scheduler, 'tokens_available', return_value=False):
            self.assertFalse(c.resume_pending(self.scheduler))
        with patch('autopilot.off_peak.waiting', return_value={'reason': '等待错峰'}):
            self.assertFalse(c.resume_pending(self.scheduler))
        run = self.fresh()
        self.assertNotIn('execution_grants', run)
        self.assertEqual(run['revisions'], 3)

    def test_revoked_watch_authority_and_uncertain_calls_cannot_renew(self):
        self.product = self.ledger.update('products', self.product['id'], self.product['version'],
            {'coordinator': {'human_authority': True}})
        self.exhaust()
        self.apply()
        self.product = self.ledger.update('products', self.product['id'], self.product['version'],
            {'coordinator': {'human_authority': False}})
        self.assertFalse(c.resume_pending(self.scheduler))
        self.assertNotIn('execution_grants', self.fresh())
        self.assertIn('执行时间', quota_reason_for(self.ledger, self.fresh(), self.product, 'runs', True))
        run = self.fresh()
        run = self.ledger.update('runs', run['id'], run['version'], {'uncertain': True})
        with self.assertRaises(Conflict):
            manual_run_retry(self.ledger, run['id'], run['version'])
        self.assertNotIn('execution_grants', self.fresh())

    def test_automatic_failure_retry_never_renews_time_by_waiting(self):
        original = self.exhaust()
        run = recover(self.scheduler, 'runs', original, self.product, now=original['updated'] + 86400)
        self.assertEqual(run['status'], 'blocked')
        self.assertIn('执行时间', run['auto_retry_wait_reason'])
        self.assertNotIn('execution_grants', run)

    def test_automatic_repair_has_one_window_and_stops_at_revision_limit(self):
        run = self.exhaust()
        run = self.ledger.update('runs', run['id'], run['version'], {'revisions': 2}, 'verifying')
        run = self.scheduler.revise(run, self.product, '浏览器验证失败')
        self.assertEqual(run['revisions'], 3)
        self.assertAlmostEqual(snapshot(run, self.product)['remaining_seconds'], 3600)
        before = run['extra_execution_seconds']
        run = self.scheduler.revise(run, self.product, '验证仍失败')
        self.assertEqual(run['status'], 'blocked')
        self.assertEqual(run['extra_execution_seconds'], before)
        self.assertEqual(len(run['execution_grants']), 1)

    def test_manual_verification_authorization_renews_only_when_it_enters_repair(self):
        run = self.exhaust()
        run = self.ledger.update('runs', run['id'], run['version'], {'resume_status': 'verifying'})
        run = manual_run_retry(self.ledger, run['id'], run['version'])
        self.assertNotIn('execution_grants', run)
        run = self.scheduler.revise(run, self.product, '新的验证失败')
        self.assertAlmostEqual(snapshot(run, self.product)['remaining_seconds'], 3600)
        self.assertEqual(run['revisions'], 3)

    def test_call_timeout_uses_cap_and_smaller_remaining_budget_without_launching(self):
        self.product = self.ledger.update('products', self.product['id'], self.product['version'],
            {'executor': ['fake-executor']})
        for used, extra, expected in [(0, 0, 900), (3590, 0, 10), (4000, 3600, 900)]:
            run = self.fresh()
            run = self.ledger.update('runs', run['id'], run['version'],
                {'execution_seconds': used, 'extra_execution_seconds': extra}, 'planning')
            with patch.object(self.scheduler, 'start_call') as start:
                self.scheduler.advance(run)
            self.assertEqual(start.call_args.kwargs['timeout'], expected)

    def test_same_authorization_cannot_top_up_again_after_more_time_is_spent(self):
        run = self.exhaust()
        changes = renew(run, self.product, 'test-authorization', 'human')
        run |= changes
        run['execution_seconds'] += 900
        self.assertEqual(renew(run, self.product, 'test-authorization', 'human'), {})
        self.assertAlmostEqual(snapshot(run, self.product)['remaining_seconds'], 2700)


if __name__ == '__main__':
    unittest.main()
