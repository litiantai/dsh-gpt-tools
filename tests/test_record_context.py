"""关联详情必须按真实引用补全，并保留跨项目隔离与缺失提示。"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'dsh-gpt-supervisor/scripts')]
from review_core import Store
from autopilot.api import Control


class RecordContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.control = Control(Store(Path(self.tmp.name), {'home': self.tmp.name}))
        self.ledger = self.control.ledger
        self.pid = self.ledger.create('products', {})['id']

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, kind, **data):
        return self.ledger.create(kind, {'product_id': self.pid, **data})

    def context(self, kind, record):
        return self.control.get(f"/{kind}/{record['id']}/context")

    def ids(self, context):
        return {item['record']['id'] for item in context['related']}

    def test_requirement_finds_reverse_task_and_source_without_batch_siblings(self):
        signal = self.make('signals')
        req = self.make('requirements', signal_ids=[signal['id']])
        run = self.make('runs', requirement_id=req['id'], receipts=[{'action':'verify','result':{'checks':[{'name':'真实检查','status':'pass'}]}}])
        other = self.make('runs')
        batch = self.make('deliveries', run_ids=[run['id'], other['id']])
        data = self.context('requirements', req)
        self.assertEqual(self.ids(data), {signal['id'], run['id']})
        task_context = self.context('runs', run)
        self.assertIn(batch['id'], self.ids(task_context))
        self.assertNotIn(other['id'], self.ids(task_context))

    def test_foreign_and_missing_references_are_not_exposed(self):
        foreign = self.make('runs', product_id='foreign', reason='不可展示')
        req = self.make('requirements', run_id=foreign['id'], signal_ids=['missing'])
        self.make('runs', product_id='foreign', requirement_id=req['id'])
        data = self.context('requirements', req)
        self.assertEqual(data['related'], [])
        self.assertEqual({m['id'] for m in data['missing']}, {foreign['id'], 'missing'})

    def test_json_references_and_archived_receipt_evidence(self):
        run = self.make('runs', receipts=[{'call_id':'old-call','action':'verify','result':{'status':'pass'}}])
        call = self.make('evidence', call_id='old-call', details={'status':'pass'})
        saved = self.make('evidence', run_id=run['id'], details={'checks':[{'status':'blocked'}]})
        self.make('evidence', call_id='other-call')
        data = self.context('runs', run)
        self.assertEqual(self.ids(data), {call['id'], saved['id']})
        req = self.make('requirements', evidence=json.dumps({'evidence_id':saved['id']}))
        self.assertEqual(self.ids(self.context('requirements', req)), {saved['id']})

    def test_inspection_steps_support_forward_and_reverse_links(self):
        step = self.make('evidence', actual='历史步骤')
        inspection = self.make('inspections', steps=[step['id'], 'lost-step'])
        reverse = self.make('evidence', inspection_id=inspection['id'])
        data = self.context('inspections', inspection)
        self.assertEqual(self.ids(data), {step['id'], reverse['id']})
        self.assertEqual(data['missing'], [{'kind':'evidence','id':'lost-step'}])

    def test_context_is_not_limited_to_latest_thousand_records(self):
        old = self.make('signals')
        with self.ledger.store.transaction() as db:
            for index in range(1001):
                self.ledger.create('signals', {'product_id':self.pid}, ident=f'new-{index}', db=db)
        req = self.make('requirements', signal_ids=[old['id']])
        self.assertEqual(self.ids(self.context('requirements', req)), {old['id']})


if __name__ == '__main__':
    unittest.main()
