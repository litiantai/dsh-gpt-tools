"""合并放行：失败归属、完整回执及提交版本均不可绕过。"""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'dsh-gpt-supervisor/scripts')]
from autopilot import merge_validation as gate
from autopilot.verification_findings import unrelated_failure
from autopilot.delivery_worker import merge


class MergeValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.request = {'state_root': self.tmp.name, 'product': {'id': 'p', 'functional_findings_policy': 'backlog'},
            'record': {'id': 'delivery', 'day': '20261012', 'flow': 'review_before_release', 'frozen': True,
                       'cutoff': 1, 'pr_number': 6, 'release_sync_pass': {'head_sha': 'head', 'base_sha': 'base'}},
            'requirements': [{'id': 'a', 'acceptance': ['入口不可抢跑', '身份失败后禁用']},
                             {'id': 'b', 'acceptance': ['恢复后可用']}]}
        self.report = {'status': 'fail', 'reason': '无关的新问题', 'acceptance_results': [
            {'criterion_id': key, 'status': 'pass', 'evidence': '真实用例日志'} for key in ('a:0', 'a:1', 'b:0')],
            'findings': [{'scope': 'unrelated', 'requirement_ids': [], 'reason': '导出问题',
                         'evidence': '基线可复现，需求与变更均未涉及导出'}]}
        self.result = {'status': 'pass', 'checks': [{'name': 'tests', 'status': 'pass', 'required': True},
            {'name': '独立业务验证', 'status': 'fail', 'required': False, 'disposition': 'backlog',
             'requirement_id': 'finding', 'evidence': self.report}]}

    def seal(self):
        result = gate.certify(self.request, self.result, 'head', 'base')
        self.assertEqual(result['status'], 'pass', result)
        self.request['record']['validation_pass'] = {'head_sha': 'head', 'base_sha': 'base', 'gate': result['merge_gate']}
        return Path(result['merge_gate']['path'])

    def test_only_complete_unrelated_failure_can_be_deferred(self):
        self.assertTrue(unrelated_failure(self.request, self.report))
        variants = []
        for scope in ('current_requirement', 'regression', 'unknown', None):
            item = copy.deepcopy(self.report); item['findings'][0]['scope'] = scope; variants.append(item)
        for key in ('findings', 'acceptance_results'):
            item = copy.deepcopy(self.report); item.pop(key); variants.append(item)
        for status in ('fail', 'blocked'):
            item = copy.deepcopy(self.report); item['acceptance_results'][0]['status'] = status; variants.append(item)
        for key, value in [('criterion_id', 'foreign:0'), ('criterion_id', 'a:1'), ('evidence', '')]:
            item = copy.deepcopy(self.report); item['acceptance_results'][0][key] = value; variants.append(item)
        for key, value in [('requirement_ids', ['a']), ('evidence', ''), ('reason', '')]:
            item = copy.deepcopy(self.report); item['findings'][0][key] = value; variants.append(item)
        item = copy.deepcopy(self.report); item['acceptance_results'].pop(); variants.append(item)
        item = copy.deepcopy(self.report); item['findings'].append({'scope': 'unknown'}); variants.append(item)
        for item in variants:
            with self.subTest(report=item):
                self.assertFalse(unrelated_failure(self.request, item))
                result = copy.deepcopy(self.result); result['checks'][-1]['evidence'] = item
                self.assertEqual(gate.certify(self.request, result, 'head', 'base')['status'], 'blocked')

    def test_all_requirements_and_resolution_probes_need_evidence(self):
        self.request['requirements'][1]['resolution_probes'] = [{'path': 'check:recovery', 'expected': 'pass'}]
        self.assertFalse(unrelated_failure(self.request, self.report))
        self.report['acceptance_results'].append({'criterion_id': 'b:probe:0', 'status': 'pass', 'evidence': '恢复探针通过'})
        self.assertTrue(unrelated_failure(self.request, self.report))
        self.request['requirements'].append({'id': 'unknown', 'acceptance': []})
        self.assertFalse(unrelated_failure(self.request, self.report))

    def test_merge_calls_github_only_with_current_complete_receipt(self):
        gh = Mock(); gh.checks_pass.return_value = True
        gh.merge.return_value = {'merge_commit_sha': 'merged'}
        pr = {'head': {'sha': 'head'}, 'base': {'sha': 'base'}, 'mergeable': True, 'mergeable_state': 'clean'}
        self.request['record']['validation_pass'] = {'head_sha': 'head', 'base_sha': 'base'}
        with patch('autopilot.delivery_worker.load_pull_request', return_value=(gh, pr)), patch('autopilot.project_groups.check_merge_request', return_value=''):
            self.assertTrue(merge(self.request)['stale'])
            gh.merge.assert_not_called()
            self.seal()
            self.assertTrue(merge(self.request)['merged'])
            gh.merge.assert_called_once_with(6, 'head')
            gh.merge.reset_mock()
            self.request['product']['functional_findings_policy'] = 'block'
            self.assertTrue(merge(self.request)['stale'])
            gh.merge.assert_not_called()

    def test_version_scope_policy_and_receipt_changes_expire_proof(self):
        path = self.seal()
        self.assertEqual(gate.verify(self.request, 'head', 'base'), '')
        self.assertTrue(gate.verify(self.request, 'new-head', 'base'))
        self.assertTrue(gate.verify(self.request, 'head', 'new-base'))
        for mutate in (lambda r: r['product'].update(functional_findings_policy='block'),
                       lambda r: r['requirements'][0]['acceptance'].append('新增条件'),
                       lambda r: r['product'].update(project_config={'test': 'new check'}),
                       lambda r: r['record'].update(id='other'),
                       lambda r: r['record'].update(sequence=2)):
            request = copy.deepcopy(self.request); mutate(request)
            self.assertTrue(gate.verify(request, 'head', 'base'))
        path.write_text(path.read_text() + ' ')
        self.assertTrue(gate.verify(self.request, 'head', 'base'))
        path.unlink()
        self.assertTrue(gate.verify(self.request, 'head', 'base'))

    def test_old_nonrequired_failure_cannot_be_reused_even_with_matching_hash(self):
        path = self.seal()
        saved = json.loads(path.read_text())
        saved['result']['checks'][-1]['evidence'].pop('findings')
        path.write_text(json.dumps(saved))
        self.request['record']['validation_pass']['gate']['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertTrue(gate.verify(self.request, 'head', 'base'))

    def test_missing_tests_blocked_tests_and_inconsistent_pass_are_rejected(self):
        for checks in ([], [{'status': 'blocked', 'required': True}], [{'status': 'pass', 'required': False}]):
            self.assertEqual(gate.certify(self.request, {'status': 'pass', 'checks': checks}, 'head', 'base')['status'], 'blocked')
        result = copy.deepcopy(self.result)
        result['checks'][-1].update(status='pass', required=True)
        result['checks'][-1]['evidence']['status'] = 'pass'
        self.assertEqual(gate.certify(self.request, result, 'head', 'base')['status'], 'blocked')

    def test_block_policy_and_feature_checks_cannot_defer(self):
        for changes in ({'flow': 'legacy'}, {'review_target': 'feature'}):
            request = copy.deepcopy(self.request); request['record'].update(changes)
            self.assertEqual(gate.certify(request, self.result, 'head', 'base')['status'], 'blocked')
        self.request['product']['functional_findings_policy'] = 'block'
        self.assertEqual(gate.certify(self.request, self.result, 'head', 'base')['status'], 'blocked')


if __name__ == '__main__':
    unittest.main()
