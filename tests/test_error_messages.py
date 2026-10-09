"""中文异常输出与原始诊断分离、脱敏及重试语义的回归。"""
import json
from pathlib import Path
import sys
import unittest
from urllib.error import URLError
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parents[1]/'dsh-gpt-supervisor/scripts')]
from error_messages import error_info, failure_fields, present_errors, sanitize
from autopilot.retry import abnormal


class ErrorMessageTests(unittest.TestCase):
    def test_native_and_legacy_connection_refused_have_same_description(self):
        native=error_info(URLError(ConnectionRefusedError(61,'Connection refused')),'应用服务')
        legacy=error_info('<urlopen error [Errno 61] Connection refused>','应用服务')
        self.assertEqual(native,legacy)
        self.assertEqual(native['code'],'CONNECTION_REFUSED')
        self.assertIn('应用服务',native['message'])

    def test_shared_catalog_covers_failures_and_preserves_business_messages(self):
        for raw,code in [('Insufficient Balance','MODEL_BALANCE_INSUFFICIENT'),('TimeoutError','REQUEST_TIMEOUT'),
                         ('strict mode violation','ACCEPTANCE_TOOL_ERROR'),('Failed to fetch','NETWORK_UNAVAILABLE'),
                         ('FileNotFoundError','FILE_MISSING'),('Expecting value: line 1 column 1','INVALID_RECEIPT'),
                         ('HTTP Error 403: Forbidden','ACCESS_DENIED'),('HTTP 429','RATE_LIMITED')]:
            with self.subTest(raw=raw):
                info=error_info(raw)
                self.assertEqual(info['code'],code)
                self.assertNotIn(raw,info['message'])
        self.assertEqual(error_info('已有任务正在执行，请稍后重试')['code'],'BUSINESS')
        self.assertEqual(error_info('Something strange happened')['code'],'UNKNOWN')
        self.assertEqual(error_info('操作失败：Traceback (most recent call last)')['code'],'UNKNOWN')

    def test_diagnostics_preserve_evidence_but_hide_credentials(self):
        raw='Error: api_key="private-key" Authorization: Bearer secret\nhttps://example.test/api?token=hidden'
        clean=sanitize({'diagnostic':raw,'password':'other-secret'})
        for secret in ['private-key','Bearer secret','token=hidden','other-secret']:
            self.assertNotIn(secret,json.dumps(clean))
        result=present_errors({'status':'blocked','reason':'<urlopen error [Errno 61] Connection refused>','tokens_used':42})
        self.assertIn('Connection refused',json.dumps(result['diagnostic']))
        self.assertNotIn('Connection refused',result['reason'])
        self.assertEqual(result['tokens_used'],42)
        self.assertEqual(present_errors(result),result)

    def test_translated_description_does_not_control_structured_retry(self):
        result={'status':'blocked','retryable':True,**failure_fields('Connection refused')}
        self.assertTrue(abnormal({'reason':result['reason'],'receipts':[{'result':result}]}))
        result={'status':'blocked','retryable':False,**failure_fields('Insufficient Balance')}
        self.assertFalse(abnormal({'reason':result['reason'],'receipts':[{'result':result}]}))

    def test_repair_prompt_keeps_technical_evidence_after_localization(self):
        import tempfile
        from review_core import Store
        from autopilot.scheduler import Scheduler
        with tempfile.TemporaryDirectory() as tmp:
            scheduler=Scheduler(Store(Path(tmp)))
            product=scheduler.ledger.create('products',{})
            run=scheduler.ledger.create('runs',{'product_id':product['id']},'verifying')
            result=present_errors({'status':'fail','reason':'Command failed: src/main.ts(8) TS2322'})
            updated=scheduler.complete_action(run,product,'verify',result)
            self.assertEqual(updated['status'],'developing')
            self.assertIn('TS2322',updated['feedback'])
            self.assertNotIn('TS2322',result['reason'])
