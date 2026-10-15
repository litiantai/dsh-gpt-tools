"""自动日报与早七邮件的完整证据、冻结附件及重启幂等验证。"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'dsh-gpt-supervisor/scripts')]
from review_core import Store
from autopilot.api import Control
from autopilot.scheduler import Scheduler
from autopilot import daily, notifications as notify

ZONE = ZoneInfo('Asia/Shanghai')


class DailyMailIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / 'state')
        self.ledger = Control(self.store).ledger
        self.start = dt.datetime(2026, 10, 13, tzinfo=ZONE).timestamp()
        self.now = self.start + 86400 + 7 * 3600
        with patch('autopilot.store.time.time', return_value=self.start - 86400):
            self.product = self.ledger.create('products', {
                'name': '完整复盘测试', 'source': self.temporary.name, 'goal': '发现所有卡点',
                'daily_report_enabled': True, 'daily_report_enabled_at': self.start - 86400,
                'notifications': {'enabled': True, 'sender': 'sender@example.test',
                    'recipients': ['recipient@example.test'], 'morning_hour': 7,
                    'events': {'morning_retro': True}},
            }, 'paused')
        self.scheduler = Scheduler(self.store)
        self.environment = patch.dict(os.environ, {'DSH_HOME': str(Path(self.temporary.name) / 'home')})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def facts(self):
        with patch('autopilot.store.time.time', return_value=self.start + 3600):
            requirement = self.ledger.create('requirements', {
                'product_id': self.product['id'], 'title': '已恢复的任务',
                'evidence': '失败回执', 'acceptance': ['重试通过'], 'impact': '回执丢失'}, 'online')
            self.ledger.create('runs', {'product_id': self.product['id'],
                'requirement_id': requirement['id'], 'title': '历史验证失败后来上线',
                'online_at': self.start + 7200,
                'receipts': [{'call_id': 'historic-call', 'at': self.start + 3600,
                    'action': 'verify', 'result': {'status': 'fail', 'reason': '历史断言未通过'}},
                    {'call_id': 'recover-call', 'at': self.start + 7200,
                    'action': 'verify', 'result': {'status': 'pass', 'reason': '断言已修复'}}]}, 'online')
            self.ledger.create('inspections', {'product_id': self.product['id'],
                'title': '没有即时告警的巡检', 'judgement': '定位器异常', 'steps': [], 'finished_at': self.start + 3600}, 'blocked')
        with patch('autopilot.store.time.time', return_value=self.now - 300):
            self.ledger.create('runs', {'product_id': self.product['id'],
                'requirement_id': requirement['id'], 'title': '等待用户回复的任务',
                'reason': '返修次数达到上限，等待回复'}, 'waiting_for_reply')

    def morning(self):
        return notify._retro_events(self.ledger, self.product, self.now,
                                  notify.normalize(self.product), scheduler=self.scheduler)

    def test_morning_retains_recovered_task_inspection_and_reply_wait_in_html_attachment(self):
        self.facts()
        item = self.morning()
        self.assertEqual(item['status'], 'queued')
        payload = item['payload']
        self.assertIn('历史断言未通过', payload['text'])
        self.assertIn('定位器异常', payload['text'])
        self.assertIn('等待用户回复的任务', payload['text'])
        self.assertIn('完整复盘', payload['html'])
        self.assertIn('智能复盘等待异步执行', payload['html'])
        attachment = notify._report_attachment(payload, self.scheduler.root)
        message = notify.build_message('sender@example.test', ['recipient@example.test'],
            item['subject'], payload['text'], payload['html'], attachment)
        files = list(message.iter_attachments())
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].get_content_type(), 'text/html')
        self.assertIn('历史断言未通过', files[0].get_content())
        self.assertIn('定位器异常', files[0].get_content())
        report = self.ledger.get('daily_reports', payload['report_id'])
        prepared = report['retrospective_evidence']
        evidence = json.loads(Path(prepared['evidence_path']).read_text())
        self.assertEqual(evidence['summary']['run_failure_receipts'], 1)
        self.assertEqual(evidence['summary']['inspection_blocked_records'], 1)
        self.assertEqual(report['report_type'], 'morning')
        self.assertEqual(report['window_start'], self.start)
        self.assertEqual(report['cutoff'], self.start + 86400)
        self.assertTrue(report['analysis_pending'])

    def test_restart_existing_sent_mail_still_creates_report_without_duplicate_delivery(self):
        event_key = 'retro:2026-10-13'
        ident = notify.notification_id(self.product['id'], event_key)
        self.ledger.create('notifications', {'product_id': self.product['id'],
            'kind': 'morning_retro', 'event_key': event_key, 'payload': {}}, 'sent', ident)
        self.assertIsNone(self.morning())
        reports = self.ledger.scoped('daily_reports', self.product['id'])
        self.assertEqual(len(reports), 1)
        self.assertTrue(Path(reports[0]['html_report']['path']).is_file())
        self.assertIsNone(self.morning())
        self.assertEqual(len(self.ledger.scoped('notifications', self.product['id'])), 1)
        self.assertEqual(len(self.ledger.scoped('daily_reports', self.product['id'])), 1)

    def test_morning_never_changes_evening_cutoff_and_consumes_active_call_first(self):
        report = daily.snapshot(self.ledger, self.product, self.start + 19 * 3600)
        self.morning()
        tomorrow = daily.snapshot(self.ledger, self.product, self.start + 86400 + 19 * 3600)
        self.assertEqual(tomorrow['window_start'], report['cutoff'])
        self.assertEqual(daily._next_report(self.ledger)['report_type'], 'morning')
        old = self.ledger.get('daily_reports', report['id'])
        self.ledger.update('daily_reports', old['id'], old['version'], {'call': {'action': 'daily_attribution'}})
        self.assertEqual(daily._next_report(self.ledger)['id'], old['id'])

    def test_analysis_has_complete_evidence_and_failure_still_finishes_html(self):
        self.facts()
        item = self.morning()
        report_id = item['payload']['report_id']
        with patch('autopilot.daily.time.time', return_value=self.now), patch.object(self.scheduler, 'start_call') as start:
            daily.tick(self.scheduler)
        material = start.call_args.args[5]['daily_report']['comprehensive_evidence']['material']
        self.assertEqual(material['summary']['run_failure_receipts'], 1)
        self.assertTrue(any('历史断言未通过' in row['reason'] for row in material['problem_items']))
        report = self.ledger.get('daily_reports', report_id)
        self.ledger.update('daily_reports', report_id, report['version'], {'receipts': [{
            'action': 'daily_retrospective', 'result': {'status': 'blocked', 'reason': '模型账户余额不足'}}]})
        with patch('autopilot.daily.time.time', return_value=self.now):
            daily.tick(self.scheduler)
        finished = self.ledger.get('daily_reports', report_id)
        self.assertEqual(finished['status'], 'blocked')
        self.assertFalse(finished['analysis_pending'])
        self.assertIn('模型账户余额不足', Path(finished['html_report']['path']).read_text())
        self.assertIn('历史断言未通过', Path(finished['html_report']['path']).read_text())
        # 智能结论覆盖主报告后，随早七邮件冻结的附件仍可投递，且摘要不发生漂移。
        notify._report_attachment(item['payload'], self.scheduler.root)

    def test_attachment_tampering_and_external_path_rejected_before_smtp(self):
        item = self.morning()
        metadata = item['payload']['html_report']
        path = Path(metadata['path'])
        path.write_text('changed')
        with self.assertRaisesRegex(ValueError, '摘要不匹配'):
            notify._report_attachment(item['payload'], self.scheduler.root)
        outside = Path(self.temporary.name) / 'private.html'
        outside.write_text('private')
        with self.assertRaisesRegex(ValueError, '路径无效'):
            notify._report_attachment({'html_report': {'path': str(outside),
                'sha256': hashlib.sha256(outside.read_bytes()).hexdigest()}}, self.scheduler.root)

    def test_passed_analysis_sends_conversion_once_and_keeps_initial_attachment_frozen(self):
        self.facts()
        with patch('autopilot.store.time.time', return_value=self.start + 5400):
            signal = self.ledger.create('signals', {'product_id': self.product['id'],
                'source': 'inspection', 'code': 'UNCLAIMED_FAILURE', 'summary': '需要继续归因',
                'evidence': {'status': 'blocked', 'reason': '页面定位证据不足'}}, 'abnormal')
        initial = self.morning()
        original_hash = initial['payload']['html_report']['sha256']
        code_file = notify.credential_file()
        code_file.parent.mkdir(parents=True)
        code_file.write_text('synthetic-local-code')
        sent = []
        class SMTP:
            def login(instance, sender, code):
                self.assertEqual(code, 'synthetic-local-code')
            def send_message(instance, message, **kwargs):
                sent.append(message)
                return {}
            def quit(instance):
                pass
        with patch.object(notify, 'connect', return_value=SMTP()):
            notify.send(self.scheduler, initial, self.now)
        report = self.ledger.get('daily_reports', initial['payload']['report_id'])
        result = {'status': 'pass', 'reason': '事实已核实', 'summary': '发现巡检归因证据缺口，已转调查需求。',
            'accomplishments': [], 'problems': ['定位器异常需要补证'], 'lessons': [],
            'next_actions': ['核对定位器与补充重现步骤'],
            'requirement_candidates': [{'title': '补齐页面定位调查证据', 'signal_ids': [signal['id']],
                'evidence': '巡检原始异常与缺少定位器契约', 'reproduction': '运行相同巡检路径',
                'impact': '巡检不能确认业务状态', 'acceptance': ['记录实际定位器与预期页面'],
                'classification': 'investigation', 'in_scope': True, 'priority': 1}]}
        self.ledger.update('daily_reports', report['id'], report['version'], {'receipts': [{
            'action': 'daily_retrospective', 'result': result}]})
        with patch('autopilot.daily.time.time', return_value=self.now):
            daily.tick(self.scheduler)
        finished = self.ledger.get('daily_reports', report['id'])
        self.assertEqual(finished['conversion']['created'], 1)
        completion = [r for r in self.ledger.scoped('notifications', self.product['id'])
                      if r.get('event_key') == 'retro-analysis:2026-10-13'][0]
        self.assertIn('复盘完成', completion['subject'])
        self.assertIn('补齐页面定位调查证据', completion['payload']['text'])
        self.assertIn('已新增需求', completion['payload']['html'])
        self.assertIsNone(notify.enqueue_retrospective_completion(self.scheduler, self.product, finished, self.now))
        self.morning()
        with patch.object(notify, 'connect', return_value=SMTP()):
            notify.process(self.scheduler, self.now, {self.product['id']})
            notify.process(self.scheduler, self.now, {self.product['id']})
        self.assertEqual(len(sent), 2)
        self.assertEqual(hashlib.sha256(Path(initial['payload']['html_report']['path']).read_bytes()).hexdigest(), original_hash)
        self.assertEqual(len([r for r in self.ledger.scoped('notifications', self.product['id'])
                              if r.get('event_key') == 'retro-analysis:2026-10-13']), 1)

    def test_completed_analysis_upgrades_unsent_fact_email_without_second_send(self):
        item = self.morning()
        report = self.ledger.get('daily_reports', item['payload']['report_id'])
        self.ledger.update('daily_reports', report['id'], report['version'], {'receipts': [{
            'action': 'daily_retrospective', 'result': {'status': 'pass', 'summary': '完整证据已核对',
                'next_actions': ['继续验证覆盖缺口'], 'requirement_candidates': []}}]})
        with patch('autopilot.daily.time.time', return_value=self.now):
            daily.tick(self.scheduler)
        records = self.ledger.scoped('notifications', self.product['id'])
        self.assertEqual(sum(r['status'] == 'queued' for r in records), 1)
        self.assertEqual(sum(r['status'] == 'cancelled' for r in records), 1)
        upgraded = self.ledger.get('notifications', item['id'])
        self.assertIn('智能复盘结论', upgraded['payload']['text'])
        self.assertIn('完整证据已核对', upgraded['payload']['html'])


if __name__ == '__main__':
    unittest.main()
