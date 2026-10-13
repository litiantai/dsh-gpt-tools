"""验证失败分类、同版本复验限制和按真实用例判断返修进展。"""
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

from tests import test_coordinator as fixtures
from autopilot import coordinator as c
from autopilot.delivery_budget import snapshot, wait_reason
from autopilot.delivery_progress import changes, hold
from autopilot.failures import verification_failure
from autopilot.verification_control import identity, repeat_reason
from error_messages import present_errors


class RepairControlTests(unittest.TestCase):
    setUp = fixtures.CoordinatorTests.setUp
    tearDown = fixtures.CoordinatorTests.tearDown
    fresh = fixtures.CoordinatorTests.fresh
    decide = fixtures.CoordinatorTests.decide
    apply = fixtures.CoordinatorTests.apply

    def test_successful_explanation_keeps_previous_timeout_as_context(self):
        result = {'status':'pass','reason':'上次 Timeout 已修复，代码修改完成'}
        self.assertEqual(present_errors(result), result)

    def test_assertion_timeout_is_not_a_network_retry(self):
        result = {'status':'fail','reason':'Timeout 5000ms','checks':[{'name':'browser-0','status':'fail',
            'log_tail':"1) tests/browser/demo.spec.ts:37:1 › missing label\nError: expect(locator).toBeVisible() failed\nTimeout: 5000ms"}]}
        value = verification_failure(result)
        self.assertEqual(value['error_info']['code'], 'TEST_ASSERTION_FAILED')
        self.assertFalse(value['retryable'])
        self.assertEqual(value['failed_test_ids'], ['tests/browser/demo.spec.ts › missing label'])

    def test_missing_evidence_stays_blocked_and_does_not_request_code_repair(self):
        result={'status':'fail','checks':[{'name':'独立业务验证','status':'blocked','reason':'必需验证证据缺失'}]}
        value=verification_failure(result)
        self.assertEqual(value['status'],'blocked')
        self.assertEqual(value['failure_kind'],'verification_evidence')
        self.assertFalse(value['retryable'])

    def test_partial_repair_requires_actual_pass_line_not_disappearing_failure(self):
        compare = runpy.run_path('skills/dsh-role-coordinator/scripts/compare_progress.py')['compare']
        before = {'receipt_id':'old','checks':{'browser-0':'fail'},'failed_tests':['case-a']}
        after = {'receipt_id':'new','checks':{'browser-0':'fail'},'failed_tests':['case-b']}
        self.assertFalse(compare(before, after)['improved'])
        after['passed_tests'] = ['case-a']
        value = compare(before, after)
        self.assertTrue(value['improved'])
        self.assertEqual(value['resolved_tests'], ['case-a'])
        self.assertEqual(value['new_failed_tests'], ['case-b'])

    def test_log_parsing_uses_trusted_logs_and_preserves_failed_and_passed_cases(self):
        log = self.root/'check.log'
        log.write_text('test_old (tests.demo.Test.test_old) ... ok\nFAIL: test_new (tests.demo.Test.test_new)\nAssertionError: wrong value\n')
        result = {'status':'fail','checks':[{'name':'test-1','status':'fail','log':str(log)}]}
        value = verification_failure(result, self.root)
        self.assertEqual(value['passed_test_ids'], ['test_old (tests.demo.Test.test_old)'])
        self.assertEqual(len(value['failed_test_ids']), 1)
        with tempfile.TemporaryDirectory() as outside:
            private = Path(outside)/'private.log';private.write_text('FAIL: private fixture\n')
            result['checks'][0]['log'] = str(private)
            self.assertEqual(verification_failure(result,self.root)['failed_test_ids'], [])

    def test_same_code_known_failure_blocks_but_code_and_verifier_changes_resume(self):
        run = self.run | {'commit':'candidate-a'}
        receipt = {'action':'verify','verification_identity':identity(run,self.product,'verify'),
                   'result':{'status':'fail','checks':[{'name':'browser-0','status':'fail'}]}}
        run['receipts'] = [receipt]
        self.assertIn('未变化', repeat_reason(run,self.product,'verify'))
        self.assertEqual(repeat_reason(run|{'commit':'candidate-b'},self.product,'verify'), '')
        self.assertEqual(repeat_reason(run,self.product|{'adapter':['new-verifier']},'verify'), '')
        receipt['result'] = {'status':'blocked','failure_kind':'dependency_network','retryable':True}
        self.assertEqual(repeat_reason(run,self.product,'verify'), '')

    def test_dispatch_guard_prevents_process_and_manual_retry_enters_repair_plan(self):
        run = self.fresh()
        run = self.ledger.update('runs',run['id'],run['version'],{'commit':'candidate-a','resume_status':'verifying',
            'receipts':[{'action':'verify','verification_identity':identity(run|{'commit':'candidate-a'},self.product,'verify'),
                'result':{'status':'fail','checks':[{'name':'browser-0','status':'fail'}]}}]},'verifying')
        with patch('autopilot.scheduler.subprocess.Popen') as process:
            stopped = self.scheduler.start_call('runs',run,self.product,'verify',['fake'])
        process.assert_not_called()
        self.assertEqual(stopped['status'],'blocked')
        resumed = self.scheduler.control.mutate(f'/runs/{run["id"]}/retry',{'version':stopped['version']})
        self.assertEqual(resumed['status'],'planning')
        self.assertTrue(resumed['repair_plan_required'])
        self.assertFalse(resumed['plan_approved'])

    def test_delegated_authority_cannot_answer_its_own_no_progress_stop(self):
        self.product = self.ledger.update('products',self.product['id'],self.product['version'],
            {'coordinator':{'human_authority':True}})
        run = self.fresh()
        self.ledger.update('runs',run['id'],run['version'],{'coordination_no_progress':2})
        decision = self.apply()
        self.assertEqual(decision['status'],'needs_human')
        run = self.fresh()
        self.assertTrue(run['coordination_progress_hold'])
        self.assertIsNone(c.delegated_question(self.ledger,self.product,run))
        self.assertEqual(self.apply()['status'],'blocked')
        self.assertEqual(len(self.ledger.scoped('agent_messages',self.product['id'])),1)

    def test_missing_fresh_evidence_does_not_count_as_code_no_progress(self):
        baseline = c.checks_snapshot(self.run)
        run = self.run | {'coordination_baseline':baseline,'coordination_no_progress':1,
            'receipts':self.run['receipts']+[{'call_id':'timeout-plan','action':'plan',
                'result':{'status':'blocked','failure_kind':'agent_execution'}}]}
        value = c.progress(run)
        self.assertEqual(value['coordination_no_progress'],1)
        self.assertEqual(value['coordination_execution_failures'],1)

    def test_delivery_budget_is_independent_and_does_not_block_merges(self):
        product = self.product | {'code_review':{'tokens_per_day':100}}
        from autopilot.usage import register, collect
        trace=self.root/'delivery-usage.jsonl'
        trace.write_text(json.dumps({'type':'turn.completed','usage':{'input_tokens':100}})+'\n')
        register(self.ledger,product['id'],trace,budget_kind='code_delivery_tokens')
        collect(self.ledger)
        self.assertTrue(snapshot(self.ledger,product)['exhausted'])
        for action in ('review_feature','repair_release','validate_release'):
            self.assertIn('交付 Token',wait_reason(self.ledger,product,action))
        for action in ('merge_release','merge_feature','sync_release'):
            self.assertEqual(wait_reason(self.ledger,product,action),'')
        self.assertEqual(self.ledger.budget_used(product['id'],'tokens'),0)
        self.assertEqual(wait_reason(self.ledger,product|{'code_review':{'tokens_per_day':200}},'repair_release'),'')

    def test_delivery_budget_stops_before_allocating_a_review_round_and_resets_by_day(self):
        from autopilot.staged_delivery import tick_product
        from autopilot.usage import register, collect
        import time
        self.product=self.ledger.update('products',self.product['id'],self.product['version'],{'code_review':{'tokens_per_day':1}})
        trace=self.root/'usage.jsonl';trace.write_text(json.dumps({'type':'turn.completed','usage':{'input_tokens':2}})+'\n')
        register(self.ledger,self.product['id'],trace,budget_kind='code_delivery_tokens');collect(self.ledger)
        batch=self.ledger.create('deliveries',{'product_id':self.product['id'],'day':'20990101','title':'batch',
            'run_ids':[],'integrated_ids':[],'revisions':0,'cutoff':time.time()+100000,'flow':'review_before_release'},'repairing_release')
        with patch.object(self.scheduler,'start_call') as start:
            tick_product(self.scheduler,self.product,time.time())
        start.assert_not_called()
        self.assertEqual(self.ledger.scoped('code_reviews',self.product['id']),[])
        self.assertEqual(self.ledger.get('deliveries',batch['id'])['revisions'],0)
        with self.store.connect() as db:
            db.execute("UPDATE auto_budget SET day='2000-01-01' WHERE kind='code_delivery_tokens'")
        self.assertEqual(wait_reason(self.ledger,self.product,'repair_release'),'')

    def test_delivery_no_progress_stops_after_two_real_new_failures(self):
        batch = self.ledger.create('deliveries',{'product_id':self.product['id']},'release_failed')
        for number in range(3):
            result = {'status':'fail','checks':[{'name':'browser-0','status':'fail'}]}
            batch = self.ledger.update('deliveries',batch['id'],batch['version'],
                {'receipts':[{'action':'validate_release','call_id':f'check-{number}','result':result}]})
            measured = changes(batch,result,'validate_release')
            batch = self.ledger.update('deliveries',batch['id'],batch['version'],measured)
        batch, stopped = hold(self.scheduler,batch)
        self.assertTrue(stopped)
        self.assertEqual(batch['delivery_no_progress'],2)
        resumed = self.scheduler.control.mutate(f'/deliveries/{batch["id"]}/retry',{'version':batch['version']})
        self.assertFalse(resumed['delivery_progress_hold'])
        self.assertEqual(resumed['delivery_no_progress'],0)
        self.assertEqual(resumed['delivery_manual_retries'],1)


if __name__ == '__main__':
    unittest.main()
