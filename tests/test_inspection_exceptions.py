"""巡检异常留档、历史转换与去重状态验证。"""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'dsh-gpt-supervisor/scripts')]
from review_core import Store
from autopilot.store import Ledger


class InspectionExceptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name)/'state')
        self.ledger = Ledger(self.store)
        self.product = self.ledger.create('products', {'name': '异常留档'})

    def signal(self, status, **changes):
        return self.ledger.signal(self.product['id'], {'source': 'inspection', 'code': 'PROJECT_HEALTH',
            'summary': '项目运行巡检', 'evidence': {'status': status, 'reason': 'timed out'}} | changes)

    def test_failures_are_archived_without_hiding_feedback_or_observations(self):
        for status in ('blocked', 'fail', 'failed', 'error'):
            with self.subTest(status=status):
                record = self.signal(status, version=status)
                self.assertEqual(record['status'], 'abnormal')
                self.assertEqual(record['evidence']['status'], status)
        self.assertEqual(self.ledger.metrics()['pending_signals'], 0)
        self.assertEqual(self.signal('fail', source='feedback')['status'], 'pending')
        self.assertEqual(self.signal('observed')['status'], 'pending')

    def test_dedup_changes_pending_state_but_preserves_classified_links(self):
        record = self.signal('pass')
        failed = self.signal('blocked')
        self.assertEqual(failed['id'], record['id'])
        self.assertEqual(failed['status'], 'abnormal')
        self.assertEqual(failed['count'], 2)
        recovered = self.signal('pass')
        self.assertEqual(recovered['status'], 'pending')
        self.ledger.update('signals', recovered['id'], recovered['version'], {'requirement_id': 'req'}, 'classified')
        classified = self.signal('blocked')
        self.assertEqual(classified['status'], 'classified')
        self.assertEqual(classified['requirement_id'], 'req')

    def test_existing_pending_failures_migrate_once_and_keep_evidence(self):
        record = self.signal('blocked')
        feedback = self.signal('blocked', source='feedback')
        with self.store.connect() as db:
            db.execute("UPDATE auto_signals SET status='pending' WHERE id=?", (record['id'],))
        migrated = Ledger(self.store).get('signals', record['id'])
        self.assertEqual(migrated['status'], 'abnormal')
        self.assertEqual(migrated['evidence'], record['evidence'])
        self.assertEqual(migrated['created'], record['created'])
        self.assertEqual(Ledger(self.store).get('signals', record['id'])['version'], migrated['version'])
        self.assertEqual(self.ledger.get('signals', feedback['id'])['status'], 'pending')

    def test_current_ledger_can_be_constructed_inside_a_write_transaction(self):
        self.signal('blocked')
        self.signal('observed', version='observed')
        with self.store.transaction() as db:
            record = self.ledger.create('requirements', {'product_id': self.product['id'], 'title': '事务内需求'}, db=db)
            nested = Ledger(self.store)
            self.assertEqual(nested.get('requirements', record['id'], db)['title'], '事务内需求')


if __name__ == '__main__':
    unittest.main()
