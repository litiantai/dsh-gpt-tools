"""邮件通知能力：凭据隔离、收件人、五类触发、简报质量与失败退避的行为验证。

全部使用注入的假 SMTP 客户端：测试零网络、零真实凭据，不启动服务或后台进程。
"""
import datetime as dt
import json
import os
from pathlib import Path
import smtplib
import socketserver
import sqlite3
import stat
import subprocess
import threading
from contextlib import contextmanager
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'dsh-gpt-supervisor/scripts'), str(ROOT / 'tests')]
from review_core import Store, Conflict
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

    def active_path(self, sender=None, product_id=None):
        """测试通过持久化配置引用读取当前凭据，不依赖旧可覆盖文件。"""
        product_id = product_id or self.pid
        config = self.ledger.get('products', product_id)['notifications']
        return notify.credential_file(product_id, sender or config['sender'],
                                      credential_ref=config['credential_ref'])

    def active_code(self, product_id=None):
        product_id = product_id or self.pid
        product = self.ledger.get('products', product_id)
        return notify.credential_from_config(product_id, notify.normalize(product))

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
            run = self.ledger.update('runs', run['id'], run['version'], {'accepted_at': time.time() + 0.5})
        return run

    # -------------------------------------------------- 凭据与配置

    def test_configure_masks_sender_and_never_persists_credential(self):
        result = self.configure()
        self.assertTrue(result['credential_configured'])
        self.assertTrue(result['sender_configured'])
        self.assertEqual(result['sender_masked'], 'sen****@example.test')
        self.assertNotIn('sender', result)
        path = self.active_path('sender@example.test')
        self.assertEqual(path.read_text(), 'synthetic-code')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertNotIn('synthetic-code', json.dumps(self.ledger.get('products', self.pid)))
        with self.store.connect() as db:
            events = ''.join(row[0] for row in db.execute('SELECT detail FROM events'))
        self.assertNotIn('synthetic-code', events)

    def test_invalid_credential_does_not_overwrite_existing(self):
        self.configure()
        before = self.active_path('sender@example.test').read_text()

        class RejectingSMTP(FakeSMTP):
            def login(self, user, code):
                raise smtplib.SMTPAuthenticationError(535, b'bad')
        with patch.object(notify.smtplib, 'SMTP_SSL', RejectingSMTP):
            with self.assertRaises(smtplib.SMTPAuthenticationError):
                self.control.mutate(f'/products/{self.pid}/notifications/configure',
                                    {'config': {'enabled': True, 'sender': 'sender@example.test',
                                                'recipients': ['ops@example.test']},
                                     'credential': 'replacement-code'})
        self.assertEqual(self.active_path('sender@example.test').read_text(), before)

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

    def _configure_product(self, product_id, sender, code, recipients, events=None):
        config = {
            'enabled': True, 'sender': sender, 'recipients': recipients,
            'smtp': {'host': 'smtp.example.test', 'port': 465, 'tls': 'ssl'},
            'events': {key: bool((events or {}).get(key)) for key in notify.EVENTS},
            'morning_hour': 7,
        }
        with patch.object(notify.smtplib, 'SMTP_SSL', FakeSMTP):
            return self.control.mutate(f'/products/{product_id}/notifications/configure',
                                       {'config': config, 'credential': code})

    def test_credentials_isolated_per_product_and_sender(self):
        # 项目 B 保存不同发件账户时不得覆盖项目 A 已有授权码。
        self.configure(events={'alert': True}, sender='a@example.test', credential='code-a')
        other = self.ledger.create('products', {'name': '项目乙', 'source': str(self.root),
                                                'goal': '验证凭据隔离'}, 'paused')
        self._configure_product(other['id'], 'b@example.test', 'code-b', ['ops-b@example.test'])
        path_a = self.active_path('a@example.test')
        path_b = self.active_path('b@example.test', other['id'])
        self.assertNotEqual(path_a, path_b)
        self.assertEqual(path_a.read_text(), 'code-a')
        self.assertEqual(path_b.read_text(), 'code-b')
        self.assertEqual(self.active_code(), 'code-a')
        self.assertEqual(self.active_code(other['id']), 'code-b')
        self.assertEqual(path_a.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path_a.parent.stat().st_mode & 0o777, 0o700)
        # 项目 A 实际投递时必须使用 A 自己的授权码，而不是最后写入的 B 凭据。
        self.add_run(status='blocked', title='凭据隔离投递', reason='依赖缺失')
        self.tick()
        self.assertIn(('a@example.test', 'code-a'), FakeSMTP.logins)
        self.assertNotIn(('a@example.test', 'code-b'), FakeSMTP.logins)

    def test_legacy_global_credential_migrates_and_is_readable(self):
        legacy = notify.credential_file()
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text('legacy-code')
        self.assertTrue(notify.credential_configured(self.pid, 'sender@example.test'))
        self.assertEqual(notify.credential(self.pid, 'sender@example.test'), 'legacy-code')
        scoped = notify.credential_file(self.pid, 'sender@example.test')
        self.assertFalse(scoped.is_file())
        self.assertEqual(legacy.read_text(), 'legacy-code')
        # 为同一发件人保存新码只更新作用域副本；其他发件人仍能回退读到旧全局码。
        self.configure(sender='sender@example.test', credential='new-code')
        self.assertEqual(self.active_code(), 'new-code')
        self.assertEqual(notify.credential(self.pid, 'other@example.test'), 'legacy-code')
        self.assertEqual(legacy.read_text(), 'legacy-code')

    # -------------------------------------------------- 主干 SMTP 协议与拒收回归

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
        # 先在当天真实观察发生，再测试翌日复盘；不能由明天的当前状态倒推昨天。
        self.tick()
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
        before = self.active_path('sender@example.test').read_text()
        self.add_run(status='blocked', title='认证失败用例', reason='依赖缺失')
        FakeSMTP.auth_fail = True
        self.tick()
        rows = self.items('alert')
        self.assertEqual(rows[0]['status'], 'blocked')
        self.assertIn('认证失败', rows[0]['error'])
        self.assertEqual(self.active_path('sender@example.test').read_text(), before)
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

    # -------------------------------------------------- 缺陷回归

    def test_refused_recipient_is_recorded_and_retried_alone(self):
        # send_message 返回的拒收地址不得被记为已发送，且重试只针对被拒地址。
        self.configure(recipients=['a@example.test', 'b@example.test'], events={'alert': True})
        self.add_run(status='blocked', title='拒收用例', reason='依赖缺失')

        class PartialSMTP(FakeSMTP):
            refuse = {'b@example.test': (550, b'no such user')}

            def send_message(self, message, from_addr=None, to_addrs=None):
                FakeSMTP.sent.append((message, from_addr, list(to_addrs or [])))
                return {address: self.refuse[address] for address in (to_addrs or [])
                        if address in self.refuse}

        with patch.object(notify.smtplib, 'SMTP_SSL', PartialSMTP):
            notify.tick(Scheduler(self.store))
        row = self.items('alert')[0]
        self.assertEqual(row['status'], 'queued')
        self.assertEqual(row['attempts'], 1)
        self.assertEqual(row['receipt']['to'], ['a@example.test'])
        self.assertEqual(set(row['receipt']['refused']), {'b@example.test'})
        self.assertEqual(row['receipt']['refused']['b@example.test']['code'], 550)
        self.assertIn('b@example.test', row['error'])
        self.assertEqual(FakeSMTP.sent[0][2], ['a@example.test', 'b@example.test'])

        PartialSMTP.refuse = {}
        with patch.object(notify.smtplib, 'SMTP_SSL', PartialSMTP):
            self.tick(row['next_attempt_at'] + 1)
        row = self.ledger.get('notifications', row['id'])
        self.assertEqual(row['status'], 'sent')
        self.assertEqual(row['receipt']['to'], ['a@example.test', 'b@example.test'])
        self.assertEqual(FakeSMTP.sent[1][2], ['b@example.test'])

    def test_queued_notification_beyond_list_window_is_delivered(self):
        # Ledger.list 只回最近 1000 条；待发送通知不能被历史记录挤出而永久滞留。
        self.configure(events={})
        queued = self.ledger.create('notifications', {
            'product_id': self.pid, 'kind': 'test', 'event_key': 'old-queued',
            'subject': '旧队列通知', 'payload': {'text': 't', 'html': '<p>t</p>'},
            'attempts': 0, 'next_attempt_at': 0, 'receipt': None, 'error': ''}, 'queued')
        clock = {'value': queued['created']}

        def advance():
            clock['value'] += 0.001
            return clock['value']

        with patch('time.time', advance):
            for index in range(1005):
                self.ledger.create('notifications', {
                    'product_id': self.pid, 'kind': 'test', 'event_key': f'history:{index}',
                    'subject': '历史记录', 'payload': {}, 'attempts': 0,
                    'next_attempt_at': 0, 'receipt': None, 'error': ''}, 'sent')
        self.assertNotIn(queued['id'], [row['id'] for row in self.ledger.list('notifications')])
        self.tick()
        self.assertEqual(self.ledger.get('notifications', queued['id'])['status'], 'sent')

    def test_problem_ids_are_isolated_per_project(self):
        # 不同项目的同名同因问题必须各自落库，不能被另一项目的记录覆盖关联。
        self.configure(events={})
        other = self.ledger.create('products', {'name': '另一项目', 'source': str(self.root),
                                                'goal': '验证问题隔离'}, 'paused')
        issues = [{'source': 'task', 'id': 'run-1', 'title': '同名问题', 'cause': '同因'}]
        first = notify.record_problems(self.ledger, self.ledger.get('products', self.pid), issues)
        second = notify.record_problems(self.ledger, self.ledger.get('products', other['id']), issues)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first[0]['id'], second[0]['id'])
        self.assertEqual(len(self.ledger.scoped('problems', self.pid)), 1)
        self.assertEqual(len(self.ledger.scoped('problems', other['id'])), 1)
        self.assertEqual(second[0]['product_id'], other['id'])

    def test_problem_persisted_on_occurrence_survives_recovery_before_retro(self):
        # 昨日发生、早七前已恢复的问题必须仍进入昨日复盘，而不是因当前快照无阻塞而丢失。
        self.configure(events={'morning_retro': True})
        run = self.add_run(status='blocked', title='早七前恢复', reason='瞬时依赖缺失')
        local = dt.datetime.fromtimestamp(time.time(), ZONE)
        target = (local.replace(hour=7, minute=0, second=0, microsecond=0) + dt.timedelta(days=1)).timestamp()
        _, start, end = notify.window_for_retro(target)
        occurred = start + 3600
        product = self.ledger.get('products', self.pid)
        notify.observe_problems(self.ledger, product, now=occurred)
        problems = self.ledger.scoped('problems', self.pid)
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]['status'], 'open')
        self.assertEqual(problems[0]['first_seen'], occurred)
        # 早七前恢复：run 变 accepted 后同一问题应被关闭并保留 closed_at，仍在复盘窗口内。
        self.ledger.update('runs', run['id'], run['version'],
                           {'status': 'accepted', 'accepted_at': end - 3600}, 'accepted')
        self.tick(target)
        problems = self.ledger.scoped('problems', self.pid)
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]['status'], 'resolved')
        self.assertEqual(problems[0]['closed_at'], target)
        rows = self.items('morning_retro')
        self.assertEqual(len(rows), 1)
        self.assertIn('早七前恢复', rows[0]['payload']['text'])
        self.assertIn('已恢复', rows[0]['payload']['text'])
        self.assertIn('早七前恢复', rows[0]['payload']['html'])
        self.assertIn('已恢复', rows[0]['payload']['html'])
        for body in (rows[0]['payload']['text'], rows[0]['payload']['html']):
            self.assertIn('窗口结束后、截至复盘时间已恢复', body)
            self.assertNotIn('窗口内已恢复', body)

    def test_observe_problems_closes_and_reopens_by_state_change(self):
        # 状态变化即时持久化：阻塞→恢复→再次阻塞，同一 (source, source_id) 只有一条记录。
        self.configure(events={})
        run = self.add_run(status='blocked', title='状态变化问题', reason='依赖缺失')
        product = self.ledger.get('products', self.pid)
        base = time.time()
        notify.observe_problems(self.ledger, product, now=base)
        problems = self.ledger.scoped('problems', self.pid)
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]['occurrences'], 1)
        first_seen = problems[0]['first_seen']
        run = self.ledger.update('runs', run['id'], run['version'], {'status': 'accepted'}, 'accepted')
        notify.observe_problems(self.ledger, product, now=base + 10)
        problems = self.ledger.scoped('problems', self.pid)
        self.assertEqual(problems[0]['status'], 'resolved')
        self.assertEqual(problems[0]['closed_at'], base + 10)
        run = self.ledger.update('runs', run['id'], run['version'],
                                 {'status': 'blocked', 'reason': '依赖再次缺失'}, 'blocked')
        notify.observe_problems(self.ledger, product, now=base + 20)
        problems = self.ledger.scoped('problems', self.pid)
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]['status'], 'open')
        self.assertEqual(problems[0]['occurrences'], 2)
        self.assertIsNone(problems[0]['closed_at'])
        self.assertEqual(problems[0]['first_seen'], first_seen)

    def test_fail_run_reason_reaches_plain_text_brief(self):
        # fail 任务与 focus 都必须进入纯文本简报，避免告警丢失失败原因。
        self.configure(events={'alert': True})
        self.add_run(status='fail', title='失败任务', reason='构建产物缺失')
        self.tick()
        rows = self.items('alert')
        self.assertEqual(len(rows), 1)
        self.assertIn('失败任务', rows[0]['payload']['text'])
        self.assertIn('构建产物缺失', rows[0]['payload']['text'])

    def test_same_project_distinct_runs_same_summary_stay_separate(self):
        # 同项目两个不同 run 同名同因必须各自落库，旧记录的来源关联不得被覆盖。
        self.configure(events={})
        signal = self.ledger.create('signals', {'product_id': self.pid, 'source': 'test', 'code': 'code',
            'summary': '信号', 'evidence': '证据'}, 'pending')
        requirement = self.ledger.create('requirements', {'product_id': self.pid, 'title': '关联需求',
            'signal_ids': [signal['id']], 'acceptance': ['验收'], 'evidence': '证据', 'impact': '重要'}, 'pending')
        run_a = self.add_run(status='blocked', title='同名阻塞', reason='同因',
                             requirement_id=requirement['id'])
        run_b = self.add_run(status='blocked', title='同名阻塞', reason='同因')
        product = self.ledger.get('products', self.pid)
        notify.observe_problems(self.ledger, product)
        problems = self.ledger.scoped('problems', self.pid)
        self.assertEqual(len(problems), 2)
        by_source = {problem['source_id']: problem for problem in problems}
        self.assertEqual(set(by_source), {run_a['id'], run_b['id']})
        self.assertEqual(by_source[run_a['id']]['run_id'], run_a['id'])
        self.assertEqual(by_source[run_a['id']]['requirement_id'], requirement['id'])
        self.assertEqual(by_source[run_a['id']]['signal_ids'], [signal['id']])
        self.assertEqual(by_source[run_b['id']]['run_id'], run_b['id'])
        self.assertIsNone(by_source[run_b['id']]['requirement_id'])
        self.assertEqual(by_source[run_b['id']]['signal_ids'], [])
        for problem in problems:
            self.assertEqual(problem['occurrences'], 1)
            self.assertEqual(problem['status'], 'open')

    def test_same_project_distinct_deliveries_same_summary_stay_separate(self):
        # 同一项目两个不同交付批次的同名同因问题也各自独立，source_id 不被互相覆盖。
        self.configure(events={})
        first = self.ledger.create('deliveries', {'product_id': self.pid, 'branch': 'feat/same',
            'title': '同一批次', 'reason': '同一原因'}, 'blocked')
        second = self.ledger.create('deliveries', {'product_id': self.pid, 'branch': 'feat/same',
            'title': '同一批次', 'reason': '同一原因'}, 'blocked')
        product = self.ledger.get('products', self.pid)
        notify.observe_problems(self.ledger, product)
        problems = self.ledger.scoped('problems', self.pid)
        self.assertEqual(len(problems), 2)
        self.assertEqual({problem['source_id'] for problem in problems}, {first['id'], second['id']})
        self.assertTrue(all(problem['source'] == 'delivery' for problem in problems))

    def test_same_identity_reappearance_never_overwrites_provenance(self):
        # 同一来源身份再次出现时只刷新状态，不得用新入参覆盖旧记录的需求/信号关联。
        self.configure(events={})
        signal_a = self.ledger.create('signals', {'product_id': self.pid, 'source': 'test', 'code': 'a',
            'summary': '信号A', 'evidence': '证据'}, 'pending')
        requirement_a = self.ledger.create('requirements', {'product_id': self.pid, 'title': '需求A',
            'signal_ids': [signal_a['id']], 'acceptance': ['验收'], 'evidence': '证据', 'impact': '重要'}, 'pending')
        signal_b = self.ledger.create('signals', {'product_id': self.pid, 'source': 'test', 'code': 'b',
            'summary': '信号B', 'evidence': '证据'}, 'pending')
        requirement_b = self.ledger.create('requirements', {'product_id': self.pid, 'title': '需求B',
            'signal_ids': [signal_b['id']], 'acceptance': ['验收'], 'evidence': '证据', 'impact': '重要'}, 'pending')
        product = self.ledger.get('products', self.pid)
        notify.record_problems(self.ledger, product, [
            {'source': 'task', 'id': 'run-1', 'title': '同一来源', 'cause': '原因A',
             'requirement_id': requirement_a['id']}])
        before = self.ledger.scoped('problems', self.pid)[0]
        notify.record_problems(self.ledger, product, [
            {'source': 'task', 'id': 'run-1', 'title': '同一来源', 'cause': '原因B',
             'requirement_id': requirement_b['id']}])
        problems = self.ledger.scoped('problems', self.pid)
        self.assertEqual(len(problems), 1)
        after = problems[0]
        self.assertEqual(after['id'], before['id'])
        self.assertEqual(after['source_id'], 'run-1')
        self.assertEqual(after['run_id'], 'run-1')
        self.assertEqual(after['requirement_id'], requirement_a['id'])
        self.assertEqual(after['signal_ids'], [signal_a['id']])
        self.assertEqual(after['occurrences'], 1)

    def test_observe_problems_resolves_only_recovered_duplicate(self):
        # 只关闭真正恢复的来源身份：同名同因的另一 run 仍保持 open。
        self.configure(events={})
        run_a = self.add_run(status='blocked', title='同名阻塞', reason='同因')
        run_b = self.add_run(status='blocked', title='同名阻塞', reason='同因')
        product = self.ledger.get('products', self.pid)
        base = time.time()
        notify.observe_problems(self.ledger, product, now=base)
        self.assertEqual(len(self.ledger.scoped('problems', self.pid)), 2)
        run_b = self.ledger.update('runs', run_b['id'], run_b['version'], {'status': 'accepted'}, 'accepted')
        notify.observe_problems(self.ledger, product, now=base + 10)
        problems = {problem['source_id']: problem for problem in self.ledger.scoped('problems', self.pid)}
        self.assertEqual(len(problems), 2)
        self.assertEqual(problems[run_a['id']]['status'], 'open')
        self.assertIsNone(problems[run_a['id']]['closed_at'])
        self.assertEqual(problems[run_b['id']]['status'], 'resolved')
        self.assertEqual(problems[run_b['id']]['closed_at'], base + 10)
        self.ledger.update('runs', run_a['id'], run_a['version'], {'status': 'accepted'}, 'accepted')
        notify.observe_problems(self.ledger, product, now=base + 20)
        problems = {problem['source_id']: problem for problem in self.ledger.scoped('problems', self.pid)}
        self.assertEqual(problems[run_a['id']]['status'], 'resolved')
        self.assertEqual(problems[run_a['id']]['closed_at'], base + 20)
        self.assertEqual(problems[run_b['id']]['closed_at'], base + 10)

    def test_legacy_summary_issue_does_not_absorb_identified_problem(self):
        # 来源身份不一致（无 id 的历史问题 vs 带 run id 的问题）不得因 summary 相同而合并。
        self.configure(events={})
        product = self.ledger.get('products', self.pid)
        notify.record_problems(self.ledger, product, [
            {'source': 'task', 'title': '同名问题', 'cause': '同因'}])
        notify.record_problems(self.ledger, product, [
            {'source': 'task', 'id': 'run-1', 'title': '同名问题', 'cause': '同因'}])
        problems = self.ledger.scoped('problems', self.pid)
        self.assertEqual(len(problems), 2)
        self.assertEqual(sorted(problem.get('source_id') or '' for problem in problems), ['', 'run-1'])

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

    def test_repository_contains_no_real_recipient_credentials(self):
        # 需求证据只用于说明配置方式；真实邮箱与授权码不得进入仓库任何文件。
        needles = ['19166' + '89406@qq.com', 'bymr' + 'weitbfsecjaa']
        skip = {'.git', 'node_modules', 'dist', '.playwright', '__pycache__', '.venv'}
        hits = []
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [name for name in dirs if name not in skip]
            for name in files:
                path = Path(base) / name
                try:
                    if path.stat().st_size > 2_000_000:
                        continue
                    content = path.read_text(errors='ignore')
                except OSError:
                    continue
                if any(needle in content for needle in needles):
                    hits.append(str(path.relative_to(ROOT)))
        self.assertEqual(hits, [])

    # -------------------------------------------------- 窗口、版本与不可变凭据事务回归

    def save_body(self, body):
        with patch.object(notify.smtplib, 'SMTP_SSL', FakeSMTP):
            return self.control.mutate(f'/products/{self.pid}/notifications/configure', body)

    def notification_snapshot(self):
        return self.ledger.get('products', self.pid)['notifications']

    def assert_saved_state_unchanged(self, before, code='synthetic-code'):
        self.assertEqual(self.ledger.get('products', self.pid), before)
        self.assertEqual(self.active_code(), code)

    def blocked_item(self):
        item = notify.send_test(self.ledger, self.pid)
        return self.ledger.update('notifications', item['id'], item['version'],
                                  {'error': '认证失败'}, 'blocked')

    def test_window_labels_match_actual_intervals_in_both_bodies(self):
        self.configure()
        product = self.ledger.get('products', self.pid)
        start = dt.datetime(2026, 10, 9, tzinfo=ZONE)
        cases = [
            (start, start + dt.timedelta(days=1), '2026-10-09 00:00–24:00', True),
            (start + dt.timedelta(hours=10), start + dt.timedelta(hours=11), '10:00', False),
            (start + dt.timedelta(hours=10), start + dt.timedelta(days=1, hours=10), '2026-10-10 10:00', False),
            (start + dt.timedelta(hours=23), start + dt.timedelta(days=1, hours=2), '2026-10-10 02:00', False),
            (start, start, '00:00', False),
        ]
        for left, right, expected, full_day in cases:
            with self.subTest(start=left, end=right):
                label = notify._window_label(left.timestamp(), right.timestamp())
                self.assertIn(expected, label)
                self.assertEqual('00:00–24:00' in label, full_day)
                _, payload = notify.render(self.ledger, product, 'test', right.timestamp(),
                    {'start': left.timestamp(), 'end': right.timestamp()})
                for body in (payload['text'], payload['html']):
                    self.assertIn(label, body)
                    self.assertEqual('00:00–24:00' in body, full_day)
        with self.assertRaises(ValueError):
            notify._window_label((start + dt.timedelta(hours=1)).timestamp(), start.timestamp())
        with self.assertRaises(ValueError):
            notify.render(self.ledger, product, 'test', start.timestamp(),
                          {'start': (start + dt.timedelta(hours=1)).timestamp(), 'end': start.timestamp()})

    def test_new_credentials_are_immutable_and_configuration_owns_reference(self):
        first = self.configure(credential='code-a')
        old_path = self.active_path()
        old_ref = self.notification_snapshot()['credential_ref']
        self.assertRegex(old_ref, r'^[0-9a-f]{32}$')
        second = self.save_body({'config_version': first['config_version'],
                                'config': {}, 'credential': 'code-b'})
        self.assertEqual(second['config_version'], first['config_version'] + 1)
        self.assertNotEqual(self.notification_snapshot()['credential_ref'], old_ref)
        self.assertEqual(old_path.read_text(), 'code-a')
        self.assertEqual(self.active_code(), 'code-b')
        self.assertEqual(self.active_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.active_path().parent.stat().st_mode & 0o777, 0o700)
        with self.assertRaises(FileExistsError):
            notify._write_credential(old_path, 'attempted-overwrite')
        self.assertEqual(old_path.read_text(), 'code-a')

    def test_stale_notification_and_legacy_product_versions_do_not_write(self):
        first = self.configure()
        old_product_version = self.ledger.get('products', self.pid)['version']
        second = self.save_body({'config_version': first['config_version'],
                                'config': {'morning_hour': 8}})
        before = self.ledger.get('products', self.pid)
        bodies = [
            {'config_version': first['config_version']},
            {'version': old_product_version},
            {'config_version': second['config_version'], 'version': old_product_version},
        ]
        for versions in bodies:
            with self.subTest(versions=versions), patch.object(notify, 'verify') as verify, \
                 patch.object(notify, 'save_credential') as save:
                with self.assertRaises(Conflict):
                    self.save_body(versions | {'config': {}, 'credential': 'rejected-code'})
                verify.assert_not_called()
                save.assert_not_called()
                self.assert_saved_state_unchanged(before)

    def test_implicit_version_detects_configuration_change_during_verify(self):
        self.configure()
        reference = self.notification_snapshot()['credential_ref']

        def concurrent_configuration(*_):
            notify.configure(self.ledger, self.pid, {'config': {'morning_hour': 8}})

        with patch.object(notify, 'verify', side_effect=concurrent_configuration):
            with self.assertRaises(Conflict):
                notify.configure(self.ledger, self.pid, {'config': {}, 'credential': 'stale-code'})
        self.assertEqual(self.notification_snapshot()['credential_ref'], reference)
        self.assertEqual(self.notification_snapshot()['morning_hour'], 8)
        self.assertEqual(self.active_code(), 'synthetic-code')

    def test_runtime_product_updates_do_not_conflict_with_notification_revision(self):
        initial = self.configure()

        def runtime_probe(*_):
            product = self.ledger.get('products', self.pid)
            self.ledger.update('products', self.pid, product['version'], {'probe_at': time.time()})

        with patch.object(notify, 'verify', side_effect=runtime_probe):
            result = notify.configure(self.ledger, self.pid,
                {'config_version': initial['config_version'], 'config': {}, 'credential': 'new-code'})
        self.assertEqual(result['config_version'], initial['config_version'] + 1)
        self.assertEqual(self.active_code(), 'new-code')
        current = self.ledger.get('products', self.pid)
        self.ledger.update('products', self.pid, current['version'], {'probe_at': time.time() + 1})
        no_change = self.save_body({'config_version': result['config_version'], 'config': {}})
        self.assertEqual(no_change['config_version'], result['config_version'])

    def test_two_requests_with_same_notification_revision_only_one_commits(self):
        initial = self.configure()
        barrier = threading.Barrier(2)
        results = []

        def verify(*_):
            barrier.wait(timeout=5)

        def save(code):
            try:
                result = notify.configure(self.ledger, self.pid,
                    {'config_version': initial['config_version'], 'config': {}, 'credential': code})
                results.append(('pass', code, result))
            except Exception as exc:
                results.append(('error', code, exc))

        with patch.object(notify, 'verify', side_effect=verify):
            workers = [threading.Thread(target=save, args=(code,)) for code in ('code-a', 'code-b')]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)
            self.assertFalse(any(worker.is_alive() for worker in workers))
        passed = [result for result in results if result[0] == 'pass']
        errors = [result for result in results if result[0] == 'error']
        self.assertEqual(len(passed), 1, results)
        self.assertEqual(len(errors), 1, results)
        self.assertIsInstance(errors[0][2], Conflict)
        self.assertEqual(self.active_code(), passed[0][1])
        self.assertEqual(self.notification_snapshot()['config_version'], initial['config_version'] + 1)

    def test_credential_file_write_failure_preserves_pointer_and_blocked_item(self):
        self.configure()
        blocked = self.blocked_item()
        before = self.ledger.get('products', self.pid)
        with patch.object(notify, '_write_credential', side_effect=OSError('disk write failed')):
            with self.assertRaises(OSError):
                self.save_body({'config': {}, 'credential': 'uncommitted-code'})
        self.assert_saved_state_unchanged(before)
        self.assertEqual(self.ledger.get('notifications', blocked['id']), blocked)

    def test_directory_fsync_failure_never_activates_new_credential(self):
        self.configure()
        before = self.ledger.get('products', self.pid)
        fsync = os.fsync
        directories = []

        def fail_directory(fd):
            written = any(path.read_text() == 'uncommitted-code'
                          for path in notify.credential_dir().rglob('*.code'))
            if stat.S_ISDIR(os.fstat(fd).st_mode) and written:
                directories.append(fd)
                raise OSError('directory fsync failed')
            return fsync(fd)

        with patch.object(notify.os, 'fsync', side_effect=fail_directory):
            with self.assertRaises(OSError):
                self.save_body({'config': {}, 'credential': 'uncommitted-code'})
        self.assertTrue(directories)
        self.assert_saved_state_unchanged(before)

    def test_database_update_failure_keeps_old_pointer_and_credential(self):
        self.configure()
        before = self.ledger.get('products', self.pid)
        update = self.ledger.update

        def fail_product(kind, *args, **kwargs):
            if kind == 'products':
                raise sqlite3.OperationalError('database update failed')
            return update(kind, *args, **kwargs)

        with patch.object(self.ledger, 'update', side_effect=fail_product):
            with self.assertRaises(sqlite3.OperationalError):
                self.save_body({'config': {}, 'credential': 'orphan-code'})
        self.assert_saved_state_unchanged(before)
        self.assertTrue(any(path.read_text() == 'orphan-code'
                            for path in notify.credential_dir().rglob('*.code')))

    def test_commit_failure_rolls_back_configuration_requeue_and_audit(self):
        self.configure()
        blocked = self.blocked_item()
        before = self.ledger.get('products', self.pid)
        with self.store.connect() as db:
            audit_before = db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        connect = sqlite3.connect

        class FailedCommit(sqlite3.Connection):
            def __exit__(connection, exc_type, exc_value, traceback):
                if exc_type is None and connection.in_transaction:
                    connection.rollback()
                    raise sqlite3.OperationalError('commit failed')
                return super().__exit__(exc_type, exc_value, traceback)

        def failing_connect(*args, **kwargs):
            return connect(*args, factory=FailedCommit, **kwargs)

        with patch.object(sqlite3, 'connect', side_effect=failing_connect):
            with self.assertRaises(sqlite3.OperationalError):
                self.save_body({'config': {}, 'credential': 'orphan-code'})
        self.assert_saved_state_unchanged(before)
        self.assertEqual(self.ledger.get('notifications', blocked['id']), blocked)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0], audit_before)

    def test_audit_failure_cannot_partially_commit_requeue_and_configuration(self):
        self.configure()
        blocked = self.blocked_item()
        before = self.ledger.get('products', self.pid)
        event = self.store.event

        def fail_audit(kind, *args, **kwargs):
            if kind == 'autopilot_notifications_configured':
                raise sqlite3.OperationalError('audit write failed')
            return event(kind, *args, **kwargs)

        with patch.object(self.store, 'event', side_effect=fail_audit):
            with self.assertRaises(sqlite3.OperationalError):
                self.save_body({'config': {}, 'credential': 'orphan-code'})
        self.assert_saved_state_unchanged(before)
        self.assertEqual(self.ledger.get('notifications', blocked['id']), blocked)

    def test_process_exit_before_and_after_commit_preserves_reference_semantics(self):
        self.configure()
        blocked = self.blocked_item()
        before = self.ledger.get('products', self.pid)
        script = r'''
import os, sys
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch
root, state, pid, phase = sys.argv[1:]
sys.path[:0] = [root, str(Path(root)/'tests'), str(Path(root)/'dsh-gpt-supervisor/scripts')]
from test_notifications import FakeSMTP
from review_core import Store
from autopilot.api import Control
from autopilot import notifications as notify
control = Control(Store(Path(state)))
original = control.ledger.store.transaction
if phase == 'before':
    @contextmanager
    def transaction():
        with original() as db:
            yield db
            os._exit(61)
    control.ledger.store.transaction = transaction
with patch.object(notify.smtplib, 'SMTP_SSL', FakeSMTP):
    notify.configure(control.ledger, pid, {'config': {}, 'credential': 'restart-code'})
os._exit(62)
'''
        for phase, expected_exit in [('before', 61), ('after', 62)]:
            with self.subTest(phase=phase):
                process = subprocess.run([sys.executable, '-c', script, str(ROOT),
                    str(self.store.state), self.pid, phase], capture_output=True, text=True, timeout=20)
                self.assertEqual(process.returncode, expected_exit, process.stderr)
                restarted = Control(Store(self.store.state)).ledger
                product = restarted.get('products', self.pid)
                config = notify.normalize(product)
                if phase == 'before':
                    self.assertEqual(product, before)
                    self.assertEqual(notify.credential_from_config(self.pid, config), 'synthetic-code')
                    self.assertEqual(restarted.get('notifications', blocked['id'])['status'], 'blocked')
                else:
                    self.assertNotEqual(config['credential_ref'], before['notifications']['credential_ref'])
                    self.assertEqual(notify.credential_from_config(self.pid, config), 'restart-code')
                    self.assertEqual(restarted.get('notifications', blocked['id'])['status'], 'queued')

    def test_inflight_send_uses_one_account_smtp_and_immutable_credential_snapshot(self):
        self.configure(credential='old-code')
        item = notify.send_test(self.ledger, self.pid)
        old_path = self.active_path()
        snapshots = []

        def concurrent_rotation(smtp):
            snapshots.append(dict(smtp))
            self.save_body({'config': {'sender': 'new@example.test',
                           'smtp': {'host': 'new-smtp.example.test', 'port': 587, 'tls': 'starttls'}},
                            'credential': 'new-code'})
            return FakeSMTP(smtp['host'], smtp['port'])

        with patch.object(notify, 'connect', side_effect=concurrent_rotation), \
             patch.object(notify, 'verify'):
            notify.send(Scheduler(self.store), item, 100)
        self.assertIn(('sender@example.test', 'old-code'), FakeSMTP.logins)
        self.assertNotIn(('sender@example.test', 'new-code'), FakeSMTP.logins)
        self.assertEqual(FakeSMTP.sent[-1][1], 'sender@example.test')
        self.assertEqual(snapshots[0]['host'], 'smtp.example.test')
        self.assertEqual(old_path.read_text(), 'old-code')
        self.assertEqual(self.active_code(), 'new-code')

    def test_auth_failure_after_rotation_is_retried_using_new_configuration(self):
        self.configure(credential='old-code')
        item = notify.send_test(self.ledger, self.pid)

        class RotatingSMTP(FakeSMTP):
            def login(server, user, code):
                self.save_body({'config': {}, 'credential': 'new-code'})
                raise smtplib.SMTPAuthenticationError(535, b'old code revoked')

        with patch.object(notify, 'connect', return_value=RotatingSMTP('smtp.example.test', 465)), \
             patch.object(notify, 'verify'):
            notify.send(Scheduler(self.store), item, 100)
        pending = self.ledger.get('notifications', item['id'])
        self.assertEqual(pending['status'], 'queued')
        with patch.object(notify.smtplib, 'SMTP_SSL', FakeSMTP):
            notify.send(Scheduler(self.store), pending, 160)
        self.assertEqual(self.ledger.get('notifications', item['id'])['status'], 'sent')
        self.assertIn(('sender@example.test', 'new-code'), FakeSMTP.logins)

    def test_explicit_null_and_missing_active_ref_do_not_use_legacy_credentials(self):
        legacy = notify.credential_file()
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text('legacy-code')
        self.configure()
        self.active_path().unlink()
        self.assertIsNone(self.active_code())
        self.assertFalse(notify.get(self.ledger, self.pid)['credential_configured'])
        item = self.ledger.create('notifications', {'product_id': self.pid, 'kind': 'test',
            'subject': 'missing file', 'payload': {}, 'attempts': 0}, 'queued')
        with patch.object(notify.smtplib, 'SMTP_SSL', FakeSMTP):
            notify.send(Scheduler(self.store), item, 100)
        self.assertEqual(self.ledger.get('notifications', item['id'])['status'], 'blocked')
        self.assertEqual(FakeSMTP.sent, [])
        product = self.ledger.get('products', self.pid)
        config = dict(product['notifications']) | {'credential_ref': None}
        self.ledger.update('products', self.pid, product['version'], {'notifications': config})
        self.assertIsNone(self.active_code())
        self.assertFalse(notify.get(self.ledger, self.pid)['credential_configured'])
        self.assertEqual(legacy.read_text(), 'legacy-code')

    def test_legacy_scoped_precedes_global_and_first_save_migrates_without_read_writes(self):
        sender = 'sender@example.test'
        scoped = notify.credential_file(self.pid, sender)
        scoped.parent.mkdir(parents=True, exist_ok=True)
        scoped.write_text('scoped-code')
        global_path = notify.credential_file()
        global_path.parent.mkdir(parents=True, exist_ok=True)
        global_path.write_text('global-code')
        product = self.ledger.get('products', self.pid)
        old = {'enabled': False, 'sender': sender, 'recipients': ['ops@example.test']}
        self.ledger.update('products', self.pid, product['version'], {'notifications': old})
        before = {path: path.read_bytes() for path in scoped.parent.rglob('*') if path.is_file()}
        self.assertEqual(self.active_code(), 'scoped-code')
        self.assertEqual({path: path.read_bytes() for path in scoped.parent.rglob('*') if path.is_file()}, before)
        saved = self.save_body({'config': {'morning_hour': 8}})
        self.assertTrue(saved['credential_configured'])
        self.assertEqual(self.active_code(), 'scoped-code')
        self.assertIsNotNone(self.notification_snapshot()['credential_ref'])
        self.assertEqual(scoped.read_text(), 'scoped-code')
        self.assertEqual(global_path.read_text(), 'global-code')

    def test_existing_sender_switch_requires_new_verified_code(self):
        self.configure()
        before = self.ledger.get('products', self.pid)
        with self.assertRaises(ValueError):
            self.save_body({'config': {'sender': 'other@example.test'}})
        self.assert_saved_state_unchanged(before)
        with patch.object(notify, 'verify', side_effect=smtplib.SMTPAuthenticationError(535, b'rejected')):
            with self.assertRaises(smtplib.SMTPAuthenticationError):
                notify.configure(self.ledger, self.pid,
                                 {'config': {'sender': 'other@example.test'}, 'credential': 'bad-code'})
        self.assert_saved_state_unchanged(before)

    def test_all_external_write_entries_reject_internal_credential_fields(self):
        self.configure()
        before = self.ledger.get('products', self.pid)
        for field, value in [('credential_ref', '0' * 32), ('config_version', 99)]:
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    self.save_body({'config': {field: value}})
                with self.assertRaises(ValueError):
                    self.control.mutate('/products', {'config': {'name': 'invalid',
                        'source': str(self.root), 'goal': 'test', 'notifications': {field: value}}})
                with self.assertRaises(ValueError):
                    self.control.configure_product(self.pid,
                        {'version': before['version'], 'config': {'notifications': {field: value}}})
                self.assert_saved_state_unchanged(before)
        with self.assertRaises(ValueError):
            self.save_body({'credential_ref': '0' * 32, 'config': {}})

    def test_generic_create_and_configure_share_revision_and_preserve_active_reference(self):
        created = self.control.mutate('/products', {'config': {'name': '新项目',
            'source': str(self.root), 'goal': '通知草稿',
            'notifications': {'sender': 'draft@example.test', 'enabled': False}}})
        self.assertEqual(created['notifications']['config_version'], 0)
        self.assertIsNone(created['notifications']['credential_ref'])
        self.configure()
        before = self.ledger.get('products', self.pid)
        old_config = before['notifications']
        result = self.control.configure_product(self.pid,
            {'version': before['version'], 'config_version': old_config['config_version'],
             'config': {'notifications': {'morning_hour': 8}}})
        new_config = result['notifications']
        self.assertEqual(new_config['config_version'], old_config['config_version'] + 1)
        self.assertEqual(new_config['credential_ref'], old_config['credential_ref'])
        self.assertEqual(new_config['sender'], old_config['sender'])
        with self.assertRaises(Conflict):
            self.save_body({'config_version': old_config['config_version'], 'config': {'morning_hour': 9}})
        latest = self.ledger.get('products', self.pid)
        with self.assertRaises(ValueError):
            self.control.configure_product(self.pid, {'version': latest['version'],
                'config': {'notifications': {'sender': 'other@example.test'}}})

    def test_append_recipients_uses_latest_configuration_and_revision(self):
        initial = self.configure(recipients=['a@example.test'])
        start = threading.Barrier(2)
        results = []

        def append(address):
            try:
                start.wait(timeout=5)
                result = self.control.mutate(f'/products/{self.pid}/notifications/recipients',
                                             {'recipient': address})
                results.append(result)
            except Exception as exc:
                results.append(exc)

        workers = [threading.Thread(target=append, args=(address,))
                   for address in ('b@example.test', 'c@example.test')]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertTrue(all(isinstance(result, dict) for result in results), results)
        current = notify.get(self.ledger, self.pid)
        self.assertEqual(set(current['recipients']), {'a@example.test', 'b@example.test', 'c@example.test'})
        self.assertEqual(current['config_version'], initial['config_version'] + 2)
        duplicate = self.control.mutate(f'/products/{self.pid}/notifications/recipients',
                                       {'recipient': 'a@example.test'})
        self.assertEqual(duplicate['config_version'], current['config_version'])
        with self.assertRaises(Conflict):
            self.control.mutate(f'/products/{self.pid}/notifications/recipients',
                {'config_version': initial['config_version'], 'recipient': 'stale@example.test'})
        self.assertNotIn('stale@example.test', notify.get(self.ledger, self.pid)['recipients'])


    def test_file_fsync_failure_never_activates_written_credential(self):
        self.configure()
        before = self.ledger.get('products', self.pid)
        fsync = os.fsync
        files = []

        def fail_file(fd):
            if stat.S_ISREG(os.fstat(fd).st_mode):
                files.append(fd)
                raise OSError('file fsync failed')
            return fsync(fd)

        with patch.object(notify.os, 'fsync', side_effect=fail_file):
            with self.assertRaises(OSError):
                self.save_body({'config': {}, 'credential': 'uncommitted-code'})
        self.assertTrue(files)
        self.assert_saved_state_unchanged(before)

    def test_legacy_get_reads_snapshot_without_migration_or_database_writes(self):
        sender = 'sender@example.test'
        global_path = notify.credential_file()
        global_path.parent.mkdir(parents=True, exist_ok=True)
        global_path.write_text('legacy-code')
        product = self.ledger.get('products', self.pid)
        self.ledger.update('products', self.pid, product['version'], {'notifications': {
            'enabled': False, 'sender': sender, 'recipients': ['ops@example.test']}})
        before = self.ledger.get('products', self.pid)
        with patch.object(notify, '_write_credential') as write:
            result = notify.get(self.ledger, self.pid)
            self.assertTrue(result['credential_configured'])
            self.assertEqual(result['config_version'], 0)
            self.assertEqual(self.active_code(), 'legacy-code')
            write.assert_not_called()
        self.assertEqual(self.ledger.get('products', self.pid), before)
        self.assertFalse(notify.credential_file(self.pid, sender).exists())

    def test_reference_is_scoped_to_its_original_product_and_account(self):
        self.configure(sender='same@example.test', credential='code-a')
        first = notify.normalize(self.ledger.get('products', self.pid))
        other = self.ledger.create('products', {'name': '另一个项目', 'source': str(self.root),
                                               'goal': '引用隔离'}, 'paused')
        self._configure_product(other['id'], 'same@example.test', 'code-b', ['ops@example.test'])
        second = notify.normalize(self.ledger.get('products', other['id']))
        self.assertIsNone(notify.credential_from_config(self.pid,
                         first | {'credential_ref': second['credential_ref']}))
        self.assertIsNone(notify.credential_from_config(other['id'],
                         second | {'credential_ref': first['credential_ref']}))
        self.assertIsNone(notify.credential_from_config(self.pid,
                         first | {'sender': 'other@example.test'}))
        self.assertIsNone(notify.credential(self.pid, 'same@example.test', credential_ref='../escape'))
        self.assertEqual(self.active_code(), 'code-a')
        self.assertEqual(self.active_code(other['id']), 'code-b')


    def test_rejected_generic_notification_requests_never_log_credentials(self):
        import dashboard_server as dashboard
        self.configure()
        app = dashboard.Dashboard(self.root / 'state')
        cases = [
            ('/products', {'config': {'name': 'invalid', 'source': str(self.root), 'goal': 'test',
                                     'notifications': {'credential': 'wrong-location-code'}}}),
            (f'/products/{self.pid}/configure',
             {'version': self.ledger.get('products', self.pid)['version'],
              'config': {'notifications': {'credential': 'wrong-location-code'}}}),
        ]
        for path, request in cases:
            with self.subTest(path=path), self.assertRaises(ValueError):
                app.operation(path, request | {'operation_id': str(uuid.uuid4())})
        with self.store.connect() as db:
            rows = [dict(row) for row in db.execute('SELECT fingerprint,response FROM operations')]
            events = ''.join(row[0] for row in db.execute('SELECT detail FROM events'))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row['fingerprint'].startswith('sha256:') for row in rows))
        self.assertNotIn('wrong-location-code', json.dumps(rows))
        self.assertNotIn('wrong-location-code', events)


    def test_withdrawn_partial_recipient_is_never_retried_or_reported_as_full_success(self):
        self.configure(recipients=['first@example.test', 'second@example.test'])
        item = notify.send_test(self.ledger, self.pid)
        FakeSMTP.refused = {'second@example.test': (450, b'try later')}
        with patch.object(notify.smtplib, 'SMTP_SSL', FakeSMTP):
            notify.send(Scheduler(self.store), item, 100)
        item = self.ledger.get('notifications', item['id'])
        self.assertEqual(item['status'], 'queued')
        self.assertEqual(item['receipt']['to'], ['first@example.test'])
        self.save_body({'config': {'recipients': ['first@example.test']}})
        with patch.object(notify, 'connect') as connect:
            notify.send(Scheduler(self.store), item, 160)
            connect.assert_not_called()
        result = self.ledger.get('notifications', item['id'])
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['receipt']['requested'], ['first@example.test', 'second@example.test'])
        self.assertEqual(result['receipt']['to'], ['first@example.test'])
        self.assertEqual(result['receipt']['withdrawn'], ['second@example.test'])
        self.assertEqual(len(FakeSMTP.sent), 1)
        self.assertEqual(notify.get(self.ledger, self.pid)['stats']['cancelled'], 1)
        self.assertEqual(notify.get(self.ledger, self.pid)['stats']['sent'], 0)

    def test_all_refused_recipients_withdrawn_cancel_without_smtp_or_sent_receipt(self):
        self.configure(recipients=['first@example.test', 'second@example.test'])
        item = notify.send_test(self.ledger, self.pid)
        FakeSMTP.refused = {'first@example.test': (450, b'try later'),
                            'second@example.test': (450, b'try later')}
        with patch.object(notify.smtplib, 'SMTP_SSL', FakeSMTP):
            notify.send(Scheduler(self.store), item, 100)
        item = self.ledger.get('notifications', item['id'])
        self.assertEqual(item['status'], 'queued')
        self.assertEqual(item['receipt']['to'], [])
        self.save_body({'config': {'enabled': False, 'recipients': []}})
        with patch.object(notify, 'connect') as connect:
            notify.send(Scheduler(self.store), item, 160)
            connect.assert_not_called()
        result = self.ledger.get('notifications', item['id'])
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['receipt']['requested'], ['first@example.test', 'second@example.test'])
        self.assertEqual(result['receipt']['to'], [])
        self.assertEqual(set(result['receipt']['withdrawn']), {'first@example.test', 'second@example.test'})
        self.assertEqual(len(FakeSMTP.sent), 1)
        self.assertEqual(notify.get(self.ledger, self.pid)['stats']['sent'], 0)


    def test_legacy_null_product_version_remains_optional_but_notification_version_is_strict(self):
        initial = self.configure()
        result = self.save_body({'version': None, 'config': {'morning_hour': 8}})
        self.assertEqual(result['config_version'], initial['config_version'] + 1)
        self.assertEqual(result['morning_hour'], 8)
        before = self.ledger.get('products', self.pid)
        with self.assertRaises(ValueError):
            self.save_body({'config_version': None, 'config': {'morning_hour': 9}})
        self.assert_saved_state_unchanged(before)


    def test_stale_enabled_product_set_cannot_send_after_notifications_are_disabled(self):
        self.configure(events={'alert': True})
        self.add_run(status='blocked', title='关闭开关并发通知', reason='依赖缺失')
        product = self.ledger.get('products', self.pid)
        notify.enqueue(self.ledger, product, notify.normalize(product), time.time())
        item = self.items('alert')[0]
        self.assertEqual(item['status'], 'queued')
        self.save_body({'config': {'enabled': False}})
        # 模拟 tick 已捕获 enabled 集合，而用户随后在发送开始前关闭了通知。
        with patch.object(notify, 'connect') as connect:
            notify.process(Scheduler(self.store), time.time(), enabled_products={self.pid})
            connect.assert_not_called()
        self.assertEqual(self.ledger.get('notifications', item['id'])['status'], 'queued')
        self.assertEqual(FakeSMTP.sent, [])
        manual = notify.send_test(self.ledger, self.pid)
        self.tick()
        self.assertEqual(self.ledger.get('notifications', manual['id'])['status'], 'sent')
        self.assertEqual(self.ledger.get('notifications', item['id'])['status'], 'queued')
        self.assertEqual(len(FakeSMTP.sent), 1)


    # -------------------------------------------------- 历史问题区间与原因快照

    def history_time(self, day, hour=0, minute=0):
        return dt.datetime(2026, 10, day, hour, minute, tzinfo=ZONE).timestamp()

    def history_fixture(self):
        with patch('time.time', return_value=self.history_time(9)):
            product = self.ledger.create('products', {'name': '历史复盘项目',
                'source': str(self.root), 'goal': '验证问题真实历史区间'}, 'paused')
        self.pid = product['id']
        self.configure(events={'morning_retro': True})

    def history_begin(self, at, title, cause):
        with patch('time.time', return_value=at):
            run = self.add_run(status='blocked', title=title, reason=cause)
            notify.observe_problems(self.ledger, self.ledger.get('products', self.pid), now=at)
        return run

    def history_change(self, run, at, status, cause=None):
        changes = {'status': status}
        if cause is not None:
            changes['reason'] = cause
        if status == 'accepted':
            changes['accepted_at'] = at
        with patch('time.time', return_value=at):
            current = self.ledger.get('runs', run['id'])
            run = self.ledger.update('runs', current['id'], current['version'], changes, status)
            notify.observe_problems(self.ledger, self.ledger.get('products', self.pid), now=at)
        return run

    def history_payload(self, start, end, now=None, kind='test'):
        now = end + 7 * 3600 if now is None else now
        if kind == 'morning_retro':
            context = {'day': dt.datetime.fromtimestamp(start, ZONE).date(), 'window': [start, end]}
        else:
            context = {'start': start, 'end': end}
        _, payload = notify.render(self.ledger, self.ledger.get('products', self.pid), kind, now, context)
        return payload

    def problem_sections(self, payload):
        text = payload['text'].split('三、阻塞与问题', 1)[1].split('四、额度使用', 1)[0]
        html = payload['html'].split('<h2>三、阻塞与问题</h2>', 1)[1].split('<h2>四、额度使用</h2>', 1)[0]
        return text, html

    def test_reopened_problem_preserves_recovery_gap_and_old_morning_cause(self):
        self.history_fixture()
        title, cause_a, cause_b = '隔日重开问题', '历史原因甲', '今日原因乙'
        first = self.history_time(10, 9)
        run = self.history_begin(first, title, cause_a)
        run = self.history_change(run, self.history_time(10, 14), 'accepted')
        run = self.history_change(run, self.history_time(12, 6), 'blocked', cause_b)
        self.tick(self.history_time(12, 7))
        yesterday = self.items('morning_retro')[0]['payload']
        self.assertEqual(yesterday['window'], [self.history_time(11), self.history_time(12)])
        for body in (yesterday['text'], yesterday['html']):
            self.assertNotIn(title, body)
            self.assertNotIn(cause_a, body)
            self.assertNotIn(cause_b, body)
        # 今天的重开与原因不能改写10日晨报视图：两种正文仍是甲/已恢复。
        old = self.history_payload(self.history_time(10), self.history_time(11),
                                   now=self.history_time(12, 7), kind='morning_retro')
        for section in self.problem_sections(old):
            self.assertIn(title, section)
            self.assertIn(cause_a, section)
            self.assertIn('已恢复', section)
            self.assertNotIn(cause_b, section)
            self.assertNotIn('持续跟踪', section)
        problems = self.ledger.scoped('problems', self.pid)
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]['occurrences'], 2)
        self.assertEqual(problems[0]['first_seen'], first)
        self.assertEqual(problems[0]['status'], 'open')
        self.assertEqual(problems[0]['cause'], cause_b)

    def test_same_day_recovery_and_reopening_keep_distinct_cause_intervals(self):
        self.history_fixture()
        title, cause_a, cause_b = '同日重开问题', '上午原因甲', '下午原因乙'
        run = self.history_begin(self.history_time(10, 9), title, cause_a)
        run = self.history_change(run, self.history_time(10, 12), 'accepted')
        run = self.history_change(run, self.history_time(10, 16), 'blocked', cause_b)
        self.history_change(run, self.history_time(10, 18), 'accepted')
        cases = [
            (9, 12, [cause_a], [cause_b]),
            (12, 16, [], [cause_a, cause_b]),
            (16, 18, [cause_b], [cause_a]),
            (0, 24, [cause_a, cause_b], []),
        ]
        base = self.history_time(10)
        for left, right, included, excluded in cases:
            with self.subTest(window=(left, right)):
                payload = self.history_payload(base + left * 3600, base + right * 3600)
                for section in self.problem_sections(payload):
                    for cause in included:
                        self.assertIn(cause, section)
                    for cause in excluded:
                        self.assertNotIn(cause, section)
                    if not included:
                        self.assertNotIn(title, section)
        problems = self.ledger.scoped('problems', self.pid)
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]['occurrences'], 2)
        self.assertEqual(problems[0]['status'], 'resolved')

    def test_continuous_problem_spans_days_without_future_resolution_pollution(self):
        self.history_fixture()
        title, cause = '跨日持续问题', '持续原因甲'
        run = self.history_begin(self.history_time(10, 22), title, cause)
        run = self.history_change(run, self.history_time(11, 12), 'blocked', cause)
        self.history_change(run, self.history_time(12, 16), 'accepted')
        for day in (10, 11):
            with self.subTest(day=day):
                payload = self.history_payload(self.history_time(day), self.history_time(day + 1),
                    now=self.history_time(13, 7), kind='morning_retro')
                for section in self.problem_sections(payload):
                    self.assertIn(title, section)
                    self.assertIn(cause, section)
                    self.assertIn('持续跟踪', section)
                    self.assertNotIn('已恢复', section)
        payload = self.history_payload(self.history_time(12), self.history_time(13),
                                       kind='morning_retro')
        for section in self.problem_sections(payload):
            self.assertIn(cause, section)
            self.assertIn('已恢复', section)
        self.assertEqual(self.ledger.scoped('problems', self.pid)[0]['occurrences'], 1)

    def test_problem_window_intersection_is_half_open_and_empty_windows_are_empty(self):
        self.history_fixture()
        title, cause = '半开区间问题', '边界原因甲'
        run = self.history_begin(self.history_time(10, 10), title, cause)
        self.history_change(run, self.history_time(10, 20), 'accepted')
        base = self.history_time(10)
        # 独立于实现的相交性质：问题区间[10,20)，窗口[左,右)。
        cases = [(0, 10, False), (10, 11, True), (11, 19, True), (19, 20, True),
                 (20, 21, False), (21, 24, False), (0, 24, True), (12, 12, False)]
        for left, right, expected in cases:
            with self.subTest(window=(left, right)):
                payload = self.history_payload(base + left * 3600, base + right * 3600)
                for section in self.problem_sections(payload):
                    self.assertEqual(cause in section, expected)
                    self.assertEqual(title in section, expected)
        # 仍open的问题刚好在窗口结束时出现，也不得混入前一窗口。
        late = self.history_begin(self.history_time(11), '边界新开问题', '边界新原因乙')
        payload = self.history_payload(self.history_time(10), self.history_time(11))
        for section in self.problem_sections(payload):
            self.assertNotIn('边界新开问题', section)
            self.assertNotIn('边界新原因乙', section)
        self.assertEqual(self.ledger.get('runs', late['id'])['status'], 'blocked')

    def test_continuous_problem_reason_changes_use_each_windows_historical_snapshot(self):
        self.history_fixture()
        title, cause_a, cause_b = '持续原因变更问题', '变更前原因甲', '变更后原因乙'
        run = self.history_begin(self.history_time(10, 8), title, cause_a)
        run = self.history_change(run, self.history_time(11, 12), 'blocked', cause_b)
        self.history_change(run, self.history_time(12, 18), 'accepted')
        cases = [
            (self.history_time(10), self.history_time(11), [cause_a], [cause_b]),
            (self.history_time(11), self.history_time(11, 12), [cause_a], [cause_b]),
            (self.history_time(11, 12), self.history_time(12), [cause_b], [cause_a]),
            (self.history_time(11), self.history_time(12), [cause_a, cause_b], []),
        ]
        for start, end, included, excluded in cases:
            with self.subTest(window=(start, end)):
                payload = self.history_payload(start, end, now=self.history_time(13, 7))
                for section in self.problem_sections(payload):
                    self.assertIn(title, section)
                    self.assertIn('持续跟踪', section)
                    self.assertNotIn('已恢复', section)
                    for cause in included:
                        self.assertIn(cause, section)
                    for cause in excluded:
                        self.assertNotIn(cause, section)
        problems = self.ledger.scoped('problems', self.pid)
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]['occurrences'], 1)
        self.assertEqual(problems[0]['cause'], cause_b)

    def test_legacy_multiple_occurrences_without_history_do_not_invent_continuous_span(self):
        self.history_fixture()
        title, old_cause = '历史区间未知问题', '历史不完整原因'
        first, last = self.history_time(10, 9), self.history_time(12, 6)
        self.ledger.create('problems', {'product_id': self.pid, 'source': 'task',
            'source_id': 'legacy-run', 'run_id': 'legacy-run', 'summary': title + '：' + old_cause,
            'cause': old_cause, 'first_seen': first, 'last_seen': last, 'closed_at': None,
            'occurrences': 2, 'requirement_id': None, 'signal_ids': []}, 'open')
        for before_observation in (True, False):
            with self.subTest(before_observation=before_observation):
                if not before_observation:
                    with patch('time.time', return_value=last):
                        self.ledger.create('runs', {'product_id': self.pid, 'title': title,
                            'reason': '新确认原因乙'}, 'blocked', ident='legacy-run')
                        notify.observe_problems(self.ledger, self.ledger.get('products', self.pid), now=last)
                payload = self.history_payload(self.history_time(11), self.history_time(12),
                    now=self.history_time(12, 7), kind='morning_retro')
                for section in self.problem_sections(payload):
                    self.assertNotIn(title, section)
                    self.assertNotIn(old_cause, section)
                    self.assertNotIn('新确认原因乙', section)
        known = self.history_payload(self.history_time(12), self.history_time(12, 7))
        for section in self.problem_sections(known):
            self.assertIn(title, section)
            self.assertIn('新确认原因乙', section)
        problems = self.ledger.scoped('problems', self.pid)
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]['first_seen'], first)
        self.assertEqual(problems[0]['occurrences'], 2)


    def test_legacy_single_occurrence_keeps_unobserved_earlier_cause_unknown(self):
        for closed_at_last in (False, True):
            with self.subTest(closed_at_last=closed_at_last):
                self.history_fixture()
                title, latest_cause = '单次旧历史问题', '最后确认原因乙'
                first = self.history_time(10, 9)
                last = self.history_time(11, 12)
                closed = last if closed_at_last else self.history_time(12, 3)
                self.ledger.create('problems', {'product_id': self.pid, 'source': 'task',
                    'source_id': 'legacy-single-run', 'run_id': 'legacy-single-run',
                    'summary': title + '：' + latest_cause, 'cause': latest_cause,
                    'first_seen': first, 'last_seen': last, 'closed_at': closed,
                    'occurrences': 1, 'requirement_id': None, 'signal_ids': []}, 'resolved')
                earlier = self.history_payload(self.history_time(10), self.history_time(11),
                                               now=self.history_time(13, 7))
                for section in self.problem_sections(earlier):
                    self.assertIn(title, section)
                    self.assertIn('历史不完整', section)
                    self.assertNotIn(latest_cause, section)
                if closed_at_last:
                    before_closure = self.history_payload(self.history_time(11), last)
                    for section in self.problem_sections(before_closure):
                        self.assertIn(title, section)
                        self.assertIn('历史不完整', section)
                        self.assertNotIn(latest_cause, section)
                else:
                    confirmed = self.history_payload(last, last + 3600)
                    for section in self.problem_sections(confirmed):
                        self.assertIn(title, section)
                        self.assertIn(latest_cause, section)


if __name__ == '__main__':
    unittest.main()
