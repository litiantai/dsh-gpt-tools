"""每日源码交付：真实临时 Git 仓库，受控 GitHub 与模型边界。"""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'dsh-gpt-supervisor/scripts')]
from review_core import Store, Conflict
from autopilot.api import Control
from autopilot.scheduler import Scheduler
from autopilot.workspace import git, digest
from autopilot.delivery import config, create_batch, attach_accepted, tick, complete, mark_online, start_migration, day_at
from autopilot.delivery_worker import prepare, paths, merge, sync, scan_publishable, exclude_dependency_cache
from autopilot.delivery_review import validate_coverage
from autopilot.github import repository, GitHub, connection_status, credential, save_credential, load_pull_request


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        git(self.source, 'init', '-b', 'master')
        git(self.source, 'config', 'user.name', 'Test')
        git(self.source, 'config', 'user.email', 'test@localhost')
        (self.source / 'app.py').write_text('value = 0\n')
        (self.source / '.gitignore').write_text('.env\nnode_modules/\n')
        git(self.source, 'add', '.')
        git(self.source, 'commit', '-m', 'initial')
        self.initial = git(self.source, 'rev-parse', 'HEAD')
        (self.source / 'app.py').write_text('value = 1\n')
        (self.source / 'untracked.py').write_text('existing = True\n')
        (self.source / '.env').write_text('PASSWORD=do-not-publish')
        self.remote = self.root / 'remote.git'
        git(self.source, 'init', '--bare', str(self.remote))
        self.store = Store(self.root / 'state', {'home': str(self.root / 'home')})
        self.control = Control(self.store)
        self.ledger = self.control.ledger
        agents = {k: {'provider': 'harness' if k == 'implementation' else 'codex', 'model': k,
                       **({'model_provider': 'test'} if k == 'implementation' else {})}
                  for k in ('discovery', 'implementation', 'verification', 'acceptance')}
        self.product = self.ledger.create('products', {'name': 'test', 'source': str(self.source), 'goal': 'test delivery',
            'delivery_flow': 'legacy',
            'git': {'url': 'https://github.com/example/repo', 'base_branch': 'master', 'enabled': True}, 'agents': agents,
            'policy': {'tokens_per_day': 0}, 'repository': str(self.source)}, 'active')
        self.scheduler = Scheduler(self.store)
        self.net = patch('autopilot.delivery_worker.network_git', side_effect=lambda root, *args: git(root, '-c', 'url.' + str(self.remote) + '.insteadOf=https://github.com/example/repo.git', *args))
        self.net.start()
        self.gh = patch('autopilot.delivery_worker.GitHub')
        self.github = self.gh.start().return_value
        self.github.repo = 'example/repo'
        self.github.review_report.return_value = {'html_url': 'https://github.com/example/repo/pull/1#issuecomment-1'}
        self.reader = patch('autopilot.github.GitHub', return_value=self.github)
        self.reader.start()
        self.github.pull_request.return_value = {'number': 1, 'html_url': 'https://github.com/example/repo/pull/1'}

    def tearDown(self):
        self.net.stop()
        self.gh.stop()
        self.reader.stop()
        self.tmp.cleanup()

    def request(self, batch, runs=None):
        return {'state_root': str(self.store.state / 'autopilot'), 'product': self.product, 'record': batch, 'runs': runs or [], 'requirements': []}

    def bootstrap(self):
        batch = start_migration(self.ledger, self.product)
        result = prepare(self.request(batch))
        complete(self.scheduler, batch, result, 'prepare')
        self.github.api.return_value = self.pr(batch, result['head_sha'], result['base_sha'])
        return self.ledger.get('deliveries', batch['id']), result

    def test_bare_repo_cache_exclusion_applies_to_linked_workspace(self):
        workspace = self.root / 'cache-workspace'
        git(self.remote, 'fetch', str(self.source), 'HEAD:refs/heads/master')
        git(self.remote, 'worktree', 'add', str(workspace), 'master')
        cache = workspace / '.pnpm-store' / 'v11'
        cache.mkdir(parents=True)
        (cache / 'generated.json').write_text('{}')
        (workspace / 'new-source.py').write_text('value = 2\n')
        (workspace / 'app.py').write_text('value = 3\n')
        exclude = self.remote / 'info' / 'exclude'
        exclude.write_text('# existing rule\ncustom-cache/\n')
        exclude_dependency_cache(self.remote)
        exclude_dependency_cache(self.remote)
        status = git(workspace, 'status', '--porcelain')
        self.assertNotIn('.pnpm-store', status)
        self.assertIn('new-source.py', status)
        self.assertIn('app.py', status)
        self.assertIn('custom-cache/', exclude.read_text())
        self.assertEqual(exclude.read_text().count('.pnpm-store/'), 1)

    def pr(self, batch, head, base):
        return {'number': 1, 'state': 'open', 'merged': False,
            'head': {'sha': head, 'ref': batch['branch'], 'repo': {'full_name': 'example/repo'}},
            'base': {'sha': base, 'ref': 'master', 'repo': {'full_name': 'example/repo'}}}

    def accepted(self, base=None, content='value = 2\n'):
        req = self.ledger.create('requirements', {'product_id': self.product['id'], 'title': 'Change value'}, 'accepted')
        run = self.ledger.create('runs', {'product_id': self.product['id'], 'title': 'Change value', 'requirement_id': req['id'], 'accepted_at': time.time()}, 'accepted')
        destination = self.root / run['id']
        git(self.source, 'clone', '--no-hardlinks', str(self.source), str(destination))
        git(destination, 'config', 'user.name', 'Test')
        git(destination, 'config', 'user.email', 'test@localhost')
        if base:
            git(destination, 'fetch', base[0], base[1])
            git(destination, 'checkout', '--detach', 'FETCH_HEAD')
        base_sha = git(destination, 'rev-parse', 'HEAD')
        (destination / 'app.py').write_text(content)
        git(destination, 'add', '.')
        git(destination, 'commit', '-m', 'accepted change')
        return self.ledger.update('runs', run['id'], run['version'], {'base_commit': base_sha, 'commit': git(destination, 'rev-parse', 'HEAD'),
            'workspace': str(destination), 'source_digest': digest(destination)})

    def test_bootstrap_preserves_source_index_and_ignored_files(self):
        before = git(self.source, 'status', '--porcelain')
        index = (self.source / '.git/index').read_bytes()
        batch, result = self.bootstrap()
        self.assertEqual(before, git(self.source, 'status', '--porcelain'))
        self.assertEqual(index, (self.source / '.git/index').read_bytes())
        self.assertTrue(Path(result['backup']).exists())
        tree = git(result['workspace'], 'ls-tree', '-r', '--name-only', 'HEAD')
        self.assertIn('untracked.py', tree)
        self.assertNotIn('.env', tree)
        self.assertEqual(git(self.remote, 'rev-parse', 'master'), self.initial)
        self.assertEqual(batch['status'], 'code_review')

    def test_preparation_replay_reuses_branch_and_snapshot(self):
        batch, first = self.bootstrap()
        (self.source / 'app.py').write_text('later_user_edit = True\n')
        second = prepare(self.request(batch))
        self.assertEqual(first['head_sha'], second['head_sha'])
        self.assertEqual(git(self.remote, 'rev-parse', batch['branch']), first['head_sha'])

    def test_same_day_append_invalidates_review_and_import_is_idempotent(self):
        batch, initial = self.bootstrap()
        run = self.accepted((initial['repository'], initial['head_sha']))
        product = self.ledger.get('products', self.product['id'])
        self.product = self.ledger.update('products', product['id'], product['version'], {'git_migration': {'status': 'completed'}})
        current = self.ledger.get('deliveries', batch['id'])
        self.ledger.update('deliveries', current['id'], current['version'], {'review_pass': {'head_sha': initial['head_sha']}}, 'awaiting_merge')
        attach_accepted(self.ledger, self.product, time.time())
        batch = self.ledger.get('deliveries', batch['id'])
        self.assertIsNone(batch['review_pass'])
        self.assertEqual(batch['run_ids'], [run['id']])
        result = prepare(self.request(batch, [run]))
        complete(self.scheduler, batch, result, 'prepare')
        self.assertEqual(self.ledger.get('runs', run['id'])['status'], 'delivered')
        self.assertEqual(prepare(self.request(batch, [run]))['head_sha'], result['head_sha'])

    def test_acceptance_source_mutation_is_rejected(self):
        batch, initial = self.bootstrap()
        run = self.accepted((initial['repository'], initial['head_sha']))
        Path(run['workspace'], 'app.py').write_text('unreviewed = True\n')
        with self.assertRaisesRegex(ValueError, '验收版本'):
            prepare(self.request(batch, [run]))

    def test_real_patch_conflict_is_sent_to_repair(self):
        batch, initial = self.bootstrap()
        run = self.accepted(content='value = 3\n')
        result = prepare(self.request(batch, [run]))
        self.assertEqual(result['status'], 'fail')
        self.assertIn('app.py', result['conflicts'])
        complete(self.scheduler, batch, result, 'prepare')
        self.assertEqual(self.ledger.get('deliveries', batch['id'])['status'], 'repairing')

    def test_online_requires_github_confirmation_and_updates_requirements(self):
        batch, initial = self.bootstrap()
        run = self.accepted((initial['repository'], initial['head_sha']))
        batch = self.ledger.update('deliveries', batch['id'], batch['version'], {'integrated_ids': [run['id']]})
        with self.assertRaises(ValueError):
            mark_online(self.ledger, batch, {'status': 'pass'})
        mark_online(self.ledger, batch, {'merged': True, 'merge_sha': 'remote-confirmed'})
        self.assertEqual(self.ledger.get('runs', run['id'])['status'], 'online')
        self.assertEqual(self.ledger.get('requirements', run['requirement_id'])['status'], 'online')
        self.assertEqual(self.ledger.get('products', self.product['id'])['git_migration']['status'], 'completed')

    def test_merge_rejects_stale_review_and_waits_for_ci(self):
        batch = create_batch(self.ledger, self.product, time.time())
        batch.update(pr_number=1, pr_url='https://github.com/example/repo/pull/1', frozen=True, cutoff=1, review_pass={'head_sha': 'a'*40, 'base_sha': 'b'*40}, validation_pass={'head_sha': 'a'*40, 'base_sha': 'b'*40})
        self.github.api.return_value = self.pr(batch, 'c'*40, 'b'*40)
        self.assertTrue(merge(self.request(batch))['stale'])
        self.github.api.return_value = self.pr(batch, 'a'*40, 'b'*40)
        self.github.checks_pass.return_value = False
        self.assertEqual(merge(self.request(batch))['status'], 'busy')
        self.github.merge.assert_not_called()

    def test_midnight_freezes_batch_and_snapshots_next_model(self):
        now = dt.datetime(2026, 10, 8, 23, 59, tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp()
        batch = create_batch(self.ledger, self.product, now)
        batch = self.ledger.update('deliveries', batch['id'], batch['version'], {'head_sha': 'h', 'base_sha': 'b'}, 'code_review')
        self.product = self.ledger.update('products', self.product['id'], self.product['version'], {'code_review': {'reviewer': {'provider': 'claude', 'model': 'configured-review'}}})
        with patch.object(self.scheduler, 'start_call') as call:
            tick(self.scheduler, now + 61)
            item = call.call_args.args[1]
            self.assertTrue(item['frozen'])
            self.assertEqual(item['selection']['model'], 'configured-review')
            review = self.ledger.get('code_reviews', item['round_id'])
            self.assertEqual(review['selection']['provider'], 'claude')
        self.assertEqual(day_at(now + 61), '20261009')

    def test_no_results_no_empty_batch_and_rolled_back_not_attached(self):
        self.product['git_migration'] = {'status': 'completed'}
        run = self.accepted()
        self.ledger.update('runs', run['id'], run['version'], {}, 'rolled_back')
        attach_accepted(self.ledger, self.product, time.time())
        self.assertEqual(self.ledger.list('deliveries'), [])

    def test_round_limit_queues_until_next_day_without_blocking(self):
        self.product = self.ledger.update('products', self.product['id'], self.product['version'], {'code_review': {'max_revisions': 3}})
        batch = create_batch(self.ledger, self.product, time.time())
        self.ledger.update('deliveries', batch['id'], batch['version'], {'revisions': 3}, 'repairing')
        for _ in range(3):
            self.ledger.create('code_reviews', {'product_id': self.product['id'], 'delivery_id': batch['id'],
                'phase': 'repair', 'started_at': time.time()}, 'pass')
        with patch.object(self.scheduler, 'start_call') as call:
            tick(self.scheduler)
            call.assert_not_called()
        waiting = self.ledger.get('deliveries', batch['id'])
        self.assertEqual(waiting['status'], 'repairing')
        with patch.object(self.scheduler, 'start_call') as call:
            tick(self.scheduler, waiting['repair_deferred_until'])
            self.assertEqual(call.call_args.args[3], 'repair')
        self.assertEqual(self.ledger.get('deliveries', batch['id'])['revisions'], 4)

    def test_unlimited_repairs_continue_after_many_rounds(self):
        for choices in ({}, {'max_revisions': 0}):
            with self.subTest(choices=choices):
                product = self.ledger.get('products', self.product['id'])
                self.control.configure_product(product['id'], {'version': product['version'], 'config': {'code_review': choices}})
                batch = create_batch(self.ledger, self.product, time.time())
                self.ledger.update('deliveries', batch['id'], batch['version'], {'revisions': 100}, 'repairing')
                with patch.object(self.scheduler, 'start_call') as call:
                    tick(self.scheduler)
                    self.assertEqual(call.call_args.args[3], 'repair')
                current = self.ledger.get('deliveries', batch['id'])
                self.assertEqual(current['status'], 'repairing')
                self.assertEqual(current['revisions'], 101)
                self.ledger.update('deliveries', current['id'], current['version'], {}, 'cancelled')

    def test_live_review_model_configuration_is_independent_and_not_retroactive(self):
        self.ledger.create('runs', {'product_id': self.product['id']}, 'developing')
        previous = config(self.product)
        values = {'reviewer': {'provider': 'claude', 'model': 'review-only'}, 'fixer': {'provider': 'codex', 'model': 'fix-only'}, 'max_revisions': 4}
        changed = self.control.configure_product(self.product['id'], {'version': self.product['version'], 'config': {'code_review': values}})
        self.assertEqual(changed['agents'], self.product['agents'])
        self.assertEqual(previous['reviewer']['model'], 'acceptance')
        self.assertEqual(config(changed)['fixer']['model'], 'fix-only')

    def test_invalid_git_and_review_configuration(self):
        for url in ('https://token@github.com/a/b', 'file:///tmp/repo', 'https://example.com/a/b'):
            with self.assertRaises(ValueError):
                repository(url)
        self.assertEqual(repository('git@github.com:owner/repo.git'), 'owner/repo')
        for value in ({'max_revisions': True}, {'max_revisions': -1}, {'max_revisions': 21}, {'max_revisions': 0.5}, {'reviewer': {'provider': 'codex', 'model': ''}}, {'fixer': {'provider': 'unknown', 'model': 'x'}}):
            with self.assertRaises(ValueError):
                self.control.validate_product(self.product | {'code_review': value})

    def test_coverage_skips_duplicates_and_medium_issues_cannot_pass(self):
        good = {'status': 'pass', 'coverage': [{'path': 'app.py', 'status': 'reviewed'}], 'issues': []}
        self.assertTrue(validate_coverage(good, ['app.py']))
        for coverage in ([], good['coverage'] * 2, [{'path': 'app.py', 'status': 'skipped'}]):
            with self.assertRaises(ValueError):
                validate_coverage(good | {'coverage': coverage}, ['app.py'])
        self.assertFalse(validate_coverage(good | {'issues': [{'severity': 'medium'}]}, ['app.py']))

    def test_first_pending_day_prevents_later_merge(self):
        early = create_batch(self.ledger, self.product, 1791388800)
        self.ledger.update('deliveries', early['id'], early['version'], {'reason': 'needs repair'}, 'blocked')
        later = create_batch(self.ledger, self.product, 1791475200)
        self.ledger.update('deliveries', later['id'], later['version'], {}, 'merging')
        with patch.object(self.scheduler, 'start_call') as call:
            tick(self.scheduler, 1791561600)
            call.assert_not_called()

    def test_prepared_result_is_recovered_after_controller_restart(self):
        batch, result = self.bootstrap()
        current = self.ledger.get('deliveries', batch['id'])
        self.ledger.update('deliveries', current['id'], current['version'], {'pending_result': {'action': 'prepare', 'result': result}}, 'preparing')
        with patch.object(self.scheduler, 'start_call') as call:
            tick(self.scheduler)
            call.assert_not_called()
        current = self.ledger.get('deliveries', batch['id'])
        self.assertEqual(current['status'], 'code_review')
        self.assertIsNone(current['pending_result'])

    def test_baseline_includes_accepted_dependencies_and_supports_explicit_close(self):
        first = self.accepted(content='value = 2\n')
        second = self.accepted(content='value = 3\n')
        second = self.ledger.update('runs', second['id'], second['version'], {'dependencies': [{'run_id': first['id'], 'commit': first['commit']}]})
        batch = start_migration(self.ledger, self.product)
        self.assertEqual(batch['run_ids'], [first['id'], second['id']])
        self.assertEqual(self.ledger.get('runs', first['id'])['delivery_id'], batch['id'])
        closed = self.control.mutate('/deliveries/' + batch['id'] + '/close', {'version': batch['version']})
        self.assertTrue(closed['frozen'])
        self.assertLessEqual(closed['cutoff'], time.time())
        self.assertEqual(closed['run_ids'], batch['run_ids'])

    def test_delivery_usage_is_separate_and_development_quota_does_not_block_review(self):
        from autopilot.usage import register, collect
        ordinary, review = self.root/'development.jsonl', self.root/'code-review.jsonl'
        ordinary.write_text(json.dumps({'type':'turn.completed','usage':{'input_tokens':100,'output_tokens':1}})+'\n')
        review.write_text(json.dumps({'type':'turn.completed','usage':{'input_tokens':900,'output_tokens':1}})+'\n')
        register(self.ledger, self.product['id'], ordinary)
        register(self.ledger, self.product['id'], review, 'code_delivery_tokens')
        collect(self.ledger); collect(self.ledger)
        product = self.ledger.update('products',self.product['id'],self.product['version'],{'policy':{'tokens_per_day':100}})
        self.assertFalse(self.scheduler.tokens_available(product))
        metrics = self.control.get('/products/'+product['id']+'/automation')
        self.assertEqual(metrics['tokens_used'],101)
        self.assertEqual(metrics['code_delivery_tokens_used'],901)
        batch = create_batch(self.ledger,product,time.time())
        self.ledger.update('deliveries',batch['id'],batch['version'],{'head_sha':'h','base_sha':'b','pr_url':'https://github.com/example/repo/pull/1'},'code_review')
        with patch.object(self.scheduler,'start_call') as start:
            tick(self.scheduler)
            self.assertEqual(start.call_args.args[3],'review')

    def test_target_advance_is_merged_and_requires_new_review(self):
        batch, result = self.bootstrap()
        writer = self.root / 'writer'
        git(self.source, 'clone', str(self.remote), str(writer))
        git(writer, 'checkout', 'master')
        git(writer, 'config', 'user.name', 'Other')
        git(writer, 'config', 'user.email', 'other@localhost')
        (writer / 'another.py').write_text('other = True\n')
        git(writer, 'add', '.')
        git(writer, 'commit', '-m', 'independent change')
        git(writer, 'push', 'origin', 'master')
        self.github.api.return_value = self.pr(batch, result['head_sha'], git(writer, 'rev-parse', 'HEAD'))
        synced = sync(self.request(batch))
        self.assertNotEqual(synced['head_sha'], result['head_sha'])
        complete(self.scheduler, batch, synced, 'sync')
        updated = self.ledger.get('deliveries', batch['id'])
        self.assertEqual(updated['status'], 'code_review')
        self.assertIsNone(updated['review_pass'])
        self.assertTrue(Path(updated['workspace'], 'another.py').exists())

    def test_failed_review_keeps_result_when_github_status_write_fails(self):
        from autopilot.delivery_review import execute
        batch, _ = self.bootstrap()
        batch.update(round_id='failed-review', selection={'provider': 'codex', 'model': 'chosen-model'})
        def reviewer(request, folder, prompt, **kwargs):
            files = json.loads((folder.parent / 'ocr-preview.json').read_text())['reviewable_files']
            return {'status': 'fail', 'reason': '源码缺少必要校验',
                'coverage': [{'path': f['path'], 'status': 'reviewed'} for f in files],
                'issues': [{'path': files[0]['path'], 'severity': 'high', 'content': '输入未经校验', 'start_line': 1}]}
        self.github.status.side_effect = [None, ValueError('GitHub temporarily unavailable')]
        with patch('autopilot.delivery_review.model', side_effect=reviewer):
            result = execute('review', self.request(batch))
        self.assertEqual(result['status'], 'fail')
        self.assertEqual(result['reason'], '源码缺少必要校验')
        self.assertIn('reporting_error', result)
        self.assertIn(result['reason'], self.github.status.call_args.args[2])
        folder = paths(self.request(batch))[1] / 'rounds' / batch['round_id']
        self.assertEqual(json.loads((folder / 'result.json').read_text()), result)

    @unittest.skipUnless(sys.platform == 'darwin' and not os.environ.get('DSH_PROJECT_ISOLATED'), '由外层验证运行 macOS 沙箱执行器测试，系统不允许嵌套 Seatbelt')
    def test_harness_review_uses_private_profile_and_retains_failure_diagnostics(self):
        from autopilot.delivery_review import model
        from reviewers import command as real_command
        batch, _ = self.bootstrap()
        batch['selection'] = {'provider': 'harness', 'model': 'fixture', 'model_provider': 'fixture'}
        profile = self.root / 'home/profiles/supervisor-review'
        profile.mkdir(parents=True)
        (profile / 'cordis.patch.yml').write_text('[]')
        shared = profile / 'cordis.yml'
        shared.write_text('original')
        runtime = self.root / 'runtime'
        (runtime / 'node_modules').mkdir(parents=True)
        self.product['worker_runtime'] = str(runtime)
        folder = self.root / 'isolated-plan'
        temp = self.root / 'sandbox-temp'
        temp.mkdir()
        script = self.root / 'fake-harness.py'
        script.write_text("""import json, os, sys
from pathlib import Path
home = Path(os.environ['DSH_HOME'])
(home / 'profiles/autopilot-review/cordis.yml').write_text('composed')
for target in sys.argv[1:]:
    try:
        Path(target).write_text('must be denied')
    except PermissionError:
        pass
    else:
        raise AssertionError('escaped sandbox: ' + target)
print('EPERM: fixture startup failed; token=secret-fixture', file=sys.stderr)
sys.exit(1)
""")
        def fake_command(actual, dest, packet):
            _, env = real_command(actual, dest, packet)
            self.assertEqual(actual['harness_home'], str(folder / 'home'))
            self.assertEqual(actual['harness_profile'], 'autopilot-review')
            return [sys.executable, str(script), str(shared), str(Path(batch['workspace']) / 'app.py')], env
        with patch('reviewers.command', side_effect=fake_command), patch('autopilot.delivery_review.tempfile.gettempdir', return_value=str(temp)):
            result = model(self.request(batch), folder, '只读评审', review=True)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('fixture startup failed', result['detail'])
        self.assertNotIn('secret-fixture', result['detail'])
        self.assertEqual(result['model'], 'fixture')
        self.assertEqual(json.loads((folder / 'result.json').read_text()), result)
        self.assertEqual(shared.read_text(), 'original')
        self.assertEqual((folder / 'home/profiles/autopilot-review/cordis.yml').read_text(), 'composed')
        overlay = json.loads((folder / 'reviewer.patch.json').read_text())
        self.assertIn({'id': 'sandbox-policy', 'config': {'mode': 'danger-full-access'}}, overlay)

    def test_harness_repair_plan_uses_readonly_structured_executor(self):
        from autopilot.delivery_review import model
        batch, _ = self.bootstrap()
        batch['selection'] = {'provider': 'harness', 'model': 'fixture', 'model_provider': 'fixture'}
        folder = self.root / 'repair-plan'
        result = {'status': 'pass', 'plan': '补齐输入校验并运行测试', 'evidence': '/execution/evidence'}
        with patch('autopilot.executor.execute', return_value=result) as execute:
            actual = model(self.request(batch), folder, '只读制定修复方案')
        self.assertEqual(execute.call_args.args[0], 'plan')
        worker = execute.call_args.args[1]['record']
        self.assertEqual(worker['worker_home'], str(folder / 'home'))
        self.assertEqual(actual['evidence'], '/execution/evidence')
        self.assertEqual(actual['plan'], result['plan'])

    def test_codex_review_persists_terminal_usage_error_instead_of_stderr_warning(self):
        from autopilot.delivery_review import model
        batch, _ = self.bootstrap()
        batch['selection'] = {'provider': 'codex', 'model': 'fixture'}
        folder = self.root / 'codex-error'
        real_run = subprocess.run
        def failed(argv, **kwargs):
            if len(argv) > 1 and str(argv[1]).endswith('read-workflow.mjs'):
                return real_run(argv, **kwargs)
            kwargs['stdout'].write(json.dumps({'type': 'turn.failed', 'error': {
                'message': 'You’ve hit your usage limit. Try again later.'}}) + '\n')
            kwargs['stderr'].write('model list request timed out')
            return subprocess.CompletedProcess(argv, 1)
        with patch('codex_account.account_status', return_value={'status': 'available', 'checked_at': 123}) as account, \
                patch('autopilot.delivery_review.restrict', side_effect=lambda argv, *args, **kwargs: argv), \
                patch('autopilot.delivery_review.subprocess.run', side_effect=failed):
            result = model(self.request(batch), folder, '只读评审', review=True)
        self.assertEqual(account.call_args.args[1]['bin'], self.store.settings()['codex_bin'])
        self.assertEqual(result['account_snapshot']['status'], 'available')
        self.assertEqual(result['error_code'], 'QUOTA_EXHAUSTED')
        self.assertEqual(result['failure_kind'], 'agent_execution')
        self.assertFalse(result['retryable'])
        self.assertEqual(result['model'], 'fixture')
        self.assertEqual(json.loads((folder / 'result.json').read_text()), result)

    def test_live_codex_quota_blocks_generation_but_unknown_query_does_not(self):
        from autopilot.delivery_review import model
        batch, _ = self.bootstrap()
        batch['selection'] = {'provider': 'codex', 'model': 'fixture'}
        for status in ('limited', 'unauthenticated', 'unknown'):
            folder = self.root / ('preflight-' + status)
            with patch('codex_account.account_status', return_value={'status': status, 'checked_at': 123}), \
                    patch('autopilot.delivery_review.restrict', side_effect=lambda argv, *args, **kwargs: argv), \
                    patch('autopilot.delivery_review.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run, \
                    patch('reviewers.read_result', return_value={'status': 'pass'}):
                result = model(self.request(batch), folder, '只读评审', review=True)
            self.assertEqual(result['account_snapshot']['status'], status)
            self.assertEqual(json.loads((folder / 'account.json').read_text())['status'], status)
            if status == 'unknown':
                run.assert_called_once()
                self.assertEqual(result['status'], 'pass')
            else:
                run.assert_not_called()
                self.assertEqual(result['status'], 'blocked')
                self.assertFalse(result['retryable'])

    def test_review_runs_real_pinned_ocr_without_starting_runtime_verification(self):
        from autopilot.delivery_review import execute
        batch, result = self.bootstrap()
        adapter = self.root / 'verify.py'
        adapter.write_text('import json,sys\nr=json.load(sys.stdin)\nassert r.get("budget_kind")=="code_delivery_tokens"\nprint(json.dumps({"status":"pass","checks":[{"name":"required test","status":"pass","required":True}]}))\n')
        self.product['adapter'] = [sys.executable, str(adapter)]
        batch.update(round_id='review-fixture', selection={'provider': 'codex', 'model': 'chosen-model'})
        def reviewer(request, folder, prompt, review=False, write=False):
            self.assertIn(batch['pr_url'], prompt)
            preview = json.loads((folder.parent / 'ocr-preview.json').read_text())
            return {'status': 'pass', 'summary': 'checked', 'reason': '', 'coverage': [{'path': f['path'], 'status': 'reviewed', 'reason': ''} for f in preview['reviewable_files']],
                    'issues': [], 'provider': 'codex', 'model': request['record']['selection']['model']}
        with patch('autopilot.delivery_review.model', side_effect=reviewer), patch('autopilot.github.GitHub', return_value=self.github), patch('autopilot.delivery_review.verify') as verify:
            reviewed = execute('review', self.request(batch))
            verify.assert_not_called()
        self.assertEqual(reviewed['status'], 'pass')
        self.assertGreater(reviewed['total_files'], 0)
        self.assertEqual(reviewed['model'], 'chosen-model')
        self.assertEqual(reviewed['skill_version']['cli_version'], '1.12.12')

    def test_validate_passes_bare_repository_to_delivery_verify(self):
        from autopilot.delivery_review import execute as review
        batch, result = self.bootstrap()
        batch = self.ledger.update('deliveries', batch['id'], batch['version'],
            {'review_pass': {'head_sha': result['head_sha'], 'base_sha': result['base_sha']}}, 'validating')
        captured = {}
        def fake_verify(request, workspace, head, base, folder):
            captured.update(request['record'])
            return {'status': 'pass', 'checks': [{'name': 'required test', 'status': 'pass', 'required': True}]}
        with patch('autopilot.delivery_review.verify', side_effect=fake_verify):
            validated = review('validate', self.request(batch))
        self.assertEqual(validated['status'], 'pass', validated)
        repo = paths(self.request(batch))[2]
        # checkout 元数据之外的 bare 仓库必须显式下发给验证层，供 base..commit 差异使用。
        self.assertEqual(captured.get('repository'), str(repo))
        self.assertEqual(captured.get('git_dir'), str(repo))

    def test_review_uses_saved_pr_and_resyncs_changed_remote_without_model(self):
        from autopilot.delivery_review import execute
        batch, result = self.bootstrap()
        batch.update(round_id='changed-pr', selection={'provider': 'codex', 'model': 'chosen-model'})
        self.github.api.return_value = self.pr(batch, 'f'*40, result['base_sha'])
        with patch('autopilot.delivery_review.model') as model:
            reviewed = execute('review', self.request(batch))
            model.assert_not_called()
        self.assertTrue(reviewed['stale'])
        self.github.api.assert_called_with('/pulls/1')
        # No persisted round was created in this direct worker fixture.
        batch.pop('round_id')
        complete(self.scheduler, batch, reviewed, 'review')
        self.assertEqual(self.ledger.get('deliveries', batch['id'])['status'], 'syncing')

    def test_missing_data_compatibility_command_blocks_before_launching_app(self):
        from autopilot.thsoctop import verify
        store = self.source / 'packages/example/src/store.ts'
        store.parent.mkdir(parents=True)
        store.write_text('export const store = {}\n')
        git(self.source, 'add', str(store))
        git(self.source, 'commit', '-m', 'persistent change')
        request = {'product': {}, 'record': {'id': 'preflight', 'workspace': str(self.source),
            'base_commit': self.initial, 'commit': git(self.source, 'rev-parse', 'HEAD')},
            'state_root': str(self.root / 'state')}
        with patch('autopilot.thsoctop.command') as command, patch('autopilot.thsoctop.stage_runtime') as stage:
            result = verify(request)
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('兼容与恢复', result['reason'])
        self.assertEqual(result['checks'][0]['files'], ['packages/example/src/store.ts'])
        command.assert_not_called()
        stage.assert_not_called()

    def test_created_pr_address_survives_status_write_failure(self):
        from autopilot.delivery_worker import execute
        batch = start_migration(self.ledger, self.product)
        self.github.status.side_effect = ValueError('status API unavailable')
        result = execute('prepare', self.request(batch))
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['pr_url'], 'https://github.com/example/repo/pull/1')
        complete(self.scheduler, batch, result, 'prepare')
        self.assertEqual(self.ledger.get('deliveries', batch['id'])['pr_url'], result['pr_url'])

    def test_repair_records_plan_then_resolves_real_conflict(self):
        from autopilot.delivery_review import execute
        batch, result = self.bootstrap()
        run = self.accepted(content='value = 3\n')
        failed = prepare(self.request(batch, [run]))
        batch.update(round_id='repair-fixture', selection={'provider': 'codex', 'model': 'fix-model'}, feedback=failed)
        phases = []
        def fixer(request, folder, prompt, review=False, write=False):
            phases.append(write)
            if write:
                Path(result['workspace'], 'app.py').write_text('value = 3\n')
            return {'status': 'pass', 'summary': 'preserve existing baseline; resolve value conflict', 'reason': ''}
        with patch('autopilot.delivery_review.model', side_effect=fixer):
            repaired = execute('repair', self.request(batch, [run]))
        self.assertEqual(phases, [False, True])
        self.assertEqual(repaired['status'], 'pass')
        self.assertEqual(git(result['workspace'], 'diff', '--name-only', '--diff-filter=U'), '')
        self.assertEqual(prepare(self.request(batch, [run]))['integrated_ids'], [run['id']])

    def test_configuration_checks_remote_and_preserves_other_projects(self):
        before = copy.deepcopy(self.product)
        with patch.object(self.github, 'access', return_value={'accessible': True}) as access:
            updated = self.control.configure_product(self.product['id'], {'version': self.product['version'],
                'config': {'git': {'url': 'https://github.com/example/other', 'base_branch': 'master', 'enabled': False}}})
            access.assert_called_once_with(write=False)
        self.assertEqual(updated['agents'], before['agents'])
        self.assertEqual(updated['git_migration']['status'], 'required')

    def test_configured_but_disabled_git_waits_for_baseline_instead_of_legacy_deployment(self):
        from autopilot.delivery import ready
        product = self.ledger.update('products', self.product['id'], self.product['version'],
            {'git': self.product['git'] | {'enabled': False}})
        self.assertFalse(ready(product))
        req = self.ledger.create('requirements', {'product_id': product['id'], 'title': 'queued'}, 'queued')
        run = self.ledger.create('runs', {'product_id': product['id'], 'requirement_id': req['id']}, 'queued')
        with patch.object(self.scheduler, 'start_call') as start:
            self.scheduler.advance(run)
            start.assert_not_called()
        self.assertEqual(self.ledger.get('runs', run['id'])['reason'], '等待首次源码基线 PR 合入')


    def test_git_status_uses_project_configuration_without_mutation(self):
        other = self.ledger.create('products', {'name': 'unconfigured'}, 'paused')
        with patch('autopilot.github.credential', return_value=None), patch.object(GitHub, 'api') as api:
            result = self.control.get('/products/' + self.product['id'] + '/git-status')
            self.assertEqual(result['repository'], 'example/repo')
            self.assertEqual(result['code'], 'missing_credentials')
            self.assertEqual(self.control.get('/products/' + other['id'] + '/git-status')['status'], 'unconfigured')
            self.assertEqual(self.ledger.get('products', self.product['id'])['version'], self.product['version'])
            api.assert_not_called()


class GitHubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'credentials/github-token'
        self.files = patch('autopilot.github.credential_file', return_value=self.path)
        self.files.start()

    def tearDown(self):
        self.files.stop()
        self.tmp.cleanup()

    def test_review_report_contains_findings_and_replay_preserves_failure_comment(self):
        gh = GitHub('https://github.com/example/repo')
        result = {'status': 'fail', 'head_sha': 'a'*40, 'base_sha': 'b'*40, 'reason': '缺少 AppKit 链接',
            'summary': '发现两个问题', 'reviewed_files': 119, 'total_files': 119,
            'issues': [{'path': 'apps/build.rs', 'start_line': 17, 'severity': 'high', 'content': '应链接 AppKit framework'},
                {'path': 'src/index.ts', 'start_line': 117, 'severity': 'medium', 'content': '输入缺少校验'}]}
        saved = {'html_url': 'https://github.com/example/repo/pull/2#issuecomment-42'}
        with patch.object(gh, 'api', side_effect=[[], saved]) as api:
            self.assertEqual(gh.review_report(2, 'round-1', result), saved)
            body = api.call_args.args[2]['body']
            self.assertIn('缺少 AppKit 链接', body)
            self.assertIn('输入缺少校验', body)
            self.assertIn('apps/build.rs#L17', body)
            self.assertIn('119 / 119', body)
            self.assertEqual(api.call_args.args[:2], ('/issues/2/comments', 'POST'))
        existing = saved | {'body': body}
        with patch.object(gh, 'api', return_value=[existing]) as api:
            self.assertEqual(gh.review_report(2, 'round-1', result), existing)
            api.assert_called_once()
        with patch.object(gh, 'api', return_value=saved) as api:
            gh.status(result['head_sha'], 'failure', result['reason'], target_url=saved['html_url'])
            self.assertEqual(api.call_args.args[2]['target_url'], saved['html_url'])

    def test_receipt_preserves_model_failure_code_when_adapter_exits_zero(self):
        from autopilot.call import main
        folder = Path(self.tmp.name) / 'receipt'
        folder.mkdir()
        request = folder / 'request.json'
        request.write_text(json.dumps({'command': [sys.executable, '-c',
            'import json; print(json.dumps({"status":"blocked","reason":"模型启动失败","exit_code":1}))'],
            'input': {}, 'timeout': 10}))
        main(request)
        result = json.loads((folder / 'result.json').read_text())
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['exit_code'], 1)
        self.assertEqual(result['adapter_exit_code'], 0)

    def test_agent_failure_report_is_an_execution_error_not_a_code_verdict(self):
        from autopilot.delivery_review import publish_result
        gh = GitHub('https://github.com/example/repo')
        result = {'status': 'blocked', 'failure_kind': 'agent_execution', 'reason': '模型账户额度不足',
            'detail': 'You’ve hit your usage limit. token=fixture-secret </pre><script>bad</script>',
            'total_files': 36, 'head_sha': 'a'*40, 'base_sha': 'b'*40}
        saved = {'html_url': 'https://github.com/example/repo/pull/2#issuecomment-43'}
        with patch.object(gh, 'api', side_effect=[[], saved, {}]) as api:
            receipt = publish_result(gh, 2, 'agent-error', Path(self.tmp.name), result, 'error', '评审 Agent 运行异常：模型启动失败')
        body = api.call_args_list[1].args[2]['body']
        self.assertIn('评审 Agent 运行异常', body)
        self.assertIn('本轮未形成有效代码评审结论', body)
        self.assertNotIn('代码评审未通过', body)
        self.assertIn('运行错误详情', body)
        self.assertIn('hit your usage limit', body)
        self.assertNotIn('fixture-secret', body)
        self.assertNotIn('<script>', body)
        self.assertIn('未形成有效统计', body)
        self.assertNotIn('文件覆盖：0 / 36', body)
        self.assertEqual(api.call_args.args[2]['state'], 'error')
        self.assertEqual(receipt['status'], 'blocked')
        self.assertEqual(json.loads((Path(self.tmp.name) / 'result.json').read_text()), receipt)

    def test_saved_pr_address_must_match_repository_number_and_branches(self):
        record = {'git_url': 'https://github.com/example/repo', 'pr_url': 'https://github.com/example/repo/pull/12',
                  'pr_number': 12, 'branch': 'release-20261008', 'base_branch': 'master'}
        pr = {'number': 12, 'head': {'ref': record['branch'], 'repo': {'full_name': 'example/repo'}, 'sha': 'a'*40},
              'base': {'ref': 'master', 'repo': {'full_name': 'example/repo'}, 'sha': 'b'*40}}
        with patch.object(GitHub, 'api', return_value=pr) as api:
            self.assertEqual(load_pull_request(record)[1], pr)
            api.assert_called_once_with('/pulls/12')
            for changes in [{'pr_url': 'https://github.com/other/repo/pull/12'}, {'pr_number': 13}, {'pr_url': ''}, {'branch': 'unrelated'}]:
                with self.assertRaises(ValueError):
                    load_pull_request(record | changes)

    def test_save_token_validates_candidate_and_retains_previous_on_failure(self):
        token = 'github_pat_TEST_ONLY_0123456789'
        with patch('autopilot.github.urllib.request.urlopen') as http:
            http.return_value.__enter__.return_value.read.return_value = json.dumps({'permissions': {'push': True}}).encode()
            http.return_value.__enter__.return_value.headers = {'Content-Type': 'application/x-git-receive-pack-advertisement'}
            result = save_credential('https://github.com/example/repo', token)
            self.assertEqual(http.call_args_list[0].args[0].get_header('Authorization'), 'Bearer ' + token)
            self.assertIn('service=git-receive-pack', http.call_args.args[0].full_url)
        self.assertTrue(result['saved'])
        self.assertNotIn(token, json.dumps(result))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        with patch.dict('os.environ', {'GH_TOKEN': 'old-environment'}):
            self.assertEqual(credential(), token)
        with patch.object(GitHub, 'api', return_value={'permissions': {'push': False}}):
            with self.assertRaisesRegex(ValueError, '推送权限'):
                save_credential('https://github.com/example/repo', 'github_pat_BAD_TEST_0123456789')
        self.assertEqual(credential(), token)

    def test_token_operation_never_persists_secret_in_audit_or_project(self):
        import uuid
        from dashboard_server import Dashboard
        app = Dashboard(Path(self.tmp.name) / 'state')
        product = app.autopilot.ledger.create('products', {'name': 'test', 'git': {'url': 'https://github.com/example/repo'}}, 'paused')
        body = {'token': 'github_pat_TEST_ONLY_0123456789', 'operation_id': str(uuid.uuid4())}
        path = '/products/' + product['id'] + '/git-token/'
        with patch.object(GitHub, 'api', return_value={'permissions': {'push': True}}) as api, patch.object(GitHub, 'probe_push'):
            result = app.operation(path, body)
            self.assertEqual(app.operation(path, body), result)
            self.assertEqual(api.call_count, 1)
            with self.assertRaises(Conflict):
                app.operation(path, body | {'token': 'different_secret_value_1234'})
        self.assertEqual(app.autopilot.get(path), {'configured': True})
        self.assertEqual(app.autopilot.ledger.get('products', product['id']), product)
        with app.store.connect() as db:
            dump = '\n'.join(db.iterdump())
        self.assertNotIn(body['token'], dump)
        self.assertNotIn('different_secret_value_1234', dump)
        self.assertIn('sha256:', dump)

    def test_account_push_permission_does_not_hide_token_push_denial(self):
        import urllib.error
        self.path.parent.mkdir()
        self.path.write_text('github_pat_OLD_TEST_0123456789')
        with patch.object(GitHub, 'api', return_value={'permissions': {'push': True}}), patch('autopilot.github.urllib.request.urlopen', side_effect=urllib.error.HTTPError('https://github.com/example/repo.git',403,'Forbidden',{},None)):
            status = connection_status('https://github.com/example/repo')
            self.assertEqual(status['status'], 'blocked')
            self.assertIn('HTTP 403', status['reason'])
            with self.assertRaisesRegex(ValueError, 'HTTP 403'):
                save_credential('https://github.com/example/repo', 'github_pat_NEW_TEST_0123456789')
        self.assertEqual(credential(), 'github_pat_OLD_TEST_0123456789')

    def test_missing_runtime_credentials_block_status_and_writes(self):
        with patch('autopilot.github.credential', return_value=None), patch.object(GitHub, 'api') as api:
            status = connection_status('https://github.com/owner/repo')
            self.assertEqual(status['code'], 'missing_credentials')
            self.assertEqual(status['status'], 'blocked')
            with self.assertRaisesRegex(ValueError, '本地运行时未找到'):
                GitHub('https://github.com/owner/repo').access(write=True)
            api.assert_not_called()
        with patch('autopilot.delivery_worker.credential', return_value=None), patch('autopilot.delivery_worker.subprocess.run') as run:
            from autopilot.delivery_worker import network_git
            with self.assertRaisesRegex(ValueError, '推送已阻塞'):
                network_git('/unused', 'push', 'origin', 'master')
            run.assert_not_called()

    def test_runtime_auth_status_permissions_recovery_and_secret_redaction(self):
        with patch('autopilot.github.credential', return_value='never-return-this-secret'), patch.object(GitHub, 'api') as api, patch.object(GitHub, 'probe_push'):
            api.return_value = {'permissions': {'push': False}}
            self.assertEqual(connection_status('https://github.com/owner/repo')['code'], 'insufficient_permissions')
            api.return_value = {'permissions': {'push': True}, 'allow_merge_commit': True}
            status = connection_status('https://github.com/owner/repo')
            self.assertEqual(status['status'], 'pass')
            self.assertNotIn('never-return-this-secret', json.dumps(status))
            api.side_effect = OSError('network error containing never-return-this-secret')
            status = connection_status('https://github.com/owner/repo')
            self.assertEqual(status['code'], 'unavailable')
            self.assertNotIn('never-return-this-secret', json.dumps(status))

    def test_credentials_use_existing_runtime_sources(self):
        with patch.dict('os.environ', {'GH_TOKEN': 'environment-secret'}, clear=True), patch('autopilot.github.subprocess.run') as run:
            self.assertEqual(credential(), 'environment-secret')
            run.assert_not_called()
        with patch.dict('os.environ', {}, clear=True), patch('autopilot.github.subprocess.run', side_effect=[FileNotFoundError(), subprocess.CompletedProcess([], 0, 'username=local\npassword=helper-secret\n')]) as run:
            self.assertEqual(credential(), 'helper-secret')
            self.assertEqual(run.call_args.kwargs['env']['GIT_TERMINAL_PROMPT'], '0')

    def test_pr_creation_unknown_result_reuses_existing(self):
        gh = GitHub('https://github.com/owner/repo')
        with patch.object(gh, 'api', side_effect=[[{'number': 4, 'state': 'open'}], {'number': 4}]) as api:
            self.assertEqual(gh.pull_request('release-20261008', 'master', 'title', 'body')['number'], 4)
            self.assertEqual(api.call_args.args[1], 'PATCH')

    def test_remote_merged_result_is_reconciled_without_second_merge(self):
        gh = GitHub('https://github.com/owner/repo')
        with patch.object(gh, 'api', return_value={'merged': True, 'merge_commit_sha': 'sha'}) as api:
            self.assertTrue(gh.merge(1, 'head')['merged'])
            self.assertEqual(api.call_count, 1)


if __name__ == '__main__':
    unittest.main()
