"""邮件通知能力：凭据隔离、收件人、五类触发、简报质量与失败退避的行为验证。

使用假 SMTP 与临时回环 SMTP 服务验证协议；不访问外部邮箱、不使用真实凭据。
"""
import datetime as dt
import json
import os
from pathlib import Path
import smtplib
import socketserver
import sys
import tempfile
import time
import threading
import unittest
import uuid
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'dsh-gpt-supervisor/scripts'), str(ROOT / 'tests')]
from review_core import Store
from autopilot.api import Control
from autopilot.scheduler import Scheduler
from autopilot import notifications as notify

ZONE = ZoneInfo('Asia/Shanghai')


class FakeSMTP:
    """记录登录与发送，不产生任何网络连接。"""
    sent = []
    logins = []
    fail = None
    auth_fail = False
    refused = {}

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout

    def starttls(self):
        return None

    def login(self, user, code):
        if FakeSMTP.auth_fail:
            raise smtplib.SMTPAuthenticationError(535, b'bad credentials')
        FakeSMTP.logins.append((user, code))

    def send_message(self, message, from_addr=None, to_addrs=None):
        if FakeSMTP.fail:
            raise FakeSMTP.fail
        FakeSMTP.sent.append((message, from_addr, list(to_addrs or [])))
        return dict(FakeSMTP.refused)

    def quit(self):
        return None

    @classmethod
    def reset(cls):
        cls.sent, cls.logins, cls.fail, cls.auth_fail = [], [], None, False
        cls.refused = {}


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'state')
        self.control = Control(self.store)
        self.ledger = self.control.ledger
        self.product = self.ledger.create('products', {
            'name': '测试项目', 'source': str(self.root), 'goal': '验证通知能力',
            'policy': {'runs_per_day': 5, 'discovery_per_day': 4, 'tokens_per_day': 1000}}, 'paused')
        self.pid = self.product['id']
        self.env = patch.dict(os.environ, {'DSH_HOME': str(self.root / 'home')})
        self.env.start()
        FakeSMTP.reset()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    # -------------------------------------------------- helpers

    def configure(self, events=None, enabled=True, recipients=None,
                  credential='synthetic-code', sender='sender@example.test', patch_smtp=True):
        config = {
            'enabled': enabled, 'sender': sender,
            'recipients': ['ops@example.test'] if recipients is None else recipients,
            'smtp': {'host': 'smtp.example.test', 'port': 465, 'tls': 'ssl'},
            'events': {key: bool((events or {}).get(key)) for key in notify.EVENTS},
            'morning_hour': 7,
        }
        body = {'config': config}
        if credential is not None:
            body['credential'] = credential
        if patch_smtp:
            with patch.object(notify.smtplib, 'SMTP_SSL', FakeSMTP):
                return self.control.mutate(f'/products/{self.pid}/notifications/configure', body)
        return self.control.mutate(f'/products/{self.pid}/notifications/configure', body)

    def items(self, kind=None):
        rows = self.ledger.scoped('notifications', self.pid)
        return [row for row in rows if kind is None or row['kind'] == kind]

    def tick(self, now=None):
        scheduler = Scheduler(self.store)
        if now is None:
            with patch.object(notify.smtplib, 'SMTP_SSL', FakeSMTP):
                notify.tick(scheduler)
        else:
            with patch.object(notify.smtplib, 'SMTP_SSL', FakeSMTP), \
                 patch('autopilot.notifications.time.time', return_value=now):
                notify.tick(scheduler)
        return scheduler

    def add_run(self, status='accepted', title='需求甲', reason='', **extra):
        run = self.ledger.create('runs', {'product_id': self.pid, 'title': title, 'reason': reason, **extra}, status)
        if status == 'accepted':
            self.ledger.update('runs', run['id'], run['version'], {'accepted_at': time.time() + 0.5})
        return run

    # -------------------------------------------------- 凭据与配置

    def test_real_loopback_smtp_partial_refusal_and_targeted_retry(self):
        self.configure(recipients=['first@example.test', 'second@example.test'])
        attempts = []
        messages = []

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.wfile.write(b'220 local-test ESMTP\r\n')
                recipients = []
                while line := self.rfile.readline():
                    command = line.split(b' ', 1)[0].strip().upper()
                    if command == b'EHLO':
                        reply = b'250-local-test\r\n250 AUTH PLAIN\r\n'
                    elif command == b'AUTH':
                        reply = b'235 authenticated\r\n'
                    elif command == b'MAIL':
                        recipients = []
                        reply = b'250 sender accepted\r\n'
                    elif command == b'RCPT':
                        address = line.decode().split('<', 1)[1].split('>', 1)[0]
                        recipients.append(address)
                        reply = b'450 temporarily refused\r\n' if not messages and address == 'second@example.test' else b'250 accepted\r\n'
                    elif command == b'DATA':
                        self.wfile.write(b'354 send data\r\n')
                        body = []
                        while (row := self.rfile.readline()) not in (b'.\r\n', b''):
                            body.append(row)
                        messages.append(b''.join(body))
                        attempts.append(recipients[:])
                        reply = b'250 queued\r\n'
                    elif command == b'QUIT':
                        self.wfile.write(b'221 bye\r\n')
                        return
                    else:
                        reply = b'250 ok\r\n'
                    self.wfile.write(reply)

        with socketserver.TCPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                item = notify.send_test(self.ledger, self.pid)
                with patch.object(notify, 'connect', side_effect=lambda _: smtplib.SMTP(*server.server_address, timeout=3)):
                    notify.send(Scheduler(self.store), item, 100)
                    item = self.ledger.get('notifications', item['id'])
                    self.assertEqual(item['status'], 'queued')
                    self.assertEqual(item['receipt']['to'], ['first@example.test'])
                    notify.send(Scheduler(self.store), item, 160)
                self.assertEqual(self.ledger.get('notifications', item['id'])['status'], 'sent')
                self.assertEqual(attempts, [['first@example.test', 'second@example.test'], ['second@example.test']])
                self.assertEqual(len(messages), 2)
            finally:
                server.shutdown()
                thread.join()

    def test_partial_refusal_retries_only_rejected_recipient_after_restart(self):
        self.configure(recipients=['first@example.test', 'second@example.test'])
        item = notify.send_test(self.ledger, self.pid)
        FakeSMTP.refused = {'second@example.test': (450, b'try later')}
        scheduler = Scheduler(self.store)
        with patch.object(notify.smtplib, 'SMTP_SSL', FakeSMTP):
            notify.send(scheduler, item, 100)
        pending = self.ledger.get('notifications', item['id'])
        self.assertEqual(pending['status'], 'queued')
        self.assertEqual(pending['receipt']['to'], ['first@example.test'])
        self.assertEqual(pending['receipt']['refused']['second@example.test']['code'], 450)
        self.assertEqual(pending['next_attempt_at'], 160)
        FakeSMTP.refused = {}
        with patch.object(notify.smtplib, 'SMTP_SSL', FakeSMTP):
            notify.process(Scheduler(self.store), 160)
        sent = self.ledger.get('notifications', item['id'])
        self.assertEqual(sent['status'], 'sent')
        self.assertEqual(sent['receipt']['to'], ['first@example.test', 'second@example.test'])
        self.assertEqual(FakeSMTP.sent[1][2], ['second@example.test'])
        self.assertEqual(FakeSMTP.sent[0][0]['Message-ID'], FakeSMTP.sent[1][0]['Message-ID'])

    def test_persistent_partial_refusal_is_never_reported_as_full_success(self):
        self.configure(recipients=['first@example.test', 'second@example.test'])
        item = notify.send_test(self.ledger, self.pid)
        FakeSMTP.refused = {'second@example.test': (550, b'unknown recipient')}
        with patch.object(notify.smtplib, 'SMTP_SSL', FakeSMTP):
            for attempt in range(notify.MAX_ATTEMPTS):
                notify.send(Scheduler(self.store), item, 100 + attempt * 3600)
                item = self.ledger.get('notifications', item['id'])
        self.assertEqual(item['status'], 'partial')
        self.assertEqual(item['receipt']['to'], ['first@example.test'])
        self.assertEqual(notify.get(self.ledger, self.pid)['stats']['partial'], 1)
        self.assertTrue(all(to == ['second@example.test'] for _, _, to in FakeSMTP.sent[1:]))
        with patch.object(notify.smtplib, 'SMTP_SSL', FakeSMTP):
            self.assertFalse(notify.process(Scheduler(self.store), 100000))

    def test_all_recipients_refused_records_failure_without_sent_recipients(self):
        self.configure()
        item = notify.send_test(self.ledger, self.pid)
        FakeSMTP.fail = smtplib.SMTPRecipientsRefused({'ops@example.test': (550, b'unknown recipient')})
        with patch.object(notify.smtplib, 'SMTP_SSL', FakeSMTP):
            for attempt in range(notify.MAX_ATTEMPTS):
                notify.send(Scheduler(self.store), item, attempt * 3600)
                item = self.ledger.get('notifications', item['id'])
        self.assertEqual(item['status'], 'failed')
        self.assertEqual(item['receipt']['to'], [])
        self.assertEqual(item['receipt']['refused']['ops@example.test']['code'], 550)

    def test_transport_failure_after_partial_success_preserves_accepted_recipients(self):
        self.configure(recipients=['first@example.test', 'second@example.test'])
        item = notify.send_test(self.ledger, self.pid)
        FakeSMTP.refused = {'second@example.test': (450, b'try later')}
        with patch.object(notify.smtplib, 'SMTP_SSL', FakeSMTP):
            notify.send(Scheduler(self.store), item, 100)
            item = self.ledger.get('notifications', item['id'])
            FakeSMTP.fail = OSError('connection reset')
            notify.send(Scheduler(self.store), item, 160)
        item = self.ledger.get('notifications', item['id'])
        self.assertEqual(item['receipt']['to'], ['first@example.test'])
        FakeSMTP.fail = None
        FakeSMTP.refused = {}
        with patch.object(notify.smtplib, 'SMTP_SSL', FakeSMTP):
            notify.send(Scheduler(self.store), item, 280)
        self.assertEqual(FakeSMTP.sent[-1][2], ['second@example.test'])

    def test_plain_text_and_html_both_include_event_focus(self):
        self.configure(events={'run_report': True, 'alert': True})
        self.add_run(status='plan_review', title='构建任务')
        self.add_run(status='blocked', title='巡检告警', reason='正式接口不可用')
        self.tick()
        for kind, title, detail in [('run_report', '构建任务', 'plan_review'),
                                    ('alert', '巡检告警', '正式接口不可用')]:
            payload = self.items(kind)[0]['payload']
            self.assertIn(title, payload['text'])
            self.assertIn(detail, payload['text'])
            self.assertIn(detail, payload['html'])

    def test_configure_masks_sender_and_never_persists_credential(self):
        result = self.configure()
        self.assertTrue(result['credential_configured'])
        self.assertTrue(result['sender_configured'])
        self.assertEqual(result['sender_masked'], 'sen****@example.test')
        self.assertNotIn('sender', result)
        path = notify.credential_file()
        self.assertEqual(path.read_text(), 'synthetic-code')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertNotIn('synthetic-code', json.dumps(self.ledger.get('products', self.pid)))
        with self.store.connect() as db:
            events = ''.join(row[0] for row in db.execute('SELECT detail FROM events'))
        self.assertNotIn('synthetic-code', events)

    def test_invalid_credential_does_not_overwrite_existing(self):
        self.configure()
        before = notify.credential_file().read_text()

        class RejectingSMTP(FakeSMTP):
            def login(self, user, code):
                raise smtplib.SMTPAuthenticationError(535, b'bad')
        with patch.object(notify.smtplib, 'SMTP_SSL', RejectingSMTP):
            with self.assertRaises(smtplib.SMTPAuthenticationError):
                self.control.mutate(f'/products/{self.pid}/notifications/configure',
                                    {'config': {'enabled': True, 'sender': 'sender@example.test',
                                                'recipients': ['ops@example.test']},
                                     'credential': 'replacement-code'})
        self.assertEqual(notify.credential_file().read_text(), before)

    def test_recipients_grow_dedupe_and_reject_header_injection(self):
        self.configure(recipients=['a@example.test'])
        result = self.control.mutate(f'/products/{self.pid}/notifications/recipients',
                                     {'recipients': ['b@example.test', 'b@example.test']})
        self.assertEqual(result['recipients'], ['a@example.test', 'b@example.test'])
        for bad in ('not-an-email', 'x@example.test\nBcc: evil@example.test', 'x@example.test, y@example.test'):
            with self.assertRaises(ValueError):
                self.control.mutate(f'/products/{self.pid}/notifications/recipients', {'recipient': bad})
        self.assertEqual(notify.get(self.ledger, self.pid)['recipients'], ['a@example.test', 'b@example.test'])

    def test_enabled_requires_sender_and_recipient(self):
        with self.assertRaises(ValueError):
            self.configure(recipients=[], patch_smtp=False)
        with self.assertRaises(ValueError):
            self.configure(sender='', patch_smtp=False)

    def test_malformed_notifications_config_is_rejected_by_project_validation(self):
        with self.assertRaises(ValueError):
            self.control.mutate('/products', {'config': {'name': '坏配置', 'source': str(self.root),
                'goal': 'g', 'notifications': {'enabled': 'yes'}}})

    # -------------------------------------------------- 发送与简报质量

    def test_send_builds_multipart_brief_with_receipt(self):
        self.configure(events={'run_report': True})
        self.add_run(title='已经验收的需求')
        self.tick()
        rows = self.items('run_report')
        self.assertEqual(len(rows), 1)
        item = self.ledger.get('notifications', rows[0]['id'])
        self.assertEqual(item['status'], 'sent')
        self.assertTrue(item['receipt']['message_id'])
        self.assertEqual(item['receipt']['to'], ['ops@example.test'])
        self.assertEqual(len(FakeSMTP.sent), 1)
        message, sender, recipients = FakeSMTP.sent[0]
        self.assertTrue(message.is_multipart())
        self.assertEqual(sender, 'sender@example.test')
        self.assertEqual(recipients, ['ops@example.test'])
        self.assertEqual(message['From'], 'sender@example.test')
        text = message.get_body(preferencelist=('plain',)).get_content()
        body_html = message.get_body(preferencelist=('html',)).get_content()
        for header in ('一、概览指标', '二、昨日完成', '三、阻塞与问题', '四、额度使用', '五、竞品动态', '六、今日待办与自进化建议'):
            self.assertIn(header, text)
            self.assertIn(header, body_html)
        self.assertIn('已经验收的需求', text)
        self.assertNotIn('synthetic-code', text)

    def test_brief_numbers_come_from_ledger(self):
        self.configure(events={'run_report': True})
        self.add_run(title='需求一')
        self.add_run(title='需求二')
        self.tick()
        text = self.items('run_report')[0]['payload']['text']
        self.assertIn('业务验收通过：2', text)

    def test_same_event_is_sent_once_across_ticks_and_restart(self):
        self.configure(events={'run_report': True})
        self.add_run()
        self.tick()
        self.tick()
        self.tick()
        self.tick()
        self.assertEqual(len(self.items('run_report')), 1)
        self.assertEqual(len(FakeSMTP.sent), 1)
        self.assertEqual(len(self.items()), 1)

    # -------------------------------------------------- 五类触发

    def test_alert_trigger_for_blocked_run(self):
        self.configure(events={'alert': True})
        self.add_run(status='blocked', title='阻塞需求', reason='缺少运行依赖')
        self.tick()
        rows = self.items('alert')
        self.assertEqual(len(rows), 1)
        self.assertIn('缺少运行依赖', rows[0]['payload']['text'])

    def test_quota_exhausted_trigger(self):
        self.ledger.update('products', self.pid, self.product['version'],
                           {'policy': {'runs_per_day': 1, 'discovery_per_day': 1, 'tokens_per_day': 1}})
        self.configure(events={'quota_exhausted': True})
        self.ledger.budget(self.pid, 'tokens', 1)
        self.tick()
        rows = self.items('quota_exhausted')
        self.assertEqual(len(rows), 1)
        self.assertIn('已用尽', rows[0]['payload']['text'])

    def test_competitor_trigger(self):
        self.configure(events={'competitor': True})
        self.ledger.create('research_jobs', {'product_id': self.pid, 'action': 'find_competitors',
            'result': {'competitors': [{'name': '竞品甲'}]}, 'reason': ''}, 'completed')
        self.ledger.create('competitors', {'product_id': self.pid, 'name': '竞品甲'}, 'active')
        self.tick()
        rows = self.items('competitor')
        self.assertEqual(len(rows), 1)
        self.assertIn('竞品甲', rows[0]['payload']['text'])

    def test_run_report_trigger_for_plan_review(self):
        self.configure(events={'run_report': True})
        self.add_run(status='plan_review', title='方案完成需求')
        self.tick()
        rows = self.items('run_report')
        self.assertEqual(len(rows), 1)
        self.assertIn('方案完成需求', rows[0]['subject'])

    def test_morning_retro_trigger_records_problems_and_is_idempotent(self):
        self.configure(events={'morning_retro': True})
        self.add_run(status='blocked', title='复盘阻塞', reason='接口超时')
        local = dt.datetime.fromtimestamp(time.time(), ZONE)
        target = (local.replace(hour=7, minute=0, second=0, microsecond=0) + dt.timedelta(days=1)).timestamp()
        self.tick(target)
        rows = self.items('morning_retro')
        self.assertEqual(len(rows), 1)
        self.assertIn('昨日研发简报', rows[0]['subject'])
        self.assertIn('复盘阻塞', rows[0]['payload']['text'])
        problems = self.ledger.scoped('problems', self.pid)
        self.assertTrue(problems)
        self.assertIn('接口超时', json.dumps(problems, ensure_ascii=False))
        self.tick(target)
        self.assertEqual(len(self.items('morning_retro')), 1)

    def test_retro_problem_links_requirement_and_signals(self):
        # 复盘问题结构化落库时必须保留需求与信号来源，供后续自进化消费。
        self.configure(events={'morning_retro': True})
        signal = self.ledger.create('signals', {'product_id': self.pid, 'source': 'test', 'code': 'code',
            'summary': '信号', 'evidence': '证据'}, 'pending')
        requirement = self.ledger.create('requirements', {'product_id': self.pid, 'title': '关联需求',
            'signal_ids': [signal['id']], 'acceptance': ['验收'], 'evidence': '证据', 'impact': '重要'}, 'pending')
        self.ledger.create('runs', {'product_id': self.pid, 'requirement_id': requirement['id'],
            'title': '关联阻塞', 'reason': '接口超时'}, 'blocked')
        local = dt.datetime.fromtimestamp(time.time(), ZONE)
        target = (local.replace(hour=7, minute=0, second=0, microsecond=0) + dt.timedelta(days=1)).timestamp()
        self.tick(target)
        problems = self.ledger.scoped('problems', self.pid)
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]['requirement_id'], requirement['id'])
        self.assertEqual(problems[0]['signal_ids'], [signal['id']])

    # -------------------------------------------------- 失败处理

    def test_auth_failure_blocks_and_keeps_previous_credential(self):
        self.configure(events={'alert': True})
        before = notify.credential_file().read_text()
        self.add_run(status='blocked', title='认证失败用例', reason='依赖缺失')
        FakeSMTP.auth_fail = True
        self.tick()
        rows = self.items('alert')
        self.assertEqual(rows[0]['status'], 'blocked')
        self.assertIn('认证失败', rows[0]['error'])
        self.assertEqual(notify.credential_file().read_text(), before)
        self.tick()
        self.assertEqual(len(FakeSMTP.sent), 0)

    def test_transient_failure_backs_off_and_tick_does_not_raise(self):
        self.configure(events={'alert': True})
        self.add_run(status='blocked', title='瞬时失败用例', reason='依赖缺失')
        FakeSMTP.fail = smtplib.SMTPConnectError(421, b'try later')
        self.tick()
        rows = self.items('alert')
        self.assertEqual(rows[0]['status'], 'queued')
        self.assertEqual(rows[0]['attempts'], 1)
        self.assertGreater(rows[0]['next_attempt_at'], time.time())
        self.assertLessEqual(rows[0]['next_attempt_at'], time.time() + 3600)
        before = rows[0]['attempts']
        self.tick()
        self.assertEqual(self.items('alert')[0]['attempts'], before)
        self.assertEqual(len(FakeSMTP.sent), 0)

    def test_reconfigure_requeues_previously_blocked_delivery(self):
        self.configure(events={'alert': True})
        self.add_run(status='blocked', title='恢复用例', reason='依赖缺失')
        FakeSMTP.auth_fail = True
        self.tick()
        self.assertEqual(self.items('alert')[0]['status'], 'blocked')
        FakeSMTP.auth_fail = False
        self.configure(events={'alert': True})
        self.assertEqual(self.items('alert')[0]['status'], 'queued')
        self.tick()
        self.assertEqual(self.items('alert')[0]['status'], 'sent')
        self.assertEqual(len(FakeSMTP.sent), 1)

    def test_disabled_notifications_do_nothing(self):
        self.configure(enabled=False, events={key: True for key in notify.EVENTS})
        self.add_run(status='blocked', title='不应通知', reason='理由')
        self.tick()
        self.assertEqual(self.items(), [])

    def test_send_test_enqueues_and_delivers(self):
        self.configure()
        queued = self.control.mutate(f'/products/{self.pid}/notifications/send-test', {})
        self.assertEqual(queued['kind'], 'test')
        self.assertEqual(queued['status'], 'queued')
        self.tick()
        rows = self.items('test')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'sent')
        self.assertIn('通知测试简报', rows[0]['subject'])

    def test_send_test_delivers_while_notifications_disabled(self):
        # 测试简报是人工验证动作，总开关关闭时也应送达；但事件类通知不得外发。
        self.configure(enabled=False, events={key: True for key in notify.EVENTS})
        self.add_run(status='blocked', title='不应因测试而外发', reason='理由')
        queued = self.control.mutate(f'/products/{self.pid}/notifications/send-test', {})
        self.assertEqual(queued['status'], 'queued')
        self.tick()
        self.assertEqual(len(FakeSMTP.sent), 1)
        rows = self.items('test')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'sent')
        self.assertEqual(self.items('alert'), [])

    # -------------------------------------------------- 集成与静态边界

    def test_scheduler_tick_invokes_notifications(self):
        with patch('autopilot.notifications.tick') as hook:
            Scheduler(self.store).tick()
        hook.assert_called_once()

    def test_operation_fingerprint_never_stores_credential(self):
        import dashboard_server as dashboard
        app = dashboard.Dashboard(self.root / 'state')
        body = {'operation_id': str(uuid.uuid4()),
                'config': {'enabled': True, 'sender': 'sender@example.test',
                           'recipients': ['ops@example.test'],
                           'smtp': {'host': 'smtp.example.test', 'port': 465, 'tls': 'ssl'}},
                'credential': 'synthetic-code'}
        with patch.object(notify.smtplib, 'SMTP_SSL', FakeSMTP):
            result = app.operation(f'/products/{self.pid}/notifications/configure', body)
        self.assertTrue(result['credential_configured'])
        with self.store.connect() as db:
            rows = [dict(row) for row in db.execute('SELECT fingerprint,response FROM operations')]
        self.assertNotIn('synthetic-code', json.dumps(rows))
        self.assertTrue(any(row['fingerprint'].startswith('sha256:') for row in rows))



if __name__ == '__main__':
    unittest.main()
