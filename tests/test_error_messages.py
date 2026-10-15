"""中文异常输出与原始诊断分离、脱敏及重试语义的回归。"""
import json
import smtplib
import ssl
from pathlib import Path
import sys
import unittest
from urllib.error import URLError
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parents[1]/'dsh-gpt-supervisor/scripts')]
from error_messages import error_info, failure_fields, present_errors, sanitize, source_text
from autopilot.retry import abnormal


class ErrorMessageTests(unittest.TestCase):
    def test_smtp_transport_failure_is_distinct_from_invalid_credential(self):
        raw = '[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1082)'
        native = error_info(ssl.SSLEOFError(8, raw), '邮件服务器')
        legacy = error_info(raw, '邮件服务器')
        self.assertEqual(native, legacy)
        self.assertEqual(native['code'], 'TLS_CONNECTION_LOST')
        self.assertIn('邮件服务器', native['message'])
        self.assertIn('代理规则', native['suggestion'])
        self.assertNotIn('授权码', native['message'] + native['suggestion'])
        fields = failure_fields(ssl.SSLEOFError(8, raw), '邮件服务器')
        self.assertIn('UNEXPECTED_EOF_WHILE_READING', fields['diagnostic'])
        self.assertEqual(present_errors(fields), fields)
        auth = error_info(smtplib.SMTPAuthenticationError(535, b'authentication failed'), '邮件服务器')
        self.assertEqual(auth['code'], 'SMTP_AUTH_FAILED')
        self.assertIn('授权码', auth['suggestion'])

    def test_tls_and_smtp_failures_are_recognized_by_type_and_legacy_text(self):
        for native, legacy, expected in [
            (ssl.SSLCertVerificationError(1, 'certificate verify failed'), 'CERTIFICATE_VERIFY_FAILED', 'TLS_CERTIFICATE_INVALID'),
            (ssl.SSLError(1, 'wrong version number'), '[SSL: WRONG_VERSION_NUMBER]', 'TLS_FAILED'),
            (smtplib.SMTPServerDisconnected('Connection unexpectedly closed'), 'Connection unexpectedly closed', 'CONNECTION_LOST'),
        ]:
            with self.subTest(code=expected):
                self.assertEqual(error_info(native)['code'], expected)
                self.assertEqual(error_info(legacy)['code'], expected)

    def test_shared_catalog_patterns_compile_in_python_and_javascript(self):
        import subprocess,re
        rows=json.loads((Path(__file__).resolve().parents[1]/'dashboard/src/error-catalog.json').read_text())
        for row in rows:
            re.compile(row['pattern'],re.I)
        subprocess.run(['node','-e',"for (const row of JSON.parse(require('fs').readFileSync(0,'utf8'))) new RegExp(row.pattern,'i');"],
                       input=json.dumps(rows),text=True,check=True,capture_output=True)
    def test_completed_defect_keeps_source_description_including_technical_references(self):
        reason = 'RepositoryScans.tsx:29 缺少 identity.isError 门控，超时后仍可提交 /scans；api_key=private-key'
        result = present_errors({'status': 'fail', 'provider': 'codex', 'reason': reason,
                                 'summary': '已复现接口 https://example.test/api?token=secret 的缺陷'})
        self.assertIn('identity.isError', result['reason'])
        self.assertIn('超时后仍可提交', result['reason'])
        self.assertEqual(result['error_info']['code'], 'BUSINESS')
        self.assertNotIn('private-key', json.dumps(result))
        self.assertNotIn('token=secret', json.dumps(result))
        self.assertEqual(present_errors(result), result)

    def test_old_localized_defect_is_recovered_but_execution_error_stays_localized(self):
        reason = 'RepositoryScans.tsx 缺少 identity.isError 判断'
        legacy = {'status': 'fail', 'provider': 'codex', **failure_fields(reason)}
        legacy['diagnostic'] = {'original': {'reason': reason}}
        result = present_errors(legacy)
        self.assertEqual(result['reason'], reason)
        self.assertEqual(source_text(legacy), reason)
        failed = present_errors({'status': 'fail', 'failure_kind': 'agent_execution',
                                 'provider': 'codex', 'reason': '模型执行失败：TimeoutError'})
        self.assertEqual(failed['error_info']['code'], 'REQUEST_TIMEOUT')

    def test_discovered_competitor_keeps_english_relevance_and_nested_source_errors(self):
        reason = 'Same audience and Error recovery in /Users/example/product/API.md; docs https://example.test/api?token=source-secret; api_key=private-key'
        result = present_errors({'status': 'pass', 'competitors': [{
            'name': 'Example', 'urls': ['https://example.test/'], 'reason': reason,
            'verified_sources': [{'status': 'failed', 'reason': 'TimeoutError', 'url': 'https://example.test/'}]}]})
        candidate = result['competitors'][0]
        self.assertIn('Same audience and Error recovery', candidate['reason'])
        self.assertIn('/Users/example/product/API.md', candidate['reason'])
        self.assertIn('https://example.test/api', candidate['reason'])
        self.assertNotIn('error_info', candidate)
        self.assertNotIn('private-key', json.dumps(result))
        self.assertNotIn('source-secret', json.dumps(result))
        self.assertEqual(candidate['verified_sources'][0]['error_info']['code'], 'REQUEST_TIMEOUT')
        self.assertNotEqual(candidate['verified_sources'][0]['reason'], 'TimeoutError')
        self.assertEqual(present_errors(result), result)

    def test_saved_competitor_relevance_is_preserved_for_all_lifecycle_states(self):
        reason = 'Related workflow using https://example.test/docs and /tmp/research.json; token=private-token'
        for status in ('active', 'disabled', 'merged'):
            with self.subTest(status=status):
                result = present_errors({'name': 'Example', 'urls': ['https://example.test/'],
                                         'identity': 'example.test/example', 'status': status, 'reason': reason})
                self.assertEqual(result['reason'], sanitize(reason))
                self.assertNotIn('error_info', result)
                self.assertEqual(present_errors(result), result)

    def test_old_localized_competitor_relevance_is_recovered_from_original(self):
        reason = 'Same users; compare https://example.test/API and APIError handling; api_key=private-key'
        legacy = {'name': 'Example', 'urls': ['https://example.test/'], **failure_fields(reason)}
        legacy['diagnostic'] = {'original': {'reason': reason}}
        result = present_errors({'status': 'pass', 'competitors': [legacy]})
        candidate = result['competitors'][0]
        self.assertEqual(candidate['reason'], sanitize(reason))
        self.assertNotIn('error_info', candidate)
        self.assertNotIn('private-key', json.dumps(result))
        self.assertEqual(present_errors(result), result)

    def test_competitor_context_does_not_change_execution_failure_classification(self):
        result = present_errors({'status': 'blocked', 'reason': 'TimeoutError', 'competitors': [{
            'name': 'Example', 'urls': ['https://example.test/'], 'reason': 'Same users'},
            {'status': 'failed', 'reason': 'Connection refused'}]})
        self.assertEqual(result['error_info']['code'], 'REQUEST_TIMEOUT')
        self.assertNotEqual(result['reason'], 'TimeoutError')
        self.assertEqual(result['competitors'][0]['reason'], 'Same users')
        self.assertEqual(result['competitors'][1]['error_info']['code'], 'CONNECTION_REFUSED')
        unrelated = present_errors({'name': 'Example', 'urls': ['https://example.test/'], 'reason': 'TimeoutError'})
        self.assertEqual(unrelated['error_info']['code'], 'REQUEST_TIMEOUT')

    def test_competitor_relevance_does_not_hide_other_error_fields(self):
        result = present_errors({'competitors': [{'name': 'Example', 'urls': ['https://example.test/'],
                                                 'reason': 'Same audience', 'reporting_error': 'Connection refused'}]})
        candidate = result['competitors'][0]
        self.assertEqual(candidate['reason'], 'Same audience')
        self.assertEqual(candidate['error_info']['code'], 'CONNECTION_REFUSED')
        self.assertEqual(candidate['diagnostic']['original'], {'reporting_error': 'Connection refused'})
        self.assertEqual(present_errors(result), result)

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
            self.assertEqual(updated['status'],'planning')
            self.assertIn('TS2322',updated['feedback'])
            self.assertNotIn('TS2322',result['reason'])
