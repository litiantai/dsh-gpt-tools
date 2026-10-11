"""协调动作、资源边界、持久化恢复与修复进展回归；不调用真实模型。"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'dsh-gpt-supervisor/scripts')]
from review_core import Store, Conflict
from autopilot.scheduler import Scheduler
from autopilot import coordinator as c
from autopilot.usage import register, collect


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root/'state')
        self.scheduler = Scheduler(self.store)
        self.ledger = self.scheduler.ledger
        self.product = self.ledger.create('products', {'source':str(self.root), 'repository':str(self.root),
            'goal':'项目协调', 'policy':{'max_revisions':3,'deepseek_off_peak_only':False},
            'agents':{r:{'provider':'codex','model':'test'} for r in ('acceptance','implementation','verification','discovery')}}, 'active')
        self.req = self.ledger.create('requirements', {'product_id':self.product['id'],'title':'通知能力'}, 'developing')
        self.run = self.ledger.create('runs', {'product_id':self.product['id'],'requirement_id':self.req['id'],
            'title':'通知能力','revisions':3,'resume_status':'verifying','reason':'返修次数已用尽：部分拒收误报成功',
            'receipts':[{'call_id':'verify-1','action':'verify','result':{'checks':[{'name':'拒收回执','status':'fail'},{'name':'项目隔离','status':'fail'}]}}]}, 'blocked')

    def tearDown(self):
        self.tmp.cleanup()

    def fresh(self):
        return self.ledger.get('runs', self.run['id'])

    def decide(self, action='repair', **extra):
        run = self.fresh()
        decision = self.ledger.create('coordination_decisions', {'product_id':self.product['id'],
            'selection':c.selected(self.product), 'config':self.product.get('coordinator',{}),
            'material':c.snapshot(self.ledger,self.product)}, 'running')
        result = {'status':'pass','action':action,'run_id':run['id'],'run_version':run['version'],
            'strategy':'修复拒收地址回执并增加部分拒收检查','resume_phase':'developing','priority':5,'to_role':'verification'} | extra
        return decision, result

    def apply(self, **extra):
        d,r = self.decide(**extra)
        return c.apply(self.scheduler,d,r)

    def test_grant_preserves_history_and_consumes_only_on_dispatch(self):
        decision = self.apply()
        self.assertEqual(decision['status'],'applied')
        self.assertEqual(self.fresh()['revisions'],3)
        self.assertTrue(c.resume_pending(self.scheduler))
        run = self.fresh()
        self.assertEqual(run['status'],'developing')
        self.assertIn('部分拒收',run['feedback'])
        run = c.consume_grant(self.scheduler,run)
        self.assertEqual((run['revisions'],run['extra_revisions']),(4,1))
        self.assertEqual(c.consume_grant(self.scheduler,run)['extra_revisions'],1)

    def test_repeat_receipt_and_restart_do_not_duplicate_grant(self):
        d,r=self.decide()
        c.apply(self.scheduler,d,r)
        restarted=Scheduler(self.store)
        c.apply(restarted,d,r)
        self.assertEqual(len(self.ledger.scoped('coordination_decisions',self.product['id'])),1)
        self.assertEqual(self.fresh()['coordination_pending']['decision_id'],d['id'])

    def test_evidence_only_resumes_verify_without_extra_revision(self):
        self.apply(action='verify', strategy='读取已登记能力检查回执后重新验证')
        c.resume_pending(self.scheduler)
        run=c.consume_grant(self.scheduler,self.fresh())
        self.assertEqual(run['status'],'verifying')
        self.assertEqual(run['revisions'],3)
        self.assertEqual(run.get('extra_revisions',0),0)

    def test_changed_version_and_foreign_task_are_rejected(self):
        d,r=self.decide()
        self.ledger.update('runs',self.run['id'],self.run['version'],{'reason':'新原因'})
        self.assertEqual(c.apply(self.scheduler,d,r)['status'],'stale')
        d,r=self.decide(run_id='another-project-task')
        self.assertEqual(c.apply(self.scheduler,d,r)['status'],'stale')
        self.assertFalse(self.fresh().get('coordination_pending'))

    def test_manual_pause_uncertain_and_publish_are_not_resumed(self):
        for values in ({'control':'pause'},{'uncertain':True},{'resume_status':'deploying'}):
            run=self.fresh();self.ledger.update('runs',run['id'],run['version'],{'control':None,'uncertain':False,'resume_status':'verifying'}|values)
            self.assertEqual(self.apply()['status'],'blocked')
        self.assertFalse(self.fresh().get('coordination_pending'))

    def test_budget_wait_does_not_consume_extra_grant(self):
        self.apply()
        with patch.object(self.scheduler,'tokens_available',return_value=False):
            self.assertFalse(c.resume_pending(self.scheduler))
        self.assertEqual(self.fresh()['revisions'],3)
        self.assertIn('额度',self.fresh()['coordination_wait'])

    def test_disabled_project_rejects_inflight_advice(self):
        d,r=self.decide()
        p=self.ledger.get('products',self.product['id'])
        self.ledger.update('products',p['id'],p['version'],{},'paused')
        self.assertEqual(c.apply(self.scheduler,d,r)['status'],'stale')

    def test_progress_requires_new_checks_and_detects_regression(self):
        before=c.checks_snapshot(self.run)
        run=self.run|{'coordination_baseline':before,'revisions':4,
            'receipts':self.run['receipts']+[{'call_id':'verify-2','action':'verify','result':{'checks':[
                {'name':'拒收回执','status':'pass'},{'name':'项目隔离','status':'fail'}]}}]}
        measured=c.progress(run)
        self.assertTrue(measured['coordination_progress']['improved'])
        self.assertEqual(measured['coordination_no_progress'],0)
        claim=self.run|{'coordination_baseline':before,'revisions':4,'summary':'全部修复完成'}
        self.assertFalse(c.progress(claim)['coordination_progress']['improved'])
        run['coordination_baseline']={'receipt_id':'old','checks':{'拒收回执':'fail','项目隔离':'pass'}}
        self.assertFalse(c.progress(run)['coordination_progress']['improved'])

    def test_two_no_progress_rounds_surface_human_once(self):
        for revision in (4,5):
            run=self.fresh()
            run=self.ledger.update('runs',run['id'],run['version'],{'revisions':revision,'coordination_baseline':c.checks_snapshot(run)})
            self.ledger.update('runs',run['id'],run['version'],c.progress(run))
        self.assertEqual(self.fresh()['coordination_no_progress'],2)
        self.assertEqual(self.apply()['status'],'needs_human')
        self.assertEqual(self.fresh()['status'],'waiting_for_reply')
        self.assertEqual(len(self.ledger.scoped('agent_messages',self.product['id'])),1)
        from autopilot.collaboration import human_reply
        msg=self.ledger.scoped('agent_messages',self.product['id'])[0]
        with self.store.transaction() as db:
            human_reply(self.ledger,self.product['id'],msg['id'],{'version':msg['version'],'content':'检查 SMTP 拒收映射的新证据'},db)
        self.assertEqual(self.fresh()['status'],'blocked')
        self.assertEqual(self.fresh()['coordination_no_progress'],0)

    def test_assistance_is_readonly_and_reenters_evaluation(self):
        self.assertEqual(self.apply(action='assist')['status'],'applied')
        run=self.fresh()
        self.assertEqual(run['status'],'waiting_for_reply')
        msg=self.ledger.scoped('agent_messages',self.product['id'])[0]
        self.assertEqual(msg['budget_kind'],'tokens')
        from autopilot.collaboration import reply
        with self.store.transaction() as db:
            reply(self.ledger,msg,'核对完成：需要修复拒收地址',db)
        self.assertEqual(self.fresh()['status'],'blocked')
        self.assertEqual(self.fresh()['revisions'],3)

    def test_coordinate_usage_exempt_but_recorded(self):
        trace=self.root/'trace.jsonl'
        trace.write_text(json.dumps({'type':'turn.completed','usage':{'input_tokens':100,'output_tokens':10}})+'\n')
        register(self.ledger,self.product['id'],trace,budget_kind='coordination_tokens',action='coordinate')
        collect(self.ledger);collect(self.ledger)
        self.assertEqual(self.ledger.budget_used(self.product['id'],'coordination_tokens'),110)
        self.assertEqual(self.ledger.budget_used(self.product['id'],'tokens'),0)

    def test_unchanged_snapshot_does_not_call_model_again(self):
        def launch(scheduler,decision,product):
            c.apply(scheduler,decision,{'status':'pass','action':'wait','reason':'等待新证据'})
        with patch('autopilot.coordinator.launch',side_effect=launch) as call:
            c.tick(self.scheduler,now=100)
            c.tick(Scheduler(self.store),now=200)
            self.assertEqual(call.call_count,1)

    def test_queued_crash_recovers_without_duplicate_decision(self):
        with patch('autopilot.coordinator.launch') as launch:
            c.tick(self.scheduler,now=100)
            c.tick(Scheduler(self.store),now=200)
        self.assertEqual(len(self.ledger.scoped('coordination_decisions',self.product['id'])),1)

    def test_existing_auxiliary_call_defers_coordinate(self):
        self.ledger.create('chat_messages',{'product_id':self.product['id'],'call':{'id':'busy'}},'running')
        with patch('autopilot.coordinator.launch') as call:
            c.tick(self.scheduler,now=100)
        call.assert_not_called()

    def test_settings_model_fallback_and_version_conflict(self):
        prefix='/products/'+self.product['id']+'/coordinator'
        api=self.scheduler.control
        self.assertTrue(api.get(prefix)['enabled'])
        self.assertEqual(api.get(prefix)['model']['model'],'test')
        api.mutate(prefix+'/settings',{'version':self.product['version'],'config':{'enabled':False,'model':{'provider':'claude','model':'other'}}})
        self.assertFalse(api.get(prefix)['enabled'])
        with self.assertRaises(Conflict):
            api.mutate(prefix+'/evaluate',{'version':self.product['version']})

    def test_no_progress_is_not_incremented_by_repeated_poll(self):
        run=self.fresh()
        run=self.ledger.update('runs',run['id'],run['version'],{'coordination_baseline':c.checks_snapshot(run)})
        changes=c.progress(run)
        run=self.ledger.update('runs',run['id'],run['version'],changes)
        self.assertIsNone(c.progress(run))

    def test_repair_requires_actual_plan_and_never_approves(self):
        self.assertEqual(self.apply(strategy='')['status'],'blocked')
        self.assertEqual(self.fresh()['status'],'blocked')
        self.assertNotIn('acceptance_review_id',self.fresh())

    def test_numeric_resources_survive_redaction(self):
        from autopilot.store import redact
        self.assertEqual(redact(c.snapshot(self.ledger,self.product))['quota']['development_used'],0)

    def test_removing_failed_checks_is_not_progress(self):
        record=self.run|{'coordination_baseline':c.checks_snapshot(self.run),'revisions':4,
            'receipts':[{'call_id':'new','action':'verify','result':{'checks':[{'name':'拒收回执','status':'pass'}]}}]}
        report=c.progress(record)['coordination_progress']
        self.assertFalse(report['improved'])
        self.assertEqual(report['missing_checks'],['项目隔离'])

    def test_complete_review_progress_counts_resolved_issues(self):
        before=c.checks_snapshot(self.run|{'last_review_result':{'review_id':'r1','decision':'revise','issues':['拒收误报','项目串数据']}})
        record=self.run|{'coordination_baseline':before,'revisions':4,
            'last_review_result':{'review_id':'r2','decision':'revise','issues':['项目串数据']}}
        result=c.progress(record)
        self.assertTrue(result['coordination_progress']['improved'])
        record['last_review_result']['issues']=['新的回归']
        self.assertFalse(c.progress(record)['coordination_progress']['improved'])

    def test_malformed_model_contract_is_blocked(self):
        d,_=self.decide()
        with patch('autopilot.intelligence_worker.run_model',return_value={'status':'pass','action':'repair'}):
            result=c.evaluate({'product':self.product,'record':d})
        self.assertEqual(result['status'],'blocked')
        self.assertIn('契约',result['reason'])

    def test_manual_queue_priority_is_preserved(self):
        run=self.fresh()
        self.ledger.update('runs',run['id'],run['version'],{'queue_first':True,'queue_first_at':100},'queued')
        self.assertEqual(self.apply(action='reprioritize',priority=10)['status'],'applied')
        self.assertTrue(self.fresh()['queue_first'])
        self.assertEqual(self.fresh()['queue_first_at'],100)

    def test_off_peak_and_execution_budget_prevent_consumption(self):
        self.apply()
        with patch('autopilot.off_peak.waiting',return_value={'reason':'等待错峰'}):
            self.assertFalse(c.resume_pending(self.scheduler))
        run=self.fresh()
        self.ledger.update('runs',run['id'],run['version'],{'execution_seconds':3600})
        self.assertFalse(c.resume_pending(self.scheduler))
        self.assertEqual(self.fresh()['revisions'],3)

    def test_failed_call_uncertainty_is_never_replayed(self):
        run=self.fresh()
        self.ledger.update('runs',run['id'],run['version'],{'uncertain':True})
        with patch('autopilot.coordinator.launch') as launch:
            c.tick(self.scheduler,now=100)
        launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
