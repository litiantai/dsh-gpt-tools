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
            'strategy':'修复拒收地址回执并增加部分拒收检查','resume_phase':'developing','priority':5,'to_role':'verification',
            'help_plan':{'basis':'控制器检查显示部分拒收误报成功', 'steps':['核对 SMTP 拒收回执','修复映射并回归'],
                         'resources':['implementation 开发槽一轮，遵守现有额度'], 'verification':['部分拒收检查通过'],
                         'stop_conditions':['两轮无进展时停止并转人工']}} | extra
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
        plan=self.ledger.get('coordination_decisions',self.fresh()['coordination_plan_id'])
        self.assertEqual(plan['result']['action'],'human')
        self.assertTrue(plan['help_plan']['steps'])
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

    def test_unchanged_abnormal_snapshot_is_rechecked_every_five_minutes(self):
        def launch(scheduler,decision,product):
            c.apply(scheduler,decision,{'status':'pass','action':'wait','reason':'等待新证据'})
        with patch('autopilot.coordinator.launch',side_effect=launch) as call:
            c.tick(self.scheduler,now=100)
            c.tick(Scheduler(self.store),now=399)
            self.assertEqual(call.call_count,1)
            c.tick(Scheduler(self.store),now=400)
            self.assertEqual(call.call_count,2)
        state=c.status(self.ledger,self.product)['instance']
        self.assertEqual(state['last_check'],400)
        self.assertEqual(state['watch']['requirements_checked'],1)
        self.assertEqual(state['watch']['abnormal_count'],1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='autopilot_requirement_watch'").fetchone()[0],2)

    def test_normal_unchanged_queue_is_checked_without_repeated_model_calls(self):
        run=self.fresh(); self.ledger.update('runs',run['id'],run['version'],{},'queued')
        def launch(scheduler,decision,product):
            c.apply(scheduler,decision,{'status':'pass','action':'wait','reason':'等待开发槽'})
        with patch('autopilot.coordinator.launch',side_effect=launch) as call:
            c.tick(self.scheduler,now=100)
            c.tick(self.scheduler,now=400)
        self.assertEqual(call.call_count,1)
        self.assertEqual(c.status(self.ledger,self.product)['instance']['last_check'],400)

    def test_watch_runs_while_coordination_call_is_busy(self):
        self.ledger.create('coordination_decisions',{'product_id':self.product['id'],'call':{'id':'slow'}},'running')
        with patch.object(self.scheduler,'call_result',return_value=None), patch('autopilot.coordinator.launch') as launch:
            c.tick(self.scheduler,now=100)
            c.tick(self.scheduler,now=400)
        launch.assert_not_called()
        self.assertEqual(c.status(self.ledger,self.product)['instance']['last_check'],400)

    def test_watch_resolves_requirement_state_and_records_broken_links(self):
        req=self.req
        self.ledger.update('requirements',req['id'],req['version'],{'run_id':self.run['id']},'queued')
        missing=self.ledger.create('requirements',{'product_id':self.product['id'],'run_id':'missing'},'queued')
        other=self.ledger.create('runs',{'product_id':'other','requirement_id':req['id']},'blocked')
        foreign=self.ledger.create('requirements',{'product_id':self.product['id'],'run_id':other['id']},'queued')
        material=c.snapshot(self.ledger,self.product)
        records={r['id']:r for r in material['requirements']}
        self.assertEqual(records[req['id']]['effective_status'],'blocked')
        self.assertEqual(records[req['id']]['issue'],'')
        self.assertIn('不存在或不属于',records[missing['id']]['issue'])
        self.assertIn('不存在或不属于',records[foreign['id']]['issue'])
        self.assertEqual(c.watch_summary(material)['abnormal_count'],3)

    def test_waiting_for_human_is_visible_without_duplicate_help(self):
        run=self.fresh()
        self.ledger.update('runs',run['id'],run['version'],{'coordinator_human':'existing-message'},'waiting_for_reply')
        with patch('autopilot.coordinator.launch') as launch:
            c.tick(self.scheduler,now=100)
            c.tick(self.scheduler,now=400)
        launch.assert_not_called()
        watch=c.status(self.ledger,self.product)['instance']['watch']
        self.assertEqual(watch['findings'][0]['action'],'等待人工回复')
        self.assertEqual(self.fresh()['status'],'waiting_for_reply')

    def test_missing_help_plan_never_dispatches(self):
        for plan in (None,{}, {'basis':'仅有一句建议'}):
            self.assertEqual(self.apply(help_plan=plan)['status'],'blocked')
            self.assertIsNone(self.fresh().get('coordination_pending'))

    def test_help_plan_is_durable_linked_and_sent_to_execution(self):
        decision=self.apply()
        saved=self.ledger.get('coordination_decisions',decision['id'])
        self.assertEqual(saved['help_plan']['verification'],['部分拒收检查通过'])
        self.assertTrue(saved['plan_saved_at'])
        run=self.fresh()
        self.assertEqual(run['coordination_plan_id'],decision['id'])
        self.assertIn('两轮无进展',run['coordination_pending']['strategy'])
        self.assertEqual(c.status(Scheduler(self.store).ledger,self.product)['decisions'][0]['help_plan'],saved['help_plan'])

    def test_paused_project_does_not_watch_or_dispatch(self):
        self.ledger.update('products',self.product['id'],self.product['version'],{},'paused')
        with patch('autopilot.coordinator.launch') as launch:
            c.tick(self.scheduler,now=400)
        launch.assert_not_called()
        self.assertEqual(self.ledger.scoped('coordinators',self.product['id']),[])

    def delegate_waiting_question(self):
        self.product=self.ledger.update('products',self.product['id'],self.product['version'],
            {'coordinator':{'human_authority':True}})
        self.apply(action='human')
        return self.ledger.get('agent_messages',self.fresh()['coordinator_human'])

    def test_delegated_watch_answers_and_repairs_with_durable_plan(self):
        question=self.delegate_waiting_question()
        run=self.fresh()
        self.ledger.update('runs',run['id'],run['version'],{'coordination_no_progress':2})
        material=c.snapshot(self.ledger,self.product)
        self.assertFalse(material['tasks'][0]['protected'])
        self.assertEqual(c.watch_summary(material)['findings'][0]['action'],'评估帮助与资源协调')
        decision=self.apply()
        self.assertEqual(decision['status'],'applied')
        self.assertEqual(decision['answered_question_id'],question['id'])
        reply=self.ledger.get('agent_messages',question['id'])
        self.assertEqual(reply['reply_role'],'coordinator')
        self.assertEqual(reply['status'],'replied')
        self.assertIn('帮助方案',reply['reply'])
        self.assertEqual(self.fresh()['coordination_plan_id'],decision['id'])
        self.assertEqual(self.fresh()['coordination_no_progress'],2)
        self.assertTrue(c.resume_pending(Scheduler(self.store)))
        run=c.consume_grant(self.scheduler,self.fresh())
        self.assertEqual((run['status'],run['revisions'],run['extra_revisions']),('developing',4,1))
        self.assertEqual(c.apply(self.scheduler,decision,decision['result'])['status'],'applied')
        self.assertEqual(self.fresh()['revisions'],4)

    def test_delegation_keeps_pause_and_uncertain_results_protected(self):
        question=self.delegate_waiting_question()
        for change in ({'control':'pause'},{'uncertain':True},{'pending_result':{'status':'pass'}}):
            run=self.fresh()
            self.ledger.update('runs',run['id'],run['version'],{'control':None,'uncertain':False,'pending_result':None}|change)
            self.assertEqual(self.apply()['status'],'blocked')
            self.assertEqual(self.ledger.get('agent_messages',question['id'])['status'],'needs_human')

    def test_delegation_rechecks_question_version_and_authorization(self):
        question=self.delegate_waiting_question()
        d,result=self.decide()
        self.ledger.update('agent_messages',question['id'],question['version'],{'content':'问题已更新'})
        self.assertEqual(c.apply(self.scheduler,d,result)['status'],'stale')
        d,result=self.decide()
        self.product=self.ledger.update('products',self.product['id'],self.product['version'],{'coordinator':{'human_authority':False}})
        self.assertEqual(c.apply(self.scheduler,d,result)['status'],'stale')
        self.assertEqual(self.fresh()['status'],'waiting_for_reply')

    def test_revoking_delegation_holds_unconsumed_repair(self):
        self.delegate_waiting_question()
        self.assertEqual(self.apply()['status'],'applied')
        self.product=self.ledger.update('products',self.product['id'],self.product['version'],{'coordinator':{'human_authority':False}})
        self.assertFalse(c.resume_pending(self.scheduler))
        with self.assertRaises(Conflict):
            c.consume_grant(self.scheduler,self.fresh())
        self.assertEqual(self.fresh()['revisions'],3)

    def test_invalid_delegated_plan_or_phase_does_not_answer(self):
        question=self.delegate_waiting_question()
        for values in ({'help_plan':None},{'resume_phase':'deploying'},{'action':'assist','to_role':'human'}):
            self.assertEqual(self.apply(**values)['status'],'blocked')
            self.assertEqual(self.ledger.get('agent_messages',question['id'])['status'],'needs_human')
            self.assertIsNone(self.fresh().get('coordination_pending'))

    def test_delegated_watch_rechecks_human_wait_every_five_minutes(self):
        self.delegate_waiting_question()
        def finish(scheduler,decision,product):
            self.assertTrue(decision['material']['human_authority'])
            c.apply(scheduler,decision,{'status':'pass','action':'wait','reason':'等待补证'})
        with patch('autopilot.coordinator.launch',side_effect=finish) as launch:
            c.tick(self.scheduler,now=100)
            self.assertEqual(launch.call_count,1)
            c.tick(self.scheduler,now=400)
            self.assertEqual(launch.call_count,2)

    def test_delegation_rejects_foreign_or_unrelated_human_question(self):
        question=self.delegate_waiting_question()
        self.ledger.update('agent_messages',question['id'],question['version'],{'product_id':'other'})
        self.assertTrue(c.snapshot(self.ledger,self.product)['tasks'][0]['protected'])
        self.assertEqual(self.apply()['status'],'blocked')
        with self.assertRaises(ValueError):
            c.validate({'human_authority':'yes'})

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

    def test_forced_watch_is_consumed_once_when_resources_are_busy(self):
        self.ledger.create('chat_messages',{'product_id':self.product['id'],'call':{'id':'busy'}},'running')
        with self.store.transaction() as db:
            state=c.instance(self.ledger,self.product,db)
            self.ledger.update('coordinators',state['id'],state['version'],{'last_check':100,'force':True},db=db)
        with patch('autopilot.coordinator.launch') as launch:
            c.tick(self.scheduler,now=101)
            c.tick(self.scheduler,now=102)
        launch.assert_not_called()
        state=c.status(self.ledger,self.product)['instance']
        self.assertEqual(state['last_check'],101)
        self.assertFalse(state['force'])

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
