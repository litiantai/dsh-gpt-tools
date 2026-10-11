"""手动提前合并及同日后续交付，Git 操作限定在临时仓库。"""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import test_staged_delivery
from review_core import Conflict
from autopilot.delivery import complete, create_batch, manual_merge_reason, tick
from autopilot.delivery_worker import paths
from autopilot.staged_delivery import execute, prepare
from autopilot.workspace import git


class ManualMergeTests(unittest.TestCase):
    def setUp(self):
        self.staged = test_staged_delivery.StagedDeliveryTests()
        self.staged.setUp()
        self.addCleanup(self.staged.tearDown)
        self.f = self.staged.f
        self.now = self.staged.day + 60
        self.f.github.api.side_effect = self.pr_api

    def pr_api(self, route, method='GET', body=None):
        pr = self.staged.pr_api(route, method, body)
        if not pr['merged']:
            for side in ('head', 'base'):
                pr[side]['sha'] = git(self.f.remote, 'rev-parse', pr[side]['ref'])
        return pr

    def request(self, batch):
        with patch('autopilot.delivery.time.time', return_value=self.now):
            return self.f.control.mutate(f"/deliveries/{batch['id']}/manual-merge", {'version': batch['version']})

    def collecting(self):
        return self.staged.review_and_merge(self.staged.bootstrap())

    def advance(self, batch, action):
        with patch.object(self.f.scheduler, 'start_call') as start:
            tick(self.f.scheduler, self.now)
            self.assertEqual(start.call_args.args[1]['id'], batch['id'])
            self.assertEqual(start.call_args.args[3], action)
        batch = self.f.ledger.get('deliveries', batch['id'])
        with patch('autopilot.delivery.time.time', return_value=self.now):
            result = execute(action, self.f.request(batch))
        self.assertEqual(result['status'], 'pass', result)
        complete(self.f.scheduler, batch, result, action)
        return self.f.ledger.get('deliveries', batch['id'])

    def test_manual_merge_updates_online_and_next_batch_delivers_same_day(self):
        f = self.f
        batch = self.collecting()
        run = f.accepted((batch['repository'], batch['head_sha']))
        with patch.object(f.scheduler, 'start_call'):
            tick(f.scheduler, self.now)
        batch = f.ledger.get('deliveries', batch['id'])
        with patch('autopilot.staged_delivery.time.time', return_value=self.now):
            result = prepare(f.request(batch, [run]))
        complete(f.scheduler, batch, result, 'prepare')
        batch = self.staged.review_and_merge(f.ledger.get('deliveries', batch['id']))
        f.github.merge.reset_mock()
        batch = f.ledger.update('deliveries', batch['id'], batch['version'], {'review_pass': None})
        queued = self.request(batch)
        self.assertLess(queued['cutoff'], batch['cutoff'])
        self.assertEqual(queued['status'], 'syncing_release')
        f.github.merge.assert_not_called()
        self.assertEqual(f.ledger.get('runs', run['id'])['status'], 'delivered')
        # A new result arrives while the earlier release is still queued for validation.
        later = f.accepted((batch['repository'], batch['head_sha']), 'value = 3\n')
        batch = self.advance(queued, 'sync_release')
        next_batch = create_batch(f.ledger, f.product, self.now)
        self.assertEqual(next_batch['sequence'], 2)
        self.assertEqual(f.ledger.get('runs', later['id'])['delivery_id'], next_batch['id'])
        self.assertNotEqual(paths(f.request(batch))[1], paths(f.request(next_batch))[1])
        with patch('autopilot.delivery_review.verify', return_value={'status': 'pass', 'checks': []}):
            batch = self.advance(batch, 'validate_release')
        self.assertIsNone(batch.get('review_pass'))
        self.assertFalse(any(row.get('phase') == 'review_release' for row in f.ledger.list('code_reviews')))
        f.github.checks_pass.return_value = False
        with patch('autopilot.delivery.time.time', return_value=self.now):
            waiting = execute('merge_release', f.request(batch))
        self.assertEqual(waiting['status'], 'busy')
        f.github.merge.assert_not_called()
        f.github.checks_pass.return_value = True
        batch = self.advance(batch, 'merge_release')
        self.assertEqual(batch['status'], 'online')
        self.assertEqual(f.ledger.get('runs', run['id'])['status'], 'online')
        self.assertEqual(f.ledger.get('requirements', run['requirement_id'])['status'], 'online')
        self.assertEqual(f.ledger.get('products', f.product['id'])['git_migration']['status'], 'completed')
        self.assertEqual(git(f.remote, 'rev-parse', 'master'), batch['merge_sha'])
        with patch.object(f.scheduler, 'start_call') as start:
            tick(f.scheduler, self.now)
            self.assertEqual(start.call_args.args[1]['id'], next_batch['id'])
        next_batch = f.ledger.get('deliveries', next_batch['id'])
        with patch('autopilot.staged_delivery.time.time', return_value=self.now):
            result = prepare(f.request(next_batch, [f.ledger.get('runs', later['id'])]))
        self.assertEqual(result['status'], 'pass', result)
        complete(f.scheduler, next_batch, result, 'prepare')
        next_batch = self.staged.review_and_merge(f.ledger.get('deliveries', next_batch['id']))
        self.assertNotEqual(next_batch['pr_url'], batch['pr_url'])
        self.assertEqual(next_batch['branch'], batch['branch'] + '-2')
        self.assertEqual(f.ledger.get('runs', later['id'])['status'], 'delivered')
        # Repeated calls reuse the open batch; another manual close gets a third batch.
        self.assertEqual(create_batch(f.ledger, f.product, self.now)['id'], next_batch['id'])
        self.request(next_batch)
        self.assertEqual(create_batch(f.ledger, f.product, self.now)['sequence'], 3)

    def test_failed_validation_does_not_merge_or_mark_online(self):
        batch = self.advance(self.request(self.collecting()), 'sync_release')
        self.f.github.merge.reset_mock()
        with patch.object(self.f.scheduler, 'start_call'):
            tick(self.f.scheduler, self.now)
        batch = self.f.ledger.get('deliveries', batch['id'])
        with patch('autopilot.delivery_review.verify', return_value={'status': 'fail', 'reason': '测试失败'}), patch('autopilot.delivery.time.time', return_value=self.now):
            result = execute('validate_release', self.f.request(batch))
        complete(self.f.scheduler, batch, result, 'validate_release')
        self.assertEqual(self.f.ledger.get('deliveries', batch['id'])['status'], 'release_failed')
        self.f.github.merge.assert_not_called()

    def test_stale_repeat_busy_and_incomplete_requests_are_rejected(self):
        batch = self.collecting()
        for changes in ({'call': {'id': 'running'}}, {'pending_result': {'action': 'sync_release'}},
                        {'uncertain': True}, {'run_ids': ['unmerged']}, {'status': 'preparing'}, {'pr_url': None}):
            with self.subTest(changes=changes):
                self.assertTrue(manual_merge_reason(batch | changes, self.f.product))
        self.assertTrue(manual_merge_reason(batch, self.f.product | {'status': 'paused'}))
        batch = self.f.ledger.update('deliveries', batch['id'], batch['version'], {'call': {'id': 'running'}})
        with self.assertRaises(Conflict):
            self.request(batch)
        batch = self.f.ledger.update('deliveries', batch['id'], batch['version'], {'call': None})
        queued = self.request(batch)
        with self.assertRaises(Conflict):
            self.request(batch)
        with self.assertRaises(Conflict):
            self.request(queued)

    def test_legacy_batch_uses_existing_sync_and_merge_flow(self):
        batch = create_batch(self.f.ledger, self.f.product, self.now)
        batch = self.f.ledger.update('deliveries', batch['id'], batch['version'],
            {'flow': 'legacy', 'pr_url': 'https://github.com/example/repo/pull/1'}, 'awaiting_merge')
        self.assertEqual(self.request(batch)['status'], 'syncing')

    def test_board_exposes_version_and_disables_already_requested_merge(self):
        batch = self.collecting()
        pr = self.staged.prs[batch['pr_number']] | {'title': batch['title'], 'updated_at': '2026-10-09T02:00:00Z'}
        with patch('autopilot.delivery_board.GitHub') as github:
            github.return_value.api.return_value = [pr]
            row = self.f.control.delivery_board.get(self.f.product)['release_prs'][0]
            self.assertTrue(row['manual_merge_allowed'])
            self.assertEqual(row['delivery_version'], batch['version'])
            self.request(batch)
            row = self.f.control.delivery_board.get(self.f.product)['release_prs'][0]
            self.assertFalse(row['manual_merge_allowed'])
            self.assertIn('已提交', row['manual_merge_reason'])

    def test_release_retry_resumes_failed_phase_without_reclosing_batch(self):
        batch = self.request(self.collecting())
        batch = self.f.ledger.update('deliveries', batch['id'], batch['version'],
            {'resume_status': 'reviewing_release', 'next_auto_retry_at': self.now + 3600, 'reason': '模型调用失败'}, 'blocked')
        pr = self.staged.prs[batch['pr_number']] | {'title': batch['title'], 'updated_at': '2026-10-09T02:00:00Z'}
        with patch('autopilot.delivery_board.GitHub') as github:
            github.return_value.api.return_value = [pr]
            row = self.f.control.delivery_board.get(self.f.product)['release_prs'][0]
            self.assertTrue(row['retryable'])
            self.assertFalse(row['manual_merge_allowed'])
            result = self.f.control.mutate(f"/deliveries/{batch['id']}/retry", {'version': row['delivery_version']})
            self.assertEqual(result['status'], 'syncing_release')
            self.assertEqual(result['manual_merge_at'], batch['manual_merge_at'])
            self.assertEqual(result['cutoff'], batch['cutoff'])
            self.assertEqual(result['pr_url'], batch['pr_url'])
            self.assertIsNone(result['next_auto_retry_at'])
            self.assertFalse(self.f.control.delivery_board.get(self.f.product)['release_prs'][0]['retryable'])
            with self.assertRaises(Conflict):
                self.f.control.mutate(f"/deliveries/{batch['id']}/retry", {'version': batch['version']})

    def test_release_retry_rejects_unfinished_or_uncertain_execution(self):
        batch = self.collecting()
        pr = self.staged.prs[batch['pr_number']] | {'title': batch['title'], 'updated_at': '2026-10-09T02:00:00Z'}
        with patch('autopilot.delivery_board.GitHub') as github:
            github.return_value.api.return_value = [pr]
            for values in ({'call': {'id': 'running'}}, {'pending_result': {'action': 'review_release'}}, {'uncertain': True}):
                with self.subTest(values=values):
                    batch = self.f.ledger.update('deliveries', batch['id'], batch['version'],
                        {'call': None, 'pending_result': None, 'uncertain': False, 'resume_status': 'reviewing_release'} | values, 'blocked')
                    row = self.f.control.delivery_board.get(self.f.product)['release_prs'][0]
                    self.assertFalse(row['retryable'])
                    self.assertTrue(row['retry_reason'])
                    with self.assertRaises(Conflict):
                        self.f.control.mutate(f"/deliveries/{batch['id']}/retry", {'version': batch['version']})


if __name__ == '__main__':
    unittest.main()
