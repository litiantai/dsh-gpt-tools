"""项目策略热变更后的只读等待投影和执行槽证据回归。"""
import unittest
from unittest.mock import patch
from tests import test_off_peak as fixtures
from autopilot.run_progress import RunProgress


class WaitTests(unittest.TestCase):
    setUp = fixtures.QueueTests.setUp
    run_record = fixtures.QueueTests.run_record

    def change(self, run, **fields):
        return self.ledger.update('runs',run['id'],run['version'],fields)

    def current(self, run):
        return self.control.get('/runs/'+run['id'])['current_progress']

    def test_policy_change_replaces_old_peak_with_actual_owner_without_write(self):
        run = self.change(self.run_record('blocked'), reason='', coordination_wait='旧高峰，等18:00',
                          coordination_pending={'phase':'planning','action':'repair'})
        owner = self.change(self.run_record(), title='另一个需求',call={'id':'call-owner','action':'plan'})
        with patch('autopilot.off_peak.time.time',return_value=fixtures.at('2026-10-09T10:00:00')):
            self.assertEqual(self.current(run)['kind'],'off_peak')
            self.product = self.control.configure_product(self.product['id'],{'version':self.product['version'],
                'config':{'policy':self.product['policy'] | {'deepseek_off_peak_only':False}}})
            state = self.current(run)
            self.assertEqual(state['kind'],'main_slot')
            self.assertEqual(state['blocking_id'],owner['id'])
            self.assertIn('另一个需求',state['reason'])
            self.assertNotIn('18:00',state['reason'])
            self.assertEqual(self.ledger.get('runs',run['id']),run)
            owner=self.ledger.update('runs',owner['id'],owner['version'],{'call':None},'delivered')
            self.assertEqual(self.current(run)['kind'],'dispatch')
            self.assertIn('技术修复方案',self.current(run)['reason'])

    def test_project_policy_isolation_and_new_quota(self):
        run=self.run_record()
        other=self.control.mutate('/products',{'config':{'name':'另一项目','source':str(self.root),
            'repository':str(self.root),'goal':'test','adapter':['fake'],'executor':['fake'],
            'agents':self.product['agents'],'policy':{'deepseek_off_peak_only':False}}})
        other=self.ledger.update('products',other['id'],other['version'],{},'active')
        second=self.ledger.create('runs',{'product_id':other['id']},'planning')
        with patch('autopilot.off_peak.time.time',return_value=fixtures.at('2026-10-09T10:00:00')):
            self.assertEqual(self.current(run)['kind'],'off_peak')
            self.assertNotEqual(self.current(second)['kind'],'off_peak')
        with patch('autopilot.run_progress.snapshot',return_value={'tokens_exhausted':True}):
            self.assertEqual(self.current(run)['kind'],'quota')

    def test_live_call_overrides_old_reason_and_new_peak_policy(self):
        run=self.change(self.run_record('developing'),reason='DeepSeek 高峰等待18:00',call={'id':'live','action':'develop'})
        state=self.current(run)
        self.assertEqual(state['kind'],'call')
        self.assertTrue(state['running'])
        self.assertNotIn('18:00',state['reason'])

    def test_review_progress_and_context_use_live_record(self):
        run=self.change(self.run_record('plan_review'),reason='旧高峰',review_id='review-1')
        with patch.object(self.store,'get',return_value={'id':'review-1','status':'completed','execution_done':1}):
            self.assertEqual(self.current(run)['reason'],'审查已结束，等待调度器处理回执')
        run=self.change(run,review_id=None,call={'id':'active','action':'plan'})
        context=self.control.get('/runs/'+run['id']+'/context')
        self.assertEqual(context['record']['current_progress']['kind'],'call')

    def test_pause_update_and_authority_are_not_replaced_by_dispatch(self):
        run=self.change(self.run_record('blocked'),coordination_pending={'phase':'planning','requires_human_authority':True})
        self.assertEqual(self.current(run)['kind'],'authority')
        drain=self.store.state/'autopilot/update-drain.json';drain.parent.mkdir(exist_ok=True);drain.write_text('{}')
        self.assertEqual(self.current(run)['kind'],'platform_update')
        drain.unlink()
        self.ledger.update('products',self.product['id'],self.product['version'],{},'paused')
        self.assertEqual(self.current(run)['kind'],'project_paused')

    def test_failure_without_approved_retry_keeps_real_failure(self):
        run=self.change(self.run_record('blocked'),reason='真实业务验证失败')
        self.assertNotIn('current_progress',self.control.get('/runs/'+run['id']))

    def test_watch_reports_resource_evidence_without_changing_task_version(self):
        from autopilot.coordinator import snapshot,watch_summary
        self.product=self.ledger.update('products',self.product['id'],self.product['version'],
            {'policy':self.product['policy'] | {'deepseek_off_peak_only':False}})
        run=self.change(self.run_record('blocked'),coordination_pending={'phase':'planning'},coordination_wait='旧高峰')
        owner=self.change(self.run_record(),title='占槽任务',call={'id':'call','action':'plan'})
        findings=watch_summary(snapshot(self.ledger,self.product))['findings']
        self.assertIn('占槽任务',next(f for f in findings if f.get('run_id')==run['id'])['reason'])
        self.assertEqual(self.ledger.get('runs',run['id'])['version'],run['version'])
