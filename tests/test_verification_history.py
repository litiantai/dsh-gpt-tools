"""核对交付验收的历史证据关联、版本绑定及日志完整性。"""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'dsh-gpt-supervisor/scripts')]
from autopilot.verification_history import collect, checksum


class VerificationHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / 'validation'
        self.folder.mkdir()
        self.log = self.root / 'test.log'
        self.log.write_text('regression passed\n')
        check = {'step_id': 'root:browser:0', 'name': 'browser-0', 'status': 'pass', 'exit_code': 0,
                 'declared_command': ['npm', 'run', 'test:e2e'], 'log': str(self.log),
                 'log_sha256': checksum(self.log), 'branch': 'feat-task', 'commit': 'task-head'}
        plan = self.root / 'plan.json'
        plan.write_text(json.dumps({'steps': [{'step_id': check['step_id'], 'command': check['declared_command']}]}))
        receipt = self.root / 'receipt.json'
        receipt.write_text(json.dumps({'status': 'pass', 'commit': 'task-head', 'source_digest': 'task-digest',
                                       'plan_hash': checksum(plan), 'checks': [check]}))
        self.run = {'id': 'task', 'requirement_id': 'requirement', 'commit': 'task-head', 'source_digest': 'task-digest',
                    'receipts': [{'action': 'verify', 'at': 10, 'result': {
                        'status': 'pass', 'quality': {'receipt': str(receipt), 'receipt_hash': checksum(receipt)},
                        'instance': {'execution': 'local', 'branch': 'feat-task', 'commit': 'task-head'},
                        'checks': [{'name': '独立业务验证', 'status': 'pass', 'evidence': {'status': 'pass'}}]}}]}
        self.request = {'state_root': str(self.root), 'runs': [self.run], 'requirements': [{'id': 'requirement'}],
                        'record': {'integrated_ids': ['task'], 'feature_prs': [{'run_ids': ['task'], 'merge_sha': 'merge-head'}]}}

    def collect(self):
        with patch('autopilot.verification_history.git', return_value='') as git:
            result = collect(self.request, self.root, 'release-head', self.folder)
        return result, git

    def test_passed_history_preserves_version_and_original_condition_evidence(self):
        items, git = self.collect()
        item = items[0]
        self.assertEqual(item['integrity']['status'], 'pass')
        self.assertEqual((item['branch'], item['commit']), ('feat-task', 'task-head'))
        self.assertEqual(Path(item['checks'][0]['log']).read_bytes(), self.log.read_bytes())
        self.assertEqual(item['checks'][0]['log_sha256'], checksum(self.log))
        git.assert_called_once_with(self.root, 'merge-base', '--is-ancestor', 'merge-head', 'release-head')

    def test_altered_logs_and_receipts_are_not_valid_historical_evidence(self):
        self.log.write_text('changed log\n')
        self.assertEqual(self.collect()[0][0]['integrity']['status'], 'blocked')
        self.log.write_text('regression passed\n')
        Path(self.run['receipts'][0]['result']['quality']['receipt']).write_text('{}')
        self.assertEqual(self.collect()[0][0]['integrity']['status'], 'blocked')

    def test_wrong_task_commit_and_instance_commit_are_blocked(self):
        for target in ('task', 'instance'):
            with self.subTest(target=target):
                original = copy.deepcopy(self.run)
                if target == 'task':
                    self.run['commit'] = 'other-head'
                else:
                    self.run['receipts'][0]['result']['instance']['commit'] = 'other-head'
                self.assertEqual(self.collect()[0][0]['integrity']['status'], 'blocked')
                self.run.clear(); self.run.update(original)

    def test_legacy_step_without_branch_uses_verified_instance_but_mismatched_branch_fails(self):
        receipt=Path(self.run['receipts'][0]['result']['quality']['receipt'])
        saved=json.loads(receipt.read_text());saved['checks'][0].pop('branch')
        receipt.write_text(json.dumps(saved));self.run['receipts'][0]['result']['quality']['receipt_hash']=checksum(receipt)
        self.assertEqual(self.collect()[0][0]['integrity']['status'],'pass')
        saved['checks'][0]['branch']='other-feature'
        receipt.write_text(json.dumps(saved));self.run['receipts'][0]['result']['quality']['receipt_hash']=checksum(receipt)
        self.assertEqual(self.collect()[0][0]['integrity']['status'],'blocked')

    def test_unintegrated_and_foreign_requirements_are_excluded(self):
        self.request['record']['integrated_ids'] = []
        self.assertEqual(self.collect()[0], [])

        self.request['record']['integrated_ids'] = ['task']
        self.request['requirements'] = [{'id': 'other'}]
        self.assertEqual(self.collect()[0], [])

    def test_baseline_validation_without_task_history_needs_no_state_root(self):
        self.assertEqual(collect({'record': {}, 'requirements': []}, self.root, 'head', self.folder), [])

    def test_unmerged_feature_cannot_prove_release_lineage(self):
        with patch('autopilot.verification_history.git', side_effect=subprocess.CalledProcessError(1, ['git'])):
            result = collect(self.request, self.root, 'release-head', self.folder)
        self.assertEqual(result[0]['integrity']['status'], 'blocked')

    def test_paths_outside_controller_evidence_are_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            path = Path(outside) / 'receipt.json'
            path.write_text('{}')
            self.run['receipts'][0]['result']['quality']['receipt'] = str(path)
            self.assertEqual(self.collect()[0][0]['integrity']['status'], 'blocked')

    def test_delivery_input_exposes_only_history_index_for_on_demand_reading(self):
        from autopilot.delivery_review import verify
        request = copy.deepcopy(self.request)
        request['product'] = {'adapter': ['adapter'], 'project_config': {}}
        request['record'].update(id='delivery', title='release 验收', repository=str(self.root), git_dir=str(self.root))
        request['requirements'][0]['acceptance'] = ['feat 分支验证通过']
        diff = self.folder / 'verification-diff.patch'
        diff.write_text('candidate diff')
        facts = self.folder / 'verification-facts.json'
        facts.write_text('{}')
        material = {'diff_file': str(diff), 'facts_file': str(facts), 'changed_files': [], 'merge_base': 'base', 'source': 'repository'}
        with patch('autopilot.verification_history.git', return_value=''), \
                patch('autopilot.codex_executor.verification_material', return_value=material), \
                patch('autopilot.delivery_review.subprocess.run', return_value=SimpleNamespace(
                    stdout=json.dumps({'status': 'pass', 'checks': [{'name': 'test', 'status': 'pass'}]}),
                    stderr='', returncode=0)) as run:
            verify(request, self.root, 'release-head', 'base', self.folder)
        payload = json.loads(run.call_args.kwargs['input'])
        self.assertNotIn('historical_verifications', payload['requirement'])
        self.assertEqual(payload['requirement']['acceptance'], ['feat 分支验证通过'])
        index = Path(payload['verification']['history_index'])
        self.assertEqual(checksum(index), payload['verification']['history_index_sha256'])
        self.assertEqual(json.loads(index.read_text())[0]['commit'], 'task-head')


if __name__ == '__main__':
    unittest.main()
