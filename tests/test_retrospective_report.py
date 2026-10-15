"""自动复盘实际控制器的完整窗口、去重、门槛与失败报告。"""
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'dsh-gpt-supervisor/scripts')]
from review_core import Store
from autopilot.scheduler import Scheduler
from autopilot.retrospective_report import prepare, finish, convert, model_report


class RetrospectiveReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'state')
        self.scheduler = Scheduler(self.store)
        self.ledger = self.scheduler.ledger
        self.product = self.ledger.create('products', {
            'name': '研发平台', 'source': self.tmp.name, 'goal': '完善日报',
            'adapter_spec': {'kind': 'command'}, 'project_config': {'readonly_paths': ['/api/check']},
            'policy': {'tokens_per_day': 0, 'deepseek_off_peak_only': False}}, 'active')
        self.start = dt.datetime(2026, 10, 13, 19, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.end = self.start + 86400
        self.report = {'id': 'daily-report', 'product_id': self.product['id'], 'day': '2026-10-14',
                       'window_start': self.start, 'cutoff': self.end}
        self.signal = self.ledger.create('signals', {'product_id': self.product['id'], 'source': 'inspection',
            'code': 'REPORT', 'summary': '检查数据缺失', 'evidence': {'status': 'fail'}, 'count': 1}, 'abnormal')
        self.evidence = {'project': {'id': self.product['id']}, 'problem_items': [
            {'signal_ids': [self.signal['id']]}], 'current_blockers': []}
        self.candidate = {'title': '修复日报缺少验证回执', 'signal_ids': [self.signal['id']],
            'existing_requirement_id': '', 'evidence': '日报与真实回执不符', 'impact': '隐藏实际失败',
            'reproduction': '检查昨日回执和邮件', 'acceptance': ['日报保留完整失败证据'],
            'classification': 'development', 'in_scope': True, 'priority': 1,
            'resolution_probes': [{'path': '/api/check', 'pointer': '/report/complete', 'operator': 'equals', 'expected': True}]}

    def tearDown(self):
        self.tmp.cleanup()

    def analysis(self, candidate=None, status='pass'):
        return {'status': status, 'reason': '已核对', 'summary': '发现可验收改进',
                'requirement_candidates': [candidate or self.candidate]}

    def test_replay_changed_title_and_explicit_existing_id_never_duplicate(self):
        first = convert(self.scheduler, self.product, self.report, self.evidence, self.analysis(), queue=False)
        item_id = first['results'][0]['requirement_id']
        rename = self.candidate | {'title': '同一根因的另一个标题', 'existing_requirement_id': item_id}
        second = convert(self.scheduler, self.product, self.report, self.evidence, self.analysis(rename), queue=False)
        explicit = rename | {'existing_requirement_id': item_id}
        third = convert(self.scheduler, self.product, self.report, self.evidence, self.analysis(explicit), queue=False)
        self.assertEqual(first['created'], 1)
        self.assertEqual(second['reused'], 1)
        self.assertEqual(third['reused'], 1)
        self.assertEqual(len(self.ledger.scoped('requirements', self.product['id'])), 1)
        self.assertEqual(second['results'][0]['requirement_id'], item_id)
        # Same report and evidence have one durable reuse trace, even when replayed.
        self.assertEqual(len(self.ledger.get('requirements', item_id)['additional_evidence']), 1)

    def test_one_signal_can_create_two_distinct_requirements_in_same_batch(self):
        other = self.candidate | {'title': '优化另一个验收展示', 'acceptance': ['展示验收图表']}
        analysis = self.analysis() | {'requirement_candidates': [self.candidate, other]}
        first = convert(self.scheduler, self.product, self.report, self.evidence, analysis, queue=False)
        second = convert(self.scheduler, self.product, self.report, self.evidence, analysis, queue=False)
        self.assertEqual(first['created'], 2)
        self.assertEqual(second['reused'], 2)
        self.assertEqual(len(self.ledger.scoped('requirements', self.product['id'])), 2)
        signal = self.ledger.get('signals', self.signal['id'])
        self.assertEqual(len(signal['requirement_ids']), 2)

    def test_changed_title_without_explicit_link_is_retained_as_ambiguous(self):
        first = convert(self.scheduler, self.product, self.report, self.evidence, self.analysis(), queue=False)
        rename = self.candidate | {'title': '未知是否同因的新标题'}
        second = convert(self.scheduler, self.product, self.report, self.evidence, self.analysis(rename), queue=False)
        self.assertEqual(second['skipped'], 1)
        self.assertEqual(second['results'][0]['suggested_requirement_ids'], [first['results'][0]['requirement_id']])
        self.assertEqual(len(self.ledger.scoped('requirements', self.product['id'])), 1)

    def test_project_and_snapshot_references_are_validated_before_writes(self):
        other = self.ledger.create('products', {'name': 'other'}, 'active')
        foreign = self.ledger.create('signals', {'product_id': other['id'], 'source': 'inspection'}, 'pending')
        variants = [self.candidate | {'signal_ids': [foreign['id']]},
                    self.candidate | {'existing_requirement_id': 'outside'},
                    self.candidate | {'in_scope': False}]
        for candidate in variants:
            output = convert(self.scheduler, self.product, self.report, self.evidence, self.analysis(candidate))
            self.assertEqual(output['skipped'], 1)
        self.assertEqual(self.ledger.scoped('requirements', self.product['id']), [])

    def test_valid_development_enters_real_queue_and_replay_does_not_queue_twice(self):
        first = convert(self.scheduler, self.product, self.report, self.evidence, self.analysis())
        second = convert(self.scheduler, self.product, self.report, self.evidence, self.analysis())
        self.assertEqual(first['queued'], 1)
        self.assertEqual(second['queued'], 0)
        self.assertEqual(len(self.ledger.scoped('runs', self.product['id'])), 1)
        item = self.ledger.get('requirements', first['results'][0]['requirement_id'])
        self.assertEqual(item['classification'], 'development')
        self.assertEqual(item['resolution_probes'], self.candidate['resolution_probes'])
        self.assertEqual(item['status'], 'queued')

    def test_invalid_probe_downgrades_to_investigation_without_a_run(self):
        bad = self.candidate | {'resolution_probes': [{'path': '/private', 'operator': 'equals', 'pointer': '/ok', 'expected': True}]}
        output = convert(self.scheduler, self.product, self.report, self.evidence, self.analysis(bad))
        item = self.ledger.get('requirements', output['results'][0]['requirement_id'])
        self.assertEqual(item['classification'], 'investigation')
        self.assertEqual(item['status'], 'pending')
        self.assertIn('按现有调查', output['results'][0]['advancement'])
        self.assertEqual(self.ledger.scoped('runs', self.product['id']), [])

    def test_existing_confirmation_gate_is_preserved(self):
        signal = self.ledger.update('signals', self.signal['id'], self.signal['version'], {'source': 'competitor'})
        output = convert(self.scheduler, self.product, self.report, self.evidence, self.analysis())
        item = self.ledger.get('requirements', output['results'][0]['requirement_id'])
        self.assertEqual(item['status'], 'pending_confirmation')
        self.assertTrue(item['confirmation_required'])
        self.assertEqual(item['signal_ids'], [signal['id']])
        self.assertEqual(self.ledger.scoped('runs', self.product['id']), [])

    def test_controller_registers_real_unlinked_failures_once_and_they_can_be_converted(self):
        with patch('autopilot.store.time.time', return_value=self.start + 60):
            run = self.ledger.create('runs', {'product_id': self.product['id'], 'title': '没有已有信号的任务',
                'receipts': [{'at': self.start + 60, 'call_id': 'unlinked-failure', 'action': 'verify',
                             'result': {'status': 'fail', 'reason': '原问题断言失败'}}]}, 'completed')
        first = prepare(self.scheduler, self.product, self.report, register_sources=True)
        evidence = json.loads(Path(first['evidence_path']).read_text())
        problem = next(row for row in evidence['problem_items'] if run['id'] in row['source_ids'])
        self.assertTrue(problem['signal_ids'])
        source = self.ledger.get('signals', problem['signal_ids'][0])
        self.assertEqual(source['source'], 'retrospective')
        self.assertIn(run['id'], source['evidence']['source_ids'])
        count = len(self.ledger.scoped('signals', self.product['id']))
        prepare(self.scheduler, self.product, self.report, register_sources=True)
        self.assertEqual(len(self.ledger.scoped('signals', self.product['id'])), count)
        candidate = self.candidate | {'signal_ids': problem['signal_ids']}
        converted = convert(self.scheduler, self.product, self.report, evidence, self.analysis(candidate))
        self.assertEqual(converted['created'], 1)
        self.assertEqual(converted['queued'], 1)

    def test_normal_pause_wait_does_not_become_a_new_discovery_signal(self):
        product = self.ledger.update('products', self.product['id'], self.product['version'], {}, 'paused')
        self.ledger.create('runs', {'product_id': product['id'], 'title': '正常暂停中的排队任务', 'receipts': []}, 'queued')
        prepared = prepare(self.scheduler, product, self.report, register_sources=True)
        evidence = json.loads(Path(prepared['evidence_path']).read_text())
        self.assertEqual(evidence['registered_sources'], [])
        self.assertEqual(len(self.ledger.scoped('signals', product['id'])), 1)

    def test_large_evidence_is_indexed_without_omitting_objects_or_exposing_private_state(self):
        directory = self.scheduler.root / 'retrospectives' / self.report['id']
        directory.mkdir(parents=True)
        evidence = {'project': {'id': self.product['id']}, 'period': {}, 'summary': {}, 'statistics': {},
            'coverage': {'complete': True}, 'problem_items': [{'id': 'large-problem', 'source_kind': 'runs',
                'title': '完整失败来源', 'reason': '原始异常' * 5000, 'signal_ids': [self.signal['id']],
                'observations': [{'reason': '重复回执' * 1000} for _ in range(1200)]}],
            'current_blockers': [], 'task_items': [{'id': 'task-'+str(i), 'title': '任务'+str(i)} for i in range(1500)],
            'requirements': [], 'notification_items': []}
        path = directory / 'evidence.json'
        path.write_text(json.dumps(evidence, ensure_ascii=False))
        import hashlib
        report = self.report | {'comprehensive_evidence': {'evidence_path': str(path),
            'evidence_hash': hashlib.sha256(path.read_bytes()).hexdigest()}, 'attribution_results': [{'reason': '旧归因结果' * 500000}]}
        root = self.scheduler.root / 'executions' / 'isolated-model';root.mkdir(parents=True)
        compact = model_report(report, root)
        self.assertLess(len(json.dumps(compact, ensure_ascii=False)), 500000)
        self.assertNotIn('attribution_results', compact)
        index = compact['comprehensive_evidence']
        self.assertEqual(len(index['task_items']), 1500)
        problem = index['problem_items'][0]
        self.assertTrue(problem['reason_excerpt'])
        full = json.loads((Path(index['bundle_path']) / problem['detail_file']).read_text())
        self.assertEqual(len(full['observations']), 1200)
        self.assertEqual(full['reason'], evidence['problem_items'][0]['reason'])
        manifest = json.loads(Path(index['manifest_path']).read_text())
        self.assertEqual(manifest['counts']['task_items'], 1500)

    def test_blocked_analysis_cannot_create_a_candidate(self):
        output = convert(self.scheduler, self.product, self.report, self.evidence, self.analysis(status='blocked'))
        self.assertEqual(output['skipped'], 1)
        self.assertEqual(self.ledger.scoped('requirements', self.product['id']), [])

    def test_custom_window_includes_all_receipts_and_tasks_and_freezes_snapshot(self):
        with patch('autopilot.store.time.time', return_value=self.start + 20):
            run = self.ledger.create('runs', {'product_id': self.product['id'], 'title': '正常完成的任务',
                'reason': '', 'receipts': [{'at': self.start + 30, 'call_id': 'failure-call', 'action': 'verify',
                    'result': {'status': 'fail', 'reason': '真实验证失败'}},
                    {'at': self.end, 'call_id': 'excluded-call', 'action': 'verify', 'result': {'status': 'pass'}}]}, 'completed')
        first = prepare(self.scheduler, self.product, self.report)
        data = json.loads(Path(first['evidence_path']).read_text())
        self.assertEqual(data['period']['window_kind'], 'custom')
        self.assertEqual(data['summary']['run_failure_receipts'], 1)
        task = next(t for t in data['task_items'] if t['id'] == run['id'])
        self.assertEqual(len(task['receipts']), 1)
        self.assertTrue(task['observed_in_period'])
        later = prepare(self.scheduler, self.product, self.report)
        self.assertEqual(later['evidence_hash'], first['evidence_hash'])
        with self.assertRaises(ValueError):
            prepare(self.scheduler, self.product, self.report | {'cutoff': self.end + 10})

    def test_model_failure_still_renders_complete_html_with_wait_and_safe_dynamic_text(self):
        with patch('autopilot.store.time.time', return_value=self.start + 40):
            self.ledger.create('runs', {'product_id': self.product['id'], 'title': '<img onerror=bad>',
                'reason': '等待协作回复 token=private-value'}, 'waiting_for_reply')
        output = finish(self.scheduler, self.product, self.report, {'status': 'blocked', 'reason': '模型余额不足'})
        source = Path(output['html_report']['path']).read_text()
        self.assertIn('模型余额不足', source)
        self.assertIn('等待协作回复', source)
        self.assertIn('全部任务与当前进展', source)
        self.assertIn('北京时间调度窗口', source)
        self.assertNotIn('北京时间自然日，', source)
        self.assertNotIn('<img', source)
        self.assertNotIn('private-value', source)
        self.assertIn('不能据此认定没有问题', source)
        self.assertNotIn('<p>没有问题', source)
        self.assertEqual(output['conversion']['created'], 0)

    def test_prepare_is_readonly_even_with_more_than_old_list_limit(self):
        with self.store.transaction() as db:
            for n in range(1002):
                self.ledger.create('runs', {'product_id': self.product['id'], 'title': 'task ' + str(n), 'receipts': []}, 'completed', db=db)
        with self.store.connect() as db:
            before = db.execute('SELECT id,version,data FROM auto_runs ORDER BY id').fetchall()
        prepared = prepare(self.scheduler, self.product, self.report)
        self.assertEqual(len(prepared['material']['task_items']), 1002)
        with self.store.connect() as db:
            after = db.execute('SELECT id,version,data FROM auto_runs ORDER BY id').fetchall()
        self.assertEqual([tuple(row) for row in before], [tuple(row) for row in after])

    def test_notification_failure_retry_and_accepted_receipts_are_scoped_and_safe(self):
        def create(status, attempts=0, error='', at=None, payload=None):
            return self.ledger.create('notifications', {'product_id': self.product['id'], 'kind': 'morning_retro',
                'subject': '早七邮件', 'attempts': attempts, 'error': error, 'next_attempt_at': self.end + 60,
                'payload': payload or {}, 'receipt': {'at': at, 'requested': ['private@example.com'],
                    'to': ['private@example.com'] if status == 'sent' else [], 'refused': {}}}, status)
        with patch('autopilot.store.time.time', return_value=self.start + 50):
            failed = create('failed', 5, 'SSL: UNEXPECTED_EOF_WHILE_READING', self.start + 55,
                            {'html': 'private-body-do-not-include', 'credential': 'real-secret-do-not-include'})
            retry = create('queued', 2, '连接中断', self.start + 56)
            normal = create('queued')
            create('sent', at=self.start + 58)
        prepared = prepare(self.scheduler, self.product, self.report)
        data = json.loads(Path(prepared['evidence_path']).read_text())
        counts = data['statistics']['notification_receipt_status_counts']
        self.assertEqual(counts, {'failed': 1, 'retry_wait': 1, 'smtp_accepted': 1})
        blocker_ids = {ident for row in data['current_blockers'] if row['source_kind'] == 'notifications' for ident in row['source_ids']}
        self.assertIn(failed['id'], blocker_ids)
        self.assertIn(retry['id'], blocker_ids)
        self.assertNotIn(normal['id'], blocker_ids)
        encoded = json.dumps(data, ensure_ascii=False)
        self.assertNotIn('private-body-do-not-include', encoded)
        self.assertNotIn('real-secret-do-not-include', encoded)
        self.assertNotIn('private@example.com', encoded)
        output = finish(self.scheduler, self.product, self.report, {'status': 'blocked', 'reason': '待归因'})
        source = Path(output['html_report']['path']).read_text()
        self.assertIn('SSL: UNEXPECTED_EOF_WHILE_READING', source)
        self.assertIn('邮件投递与运行卡点', source)
        self.assertIn('SMTP 接受仅确认服务器接受', source)


if __name__ == '__main__':
    unittest.main()
