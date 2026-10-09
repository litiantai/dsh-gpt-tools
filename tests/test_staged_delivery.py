"""真实 Git 验证：Code Review 门禁、每日汇集、23:30 封板和统一验证。"""
import datetime as dt
import json
import sys
from pathlib import Path
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import test_delivery
from autopilot.delivery import create_batch, complete, tick, ZONE
from autopilot.staged_delivery import prepare, execute, feature_request
from autopilot.workspace import git


class StagedDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.f = test_delivery.DeliveryTests()
        self.f.setUp()
        f = self.f
        f.product = f.ledger.update('products', f.product['id'], f.product['version'], {'delivery_flow': 'review_before_release'})
        self.day = dt.datetime.now(ZONE).replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
        self.prs = {}
        f.github.pull_request.side_effect = self.pull_request
        f.github.api.side_effect = self.pr_api
        f.github.merge.side_effect = self.merge_pr
        f.github.checks_pass.return_value = True
        self.net = patch('autopilot.staged_delivery.network_git', side_effect=lambda root, *args: git(root, '-c', 'url.' + str(f.remote) + '.insteadOf=https://github.com/example/repo.git', *args))
        self.net.start()
        self.gh = patch('autopilot.staged_delivery.GitHub', return_value=f.github)
        self.gh.start()

    def tearDown(self):
        self.net.stop()
        self.gh.stop()
        self.f.tearDown()

    def pull_request(self, branch, base, title, body, successive=False):
        f = self.f
        for pr in self.prs.values():
            if pr['head']['ref'] == branch and pr['base']['ref'] == base and not pr['merged']:
                pr['head']['sha'] = git(f.remote, 'rev-parse', branch)
                pr['base']['sha'] = git(f.remote, 'rev-parse', base)
                return pr
        number = len(self.prs) + 1
        pr = {'number': number, 'html_url': f'https://github.com/example/repo/pull/{number}', 'state': 'open', 'merged': False,
            'mergeable': True, 'mergeable_state': 'clean',
            'head': {'sha': git(f.remote, 'rev-parse', branch), 'ref': branch, 'repo': {'full_name': 'example/repo'}},
            'base': {'sha': git(f.remote, 'rev-parse', base), 'ref': base, 'repo': {'full_name': 'example/repo'}}}
        self.prs[number] = pr
        return pr

    def merge_pr(self, number, head):
        f = self.f
        pr = self.prs[number]
        self.assertEqual(head, pr['head']['sha'])
        work = f.root / f'merge-{number}'
        git(f.source, 'clone', str(f.remote), str(work))
        git(work, 'config', 'user.name', 'GitHub test')
        git(work, 'config', 'user.email', 'github@test')
        git(work, 'checkout', pr['base']['ref'])
        git(work, 'merge', '--no-ff', '-m', 'Merge reviewed feature', 'origin/' + pr['head']['ref'])
        git(work, 'push', 'origin', pr['base']['ref'])
        pr.update(merged=True, state='closed', merge_commit_sha=git(work, 'rev-parse', 'HEAD'))
        return pr

    def pr_api(self, route, method='GET', body=None):
        pr = self.prs[int(route.split('/')[-1])]
        if method == 'PATCH' and body and 'base' in body:
            pr['base']['ref'] = body['base']
        if method == 'PATCH':
            pr['head']['sha'] = git(self.f.remote, 'rev-parse', pr['head']['ref'])
            pr['base']['sha'] = git(self.f.remote, 'rev-parse', pr['base']['ref'])
        return pr

    def test_two_requirements_have_distinct_prs_and_each_reuses_its_own_pr(self):
        f = self.f
        b = self.review_and_merge(self.bootstrap())
        first = f.accepted((b['repository'], b['head_sha']), 'value = 42\n')
        second = f.accepted((first['workspace'], first['commit']), 'value = 73\n')
        urls, branches = [], []
        for current, waiting in ((first, second), (second, None)):
            with patch.object(f.scheduler, 'start_call') as start:
                tick(f.scheduler, self.day + 60)
                self.assertEqual([r['id'] for r in start.call_args.args[5]['runs']], [current['id']])
            b = f.ledger.get('deliveries', b['id'])
            self.assertEqual(b['feature_requirement_id'], current['requirement_id'])
            with patch('autopilot.staged_delivery.time.time', return_value=self.day):
                result = prepare(f.request(b, [current]))
            complete(f.scheduler, b, result, 'prepare')
            b = f.ledger.get('deliveries', b['id'])
            urls.append(b['feature_pr_url']); branches.append(b['feat_branch'])
            self.assertEqual(b['pending_ids'], [current['id']])
            requirement = f.ledger.get('requirements', current['requirement_id'])
            self.assertEqual(requirement['feature_pr_url'], b['feature_pr_url'])
            before = len(self.prs)
            with patch('autopilot.staged_delivery.time.time', return_value=self.day):
                replay = prepare(f.request(b, [current]))
            self.assertEqual(replay['feature_pr_url'], b['feature_pr_url'])
            self.assertEqual(len(self.prs), before)
            b = self.review_and_merge(b)
            self.assertEqual(f.ledger.get('runs', current['id'])['status'], 'delivered')
            if waiting:
                self.assertEqual(f.ledger.get('runs', waiting['id'])['status'], 'accepted')
                self.assertEqual(b['status'], 'preparing')
        self.assertEqual(len(set(urls)), 2)
        self.assertEqual(len(set(branches)), 2)
        self.assertEqual(len([p for p in self.prs.values() if p['base']['ref']=='master']), 1)

    def test_deferred_requirement_retargets_same_pr_without_carrying_other_requirements(self):
        from autopilot.staged_delivery import assign_requirement, defer_pending
        f = self.f
        b = self.review_and_merge(self.bootstrap())
        first = f.accepted((b['repository'], b['head_sha']), 'value = 42\n')
        second = f.accepted((first['workspace'], first['commit']), 'value = 73\n')
        with patch.object(f.scheduler, 'start_call'):
            tick(f.scheduler, self.day + 60)
        b = f.ledger.get('deliveries', b['id'])
        with patch('autopilot.staged_delivery.time.time', return_value=self.day):
            result = prepare(f.request(b, [first]))
        complete(f.scheduler, b, result, 'prepare')
        b = f.ledger.get('deliveries', b['id'])
        url, branch = b['feature_pr_url'], b['feat_branch']
        defer_pending(f.scheduler, b)
        first = f.ledger.get('runs', first['id'])
        self.assertIn('delivery_carry', first)
        self.assertNotIn('delivery_carry', f.ledger.get('runs', second['id']))
        tomorrow = create_batch(f.ledger, f.product, self.day + 86400)
        tomorrow = f.ledger.update('deliveries', tomorrow['id'], tomorrow['version'], {'run_ids':[first['id']]})
        tomorrow = assign_requirement(f.scheduler, tomorrow)
        self.assertEqual(tomorrow['feature_pr_url'], url)
        self.assertEqual(tomorrow['feat_branch'], branch)
        before = len(self.prs)
        with patch('autopilot.staged_delivery.time.time', return_value=self.day + 86400):
            result = prepare(f.request(tomorrow, [first]))
        self.assertEqual(result['status'], 'pass', result)
        self.assertEqual(result['feature_pr_url'], url)
        self.assertEqual(self.prs[result['feature_pr_number']]['base']['ref'], tomorrow['branch'])
        self.assertEqual(len(self.prs), before)

    def bootstrap(self):
        f = self.f
        b = create_batch(f.ledger, f.product, self.day, bootstrap=True)
        with patch('autopilot.staged_delivery.time.time', return_value=self.day):
            result = prepare(f.request(b))
        self.assertEqual(result['status'], 'pass', result)
        complete(f.scheduler, b, result, 'prepare')
        return f.ledger.get('deliveries', b['id'])

    def test_changed_pr_before_repair_resyncs_without_model_or_source_edits(self):
        f=self.f
        batch=self.bootstrap()
        old_proof={'head_sha':batch['feature_head_sha'],'base_sha':batch['feature_base_sha']}
        batch=f.ledger.update('deliveries',batch['id'],batch['version'],{'feature_review_pass':old_proof},'repairing_feature')
        self.assertEqual(batch['feature_review_pass'],old_proof)
        self.prs[batch['feature_pr_number']]['head']['sha']='f'*40
        with patch('autopilot.delivery_review.model') as model:
            result=execute('repair_feature',f.request(batch))
            self.assertTrue(result['stale'])
            model.assert_not_called()
        complete(f.scheduler,batch,result,'repair_feature')
        latest=f.ledger.get('deliveries',batch['id'])
        self.assertEqual(latest['status'],'syncing_feature')
        self.assertIsNone(latest.get('feature_review_pass'))

    def review_and_merge(self, b):
        f = self.f
        result = {'status': 'pass', 'head_sha': b['feature_head_sha'], 'base_sha': b['feature_base_sha']}
        # A real model result is persisted separately; this fixture exercises orchestration.
        complete(f.scheduler, b | {'round_id': None}, result, 'review_feature')
        b = f.ledger.get('deliveries', b['id'])
        with patch('autopilot.staged_delivery.time.time', return_value=self.day):
            result = execute('merge_feature', f.request(b))
        self.assertEqual(result['status'], 'pass', result)
        complete(f.scheduler, b, result, 'merge_feature')
        return f.ledger.get('deliveries', b['id'])

    def set_limit(self, limit=3):
        f = self.f
        product = f.ledger.get('products', f.product['id'])
        f.product = f.control.configure_product(product['id'], {'version': product['version'],
            'config': {'code_review': {'max_revisions': limit}}})

    def repair_rounds(self, b, count=3, at=None, phase='repair_release'):
        f = self.f
        return [f.ledger.create('code_reviews', {'product_id': f.product['id'], 'delivery_id': b['id'],
            'phase': phase, 'started_at': self.day if at is None else at}, 'pass') for _ in range(count)]

    def test_repair_limit_queues_and_recovers_at_beijing_midnight_after_restart(self):
        f = self.f
        self.set_limit()
        b = create_batch(f.ledger, f.product, self.day)
        self.repair_rounds(b)
        f.ledger.update('deliveries', b['id'], b['version'], {'revisions': 100}, 'release_failed')
        with patch.object(f.scheduler, 'start_call') as call:
            tick(f.scheduler, self.day)
            call.assert_not_called()
        waiting = f.ledger.get('deliveries', b['id'])
        self.assertEqual(waiting['status'], 'release_failed')
        midnight = dt.datetime.fromtimestamp(waiting['repair_deferred_until'], ZONE)
        self.assertEqual((midnight.hour, midnight.minute), (0, 0))
        f.scheduler = test_delivery.Scheduler(f.store)
        with patch.object(f.scheduler, 'start_call') as call:
            tick(f.scheduler, midnight.timestamp() - 1)
            call.assert_not_called()
            tick(f.scheduler, midnight.timestamp())
            self.assertEqual(call.call_args.args[3], 'repair_release')
        self.assertEqual(f.ledger.get('deliveries', b['id'])['revisions'], 101)

    def test_feature_quota_defers_pending_work_but_release_keeps_its_own_requirements(self):
        f = self.f
        self.set_limit()
        b = self.review_and_merge(self.bootstrap())
        run = f.accepted((b['repository'], b['head_sha']), 'value = 73\n')
        with patch.object(f.scheduler, 'start_call'):
            tick(f.scheduler, self.day + 1)
        b = f.ledger.get('deliveries', b['id'])
        self.repair_rounds(b, phase='repair_feature')
        f.ledger.update('deliveries', b['id'], b['version'], {'revisions': 3}, 'review_failed')
        with patch.object(f.scheduler, 'start_call') as call:
            tick(f.scheduler, self.day + 2)
            call.assert_not_called()
            tick(f.scheduler, b['cutoff'])
            call.assert_not_called()
            tick(f.scheduler, b['cutoff'] + 1)
            self.assertEqual(call.call_args.args[3], 'sync_release')
            self.assertEqual(call.call_args.args[5]['requirements'], [])
            self.assertEqual(call.call_args.args[5]['runs'], [])
        self.assertIsNone(f.ledger.get('runs', run['id'])['delivery_id'])
        self.assertEqual(f.ledger.get('runs', run['id'])['status'], 'accepted')

    def test_quota_wait_does_not_hold_later_ready_release(self):
        f = self.f
        self.set_limit()
        early = create_batch(f.ledger, f.product, self.day - 86400)
        self.repair_rounds(early)
        f.ledger.update('deliveries', early['id'], early['version'], {'revisions': 3}, 'release_failed')
        later = create_batch(f.ledger, f.product, self.day)
        f.ledger.update('deliveries', later['id'], later['version'], {'frozen': True}, 'merging')
        with patch.object(f.scheduler, 'start_call') as call:
            tick(f.scheduler, later['cutoff'] + 1)
            self.assertEqual(call.call_args.args[3], 'merge_release')
            self.assertEqual(call.call_args.args[1]['id'], later['id'])
        self.assertEqual(f.ledger.get('deliveries', early['id'])['status'], 'release_failed')

    def test_legacy_limit_block_recovers_but_unrelated_blocks_stay_blocked(self):
        f = self.f
        self.set_limit()
        b = create_batch(f.ledger, f.product, self.day - 86400)
        self.repair_rounds(b, at=self.day - 86400)
        b = f.ledger.update('deliveries', b['id'], b['version'], {'revisions': 3, 'resume_status': 'release_failed',
            'reason': '自动修复轮数已用尽；可调整上限后重试'}, 'blocked')
        with patch.object(f.scheduler, 'start_call') as call:
            tick(f.scheduler, self.day)
            self.assertEqual(call.call_args.args[3], 'repair_release')
        b = f.ledger.get('deliveries', b['id'])
        f.ledger.update('deliveries', b['id'], b['version'], {'reason': '人工暂停核对'}, 'blocked')
        with patch.object(f.scheduler, 'start_call') as call:
            tick(f.scheduler, self.day)
            call.assert_not_called()

    def test_increased_limit_and_excluded_failed_launch_allow_repairs(self):
        f = self.f
        self.set_limit()
        b = create_batch(f.ledger, f.product, self.day)
        rounds = self.repair_rounds(b)
        b = f.ledger.update('deliveries', b['id'], b['version'], {'revisions': 3}, 'release_failed')
        with patch.object(f.scheduler, 'start_call') as call:
            tick(f.scheduler, self.day)
            call.assert_not_called()
        self.set_limit(4)
        with patch.object(f.scheduler, 'start_call') as call:
            tick(f.scheduler, self.day)
            self.assertEqual(call.call_args.args[3], 'repair_release')
        self.set_limit()
        b = f.ledger.get('deliveries', b['id'])
        f.ledger.update('deliveries', b['id'], b['version'],
            {'excluded_repair_rounds': [rounds[0]['id'], b['round_id']]}, 'release_failed')
        with patch.object(f.scheduler, 'start_call') as call:
            tick(f.scheduler, self.day)
            self.assertEqual(call.call_args.args[3], 'repair_release')

    def test_at_limit_completed_release_still_validates_and_merges_to_master(self):
        f = self.f
        self.set_limit()
        b = self.bootstrap()
        # The baseline and an accepted fix pass feature review and enter release together.
        run = f.accepted((b['repository'], b['feature_head_sha']), 'value = 42\n')
        b = f.ledger.update('deliveries', b['id'], b['version'], {'run_ids': [run['id']]}, 'preparing')
        with patch('autopilot.staged_delivery.time.time', return_value=self.day):
            result = prepare(f.request(b, [run]))
        complete(f.scheduler, b, result, 'prepare')
        b = self.review_and_merge(f.ledger.get('deliveries', b['id']))
        self.repair_rounds(b)
        b = f.ledger.update('deliveries', b['id'], b['version'], {'revisions': 3})
        with patch.object(f.scheduler, 'start_call') as call:
            tick(f.scheduler, b['cutoff'])
            self.assertEqual(call.call_args.args[3], 'sync_release')
        b = f.ledger.get('deliveries', b['id'])
        complete(f.scheduler, b, {'status': 'pass', 'head_sha': b['head_sha'], 'base_sha': b['base_sha']}, 'sync_release')
        with patch.object(f.scheduler, 'start_call') as call:
            tick(f.scheduler, b['cutoff'] + 1)
            self.assertEqual(call.call_args.args[3], 'validate_release')
            self.assertEqual([r['id'] for r in call.call_args.args[5]['runs']], [run['id']])
        b = f.ledger.get('deliveries', b['id'])
        with patch('autopilot.delivery_review.verify', return_value={'status': 'pass'}), patch('autopilot.staged_delivery.time.time', return_value=b['cutoff'] + 2):
            result = execute('validate_release', f.request(b))
        complete(f.scheduler, b, result, 'validate_release')
        with patch.object(f.scheduler, 'start_call') as call:
            tick(f.scheduler, b['cutoff'] + 3)
            self.assertEqual(call.call_args.args[3], 'merge_release')
        b = f.ledger.get('deliveries', b['id'])
        with patch('autopilot.delivery_worker.time.time', return_value=b['cutoff'] + 4):
            result = execute('merge_release', f.request(b))
        self.assertEqual(result['status'], 'pass', result)
        complete(f.scheduler, b, result, 'merge_release')
        self.assertEqual(f.ledger.get('runs', run['id'])['status'], 'online')
        self.assertEqual(git(f.remote, 'show', 'master:app.py'), 'value = 42')

    def test_release_stays_on_master_until_feature_review_and_merge(self):
        f = self.f
        b = self.bootstrap()
        self.assertEqual(git(f.remote, 'rev-parse', b['branch']), f.initial)
        self.assertNotEqual(b['feature_head_sha'], f.initial)
        self.assertEqual(self.prs[1]['base']['ref'], b['branch'])
        self.assertNotIn('pr_url', b)
        target = feature_request(f.request(b))['record']
        self.assertEqual(target['pr_url'], b['feature_pr_url'])
        self.assertEqual(target['branch'], b['feat_branch'])
        b = self.review_and_merge(b)
        self.assertEqual(b['status'], 'collecting')
        self.assertEqual(len(self.prs), 2)
        self.assertEqual(self.prs[2]['base']['ref'], 'master')
        self.assertEqual(git(f.remote, 'rev-parse', 'master'), f.initial)
        with patch.object(f.scheduler, 'start_call') as start:
            tick(f.scheduler, self.day + 60)
            start.assert_not_called()

    def test_multiple_features_reuse_daily_release_and_outbound_pr(self):
        f = self.f
        b = self.review_and_merge(self.bootstrap())
        run = f.accepted((b['repository'], b['head_sha']), 'value = 42\n')
        with patch.object(f.scheduler, 'start_call'):
            tick(f.scheduler, self.day + 60)
        b = f.ledger.get('deliveries', b['id'])
        with patch('autopilot.staged_delivery.time.time', return_value=self.day):
            result = prepare(f.request(b, [f.ledger.get('runs', run['id'])]))
        complete(f.scheduler, b, result, 'prepare')
        b = self.review_and_merge(f.ledger.get('deliveries', b['id']))
        self.assertEqual(len(f.ledger.list('deliveries')), 1)
        self.assertEqual(len([p for p in self.prs.values() if p['base']['ref'] == 'master']), 1)
        self.assertEqual(len(b['feature_prs']), 2)
        self.assertEqual(f.ledger.get('runs', run['id'])['status'], 'delivered')
        self.assertIsNone(b['validation_pass'])

    def test_cutoff_is_2330_and_no_new_review_or_immediate_validation(self):
        f = self.f
        b = self.bootstrap()
        cutoff = dt.datetime.fromtimestamp(b['cutoff'], ZONE)
        self.assertEqual((cutoff.hour, cutoff.minute), (23, 30))
        with patch.object(f.scheduler, 'start_call') as start:
            tick(f.scheduler, b['cutoff'])
            start.assert_not_called()
        self.assertEqual(f.ledger.get('deliveries', b['id'])['status'], 'cancelled')
        self.assertEqual(git(f.remote, 'rev-parse', b['branch']), f.initial)

    def test_frozen_release_validation_requires_review_evidence_and_head_binding(self):
        f = self.f
        b = self.review_and_merge(self.bootstrap())
        with patch.object(f.scheduler, 'start_call') as start:
            tick(f.scheduler, b['cutoff'])
            self.assertEqual(start.call_args.args[3], 'sync_release')
        b = f.ledger.get('deliveries', b['id'])
        complete(f.scheduler, b, {'status': 'pass', 'head_sha': b['head_sha'], 'base_sha': b['base_sha']}, 'sync_release')
        with patch.object(f.scheduler, 'start_call') as start:
            tick(f.scheduler, b['cutoff'] + 1)
            self.assertEqual(start.call_args.args[3], 'validate_release')
        b = f.ledger.get('deliveries', b['id'])
        with patch('autopilot.delivery_review.verify', return_value={'status': 'pass', 'checks': [{'status': 'pass', 'required': True}]}), patch('autopilot.staged_delivery.time.time', return_value=b['cutoff'] + 2):
            result = execute('validate_release', f.request(b))
        self.assertEqual(result['status'], 'pass', result)
        complete(f.scheduler, b, result, 'validate_release')
        self.assertEqual(f.ledger.get('deliveries', b['id'])['status'], 'merging')
        # Advance master after validation: old evidence cannot authorize a merge.
        self.prs[2]['base']['sha'] = 'a' * 40
        result = execute('merge_release', f.request(f.ledger.get('deliveries', b['id'])))
        self.assertTrue(result['stale'])

    def test_validate_release_hands_self_contained_diff_to_independent_verification(self):
        """release 统一验证不再 mock 验证层：差异事实随请求下发并回填到检查证据。"""
        f = self.f
        b = self.review_and_merge(self.bootstrap())
        f.product = f.product | {'project_config': {'version': 1, 'commands': {},
            'acceptance_checks': {'generic-projects': [sys.executable, '-c', 'pass']}}}
        adapter = f.root / 'verify-adapter.py'
        adapter.write_text(
            'import json,sys\nfrom pathlib import Path\n'
            'payload=json.load(sys.stdin)\n'
            'material=payload["verification"]\n'
            'requirement=payload["requirement"]\n'
            'assert material["diff_file"] and material["facts_file"]\n'
            'assert material["changed_files"]\n'
            'assert material["diff_sha256"]\n'
            'assert material["baseline_acceptance"] is True\n'
            'assert requirement["acceptance"]\n'
            'assert requirement["resolution_probes"][0]["path"]=="check:generic-projects"\n'
            'assert "+value" in Path(material["diff_file"]).read_text()\n'
            'print(json.dumps({"status":"pass","checks":['
            '{"name":"独立业务验证","status":"pass","required":True,"evidence":{"provider":"codex"}}]}))\n')
        adapter.chmod(0o700)
        f.product['adapter'] = [sys.executable, str(adapter)]
        with patch.object(f.scheduler, 'start_call') as start:
            tick(f.scheduler, b['cutoff'])
            self.assertEqual(start.call_args.args[3], 'sync_release')
        b = f.ledger.get('deliveries', b['id'])
        complete(f.scheduler, b, {'status': 'pass', 'head_sha': b['head_sha'], 'base_sha': b['base_sha']}, 'sync_release')
        with patch.object(f.scheduler, 'start_call') as start:
            tick(f.scheduler, b['cutoff'] + 1)
            self.assertEqual(start.call_args.args[3], 'validate_release')
        b = f.ledger.get('deliveries', b['id'])
        with patch('autopilot.staged_delivery.time.time', return_value=b['cutoff'] + 2):
            result = execute('validate_release', f.request(b))
        self.assertEqual(result['status'], 'pass', result)
        check = next(c for c in result['checks'] if c['name'] == '独立业务验证')
        self.assertEqual(check['evidence']['merge_base'], b['base_sha'])
        self.assertEqual(check['evidence']['source'], 'repository')
        self.assertTrue(check['evidence']['diff_sha256'])
        self.assertTrue(check['evidence']['changed_files'])
        from autopilot.delivery_worker import paths as delivery_paths
        evidence = delivery_paths(f.request(b))[1] / 'validations' / b['validation_id']
        facts = json.loads((evidence / 'verification-facts.json').read_text())
        self.assertEqual(facts['merge_base'], b['base_sha'])
        written = json.loads((evidence / 'verification.json').read_text())
        self.assertEqual(written['verification']['merge_base'], b['base_sha'])

    def test_failed_review_never_calls_github_merge(self):
        f = self.f
        b = self.bootstrap()
        complete(f.scheduler, b, {'status': 'fail', 'issues': [{'severity': 'high'}], 'reason': '需要修复'}, 'review_feature')
        b = f.ledger.get('deliveries', b['id'])
        self.assertEqual(b['status'], 'review_failed')
        self.assertEqual(git(f.remote, 'rev-parse', b['branch']), f.initial)
        f.github.merge.assert_not_called()

    def test_failure_evidence_survives_repair_block_retry_and_receipt_replay(self):
        f = self.f
        b = self.bootstrap()
        with patch.object(f.scheduler, 'start_call'):
            tick(f.scheduler, self.day)
        b = f.ledger.get('deliveries', b['id'])
        review_id = b['round_id']
        failure = {'status': 'fail', 'reason': '构建脚本缺少 AppKit 链接',
            'issues': [{'path': 'build.rs', 'start_line': 17, 'severity': 'high', 'content': '缺少 AppKit'}]}
        complete(f.scheduler, b, failure, 'review_feature')
        original = f.ledger.get('code_reviews', review_id)
        b = f.ledger.get('deliveries', b['id'])
        self.assertEqual(b['reason'], failure['reason'])
        with patch.object(f.scheduler, 'start_call') as start:
            tick(f.scheduler, self.day + 1)
            self.assertEqual(start.call_args.args[3], 'repair_feature')
            self.assertEqual(start.call_args.args[1]['feedback'], failure)
        b = f.ledger.get('deliveries', b['id'])
        repair_id = b['round_id']
        blocked = {'status': 'blocked', 'failure_kind': 'agent_execution', 'reason': '修复进程启动失败', 'detail': 'EPERM', 'evidence': '/round/plan'}
        complete(f.scheduler, b, blocked, 'repair_feature')
        repair = f.ledger.get('code_reviews', repair_id)
        b = f.ledger.get('deliveries', b['id'])
        self.assertEqual(b['resume_status'], 'repairing_feature')
        self.assertEqual(b['revisions'], 0)
        self.assertIn(repair_id, b['excluded_repair_rounds'])
        self.assertEqual(b['feedback'], failure)
        # An old/replayed completion must not overwrite an immutable round.
        complete(f.scheduler, b, blocked | {'detail': 'different'}, 'repair_feature')
        self.assertEqual(f.ledger.get('code_reviews', repair_id), repair)
        b = f.ledger.get('deliveries', b['id'])
        f.control.mutate('/deliveries/' + b['id'] + '/retry', {'version': b['version']})
        with patch.object(f.scheduler, 'start_call') as start:
            tick(f.scheduler, self.day + 2)
            self.assertEqual(start.call_args.args[3], 'repair_feature')
        b = f.ledger.get('deliveries', b['id'])
        self.assertNotEqual(b['round_id'], repair_id)
        complete(f.scheduler, b, {'status': 'pass', 'head_sha': b['feature_head_sha']}, 'repair_feature')
        self.assertEqual(f.ledger.get('deliveries', b['id'])['status'], 'syncing_feature')
        self.assertEqual(f.ledger.get('code_reviews', review_id), original)
        self.assertEqual(f.ledger.get('code_reviews', repair_id), repair)

    def test_late_review_cannot_enter_frozen_release_and_carries_to_next_day(self):
        f = self.f
        b = self.review_and_merge(self.bootstrap())
        run = f.accepted((b['repository'], b['head_sha']), 'value = 73\n')
        with patch.object(f.scheduler, 'start_call'):
            tick(f.scheduler, self.day + 60)
        b = f.ledger.get('deliveries', b['id'])
        with patch('autopilot.staged_delivery.time.time', return_value=self.day):
            result = prepare(f.request(b, [f.ledger.get('runs', run['id'])]))
        complete(f.scheduler, b, result, 'prepare')
        b = f.ledger.get('deliveries', b['id'])
        release_before = git(f.remote, 'rev-parse', b['branch'])
        with patch.object(f.scheduler, 'start_call') as start:
            tick(f.scheduler, b['cutoff'])
            start.assert_not_called()
        deferred = f.ledger.get('runs', run['id'])
        self.assertEqual(deferred['status'], 'accepted')
        self.assertIsNone(deferred['delivery_id'])
        self.assertEqual(deferred['delivery_carry']['head_sha'], b['feature_head_sha'])
        self.assertEqual(git(f.remote, 'rev-parse', b['branch']), release_before)
        with patch.object(f.scheduler, 'start_call'):
            tick(f.scheduler, self.day + 86400)
        assigned = f.ledger.get('runs', run['id'])
        self.assertNotEqual(assigned['delivery_id'], b['id'])

    def test_merge_receipt_replay_does_not_duplicate_review_history(self):
        f = self.f
        b = self.bootstrap()
        complete(f.scheduler, b | {'round_id': None}, {'status': 'pass', 'head_sha': b['feature_head_sha'], 'base_sha': b['feature_base_sha']}, 'review_feature')
        b = f.ledger.get('deliveries', b['id'])
        with patch('autopilot.staged_delivery.time.time', return_value=self.day):
            result = execute('merge_feature', f.request(b))
        complete(f.scheduler, b, result, 'merge_feature')
        b = f.ledger.get('deliveries', b['id'])
        complete(f.scheduler, b, result, 'merge_feature')
        self.assertEqual(len(f.ledger.get('deliveries', b['id'])['feature_prs']), 1)

    def test_release_changed_after_cutoff_waits_until_next_review_window(self):
        f = self.f
        b = self.review_and_merge(self.bootstrap())
        b = f.ledger.update('deliveries', b['id'], b['version'], {'frozen': True}, 'reviewing_release')
        with patch.object(f.scheduler, 'start_call') as start:
            tick(f.scheduler, b['cutoff'] + 1)
            start.assert_not_called()
        with patch.object(f.scheduler, 'start_call') as start:
            tick(f.scheduler, self.day + 86400)
            self.assertEqual(start.call_args.args[3], 'review_release')

    def test_legacy_migration_preserves_old_branch_and_recreates_release_from_master(self):
        from autopilot.delivery_migration import migrate
        f = self.f
        f.product = f.ledger.update('products', f.product['id'], f.product['version'], {'delivery_flow': 'legacy'})
        old_batch, old_result = f.bootstrap()
        f.product = f.ledger.get('products', f.product['id'])
        f.product = f.ledger.update('products', f.product['id'], f.product['version'], {}, 'paused')
        old_head = old_result['head_sha']
        old_pr = self.prs[1]
        def api(route, method='GET', body=None):
            if route.endswith('/rename'):
                git(f.remote, 'update-ref', 'refs/heads/' + body['new_name'], old_head)
                git(f.remote, 'update-ref', '-d', 'refs/heads/' + old_batch['branch'])
                old_pr['state'] = 'closed'
                return {}
            return old_pr
        f.github.api.side_effect = api
        net = lambda root, *args: git(root, '-c', 'url.' + str(f.remote) + '.insteadOf=https://github.com/example/repo.git', *args)
        with patch('autopilot.delivery_migration.GitHub', return_value=f.github), patch('autopilot.delivery_migration.network_git', side_effect=net):
            b = migrate(f.ledger, old_batch)
        self.assertEqual(git(f.remote, 'rev-parse', 'codex/legacy-' + b['branch']), old_head)
        self.assertEqual(git(old_result['workspace'], 'rev-parse', 'HEAD'), old_head)
        f.product = f.ledger.get('products', f.product['id'])
        with patch('autopilot.staged_delivery.time.time', return_value=self.day):
            result = prepare(f.request(b))
        self.assertEqual(result['status'], 'pass', result)
        self.assertEqual(git(f.remote, 'rev-parse', b['branch']), f.initial)
        self.assertEqual(result['feature_head_sha'], old_head)


if __name__ == '__main__':
    unittest.main()
