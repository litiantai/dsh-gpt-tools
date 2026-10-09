"""交付任务关联与用量归属的回归验证。"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'dsh-gpt-supervisor/scripts')]
from review_core import Store
from autopilot.api import Control
from autopilot.usage import register, collect


class DeliveryTasksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.control = Control(Store(Path(self.tmp.name), {'home': self.tmp.name}))
        self.ledger = self.control.ledger
        self.product = self.ledger.create('products', {'policy': {'tokens_per_day': 1000}})
        self.pid = self.product['id']

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, kind, **data):
        return self.ledger.create(kind, {'product_id': self.pid, **data})

    def test_batch_membership_discovery_and_per_task_prs(self):
        inspection = self.make('inspections', title='巡检')
        evidence = self.make('evidence', inspection_id=inspection['id'])
        signal = self.make('signals', inspection_id=inspection['id'])
        requirement = self.make('requirements', signal_ids=[signal['id']], title='问题')
        run = self.make('runs', requirement_id=requirement['id'], created=1)
        unrelated = self.make('runs', title='同日无关任务')
        foreign = self.make('runs', product_id='other', title='其他项目')
        batch = self.make('deliveries', run_ids=[run['id'], foreign['id'], 'missing'], integrated_ids=[run['id']],
                          feature_prs=[{'run_ids':[run['id']], 'pr_url':'https://github.com/a/b/pull/1'},
                                       {'run_ids':[unrelated['id']], 'pr_url':'https://github.com/a/b/pull/2'}],
                          pr_url='https://github.com/a/b/pull/3')
        result = self.control.get(f"/deliveries/{batch['id']}/tasks")
        self.assertEqual(len(result['tasks']), 1)
        task = result['tasks'][0]
        self.assertEqual(task['run']['id'], run['id'])
        self.assertEqual(task['requirement']['title'], '问题')
        self.assertEqual([s['id'] for s in task['signals']], [signal['id']])
        self.assertEqual([e['id'] for e in task['evidence']], [evidence['id']])
        self.assertEqual([p['pr_url'].split('/')[-1] for p in task['prs']], ['1','3'])
        self.assertEqual(set(result['missing_ids']), {'missing', foreign['id']})

    def test_usage_counts_registered_and_legacy_sources_once_without_shared_or_foreign_cost(self):
        root = Path(self.tmp.name)
        legacy = root / 'legacy' / 'trace.jsonl'
        run = self.make('runs', receipts=[{'result': {'evidence': str(legacy.parent)}},
                                          {'result': {'evidence': str(legacy.parent)}}])
        for name, count, owner, pid in [('current', 120, run['id'], self.pid),
                                       ('legacy', 80, None, self.pid),
                                       ('shared', 500, 'batch', self.pid),
                                       ('foreign', 900, run['id'], 'other')]:
            path = root / name / 'trace.jsonl'
            path.parent.mkdir()
            path.write_text(json.dumps({'type':'turn.completed','usage':{'input_tokens':count,'output_tokens':10}})+'\n')
            register(self.ledger, pid, path, record_id=owner, action='develop')
        register(self.ledger, self.pid, root / 'pending.jsonl', record_id=run['id'], action='verify')
        collect(self.ledger)
        collect(self.ledger)
        result = self.control.get(f"/runs/{run['id']}/usage")
        self.assertEqual(result['tokens'], 220)
        self.assertEqual(len(result['sources']), 3)
        self.assertEqual(sum(s['collected'] for s in result['sources']), 2)
        self.assertEqual(result['project_tokens_used'], 730)
        self.assertEqual(result['project_tokens_limit'], 1000)
        self.assertEqual(len(result['receipts']), 2)

    def test_missing_usage_is_distinct_from_recorded_zero(self):
        run = self.make('runs')
        result = self.control.get(f"/runs/{run['id']}/usage")
        self.assertEqual(result['sources'], [])
        self.assertEqual(result['tokens'], 0)

    def test_task_evidence_survives_receipt_trimming_and_forward_only_inspection_steps(self):
        step = self.make('evidence', title='历史步骤')
        inspection = self.make('inspections', steps=[step['id']])
        req = self.make('requirements', inspection_id=inspection['id'])
        run = self.make('runs', requirement_id=req['id'], receipts=[])
        receipt = self.make('evidence', run_id=run['id'], title='持久化任务回执')
        self.make('evidence', run_id='unrelated')
        batch = self.make('deliveries', run_ids=[run['id']])
        task = self.control.get(f"/deliveries/{batch['id']}/tasks")['tasks'][0]
        self.assertEqual({e['id'] for e in task['evidence']}, {step['id'], receipt['id']})
