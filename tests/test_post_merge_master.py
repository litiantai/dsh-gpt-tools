"""合入主分支后的真实拉取、重新构建、更新回执和失败保护。"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'dsh-gpt-supervisor/scripts')]
from review_core import Store
from autopilot import generic_adapter, master
from autopilot.project import manifest_hash
from autopilot.store import Ledger
from autopilot.workspace import digest, git


class PostMergeMasterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root/'user-source'
        self.source.mkdir()
        git(self.source, 'init', '-b', 'master')
        git(self.source, 'config', 'user.name', 'Test')
        git(self.source, 'config', 'user.email', 'test@localhost')
        self.config = {
            'version': 1, 'blockers': [], 'web': False, 'startup_timeout': 5,
            'database_compatibility': 'backward-compatible', 'artifacts': ['dist'],
            'commands': {
                'build': [[sys.executable, '-c',
                    "from pathlib import Path; p=Path('dist'); p.mkdir(exist_ok=True); "
                    "c=p/'count'; c.write_text(str(int(c.read_text())+1 if c.exists() else 1)); "
                    "(p/'built.txt').write_text(Path('source.txt').read_text())"]],
                'test': [[sys.executable, '-c',
                    "from pathlib import Path; assert Path('dist/built.txt').read_text()==Path('source.txt').read_text()"]],
                'start': [[sys.executable, '-m', 'http.server', '{port}', '--bind', '127.0.0.1']],
            },
        }
        (self.source/'source.txt').write_text('baseline')
        (self.source/'lib').mkdir()
        (self.source/'lib/template.txt').write_text('tracked build template')
        (self.source/'.gitignore').write_text('dist/\n')
        (self.source/'.autopilot.json').write_text(json.dumps(self.config))
        git(self.source, 'add', '.')
        git(self.source, 'commit', '-m', 'baseline')
        self.base = git(self.source, 'rev-parse', 'HEAD')
        self.remote = self.root/'remote.git'
        git(self.root, 'clone', '--bare', str(self.source), str(self.remote))
        git(self.source, 'remote', 'add', 'origin', str(self.remote))
        self.managed = self.root/'managed.git'
        git(self.root, 'clone', '--bare', str(self.remote), str(self.managed))
        git(self.source, 'checkout', '-b', 'release-fixture')
        (self.source/'source.txt').write_text('released')
        git(self.source, 'commit', '-am', 'accepted release')
        self.release = git(self.source, 'rev-parse', 'HEAD')
        git(self.source, 'checkout', 'master')
        git(self.source, 'merge', '--no-ff', 'release-fixture', '-m', 'merge release')
        self.expected = git(self.source, 'rev-parse', 'HEAD')
        git(self.source, 'push', 'origin', 'master', 'release-fixture')
        self.store = Store(self.root/'state')
        self.ledger = Ledger(self.store)
        self.auto = self.store.state/'autopilot'
        self.product = self.ledger.create('products', {
            'name': 'fixture', 'source': str(self.source), 'repository': str(self.source),
            'delivery_repository': str(self.managed),
            'git': {'enabled': True, 'base_branch': 'master'},
            'adapter_spec': {'version': 1, 'kind': 'command',
                             'capabilities': ['master_sync', 'final_acceptance']},
            'project_config': self.config,
            'deployment': {'kind': 'self', 'origin': 'http://127.0.0.1:9000'},
            'master_update': {'status': 'pending', 'merge_sha': self.expected},
        }, 'active')
        self.request = {'product': self.product, 'record': self.product,
                        'state_root': str(self.auto)}
        self.instance = {'instance_role': 'master', 'commit': self.expected,
                         'release_id': 'master-'+self.expected,
                         'origin': self.product['deployment']['origin']}
        self.validated_path = self.auto/'candidates/release/manifest.json'
        artifact = self.validated_path.parent/'artifact'
        artifact.mkdir(parents=True)
        (artifact/'source.txt').write_text('released')
        (artifact/'autopilot-release.json').write_text(json.dumps({
            'product_id': self.product['id'], 'commit': self.release, 'release_id': 'release-fixture'}))
        self.validated = {
            'version': 1, 'kind': 'command', 'product_id': self.product['id'],
            'commit': self.release, 'release_id': 'release-fixture',
            'root': str(self.validated_path.parent), 'artifact': str(artifact),
            'artifact_hash': manifest_hash(artifact),
            'source_digest': digest(self.source),
            'source_tree': git(self.source, 'rev-parse', self.release+'^{tree}'),
            'database_compatibility': 'backward-compatible',
            'checks': [{'name': 'release business acceptance', 'status': 'pass', 'required': True}],
        }
        self.write_validated()
        proof = {'head_sha': self.release, 'base_sha': self.base}
        self.batch = self.ledger.create('deliveries', {
            'product_id': self.product['id'], 'merge_sha': self.expected,
            'validation_pass': proof,
            'receipts': [{'action': 'validate_release', 'result': proof | {
                'status': 'pass', 'manifest': str(self.validated_path)}}],
        }, 'online')
        self.network = patch('autopilot.delivery_worker.network_git',
                             side_effect=lambda repo, *args: git(repo, *args))
        self.network.start()
        self.addCleanup(self.network.stop)

    def write_validated(self, **changes):
        self.validated_path.write_text(json.dumps(self.validated | changes))

    def update_product(self, **changes):
        self.product = self.ledger.update('products', self.product['id'],
                                         self.product['version'], changes)
        self.request = self.request | {'product': self.product, 'record': self.product}

    def advance_remote(self):
        (self.source/'source.txt').write_text('next master')
        git(self.source, 'commit', '-am', 'later master')
        git(self.source, 'push', 'origin', 'master')
        return git(self.source, 'rev-parse', 'HEAD')

    def master_manifest(self):
        path = self.auto/'master-builds'/self.product['id']/self.expected/'manifest.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        artifact = path.parent/'artifact'
        if not artifact.exists():
            shutil.copytree(self.validated['artifact'], artifact)
        (artifact/'autopilot-release.json').write_text(json.dumps({
            'product_id': self.product['id'], 'commit': self.expected,
            'release_id': 'master-'+self.expected}))
        path.write_text(json.dumps(self.validated | {
            'commit': self.expected, 'release_id': 'master-'+self.expected,
            'root': str(path.parent), 'artifact': str(artifact),
            'artifact_hash': manifest_hash(artifact),
            'validated_manifest': str(self.validated_path), 'validated_commit': self.release,
            'master_build_checks': [{'name': 'build', 'status': 'pass', 'required': True}],
        }))
        return str(path)

    def real_commands(self):
        """保留真实构建/启动，仅替换操作系统隔离包装，兼容测试宿主。"""
        stack = ExitStack()
        unwrap = lambda argv, *args, **kwargs: argv
        stack.enter_context(patch('autopilot.isolated.command', side_effect=unwrap))
        stack.enter_context(patch('autopilot.onboarding.command', side_effect=unwrap))
        return stack

    def test_fetches_latest_master_and_materializes_detached_managed_workspace(self):
        self.assertEqual(git(self.managed, 'rev-parse', 'master'), self.base)
        (self.source/'user-notes.txt').write_text('keep my uncommitted work')
        before = git(self.source, 'status', '--porcelain')
        self.assertEqual(master.update_target(self.request), self.expected)
        workspace = Path(master.build_workspace(self.request, self.expected))
        self.assertEqual(workspace, self.auto/'master-builds'/self.product['id']/self.expected/'workspace')
        self.assertEqual(git(workspace, 'rev-parse', 'HEAD'), self.expected)
        self.assertEqual(git(workspace, 'branch', '--show-current'), '')
        self.assertEqual(git(workspace, 'status', '--porcelain'), '')
        self.assertEqual((workspace/'source.txt').read_text(), 'released')
        self.assertEqual(master.build_workspace(self.request, self.expected), str(workspace))
        self.assertEqual(git(self.source, 'status', '--porcelain'), before)
        self.assertEqual((self.source/'user-notes.txt').read_text(), 'keep my uncommitted work')

    def test_target_rejects_merge_that_is_not_in_remote_master(self):
        git(self.source, 'checkout', '-b', 'not-merged')
        (self.source/'source.txt').write_text('unmerged change')
        git(self.source, 'commit', '-am', 'not merged')
        pending = git(self.source, 'rev-parse', 'HEAD')
        git(self.source, 'push', 'origin', 'not-merged')
        git(self.managed, 'fetch', 'origin', 'not-merged')
        self.update_product(master_update={'status': 'pending', 'merge_sha': pending})
        with self.assertRaises(ValueError):
            master.update_target(self.request)

    def test_reused_build_workspace_rejects_changed_source(self):
        expected = master.update_target(self.request)
        workspace = Path(master.build_workspace(self.request, expected))
        (workspace/'source.txt').write_text('unexpected edits')
        with self.assertRaises(ValueError):
            master.build_workspace(self.request, expected)

    def test_wrong_validation_identity_or_tree_cannot_enqueue_update(self):
        for field, value in [('commit', self.base), ('product_id', 'another-product'),
                             ('source_tree', git(self.source, 'rev-parse', self.base+'^{tree}'))]:
            with self.subTest(field=field):
                self.write_validated(**{field: value})
                with patch('autopilot.generic_adapter.identity', side_effect=ValueError('old instance')), \
                        patch('autopilot.generic_adapter.master_build', create=True) as build, \
                        patch('autopilot.generic_adapter.request_update') as update:
                    result = generic_adapter.master_sync(self.request)
                self.assertNotEqual(result['status'], 'pass', result)
                build.assert_not_called()
                update.assert_not_called()

    def test_stale_validation_proof_cannot_enqueue_update(self):
        self.ledger.update('deliveries', self.batch['id'], self.batch['version'], {
            'validation_pass': {'head_sha': self.base, 'base_sha': self.base}})
        with patch('autopilot.generic_adapter.identity', side_effect=ValueError('old instance')), \
                patch('autopilot.generic_adapter.master_build', create=True) as build, \
                patch('autopilot.generic_adapter.request_update') as update:
            result = generic_adapter.master_sync(self.request)
        self.assertNotEqual(result['status'], 'pass', result)
        build.assert_not_called()
        update.assert_not_called()

    def test_rebuild_executes_real_commands_and_reuses_verified_new_manifest(self):
        expected = master.update_target(self.request)
        original = self.validated_path.read_bytes()
        with self.real_commands():
            path = generic_adapter.master_build(self.request, expected, str(self.validated_path))
            repeated = generic_adapter.master_build(self.request, expected, str(self.validated_path))
        self.assertEqual(path, repeated)
        manifest = generic_adapter.validate_manifest(path)
        self.assertEqual(manifest['commit'], self.expected)
        self.assertEqual(manifest['validated_commit'], self.release)
        self.assertEqual(manifest['validated_manifest'], str(self.validated_path))
        self.assertTrue(manifest['master_build_checks'])
        self.assertTrue(all(c['status'] == 'pass' for c in manifest['master_build_checks']))
        self.assertEqual((Path(manifest['artifact'])/'dist/built.txt').read_text(), 'released')
        self.assertEqual((Path(manifest['artifact'])/'dist/count').read_text(), '1')
        self.assertEqual(self.validated_path.read_bytes(), original)

    @unittest.skipIf(os.environ.get('DSH_PROJECT_ISOLATED'), '本机构建由外层控制器执行')
    def test_local_rebuild_runs_actual_build_test_and_temporary_startup(self):
        self.update_product(test_execution='local')
        expected = master.update_target(self.request)
        path = generic_adapter.master_build(self.request, expected, str(self.validated_path))
        manifest = generic_adapter.validate_manifest(path)
        self.assertTrue(any(c.get('execution') == 'local' for c in manifest['master_build_checks']))
        startup = next(c for c in manifest['master_build_checks'] if c['name'] == 'startup')
        self.assertEqual(startup['status'], 'pass')
        self.assertTrue(startup['stopped'])
        self.assertEqual((Path(manifest['artifact'])/'dist/built.txt').read_text(), 'released')

    def test_failed_rebuild_never_enqueues_update(self):
        bad = self.config | {'commands': self.config['commands'] | {
            'build': [[sys.executable, '-c', 'raise SystemExit(4)']]}}
        self.update_product(project_config=bad)
        with self.real_commands(), \
                patch('autopilot.generic_adapter.identity', side_effect=ValueError('old instance')), \
                patch('autopilot.generic_adapter.request_update') as update:
            result = generic_adapter.master_sync(self.request)
        self.assertNotEqual(result['status'], 'pass', result)
        update.assert_not_called()

    def test_rebuild_that_changes_source_never_enqueues_update(self):
        changed = self.config | {'commands': self.config['commands'] | {
            'build': [[sys.executable, '-c',
                "from pathlib import Path; Path('source.txt').write_text('modified by build'); "
                "Path('dist').mkdir(); Path('dist/built.txt').write_text('modified by build')"]]}}
        self.update_product(project_config=changed)
        with self.real_commands(), \
                patch('autopilot.generic_adapter.identity', side_effect=ValueError('old instance')), \
                patch('autopilot.generic_adapter.request_update') as update:
            result = generic_adapter.master_sync(self.request)
        self.assertNotEqual(result['status'], 'pass', result)
        update.assert_not_called()

    def test_rebuild_cannot_modify_tracked_file_excluded_from_source_digest(self):
        changed = self.config | {'commands': self.config['commands'] | {
            'build': self.config['commands']['build'] + [[sys.executable, '-c',
                "from pathlib import Path; Path('lib/template.txt').write_text('changed tracked template')"]]}}
        self.update_product(project_config=changed)
        with self.real_commands(), \
                patch('autopilot.generic_adapter.identity', side_effect=ValueError('old instance')), \
                patch('autopilot.generic_adapter.request_update') as update:
            result = generic_adapter.master_sync(self.request)
        self.assertNotEqual(result['status'], 'pass', result)
        update.assert_not_called()

    def test_cached_rebuild_rejects_changed_validation_artifact_at_same_path(self):
        expected = master.update_target(self.request)
        with self.real_commands():
            generic_adapter.master_build(self.request, expected, str(self.validated_path))
        artifact = Path(self.validated['artifact'])
        (artifact/'new-evidence.txt').write_text('replacement candidate')
        self.write_validated(artifact_hash=manifest_hash(artifact))
        with self.assertRaises(ValueError):
            generic_adapter.master_build(self.request, expected, str(self.validated_path))

    def test_cached_rebuild_requires_its_own_passed_checks_and_correct_source_tree(self):
        expected = master.update_target(self.request)
        with self.real_commands():
            path = Path(generic_adapter.master_build(self.request, expected, str(self.validated_path)))
        original = json.loads(path.read_text())
        for changes in [
                {'master_build_checks': [{'name': 'build', 'required': True, 'status': 'fail'}]},
                {'source_tree': git(self.source, 'rev-parse', self.base+'^{tree}')},
                {'validated_commit': self.base}]:
            with self.subTest(changes=changes):
                path.write_text(json.dumps(original | changes))
                with self.assertRaises(ValueError):
                    generic_adapter.master_build(self.request, expected, str(self.validated_path))

    def test_remote_advance_while_building_discards_update_request(self):
        def build(*args):
            path = self.master_manifest()
            self.advance_remote()
            return path
        with patch('autopilot.generic_adapter.identity', side_effect=ValueError('old instance')), \
                patch('autopilot.generic_adapter.master_build', side_effect=build, create=True), \
                patch('autopilot.generic_adapter.request_update') as update:
            result = generic_adapter.master_sync(self.request)
        self.assertEqual(result['status'], 'busy', result)
        update.assert_not_called()

    def test_completed_update_is_not_pass_when_running_instance_is_still_old(self):
        with patch('autopilot.generic_adapter.identity', side_effect=ValueError('instance still old')), \
                patch('autopilot.generic_adapter.master_build', return_value=self.master_manifest(), create=True), \
                patch('autopilot.generic_adapter.request_update', return_value={'status': 'pass'}) as update:
            result = generic_adapter.master_sync(self.request)
        self.assertNotEqual(result['status'], 'pass', result)
        self.assertNotIn('instance', result)
        update.assert_called_once()

    def test_completed_update_returns_real_instance_and_original_merge_identity(self):
        with patch('autopilot.generic_adapter.identity', side_effect=[ValueError('old instance'), self.instance]), \
                patch('autopilot.generic_adapter.master_build', return_value=self.master_manifest(), create=True), \
                patch('autopilot.generic_adapter.request_update', return_value={'status': 'pass'}):
            result = generic_adapter.master_sync(self.request)
        self.assertEqual(result['status'], 'pass', result)
        self.assertEqual(result['instance'], self.instance)
        self.assertEqual(result['master_update_merge_sha'], self.expected)

    def test_remote_advance_after_update_completion_invalidates_pass(self):
        def completed(*args):
            self.advance_remote()
            return {'status': 'pass'}
        with patch('autopilot.generic_adapter.identity', side_effect=[ValueError('old instance'), self.instance]), \
                patch('autopilot.generic_adapter.master_build', return_value=self.master_manifest(), create=True), \
                patch('autopilot.generic_adapter.request_update', side_effect=completed):
            result = generic_adapter.master_sync(self.request)
        self.assertEqual(result['status'], 'busy', result)


if __name__ == '__main__':
    unittest.main()
