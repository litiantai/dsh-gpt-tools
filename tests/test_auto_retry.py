"""小时级异常恢复：真实台账时序、额度门禁和不可重放边界。"""
import datetime
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'dsh-gpt-supervisor/scripts')]
from review_core import Store
from autopilot.scheduler import Scheduler
from autopilot.retry import recover
from autopilot.progress import investigate
from autopilot.delivery import repair_queue


class AutoRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.scheduler = Scheduler(Store(Path(self.tmp.name)))
        self.ledger = self.scheduler.ledger
        self.product = self.ledger.create('products', {'source':self.tmp.name, 'executor':['fake'],
            'policy':{'tokens_per_day':1000, 'runs_per_day':2, 'execution_seconds':3600, 'deepseek_off_peak_only':False}}, 'active')

    def tearDown(self):
        self.tmp.cleanup()

    def run_record(self, **extra):
        return self.ledger.create('runs', {'product_id':self.product['id'], 'reason':'timed out',
            'resume_status':'developing', **extra}, 'blocked')

    def test_hour_boundary_and_persisted_backoff_after_another_failure(self):
        run = self.run_record()
        first = recover(self.scheduler, 'runs', run, self.product, run['updated']+3599)
        self.assertEqual(first['status'], 'blocked')
        due = first['next_auto_retry_at']
        restarted = Scheduler(self.scheduler.store)
        resumed = recover(restarted, 'runs', first, self.product, due)
        self.assertEqual(resumed['status'], 'developing')
        self.assertEqual(resumed['auto_retry_count'], 1)
        with patch('autopilot.scheduler.time.time', return_value=due+10):
            failed = restarted.block(resumed, '模型执行失败')
        self.assertEqual(failed['next_auto_retry_at'], due+3610)
        self.assertEqual(recover(restarted,'runs',failed,self.product,due+3609)['status'],'blocked')
        self.assertEqual(recover(restarted,'runs',failed,self.product,due+3610)['auto_retry_count'],2)

    def test_no_tokens_preserves_due_time_and_resumes_when_available(self):
        run = self.run_record()
        due = run['updated']+3600
        with patch.object(self.scheduler,'tokens_available',return_value=False):
            waiting = recover(self.scheduler,'runs',run,self.product,due)
            self.assertEqual(waiting['status'],'blocked')
            self.assertIn('Token',waiting['auto_retry_wait_reason'])
            self.assertEqual(waiting['next_auto_retry_at'],due)
        resumed = recover(self.scheduler,'runs',waiting,self.product,due+1)
        self.assertEqual(resumed['status'],'developing')
        self.assertIsNone(resumed['auto_retry_wait_reason'])

    def test_manual_uncertain_live_call_and_non_operational_failures_stay_blocked(self):
        for extra in [{'control':'pause'},{'uncertain':True},{'call':{'id':'live'}},
                      {'pending_result':{'result':{}}},{'reason':'必需验收未全部通过或缺少产物清单'},
                      {'reason':'返修次数已用尽：执行失败'}, {'resume_status':'deploying'},
                      {'receipts':[{'result':{'uncertain':True,'failure_kind':'agent_execution'}}]}]:
            with self.subTest(extra=extra):
                run = self.run_record(**extra)
                self.assertEqual(recover(self.scheduler,'runs',run,self.product,run['updated']+7200)['status'],'blocked')
        run = self.run_record()
        self.assertEqual(recover(self.scheduler,'runs',run,self.product | {'status':'paused'},run['updated']+7200)['status'],'blocked')

    def test_execution_and_new_task_quotas_are_not_reset(self):
        run = self.run_record(execution_seconds=3600)
        waiting = recover(self.scheduler,'runs',run,self.product,run['updated']+3600)
        self.assertEqual(waiting['execution_seconds'],3600)
        self.assertEqual(waiting['status'],'blocked')
        for _ in range(2):self.ledger.budget(self.product['id'],'development',2)
        for reserved in (False,True):
            run = self.run_record(resume_status='queued',budget_reserved=reserved)
            result = recover(self.scheduler,'runs',run,self.product,run['updated']+3600)
            self.assertEqual(result['status'],'queued' if reserved else 'blocked')
        self.assertEqual(self.ledger.budget_used(self.product['id'],'development'),2)

    def test_retrying_checkout_does_not_charge_the_same_task_twice(self):
        product=self.ledger.update('products',self.product['id'],self.product['version'],{'repository':'missing'})
        requirement=self.ledger.create('requirements',{'product_id':product['id']})
        run=self.ledger.create('runs',{'product_id':product['id'],'requirement_id':requirement['id']},'queued')
        with patch('autopilot.scheduler.checkout',side_effect=TimeoutError('connection timed out')):
            self.scheduler.advance(run)
            blocked=self.ledger.get('runs',run['id'])
            self.assertTrue(blocked['budget_reserved'])
            resumed=recover(self.scheduler,'runs',blocked,product,blocked['next_auto_retry_at'])
            self.scheduler.advance(resumed)
        self.assertEqual(self.ledger.budget_used(product['id'],'development'),1)

    def test_review_quota_and_human_decision_prevent_retry(self):
        run = self.run_record(resume_status='plan_review',review_packet={'request_id':'review'})
        today = datetime.datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        (self.scheduler.store.state/'quota.json').write_text(json.dumps({'day':today,'count':999999}))
        review={'status':'blocked','execution_done':True,'packet':{'session_id':run['id']}}
        with patch.object(self.scheduler.store,'get',return_value=review):
            waiting=recover(self.scheduler,'runs',run,self.product,run['updated']+3600)
            self.assertEqual(waiting['status'],'blocked')
            self.assertIn('审查额度',waiting['auto_retry_wait_reason'])
        (self.scheduler.store.state/'quota.json').write_text(json.dumps({'day':today,'count':0}))
        with patch.object(self.scheduler.store,'get',return_value=review | {'human':{'decision':'blocked'}}):
            self.assertEqual(recover(self.scheduler,'runs',waiting,self.product,run['updated']+3601)['status'],'blocked')

    def test_delivery_uses_same_hour_and_token_gate(self):
        batch=self.ledger.create('deliveries',{'product_id':self.product['id'],'reason':'Harness 执行失败',
            'resume_status':'code_review'},'blocked')
        before,_=repair_queue(self.scheduler,batch,self.product,batch['updated']+3599)
        self.assertEqual(before['status'],'blocked')
        with patch.object(self.scheduler,'tokens_available',return_value=False):
            waiting,_=repair_queue(self.scheduler,before,self.product,batch['updated']+3600)
            self.assertEqual(waiting['status'],'blocked')
        after,_=repair_queue(self.scheduler,waiting,self.product,batch['updated']+3601)
        self.assertEqual(after['status'],'code_review')
        self.assertEqual(after['next_attempt'],0)

    def test_completed_receipt_cannot_retry_while_process_still_alive(self):
        run=self.run_record(receipts=[{'call_id':'call-1','result':{'failure_kind':'agent_execution'}}])
        folder=self.scheduler.root/'calls/call-1';folder.mkdir(parents=True)
        (folder/'process.json').write_text(json.dumps({'pid':123}))
        with patch.object(self.scheduler,'process_alive',return_value=True):
            self.assertEqual(recover(self.scheduler,'runs',run,self.product,run['updated']+7200)['status'],'blocked')

    def test_investigation_exception_retries_in_one_hour_without_development_token_gate(self):
        req=self.ledger.create('requirements',{'product_id':self.product['id'],'classification':'investigation','in_scope':True,
            'receipts':[{'call_id':'failure','action':'investigate','result':{'status':'blocked','reason':'timed out'}}]},'investigating')
        now=req['updated']
        with patch('autopilot.progress.time.time',return_value=now):
            self.assertTrue(investigate(self.scheduler))
        waiting=self.ledger.get('requirements',req['id'])
        self.assertEqual(waiting['next_investigation'],now+3600)
        with patch('autopilot.progress.time.time',return_value=now+3601),patch.object(self.scheduler,'tokens_available',return_value=False),patch.object(self.scheduler,'start_call') as start:
            self.assertTrue(investigate(self.scheduler))
            self.assertEqual(start.call_args.args[3],'investigate')


if __name__=='__main__':unittest.main()
