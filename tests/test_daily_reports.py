"""日报定时、复验恢复与独立用量账本的行为验证。"""
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'dsh-gpt-supervisor/scripts')]
from review_core import Store
from autopilot.api import Control
from autopilot.daily import snapshot, tick
from autopilot.scheduler import Scheduler
from autopilot.usage import register, collect


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/'state')
        self.control=Control(self.store)
        self.ledger=self.control.ledger
        self.midnight=dt.datetime(2026,10,6,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.product=self.ledger.create('products',{'name':'test','source':self.tmp.name,
            'daily_report_enabled':True,'daily_report_enabled_at':self.midnight,
            'policy':{'runs_per_day':1,'tokens_per_day':1}},'paused')

    def tearDown(self):
        self.tmp.cleanup()

    def test_1900_once_and_next_day_late_acceptance(self):
        self.assertIsNone(snapshot(self.ledger,self.product,self.midnight+18*3600))
        run=self.ledger.create('runs',{'product_id':self.product['id'],'title':'已经上线的需求','accepted_at':self.midnight+12*3600},'completed')
        report=snapshot(self.ledger,self.product,self.midnight+19*3600)
        self.assertEqual(report['run_ids'],[run['id']])
        self.assertEqual(snapshot(self.ledger,self.product,self.midnight+22*3600)['id'],report['id'])
        self.assertEqual(snapshot(self.ledger,self.product,self.midnight+30*3600)['id'],report['id'])
        late=self.ledger.create('runs',{'product_id':self.product['id'],'title':'晚间验收','accepted_at':self.midnight+21*3600},'accepted')
        tomorrow=snapshot(self.ledger,self.product,self.midnight+43*3600)
        self.assertEqual(tomorrow['run_ids'],[late['id']])
        self.assertEqual(tomorrow['window_start'],report['cutoff'])

    def test_exempt_meter_is_idempotent_and_does_not_consume_development(self):
        ordinary=Path(self.tmp.name)/'normal.jsonl';exempt=Path(self.tmp.name)/'daily.jsonl'
        for path,amount in [(ordinary,10),(exempt,400)]:
            path.write_text(json.dumps({'type':'turn.completed','usage':{'input_tokens':amount,'cached_input_tokens':amount//2,'output_tokens':2}})+'\n')
        register(self.ledger,self.product['id'],ordinary)
        register(self.ledger,self.product['id'],exempt,'daily_report_tokens')
        collect(self.ledger);collect(self.ledger)
        self.assertEqual(self.ledger.budget_used(self.product['id'],'tokens'),12)
        self.assertEqual(self.ledger.budget_used(self.product['id'],'daily_report_tokens'),402)
        self.assertEqual(self.ledger.budget_used(self.product['id'],'development'),0)

    def test_report_starts_even_with_all_normal_limits_exhausted(self):
        scheduler=Scheduler(self.store)
        self.ledger.budget(self.product['id'],'tokens',1)
        self.ledger.budget(self.product['id'],'development',1)
        with patch('autopilot.daily.time.time',return_value=self.midnight+19*3600),patch.object(scheduler,'start_call') as start:
            self.assertTrue(tick(scheduler))
            self.assertEqual(start.call_args.args[3],'daily_retrospective')
        self.assertFalse(scheduler.tokens_available(self.product))

    def test_receipt_recovery_does_not_rerun_completed_audit(self):
        run=self.ledger.create('runs',{'product_id':self.product['id'],'title':'待复验','accepted_at':self.midnight+3600},'accepted')
        report=snapshot(self.ledger,self.product,self.midnight+19*3600)
        self.ledger.update('daily_reports',report['id'],report['version'],{'receipts':[{'action':'daily_acceptance','result':{'status':'blocked','reason':'测试环境缺失'}}]})
        scheduler=Scheduler(self.store)
        with patch('autopilot.daily.time.time',return_value=self.midnight+20*3600),patch.object(scheduler,'start_call') as start:
            tick(scheduler)
            start.assert_not_called()
            recovered=self.ledger.get('daily_reports',report['id'])
            self.assertEqual(recovered['audits'][0]['run_id'],run['id'])
            self.assertEqual(recovered['audits'][0]['status'],'blocked')
            tick(scheduler)
            self.assertEqual(start.call_args.args[3],'daily_retrospective')

    def test_daily_completion_preserves_blocked_audit(self):
        report=snapshot(self.ledger,self.product,self.midnight+19*3600)
        self.ledger.update('daily_reports',report['id'],report['version'],{'audits':[{'status':'blocked'}],
            'receipts':[{'action':'daily_retrospective','result':{'status':'pass','summary':'如实记录'}}]})
        with patch('autopilot.daily.time.time',return_value=self.midnight+20*3600):
            tick(Scheduler(self.store))
        self.assertEqual(self.ledger.get('daily_reports',report['id'])['status'],'blocked')

    def test_reacceptance_requires_new_check_evidence_and_preserves_source(self):
        from autopilot.daily import execute
        from autopilot.workspace import git, digest
        workspace=Path(self.tmp.name)/'candidate';workspace.mkdir()
        git(workspace,'init');git(workspace,'config','user.name','Test');git(workspace,'config','user.email','test@localhost')
        (workspace/'a.txt').write_text('original')
        git(workspace,'add','.');git(workspace,'commit','-m','candidate')
        commit=git(workspace,'rev-parse','HEAD')
        manifest=Path(self.tmp.name)/'manifest.json';manifest.write_text(json.dumps({'commit':commit}))
        run={'workspace':str(workspace),'manifest':str(manifest),'commit':commit,'source_digest':digest(workspace),
            'checks':[{'required':True,'status':'pass'}]}
        with patch('autopilot.codex_executor.execute',return_value={'status':'pass','checks':[]}) as model:
            result=execute('daily_acceptance',{'audit_run':run})
            self.assertEqual(result['status'],'blocked')
            self.assertEqual(model.call_args.args[0],'daily_acceptance')
        def edit(*args):
            (workspace/'a.txt').write_text('modified')
            return {'status':'pass','checks':['tested']}
        with patch('autopilot.codex_executor.execute',side_effect=edit):
            self.assertEqual(execute('daily_acceptance',{'audit_run':run})['status'],'fail')

    def test_daily_waits_for_development_and_discovery(self):
        self.ledger.create('runs',{'product_id':self.product['id'],'call':{'action':'develop'}},'developing')
        scheduler=Scheduler(self.store)
        with patch('autopilot.daily.time.time',return_value=self.midnight+19*3600),patch.object(scheduler,'start_call') as start:
            self.assertFalse(tick(scheduler));start.assert_not_called()
        self.assertEqual(len(self.ledger.list('daily_reports')),1)

    def test_nightly_covers_backlog_and_failed_inspections_then_batches_all(self):
        self.product=self.ledger.update('products',self.product['id'],self.product['version'],{'nightly_attribution':True})
        with patch('autopilot.store.time.time',return_value=self.midnight-3600):
            old=[self.ledger.create('signals',{'product_id':self.product['id'],'summary':'积压'}) for _ in range(21)]
        with patch('autopilot.store.time.time',return_value=self.midnight+3600):
            inspection=self.ledger.create('inspections',{'product_id':self.product['id'],'title':'失败巡检','judgement':'页面打不开','steps':[]},'blocked')
        report=snapshot(self.ledger,self.product,self.midnight+19*3600)
        self.assertEqual(len(report['attribution_signal_ids']),22)
        self.assertIn('nightly-'+inspection['id'],report['attribution_signal_ids'])
        scheduler=Scheduler(self.store)
        for index in range(3):
            report=self.ledger.get('daily_reports',report['id'])
            ids=report['attribution_signal_ids'][index*8:(index+1)*8]
            with patch('autopilot.daily.time.time',return_value=self.midnight+19*3600),patch.object(scheduler,'start_call') as start:
                tick(scheduler)
                self.assertEqual(start.call_args.args[3],'daily_attribution')
                self.assertEqual([x['id'] for x in start.call_args.args[5]['signals']],ids)
            result={'status':'pass','requirements':[],'attributions':[{'signal_id':i,'outcome':'no_issue','reason':'未发现新问题'} for i in ids]}
            self.ledger.update('daily_reports',report['id'],report['version'],{'receipts':report.get('receipts',[])+[{'action':'daily_attribution','result':result}]})
            with patch('autopilot.daily.time.time',return_value=self.midnight+19*3600):tick(scheduler)
        report=self.ledger.get('daily_reports',report['id'])
        self.assertEqual(len(report['attribution_results']),3)
        self.assertTrue(all(self.ledger.get('signals',i)['status']=='reviewed' for i in report['attribution_signal_ids']))

    def test_shared_signal_creates_all_requirements_and_replay_is_idempotent(self):
        signal=self.ledger.create('signals',{'product_id':self.product['id'],'summary':'两个问题'})
        candidates=[{'title':title,'signal_ids':[signal['id']],'evidence':'证据','acceptance':['核对'],
            'reproduction':'步骤','impact':'影响','classification':'investigation','in_scope':True} for title in ['问题一','问题二']]
        scheduler=Scheduler(self.store)
        items=scheduler.accept_discovery(self.product,{'requirements':candidates},[signal['id']],'2026-10-06','report')
        self.assertEqual(len(items),2)
        self.assertEqual(len(self.ledger.get('signals',signal['id'])['requirement_ids']),2)
        scheduler.accept_discovery(self.product,{'requirements':candidates},[signal['id']],'2026-10-06','report')
        self.assertEqual(len(self.ledger.list('requirements')),2)
        self.assertTrue(all(r['requirement_day']=='2026-10-06' for r in items))

    def test_nightly_incomplete_attribution_cannot_mark_pass(self):
        from autopilot.daily import execute
        with patch('autopilot.codex_executor.execute',return_value={'status':'pass','requirements':[],'attributions':[]}):
            result=execute('daily_attribution',{'signals':[{'id':'missing'}]})
        self.assertEqual(result['status'],'blocked')

    def test_hourly_mode_disables_daytime_discovery(self):
        product=self.control.mutate('/products/'+self.product['id']+'/nightly-attribution',{})
        self.assertEqual(product['policy']['inspection_seconds'],3600)
        self.assertTrue(product['daily_report_enabled'])
        product.update(status='active',adapter=['fake'],executor=['fake'],last_probe=self.midnight,last_inspect=self.midnight)
        self.ledger.create('signals',{'product_id':product['id']})
        scheduler=Scheduler(self.store)
        with patch('autopilot.scheduler.time.time',return_value=self.midnight+10),patch.object(scheduler,'start_call') as start:
            scheduler.monitor(product)
            start.assert_not_called()

if __name__=='__main__':
    unittest.main()
