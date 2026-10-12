"""阶段并行、真实资源冲突和 Watch 授权只消费一次的回归。"""
from contextlib import ExitStack
from unittest.mock import patch, Mock
import unittest

from tests import test_coordinator as fixtures
from autopilot.scheduler import Scheduler
from autopilot.resources import ResourceSlots
from autopilot.run_progress import RunProgress
from autopilot import coordinator as c


class ResourceTests(unittest.TestCase):
    setUp = fixtures.CoordinatorTests.setUp
    tearDown = fixtures.CoordinatorTests.tearDown
    fresh = fixtures.CoordinatorTests.fresh
    decide = fixtures.CoordinatorTests.decide
    apply = fixtures.CoordinatorTests.apply

    def setUp(self):
        fixtures.CoordinatorTests.setUp(self)
        self.product=self.ledger.update('products',self.product['id'],self.product['version'],
            {'executor':['fake-executor'], 'adapter':['fake-adapter'], 'adapter_spec':{'kind':'command','capabilities':['verify']}})
        self.run=self.ledger.update('runs',self.run['id'],self.run['version'],
            {'workspace':str(self.root/'notification'), 'worker_home':str(self.root/'notification-home')})

    def owner(self, phase='verifying', **fields):
        return self.ledger.create('runs',{'product_id':self.product['id'], 'requirement_id':self.req['id'],
            'title':'扫描修复', 'workspace':str(self.root/'scan'), 'worker_home':str(self.root/'scan-home'),
            'call':{'id':'scan-call','action':{'verifying':'verify','planning':'plan','developing':'develop'}.get(phase,'idle')}}|fields,phase)

    def test_watch_resumes_plan_during_verification_and_dispatches_once(self):
        self.owner()
        decision=self.apply()
        self.assertTrue(c.resume_pending(self.scheduler))
        plan=self.fresh()
        self.assertEqual(plan['status'],'planning')
        self.assertTrue(plan['repair_plan_required'])
        self.assertFalse(plan['plan_approved'])
        self.assertEqual(plan['revisions'],3)
        self.assertEqual(RunProgress(self.ledger).current(plan)['kind'],'dispatch')
        with patch('autopilot.scheduler.subprocess.Popen',return_value=Mock(pid=123)) as launch:
            self.scheduler.advance(plan)
            launched=self.fresh()
            self.assertEqual(launched['call']['action'],'plan')
            self.assertEqual(launched['coordination_last_decision'],decision['id'])
            self.assertEqual(launched['extra_revisions'],1)
            restarted=Scheduler(self.store)
            restarted.start_call('runs',launched,self.product,'plan',['fake'])
            self.assertEqual(launch.call_count,1)
        self.assertEqual(len(ResourceSlots(self.ledger).owners),2)
        self.assertEqual(c.snapshot(self.ledger,self.product)['quota']['stage_resources']['capacities']['planning'],1)

    def test_same_stage_and_overlapping_workspaces_wait_without_consuming_grant(self):
        self.apply()
        owner=self.owner('planning')
        self.assertFalse(c.resume_pending(self.scheduler))
        self.assertEqual(RunProgress(self.ledger).current(self.fresh())['blocking_id'],owner['id'])
        owner=self.ledger.update('runs',owner['id'],owner['version'],{'call':{'id':'verify','action':'verify'},'workspace':str(self.root)},'verifying')
        self.assertFalse(c.resume_pending(self.scheduler))
        self.assertIn('共享工作目录',self.fresh()['coordination_wait'])
        self.assertEqual(self.fresh()['revisions'],3)
        with patch('autopilot.scheduler.subprocess.Popen') as launch:
            self.scheduler.start_call('runs',self.fresh(),self.product,'plan',['fake'])
        launch.assert_not_called()

    def test_legacy_shared_test_instance_and_publishing_remain_exclusive(self):
        self.product=self.ledger.update('products',self.product['id'],self.product['version'],{'adapter_spec':{'kind':'legacy'}})
        owner=self.owner()
        run=self.fresh() | {'status':'developing'}
        self.assertIn('共享测试实例',ResourceSlots(self.ledger).blocker(run)['reason'])
        self.assertIsNone(ResourceSlots(self.ledger).blocker(run | {'status':'planning'}))
        self.ledger.update('runs',owner['id'],owner['version'],{'call':None},'awaiting_release')
        self.assertIn('发布独占资源',ResourceSlots(self.ledger).blocker(run | {'status':'planning'})['reason'])

    def test_generic_develop_verify_and_review_have_independent_capacity(self):
        self.owner()
        self.assertIsNone(ResourceSlots(self.ledger).blocker(self.fresh() | {'status':'developing'}))
        review=self.owner('plan_review',call=None,review_id='review-1')
        wait=ResourceSlots(self.ledger).blocker(self.fresh() | {'status':'acceptance_review'})
        self.assertEqual(wait['blocking_id'],review['id'])
        self.assertEqual(wait['resource'],'review')

    def test_tick_does_not_return_before_resuming_watch_or_processing_other_calls(self):
        self.apply()
        owner=self.owner()
        # 所有背景工作关闭，只验证真实 tick 的选取和恢复次序。
        with ExitStack() as stack:
            for module,name in [('onboarding','tick'),('notifications','tick'),('usage','collect'),
                ('intelligence_scheduler','tick'),('coordinator','tick'),('test_chain_worker','tick'),
                ('project_groups','tick'),('inspection_routes','tick'),('delivery','tick'),
                ('daily','tick'),('progress','investigate'),('progress','recover_review')]:
                stack.enter_context(patch('autopilot.'+module+'.'+name,return_value=False))
            stack.enter_context(patch.object(self.scheduler,'monitor'))
            advance=stack.enter_context(patch.object(self.scheduler,'advance'))
            self.scheduler.tick()
            self.assertEqual(self.fresh()['status'],'planning')
            self.scheduler.tick()
            ids=[args.args[0]['id'] for args in advance.call_args_list]
            self.assertIn(owner['id'],ids)
            self.assertIn(self.run['id'],ids)

    def test_shared_worker_home_blocks_even_with_separate_workspace(self):
        owner=self.owner(worker_home=self.run['worker_home'])
        wait=ResourceSlots(self.ledger).blocker(self.fresh() | {'status':'planning'})
        self.assertEqual(wait['blocking_id'],owner['id'])
        self.assertIn('共享工作目录',wait['reason'])

    def test_multiple_publish_candidates_do_not_deadlock(self):
        first=self.owner('awaiting_release',call=None)
        second=self.owner('awaiting_release',call=None)
        self.assertIsNone(ResourceSlots(self.ledger).blocker(first))
        self.assertEqual(ResourceSlots(self.ledger).blocker(second)['blocking_id'],first['id'])

    def test_readonly_daily_call_does_not_starve_watch_or_run_receipts(self):
        self.apply()
        owner=self.owner()
        report=self.ledger.create('daily_reports',{'product_id':self.product['id'],
            'title':'日报', 'call':{'id':'daily-call','action':'daily_attribution'}},'running')
        with ExitStack() as stack:
            for module,name in [('onboarding','tick'),('notifications','tick'),('usage','collect'),
                ('intelligence_scheduler','tick'),('coordinator','tick'),('test_chain_worker','tick'),
                ('project_groups','tick'),('inspection_routes','tick'),('delivery','tick'),
                ('progress','investigate'),('progress','recover_review')]:
                stack.enter_context(patch('autopilot.'+module+'.'+name,return_value=False))
            stack.enter_context(patch.object(self.scheduler,'monitor'))
            stack.enter_context(patch.object(self.scheduler,'call_result',return_value=None))
            advance=stack.enter_context(patch.object(self.scheduler,'advance'))
            for action in ('daily_attribution','daily_retrospective'):
                report=self.ledger.update('daily_reports',report['id'],report['version'],
                    {'call':{'id':'daily-call','action':action}})
                self.scheduler.tick()
                self.assertEqual(self.fresh()['status'],'planning')
                self.assertEqual(RunProgress(self.ledger).current(self.fresh())['kind'],'dispatch')
            self.assertIn(owner['id'],[v.args[0]['id'] for v in advance.call_args_list])
            self.assertIn(self.run['id'],[v.args[0]['id'] for v in advance.call_args_list])

    def test_daily_acceptance_keeps_resource_lock_and_watch_grant(self):
        self.apply()
        report=self.ledger.create('daily_reports',{'product_id':self.product['id'],
            'title':'实际复验','call':{'id':'daily','action':'daily_acceptance'}},'running')
        self.assertFalse(c.resume_pending(self.scheduler))
        self.assertEqual(RunProgress(self.ledger).current(self.fresh())['blocking_id'],report['id'])
        with patch('autopilot.scheduler.subprocess.Popen') as launch:
            self.scheduler.start_call('runs',self.fresh(),self.product,'plan',['fake'])
        launch.assert_not_called()
        self.assertEqual(self.fresh()['revisions'],3)
