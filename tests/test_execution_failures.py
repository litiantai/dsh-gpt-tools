"""真实错误形状的回归：余额不足、验收阻塞和业务失败不能混淆。"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'dsh-gpt-supervisor/scripts')]
from autopilot.failures import harness_failure
from autopilot.thsoctop import browser_failure
from autopilot.retry import abnormal
from autopilot.scheduler import Scheduler
from review_core import Store


class ExecutionFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_quota_event_survives_empty_final_and_zero_exit(self):
        event = {'type': 'status', 'phase': 'turn_end', 'reason': {'kind': 'error',
            'error': {'message': 'Insufficient Balance (request_id: fixture)', 'code': 'QUOTA', 'status': 402}}}
        (self.root/'trace.jsonl').write_text(json.dumps(event)+'\n'+json.dumps({'type':'final','text':''}))
        (self.root/'stderr.log').write_text('irrelevant startup warning')
        for code in (0, 1):
            result = harness_failure(self.root, code)
            self.assertEqual(result['error_code'], 'MODEL_BALANCE_INSUFFICIENT')
            self.assertIn('余额不足', result['reason'])
            self.assertFalse(abnormal({'reason':result['reason'],'receipts':[{'result':result}]}))

    def test_stderr_fallback_is_redacted_and_network_errors_can_retry(self):
        (self.root/'trace.jsonl').write_text('incomplete json\n')
        (self.root/'stderr.log').write_text('connection reset api_key=fixture-secret')
        result = harness_failure(self.root, 1)
        self.assertNotIn('fixture-secret', str(result))
        self.assertTrue(abnormal({'reason':result['reason'],'receipts':[{'result':result}]}))
        self.assertIsNone(harness_failure(self.root, 0))

    def test_browser_quota_preserves_verification_stage_and_revision_budget(self):
        path = self.root/'browser-result.json'
        path.write_text(json.dumps({'status':'blocked','failure_kind':'agent_execution',
            'error_code':'MODEL_BALANCE_INSUFFICIENT','retryable':False,'reason':'模型服务余额不足'}))
        result = browser_failure({'status':'fail','summary':'process exited 2'}, path)
        scheduler = Scheduler(Store(self.root/'state'))
        product = scheduler.ledger.create('products', {})
        run = scheduler.ledger.create('runs', {'product_id':product['id'],'revisions':3,
            'receipts':[{'result':result}]}, 'verifying')
        updated = scheduler.complete_action(run, product, 'verify', result)
        self.assertEqual(updated['status'], 'blocked')
        self.assertEqual(updated['resume_status'], 'verifying')
        self.assertEqual(updated['revisions'], 3)
        self.assertIsNone(updated['next_auto_retry_at'])
        self.assertNotIn('返修次数已用尽', updated['reason'])

    def test_missing_receipt_blocks_but_business_failure_still_fails(self):
        path = self.root/'browser-result.json'
        check = {'status':'fail','summary':'process died'}
        self.assertEqual(browser_failure(check,path)['status'], 'blocked')
        path.write_text('{broken')
        self.assertEqual(browser_failure(check,path)['status'], 'blocked')
        path.write_text(json.dumps({'status':'fail','reason':'图表没有内容'}))
        self.assertEqual(browser_failure(check,path)['status'], 'fail')
        self.assertIsNone(browser_failure({'status':'pass'},path))


if __name__ == '__main__':
    unittest.main()
