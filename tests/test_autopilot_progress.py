import sys,json,time,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'dsh-gpt-supervisor/scripts')]
from review_core import Store
from autopilot.scheduler import Scheduler
from autopilot.progress import investigate,recover_review
from autopilot.thsoctop import observe

class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.scheduler=Scheduler(Store(Path(self.tmp.name)/'state'))
        self.ledger=self.scheduler.ledger
        self.product=self.ledger.create('products',{'name':'test','source':self.tmp.name,'executor':['fake'],
            'policy':{'runs_per_day':5,'tokens_per_day':1000}},'active')
        self.req=self.ledger.create('requirements',{'product_id':self.product['id'],'title':'调查','classification':'investigation','in_scope':True},'pending')
    def tearDown(self): self.tmp.cleanup()
    def receipt(self,result):
        req=self.ledger.get('requirements',self.req['id'])
        return self.ledger.update('requirements',req['id'],req['version'],{'receipts':[{'call_id':'once','result':result,'action':'investigate'}]},'investigating')
    def test_investigation_starts_with_daily_budget_and_promotes_same_requirement(self):
        with patch.object(self.scheduler,'start_call') as start:
            self.assertTrue(investigate(self.scheduler))
            self.assertEqual(start.call_args.args[3],'investigate')
        self.assertEqual(self.ledger.budget_used(self.product['id'],'development'),1)
        self.receipt({'status':'pass','outcome':'development','reason':'已定位代码问题','checks':['实际断言失败'],
            'acceptance':['修复后返回正常'], 'resolution_probes':[{'path':'/ths-octop/api/status','pointer':'/data/ok','operator':'equals','expected':True}]})
        investigate(self.scheduler)
        req=self.ledger.get('requirements',self.req['id']);self.assertEqual(req['status'],'queued')
        run=self.ledger.get('runs',req['run_id']);self.assertTrue(run['budget_reserved'])
        self.assertEqual(len(self.ledger.list('requirements')),1)
        self.assertEqual(self.ledger.budget_used(self.product['id'],'development'),1)
    def test_missing_evidence_waits_without_fake_resolution_or_development(self):
        self.receipt({'status':'pass','outcome':'resolved','checks':[]})
        investigate(self.scheduler)
        req=self.ledger.get('requirements',self.req['id']);self.assertEqual(req['status'],'awaiting_external')
        self.assertGreater(req['next_investigation'],time.time())
        self.assertFalse(investigate(self.scheduler))
    def test_quota_blocks_new_investigation(self):
        for _ in range(5):self.ledger.budget(self.product['id'],'development',5)
        with patch.object(self.scheduler,'start_call') as start:
            self.assertFalse(investigate(self.scheduler));start.assert_not_called()
    def test_recovery_needs_terminated_execution_and_retries_hourly_without_three_attempt_cap(self):
        run=self.ledger.create('runs',{'product_id':self.product['id'],'reason':'交接等待超时','resume_status':'plan_review','review_packet':{'request_id':'old'}},'blocked')
        review={'status':'blocked','execution_done':False,'packet':{'session_id':run['id']}}
        with patch('autopilot.retry.time.time',return_value=time.time()+3601),patch.object(self.scheduler.store,'get',return_value=review):
            self.assertFalse(recover_review(self.scheduler))
            review['execution_done']=True
            self.assertTrue(recover_review(self.scheduler))
        run=self.ledger.get('runs',run['id']);self.assertEqual(run['status'],'plan_review')
        self.assertIsNone(run['review_id']);self.assertEqual(run['last_failed_review_id'],'old')
        run=self.ledger.update('runs',run['id'],run['version'],{'review_retries':3,'reason':'交接等待超时','review_packet':{'request_id':'new-failure'}},'blocked')
        with patch('autopilot.retry.time.time',return_value=time.time()+7202),patch.object(self.scheduler.store,'get',return_value=review):
            self.assertTrue(recover_review(self.scheduler))
        self.assertEqual(self.ledger.get('runs',run['id'])['review_retries'],4)
    def test_observation_timeout_retries_but_mismatch_fails(self):
        request={'product':{},'record':{'id':'run'},'requirement':{}}
        with patch('autopilot.thsoctop.monitor',side_effect=TimeoutError('timeout')):
            self.assertEqual(observe(request)['status'],'busy')
        with patch('autopilot.thsoctop.monitor',return_value={'releaseId':'other'}):
            self.assertEqual(observe(request)['status'],'fail')
    def test_old_wall_clock_anomaly_can_only_be_repaired_once_with_receipt(self):
        import uuid
        ident=str(uuid.uuid4())
        run=self.ledger.create('runs',{'product_id':self.product['id'],'reason':'累计开发执行时间已用尽','resume_status':'developing','execution_seconds':10000,
            'receipts':[{'call_id':ident,'action':'plan','result':{'status':'pass','elapsed':10000}}]},'blocked')
        directory=self.scheduler.root/'calls'/ident;directory.mkdir(parents=True)
        (directory/'request.json').write_text(json.dumps({'timeout':3600,'input':{'record':{'id':run['id']}}}))
        updated=self.scheduler.control.mutate('/runs/'+run['id']+'/repair-timing',{'version':run['version']})
        self.assertEqual(updated['legacy_execution_seconds'],10000)
        self.assertEqual(updated['execution_seconds'],0)
        self.assertEqual(updated['status'],'developing')
        with self.assertRaises(Exception):self.scheduler.control.mutate('/runs/'+run['id']+'/repair-timing',{'version':updated['version']})
    def test_call_elapsed_uses_monotonic_clock(self):
        from autopilot.call import main
        directory=Path(self.tmp.name)/'clock';directory.mkdir()
        path=directory/'request.json'
        path.write_text(json.dumps({'command':[sys.executable,'-c','import json;print(json.dumps({"status":"pass"}))'],'timeout':5,'cwd':self.tmp.name,'input':{}}))
        with patch('autopilot.call.time.time',side_effect=[1,100000,200000,300000]):main(path)
        result=json.loads((directory/'result.json').read_text())
        self.assertEqual(result['status'],'pass');self.assertEqual(result['timing'],'monotonic')
        self.assertLess(result['elapsed'],5)
    def test_three_observation_failures_schedule_rollback(self):
        release=self.ledger.create('releases',{'product_id':self.product['id']},'observing')
        run=self.ledger.create('runs',{'product_id':self.product['id'],'release_id':release['id'],'healthy_since':1},'observing')
        for index in range(3):
            self.scheduler.complete_action(run,self.product,'observe',{'status':'busy','reason':'timeout'})
            run=self.ledger.get('runs',run['id'])
            self.assertEqual(run['status'],'observing' if index<2 else 'blocked')
        self.assertEqual(self.ledger.get('releases',release['id'])['status'],'rollback_pending')

if __name__=='__main__':unittest.main()
