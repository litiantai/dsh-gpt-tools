"""人工信号入池、并发去重与持久化插队调度。"""
import concurrent.futures
from contextlib import ExitStack
import datetime
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'dsh-gpt-supervisor/scripts')]
from review_core import Store, Conflict
from autopilot.api import Control
from autopilot.scheduler import Scheduler


class ManualRequirementsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / 'state')
        self.control = Control(self.store)
        self.ledger = self.control.ledger
        self.product = self.ledger.create('products', {'name': '人工入池测试'}, 'active')
        self.signal = self.ledger.signal(self.product['id'], {
            'source': 'inspection', 'code': 'manual-test', 'summary': '通知能力实现',
            'evidence': {'actual': '通知没有展示', 'evidence_id': 'evidence-original'},
        })
        self.body = {'version': self.signal['version'], 'title': '通知能力实现',
                     'evidence': '通知没有展示', 'impact': '无法看到任务结果',
                     'acceptance': ['完成后展示通知']}

    def promote(self, signal=None, **changes):
        signal = signal or self.signal
        return self.control.mutate(f"/signals/{signal['id']}/promote",
                                   self.body | {'version': signal['version']} | changes)

    def test_promote_retains_evidence_and_high_priority_through_queue(self):
        requirement = self.promote(title='  通知能力实现  ', evidence='token=secret 通知没有展示')
        self.assertEqual(requirement['title'], '通知能力实现')
        self.assertNotIn('secret', requirement['evidence'])
        self.assertEqual(requirement['status'], 'pending')
        self.assertEqual(requirement['signal_ids'], [self.signal['id']])
        self.assertEqual(requirement['product_id'], self.product['id'])
        self.assertEqual(requirement['priority'], 0)
        self.assertTrue(requirement['queue_first'])
        self.assertEqual(requirement['requirement_day'], datetime.datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat())
        signal = self.ledger.get('signals', self.signal['id'])
        self.assertEqual(signal['status'], 'classified')
        self.assertEqual(signal['evidence'], self.signal['evidence'])
        self.assertEqual(signal['requirement_ids'], [requirement['id']])
        self.assertEqual(signal['manual_requirement_id'], requirement['id'])
        self.assertFalse(signal['attribution_pending'])
        run = self.control.queue(requirement)
        self.assertEqual(run['priority'], 0)
        self.assertTrue(run['queue_first'])
        self.assertEqual(run['queue_first_at'], requirement['queue_first_at'])
        self.assertEqual(self.ledger.get('requirements', requirement['id'])['run_id'], run['id'])

    def test_concurrent_promotion_creates_only_one_requirement(self):
        def request():
            try:
                return self.promote()['id']
            except Conflict:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: request(), range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.ledger.list('requirements')), 1)
        with self.assertRaises(Conflict):
            self.promote(self.ledger.get('signals', self.signal['id']))

    def test_invalid_stale_or_classified_signal_cannot_create_requirement(self):
        for changes in ({'title': ' '}, {'evidence': {}}, {'impact': ''},
                        {'acceptance': []}, {'acceptance': [' ']}, {'acceptance': 'text'},
                        {'acceptance': [1]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.promote(**changes)
        changed = self.ledger.update('signals', self.signal['id'], self.signal['version'], {}, 'reviewed')
        with self.assertRaises(Conflict):
            self.promote()
        with self.assertRaises(Conflict):
            self.promote(changed)
        self.assertEqual(self.ledger.list('requirements'), [])

    def test_link_failure_rolls_back_requirement_creation(self):
        with patch.object(self.ledger, 'update', side_effect=RuntimeError('failed link')):
            with self.assertRaises(RuntimeError):
                self.promote()
        self.assertEqual(self.ledger.list('requirements'), [])
        self.assertEqual(self.ledger.get('signals', self.signal['id']), self.signal)

    def test_late_automatic_discovery_does_not_duplicate_manual_requirement(self):
        requirement = self.promote()
        candidate = self.body | {'title': '自动归因的另一名称', 'signal_ids': [self.signal['id']],
                                 'reproduction': '任务完成后查看通知', 'classification': 'development', 'in_scope': True}
        scheduler = Scheduler(self.store)
        result = scheduler.accept_discovery(self.product, {'requirements': [candidate]},
                                            [self.signal['id']], '2026-10-09', 'report-test')
        self.assertEqual(result, [])
        self.assertEqual(self.ledger.list('requirements'), [requirement])
        self.assertEqual(self.ledger.list('runs'), [])

    def normal_run(self, title):
        requirement = self.control.mutate('/requirements', self.body | {
            'product_id': self.product['id'], 'title': title})
        return self.control.queue(requirement)

    def dispatch(self):
        # Keep actual queue selection and persistent records; isolate unrelated background work.
        scheduler = Scheduler(self.store)
        with ExitStack() as stack:
            for name in ('autopilot.onboarding.tick', 'autopilot.usage.collect', 'autopilot.delivery.tick',
                         'autopilot.daily.tick', 'autopilot.progress.investigate', 'autopilot.progress.recover_review'):
                stack.enter_context(patch(name, return_value=False))
            stack.enter_context(patch.object(scheduler, 'monitor'))
            advance = stack.enter_context(patch.object(scheduler, 'advance'))
            scheduler.tick()
            return advance.call_args.args[0] if advance.called else None

    def test_manual_requirements_stay_ahead_of_older_and_newer_normal_runs(self):
        ordinary = self.normal_run('较早普通任务')
        first = self.control.queue(self.promote())
        second_signal = self.ledger.signal(self.product['id'], {
            'source': 'feedback', 'code': 'second', 'summary': '另一个紧急问题', 'evidence': '证据'})
        second = self.control.queue(self.promote(second_signal, title='另一个紧急问题'))
        self.normal_run('较新普通任务')
        # Recreated scheduler reads the persisted order, even after more ordinary work arrives.
        self.assertEqual(self.dispatch()['id'], second['id'])
        self.ledger.update('runs', second['id'], second['version'], {}, 'accepted')
        with self.store.transaction() as db:
            db.execute('DELETE FROM auto_leases')
        self.assertEqual(self.dispatch()['id'], first['id'])
        self.ledger.update('runs', first['id'], first['version'], {}, 'accepted')
        with self.store.transaction() as db:
            db.execute('DELETE FROM auto_leases')
        self.assertEqual(self.dispatch()['id'], ordinary['id'])

    def test_manual_priority_does_not_preempt_running_task(self):
        running = self.normal_run('执行中的任务')
        self.ledger.update('runs', running['id'], running['version'], {}, 'developing')
        self.control.queue(self.promote())
        self.assertEqual(self.dispatch()['id'], running['id'])

    def test_paused_project_does_not_dispatch_manual_requirement(self):
        self.control.queue(self.promote())
        self.ledger.update('products', self.product['id'], self.product['version'], {}, 'paused')
        self.assertIsNone(self.dispatch())


if __name__ == '__main__':
    unittest.main()
