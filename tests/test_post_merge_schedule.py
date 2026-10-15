"""Git 合并后持久排队、主实例更新优先级与旧回执隔离。"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'dsh-gpt-supervisor/scripts')]

from review_core import Store
from autopilot.delivery import mark_online
from autopilot.scheduler import Scheduler
from autopilot.store import Ledger


class PostMergeScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'state')
        self.scheduler = Scheduler(self.store)
        self.ledger = self.scheduler.ledger
        self.product = self.ledger.create('products', {
            'name': 'test', 'source': str(self.root), 'adapter': ['fixture'],
            'delivery_repository': str(self.root / 'repository'),
            'git': {'enabled': True, 'base_branch': 'master'},
            'application': str(self.root / 'app'), 'runtime': str(self.root / 'runtime'),
            'app_support': str(self.root / 'support'),
            'last_probe': 1000, 'last_inspect': 1000, 'last_master_sync': 1000,
        }, 'active')

    def tearDown(self):
        self.temp.cleanup()

    def product_now(self):
        return self.ledger.get('products', self.product['id'])

    def update_product(self, changes, status=None):
        product = self.product_now()
        return self.ledger.update('products', product['id'], product['version'], changes, status)

    def batch(self, bootstrap=False):
        requirement = self.ledger.create('requirements', {
            'product_id': self.product['id'], 'title': 'merged change',
        }, 'delivered')
        run = self.ledger.create('runs', {
            'product_id': self.product['id'], 'requirement_id': requirement['id'],
        }, 'delivered')
        batch = self.ledger.create('deliveries', {
            'product_id': self.product['id'], 'bootstrap': bootstrap,
            'repository': str(self.root / 'new-repository'), 'integrated_ids': [run['id']],
        }, 'merging')
        return batch, run, requirement

    def merge(self, sha='merged', bootstrap=False, at=1000):
        batch, run, requirement = self.batch(bootstrap)
        with patch('autopilot.delivery.time.time', return_value=at):
            mark_online(self.ledger, batch, {'merged': True, 'merge_sha': sha})
        return batch, run, requirement

    def call(self, sha='merged', action='master_sync'):
        call = {'id': 'fixture-' + action, 'action': action, 'started': 1000,
                'path': str(self.root / 'request.json'), 'master_update_merge_sha': sha}
        return self.update_product({'call': call})

    def receipt(self, result, sha='merged', at=1001):
        product = self.call(sha)
        with patch.object(self.scheduler, 'call_result', return_value=result), \
                patch('autopilot.scheduler.time.time', return_value=at):
            self.scheduler.monitor(product)
        return self.product_now()

    def test_merge_atomically_records_online_and_durable_instance_update(self):
        batch, run, requirement = self.merge()
        self.assertEqual(self.ledger.get('deliveries', batch['id'])['status'], 'online')
        self.assertEqual(self.ledger.get('runs', run['id'])['status'], 'online')
        self.assertEqual(self.ledger.get('requirements', requirement['id'])['merge_sha'], 'merged')
        expected = {'delivery_id': batch['id'], 'merge_sha': 'merged', 'status': 'pending',
                    'requested_at': 1000, 'next_attempt': 0,
                    'reason': '等待拉取最新 master 并重建主实例'}
        self.assertEqual(Ledger(Store(self.store.state)).get('products', self.product['id'])['master_update'], expected)

    def test_bootstrap_updates_migration_and_queue_in_one_product_version(self):
        before = self.product_now()
        batch, _, _ = self.merge(bootstrap=True)
        product = self.product_now()
        self.assertEqual(product['version'], before['version'] + 1)
        self.assertEqual(product['git_migration'], {
            'status': 'completed', 'delivery_id': batch['id'], 'merge_sha': 'merged'})
        self.assertEqual(product['delivery_repository'], batch['repository'])
        self.assertEqual(product['master_update']['status'], 'pending')

    def test_merge_enables_only_implemented_master_capabilities_for_self_deployment(self):
        spec = {'version': 1, 'kind': 'command',
                'capabilities': ['probe', 'inspect', 'verify', 'investigate'], 'protocol': 'existing'}
        before = self.update_product({'adapter_spec': spec, 'deployment': {'kind': 'self'}})
        self.merge()
        product = self.product_now()
        self.assertEqual(product['version'], before['version'] + 1)
        self.assertEqual(product['adapter_spec'], spec | {
            'capabilities': ['probe', 'inspect', 'verify', 'investigate', 'master_sync', 'final_acceptance']})
        with patch('autopilot.scheduler.subprocess.Popen', return_value=Mock(pid=123)), \
                patch('autopilot.scheduler.time.time', return_value=1001):
            self.assertTrue(self.scheduler.master_step(product))
        self.assertEqual(self.product_now()['call']['action'], 'master_sync')

    def test_merge_preserves_existing_capabilities_order_and_complete_spec(self):
        for capabilities, expected in (
            (['final_acceptance', 'verify'], ['final_acceptance', 'verify', 'master_sync']),
            (['master_sync', 'verify'], ['master_sync', 'verify', 'final_acceptance']),
            (['final_acceptance', 'verify', 'master_sync'], ['final_acceptance', 'verify', 'master_sync']),
        ):
            with self.subTest(capabilities=capabilities):
                spec = {'version': 1, 'kind': 'command', 'capabilities': capabilities, 'protocol': 'existing'}
                before = self.update_product({'adapter_spec': spec, 'deployment': {'kind': 'self'}})
                self.merge()
                product = self.product_now()
                self.assertEqual(product['adapter_spec'], spec | {'capabilities': expected})
                self.assertEqual(product['version'], before['version'] + 1)

    def test_merge_does_not_expand_capabilities_without_command_self_deployment(self):
        cases = [
            ({'kind': 'command', 'capabilities': ['verify']}, {'kind': 'external'}),
            ({'kind': 'legacy', 'capabilities': ['verify']}, {'kind': 'self'}),
            ({'kind': 'command', 'capabilities': ['verify']}, None),
            ({'kind': 'command', 'capabilities': ['verify']}, {}),
            (None, {'kind': 'self'}),
        ]
        for spec, deployment in cases:
            with self.subTest(spec=spec, deployment=deployment):
                before = self.update_product({'adapter_spec': spec, 'deployment': deployment})
                self.merge()
                product = self.product_now()
                self.assertEqual(product['adapter_spec'], spec)
                self.assertEqual(product['deployment'], deployment)
                self.assertEqual(product['version'], before['version'] + 1)

    def test_merge_transaction_rolls_back_all_statuses_when_association_is_invalid(self):
        batch, run, requirement = self.batch()
        self.ledger.update('runs', run['id'], run['version'], {'requirement_id': 'missing'})
        with self.assertRaises(KeyError):
            mark_online(self.ledger, batch, {'merged': True, 'merge_sha': 'merged'})
        self.assertEqual(self.ledger.get('deliveries', batch['id'])['status'], 'merging')
        self.assertEqual(self.ledger.get('runs', run['id'])['status'], 'delivered')
        self.assertEqual(self.ledger.get('requirements', requirement['id'])['status'], 'delivered')
        self.assertNotIn('master_update', self.product_now())

    def test_replayed_merge_does_not_reset_update_or_replace_later_request(self):
        batch, _, _ = self.merge()
        finished = self.product_now()['master_update'] | {'status': 'pass', 'completed_at': 1001}
        product = self.update_product({'master_update': finished})
        before_batch = self.ledger.get('deliveries', batch['id'])
        mark_online(self.ledger, batch, {'merged': True, 'merge_sha': 'merged'})
        self.assertEqual(self.product_now(), product)
        self.assertEqual(self.ledger.get('deliveries', batch['id']), before_batch)
        later, _, _ = self.merge('later', at=1002)
        product = self.product_now()
        mark_online(self.ledger, batch, {'merged': True, 'merge_sha': 'merged'})
        self.assertEqual(self.product_now(), product)
        self.assertEqual(product['master_update']['delivery_id'], later['id'])

    def test_pending_update_runs_before_due_probe_or_runtime_recovery(self):
        self.merge()
        for recovering in (False, True):
            with self.subTest(recovering=recovering):
                product = self.product_now() | {'last_probe': 0,
                    'runtime_recovery': {'enabled': recovering}}
                with patch('autopilot.scheduler.time.time', return_value=1001), \
                        patch.object(self.scheduler, 'start_call') as start:
                    self.scheduler.monitor(product)
                self.assertEqual(start.call_count, 1)
                self.assertEqual(start.call_args.args[3], 'master_sync')
                self.assertEqual(start.call_args.kwargs['timeout'], 14400)

    def test_observing_advances_queue_but_pause_and_disabled_do_not_start(self):
        self.merge()
        for status, disabled, expected in [('observing', False, True),
                                          ('paused', False, False), ('active', True, False)]:
            with self.subTest(status=status, disabled=disabled):
                product = self.product_now() | {'status': status, 'automation_disabled': disabled}
                with patch('autopilot.scheduler.time.time', return_value=1001), \
                        patch.object(self.scheduler, 'start_call') as start:
                    self.scheduler.monitor(product)
                    if not expected:
                        self.assertFalse(self.scheduler.master_step(product))
                self.assertEqual(start.called, expected)
                if expected:
                    self.assertEqual(start.call_args.args[3], 'master_sync')

    def test_queue_retry_time_prevents_early_retry_and_final_acceptance(self):
        self.merge()
        self.update_product({'master_update': self.product_now()['master_update'] | {
            'status': 'busy', 'next_attempt': 1060, 'reason': '会话仍活跃'},
            'last_master_sync_result': {'status': 'pass'}})
        with patch.object(self.scheduler, 'start_call') as start, \
                patch('autopilot.master.pending', return_value=[{'id': 'requirement'}]), \
                patch('autopilot.scheduler.time.time', return_value=1059):
            self.assertFalse(self.scheduler.master_step(self.product_now()))
            self.scheduler.monitor(self.product_now())
            start.assert_not_called()
        with patch.object(self.scheduler, 'start_call') as start, \
                patch('autopilot.scheduler.time.time', return_value=1060):
            self.assertTrue(self.scheduler.master_step(self.product_now()))
            self.assertEqual(start.call_args.args[3], 'master_sync')

    def test_periodic_sync_and_final_acceptance_remain_available(self):
        with patch.object(self.scheduler, 'start_call') as start, \
                patch('autopilot.scheduler.time.time', return_value=1060):
            self.assertTrue(self.scheduler.master_step(self.product_now()))
            self.assertEqual(start.call_args.args[3], 'master_sync')
            self.assertEqual(start.call_args.kwargs['timeout'], 14400)
        product = self.update_product({'last_master_sync_result': {'status': 'pass'}})
        with patch.object(self.scheduler, 'start_call') as start, \
                patch('autopilot.master.pending', return_value=[{'id': 'requirement'}]), \
                patch('autopilot.scheduler.time.time', return_value=1001):
            self.assertTrue(self.scheduler.master_step(product))
            self.assertEqual(start.call_args.args[3], 'final_acceptance')

    def test_ongoing_call_is_consumed_before_new_update_starts(self):
        self.merge()
        product = self.call(action='probe')
        with patch.object(self.scheduler, 'call_result', return_value=None), \
                patch.object(self.scheduler, 'start_call') as start:
            self.scheduler.monitor(product)
            self.assertFalse(self.scheduler.master_step(product))
            start.assert_not_called()

    def test_pass_requires_matching_worker_sha_and_running_instance_commit(self):
        self.merge()
        for result in (
            {'status': 'pass', 'master_update_merge_sha': 'merged'},
            {'status': 'pass', 'instance': {'commit': 'latest'}},
            {'status': 'pass', 'master_update_merge_sha': 'old', 'instance': {'commit': 'latest'}},
        ):
            with self.subTest(result=result):
                product = self.receipt(result)
                self.assertNotEqual(product['master_update']['status'], 'pass')
        product = self.receipt({'status': 'pass', 'master_update_merge_sha': 'merged',
                                'instance': {'commit': 'latest'}})
        self.assertEqual(product['master_update']['status'], 'pass')
        self.assertEqual(product['master_update']['next_attempt'], 0)
        self.assertEqual(product['master_environment']['commit'], 'latest')
        completed = product['master_update']
        replay = self.receipt({'status': 'pass', 'master_update_merge_sha': 'merged',
                              'instance': {'commit': 'latest'}}, at=1002)
        self.assertEqual(replay['master_update'], completed)

    def test_busy_blocked_and_fail_preserve_online_and_retry_after_restart(self):
        batch, _, _ = self.merge()
        for status in ('busy', 'blocked', 'fail'):
            with self.subTest(status=status):
                product = self.receipt({'status': status, 'reason': '等待处理 ' + status,
                                        'master_update_merge_sha': 'merged'})
                update = product['master_update']
                self.assertEqual(update['status'], status)
                self.assertEqual(update['reason'], '等待处理 ' + status)
                self.assertEqual(update['next_attempt'], 1061)
                self.assertEqual(self.ledger.get('deliveries', batch['id'])['status'], 'online')
                restarted = Scheduler(Store(self.store.state))
                with patch.object(restarted, 'start_call') as start, \
                        patch('autopilot.scheduler.time.time', return_value=1060):
                    self.assertFalse(restarted.master_step(product))
                    start.assert_not_called()

    def test_executor_failure_uses_persisted_call_sha_and_cannot_complete_without_worker_sha(self):
        self.merge()
        product = self.receipt({'status': 'blocked', 'reason': '执行进程已失联'})
        self.assertEqual(product['master_update']['status'], 'blocked')
        self.assertEqual(product['master_update']['reason'], '执行进程已失联')
        self.assertEqual(product['master_update']['next_attempt'], 1061)

    def test_old_inflight_receipts_never_modify_newer_merge_queue(self):
        self.merge()
        self.merge('newer', at=1001)
        update = self.product_now()['master_update']
        for result in (
            {'status': 'pass', 'master_update_merge_sha': 'merged', 'instance': {'commit': 'merged'}},
            {'status': 'busy', 'master_update_merge_sha': 'merged', 'reason': '旧会话仍忙'},
            {'status': 'blocked', 'reason': '旧调用已失联'},
        ):
            with self.subTest(result=result):
                product = self.receipt(result, sha='merged')
                self.assertEqual(product['master_update'], update)

    def test_dispatched_call_durably_records_merge_request_and_build_timeout(self):
        self.merge()
        child = Mock(pid=123)
        with patch('autopilot.scheduler.subprocess.Popen', return_value=child), \
                patch('autopilot.scheduler.time.time', return_value=1001):
            self.assertTrue(self.scheduler.master_step(self.product_now()))
        product = self.product_now()
        self.assertEqual(product['call']['master_update_merge_sha'], 'merged')
        request = json.loads(Path(product['call']['path']).read_text())
        self.assertEqual(request['timeout'], 14400)
        self.assertEqual(request['input']['product']['master_update']['merge_sha'], 'merged')


if __name__ == '__main__':
    unittest.main()
