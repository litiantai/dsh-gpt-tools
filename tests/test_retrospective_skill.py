"""复盘技能的报告内容、去重和明确写操作边界。"""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('retrospective_skill', ROOT / 'skills/dsh-role-retrospective/scripts/retrospective.py')
retro = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retro)


class FakeApi:
    def __init__(self):
        self.calls = []
        self.requirements = {'existing': {'id': 'existing', 'product_id': 'project', 'status': 'queued'}}
        self.operations = {}

    def call(self, path, body=None):
        self.calls.append((path, body))
        if path == '/products/project':
            return {'id': 'project'}
        if path.startswith('/requirements/'):
            return self.requirements[path.split('/')[-1]]
        if path == '/requirements':
            oid = body['operation_id']
            if oid not in self.operations:
                self.operations[oid] = dict(body, id='new-' + str(len(self.operations)), status='pending')
            return self.operations[oid]
        raise AssertionError(path)


class RetrospectiveSkillTests(unittest.TestCase):
    def setUp(self):
        self.evidence = {
            'project': {'id': 'project', 'name': '研发项目'}, 'period': {'date': '2026-10-13'},
            'coverage': [{'source_kind': 'runs', 'read_count': 4, 'in_window_count': 4, 'failed_count': 7, 'status': 'complete'}],
            'problem_items': [{'id': 'issue', 'title': '验证失败', 'reason': '缺少证据', 'occurrences': 7,
                               'source_kind': 'runs', 'signal_ids': ['signal'], 'requirement_ids': ['existing'],
                               'window_status': 'fail', 'current_status': 'waiting_for_reply'}],
            'current_blockers': [{'title': '仍在等协作回复', 'current_status': 'waiting_for_reply', 'occurrences': 1}],
            'requirements': [{'id': 'existing', 'title': '已有修复需求', 'status': 'queued', 'signal_ids': ['known-signal']}],
        }
        self.candidate = {'title': '新的优化需求', 'signal_ids': ['signal'], 'existing_requirement_id': '',
                          'evidence': '真实检查失败', 'impact': '影响报告判定', 'reproduction': '读取已收到报告',
                          'acceptance': ['复盘包括对应失败证据'], 'classification': 'investigation', 'in_scope': True, 'priority': 2}

    def test_report_preserves_failure_and_current_wait_with_linked_requirement(self):
        source = retro.render_report(self.evidence, {'summary': '昨日出现失败，目前仍等待回复'})
        for fact in ('验证失败', '缺少证据', '等待协作回复', '已有修复需求', 'existing', '7 次记录'):
            self.assertIn(fact, source)
        self.assertNotIn('已收集来源暂无异常记录', source)

    def test_incomplete_sources_prevent_a_no_issue_claim_and_escape_content(self):
        self.evidence['coverage'][0]['status'] = 'incomplete'
        self.evidence['problem_items'][0]['reason'] = '<img src=x onerror=bad> token=private-value'
        source = retro.render_report(self.evidence)
        self.assertIn('不能据此认定没有问题', source)
        self.assertNotIn('<img', source)
        self.assertNotIn('private-value', source)
        self.assertIn('[已隐藏]', source)

    def test_existing_requirement_id_wins_over_a_changed_title_without_writes(self):
        api = FakeApi()
        candidate = self.candidate | {'existing_requirement_id': 'existing', 'title': '同一问题的新标题'}
        result = retro.apply_requirements(self.evidence, {'requirement_candidates': [candidate]}, api)
        self.assertEqual(result['reused'], 1)
        self.assertEqual(result['results'][0]['requirement_id'], 'existing')
        self.assertTrue(all(body is None for _, body in api.calls))

    def test_out_of_project_requirement_and_signal_are_rejected(self):
        for candidate in (self.candidate | {'existing_requirement_id': 'foreign'}, self.candidate | {'signal_ids': ['foreign']}):
            with self.subTest(candidate=candidate):
                api = FakeApi()
                with self.assertRaises(ValueError):
                    retro.apply_requirements(self.evidence, {'requirement_candidates': [candidate]}, api)
                self.assertTrue(all(body is None for _, body in api.calls))

    def test_replay_uses_stable_operation_id_and_does_not_start_a_model(self):
        api = FakeApi()
        analysis = {'requirement_candidates': [self.candidate]}
        first = retro.apply_requirements(self.evidence, analysis, api)
        second = retro.apply_requirements(self.evidence, analysis, api)
        self.assertEqual(first['results'][0]['requirement_id'], second['results'][0]['requirement_id'])
        self.assertEqual(len(api.operations), 1)
        self.assertFalse(any(path.endswith('/queue') for path, _ in api.calls))

    def test_numeric_zero_and_analysis_actions_survive_html_rendering(self):
        self.evidence['statistics'] = {'receipt_status_counts': {'runs': {'fail': 6, 'blocked': 1}}}
        self.evidence['coverage'][0]['in_window_count'] = 0
        analysis = {'problem_items': [{'title': '证据缺口', 'source_kind': 'runs', 'source_id': 'run-id',
                                      'cause': '关键回执缺失', 'window_status': 'new', 'current_status': 'blocked',
                                      'next_action': '补齐真实回执再复验'}]}
        source = retro.render_report(self.evidence, analysis)
        self.assertIn('<td>0</td>', source)
        self.assertIn('<td>7</td>', source)
        self.assertIn('补齐真实回执再复验', source)


if __name__ == '__main__':
    unittest.main()
