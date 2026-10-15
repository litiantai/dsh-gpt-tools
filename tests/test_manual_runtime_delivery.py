"""手工运行时交付只收回未执行的重复任务，真实证据与状态事务一致。"""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'dsh-gpt-supervisor/scripts')]
from review_core import Conflict, Store
from autopilot.manual_runtime_delivery import register_manual_runtime_delivery
from autopilot.project import manifest_hash
from autopilot.store import Ledger


class ManualRuntimeDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = (Path(self.temporary.name)/'state').resolve()
        self.store = Store(self.state)
        self.ledger = Ledger(self.store)
        self.product = self.ledger.create('products', {'name':'test'}, 'paused', ident='p')
        self.requirement = self.ledger.create('requirements', {'product_id':'p', 'title':'完整复盘',
            'acceptance':['事实完整', '幂等入池'], 'run_id':'r'}, 'queued', ident='q')
        self.run = self.ledger.create('runs', {'product_id':'p', 'requirement_id':'q',
            'budget_reserved':False, 'receipts':[], 'revisions':0, 'execution_seconds':0}, 'queued', ident='r')
        self.release = self.state/'autopilot/manual-releases/release-test'
        self.artifact = self.release/'artifact'
        self.artifact.mkdir(parents=True)
        marker = {'product_id':'p', 'commit':'base-commit', 'release_id':'release-test'}
        (self.artifact/'autopilot-release.json').write_text(json.dumps(marker))
        (self.artifact/'production.py').write_text('READY = True\n')
        (self.release/'checks').mkdir()
        log = self.release/'checks/retrospective.log'
        log.write_text('both acceptance criteria verified\n')
        self.manifest = {'version':1, 'kind':'command', **marker,
            'artifact':str(self.artifact), 'artifact_hash':manifest_hash(self.artifact),
            'database_compatibility':'backward-compatible', 'checks':[{
                'name':'retrospective', 'status':'pass', 'required':True, 'exit_code':0,
                'cwd':str(self.artifact), 'log':str(log),
                'log_sha256':hashlib.sha256(log.read_bytes()).hexdigest()}]}
        self.manifest_path = self.release/'manifest.json'
        self.manifest_path.write_text(json.dumps(self.manifest))
        current = self.state/'platform/current'
        current.parent.mkdir(parents=True)
        current.symlink_to(self.artifact, target_is_directory=True)
        updates = self.state/'autopilot/updates'
        updates.mkdir()
        self.job_path = updates/'release-test.json'
        self.job = {'status':'completed', 'completed_at':time.time(),
            'manifest':str(self.manifest_path), 'product_id':'p', 'expected_commit':'base-commit'}
        self.job_path.write_text(json.dumps(self.job))
        self.mapping = [{'acceptance':criterion, 'status':'pass', 'checks':['retrospective']}
                        for criterion in self.requirement['acceptance']]

    def apply(self, mapping=None):
        return register_manual_runtime_delivery(self.ledger, 'q', 'r', self.manifest_path,
                                                self.mapping if mapping is None else mapping)

    def assert_unchanged(self):
        self.assertEqual(self.ledger.get('requirements','q')['status'], 'queued')
        self.assertEqual(self.ledger.get('runs','r')['status'], 'queued')
        self.assertNotIn('manual_runtime_delivery', self.ledger.get('requirements','q'))

    def test_delivery_preserves_git_truth_and_is_idempotent(self):
        result = self.apply()
        self.assertEqual(result['requirement']['status'], 'delivered')
        self.assertEqual(result['run']['status'], 'cancelled')
        proof = result['requirement']['manual_runtime_delivery']
        self.assertEqual(proof['git_delivery'], 'not_recorded')
        self.assertEqual(result['run']['manual_runtime_delivery'], proof)
        self.assertIn('release-test', result['run']['reason'])
        self.assertNotIn('merge_sha', result['requirement'])
        self.assertEqual(self.ledger.get('products','p')['status'], 'paused')
        replay = self.apply()
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['requirement']['version'], result['requirement']['version'])
        self.assertEqual(replay['run']['version'], result['run']['version'])

    def test_inflight_or_used_task_cannot_be_closed(self):
        for changes in ({'call':{'id':'active'}}, {'review_id':'review'}, {'receipts':[{'action':'plan'}]},
                        {'budget_reserved':True}, {'execution_seconds':1}, {'workspace':'started'}, {'revisions':1}):
            with self.subTest(changes=changes):
                old = self.ledger.get('runs','r')
                self.ledger.update('runs','r',old['version'],changes)
                with self.assertRaises(Conflict):
                    self.apply()
                self.assert_unchanged()
                old = self.ledger.get('runs','r')
                self.ledger.update('runs','r',old['version'],{key:self.run.get(key) for key in changes})

    def test_updater_and_current_release_must_match(self):
        for changes in ({'status':'observing'}, {'expected_commit':'other'}, {'product_id':'other'},
                        {'manifest':str(self.release/'unrelated.json')}):
            with self.subTest(changes=changes):
                self.job_path.write_text(json.dumps(self.job | changes))
                with self.assertRaises(Conflict):
                    self.apply()
                self.assert_unchanged()
        self.job_path.write_text(json.dumps(self.job))
        current = self.state/'platform/current'
        current.unlink(); current.symlink_to(self.release, target_is_directory=True)
        with self.assertRaises(Conflict):
            self.apply()
        self.assert_unchanged()

    def test_changed_artifact_or_log_rejects_registration(self):
        source = self.artifact/'production.py'
        original = source.read_text()
        source.write_text('CHANGED = True\n')
        with self.assertRaises(ValueError):
            self.apply()
        self.assert_unchanged()
        source.write_text(original)
        Path(self.manifest['checks'][0]['log']).write_text('rewritten verification\n')
        with self.assertRaises(ValueError):
            self.apply()
        self.assert_unchanged()

    def test_partial_or_unverified_acceptance_never_updates(self):
        for mapping in (self.mapping[:1], [self.mapping[0], self.mapping[0]],
                        [self.mapping[0], self.mapping[1] | {'checks':['invented']}],
                        [self.mapping[0], self.mapping[1] | {'status':'blocked'}]):
            with self.subTest(mapping=mapping):
                with self.assertRaises(ValueError):
                    self.apply(mapping)
                self.assert_unchanged()

    def test_binding_and_failed_checks_reject(self):
        old = self.ledger.get('runs','r')
        self.ledger.update('runs','r',old['version'],{'requirement_id':'other'})
        with self.assertRaises(Conflict):
            self.apply()
        self.assert_unchanged()
        self.manifest['checks'][0]['status'] = 'fail'
        self.manifest_path.write_text(json.dumps(self.manifest))
        with self.assertRaises(ValueError):
            self.apply()
        self.assert_unchanged()

    def test_audit_failure_rolls_back_both_records(self):
        original = self.store.event
        def record(kind, *args, **kwargs):
            if kind == 'autopilot_manual_runtime_delivered':
                raise RuntimeError('audit persistence failed')
            return original(kind, *args, **kwargs)
        with patch.object(self.store, 'event', side_effect=record):
            with self.assertRaises(RuntimeError):
                self.apply()
        self.assert_unchanged()
        self.assertEqual(self.ledger.get('requirements','q')['version'], self.requirement['version'])
        self.assertEqual(self.ledger.get('runs','r')['version'], self.run['version'])


if __name__ == '__main__':
    unittest.main()
