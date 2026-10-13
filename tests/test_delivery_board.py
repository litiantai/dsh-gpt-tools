"""PR 去重、远端状态与逐问题修复生命周期。"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'dsh-gpt-supervisor/scripts')]
from review_core import Store
from autopilot.api import Control
from autopilot.delivery_board import pending_issues, review_status, separate_prs, dispatch_state
from error_messages import present_errors

URL = 'https://github.com/example/repo/pull/1'


def row(ident, phase, status, at, url=URL, **extra):
    return dict(id=ident, delivery_id='batch', phase=phase, status=status, pr_url=url,
                created=at, started_at=at, updated=at + 1, **extra)


class IssueTests(unittest.TestCase):
    def validation_batch(self, checks, **result):
        return {'id': 'batch', 'product_id': 'project', 'status': 'online', 'pr_url': URL, 'updated': 20,
                'receipts': [{'call_id': 'failed', 'action': 'validate_release', 'at': 2,
                              'result': {'status': 'fail', 'head_sha': 'old', 'checks': checks, **result}}]}

    def test_matching_check_pass_closes_only_that_check_even_if_other_check_fails(self):
        first = {'name': 'test-1', 'command': ['npm', 'test'], 'status': 'fail', 'reason': '测试失败'}
        browser = {'name': 'browser-0', 'command': ['npm', 'run', 'test:e2e'], 'status': 'fail',
                   'reason': 'locator.click: Test timeout of 30000ms exceeded.'}
        batch = self.validation_batch([first, browser])
        batch['receipts'].append({'call_id': 'retry', 'action': 'validate_release', 'at': 4,
            'result': {'status': 'fail', 'head_sha': 'new', 'checks': [first | {'status': 'pass'}, browser]}})
        issues = pending_issues([batch], [])
        self.assertEqual([i['title'] for i in issues], ['browser-0：浏览器测试等待页面元素超时'])
        self.assertEqual(issues[0]['issue_kind'], 'check_failure')
        batch['receipts'].append({'call_id': 'passed', 'action': 'validate_release', 'at': 6,
            'result': {'status': 'pass', 'head_sha': 'latest', 'checks': [browser | {'status': 'pass'}]}})
        self.assertEqual(pending_issues([batch], []), [])
        self.assertEqual(batch['receipts'][0]['result']['status'], 'fail')

    def test_pass_does_not_close_changed_command_phase_pr_or_wrong_commit(self):
        check = {'name': 'test-1', 'command': ['npm', 'test'], 'status': 'fail', 'reason': '测试失败'}
        for change in ({'command': ['npm', 'run', 'test:unit']}, {'name': 'test-2'}, {'commit': 'wrong'}):
            batch = self.validation_batch([check])
            batch['receipts'].append({'call_id': 'passed', 'action': 'validate_release', 'at': 4,
                'result': {'status': 'pass', 'head_sha': 'new', 'checks': [check | {'status': 'pass'} | change]}})
            self.assertEqual(len(pending_issues([batch], [])), 1)
        for action in ('validate_feature', 'review_feature'):
            batch = self.validation_batch([check])
            # 另一阶段/PR 的验证不能关闭 release 问题。
            batch['feature_pr_url'] = URL+'2'
            batch['receipts'].append({'call_id': 'passed', 'action': action, 'at': 4,
                'result': {'status': 'pass', 'head_sha': 'new', 'checks': [check | {'status': 'pass'}]}})
            self.assertEqual(len(pending_issues([batch], [])), 1)

    def test_validation_started_before_finding_cannot_close_it(self):
        check = {'name': 'test-1', 'command': ['npm', 'test'], 'status': 'fail', 'reason': '测试失败'}
        batch = self.validation_batch([check])
        batch['receipts'].append({'call_id': 'earlier', 'action': 'validate_release', 'at': 5,
            'result': {'status': 'pass', 'head_sha': 'new', 'checks': [check | {'status': 'pass', 'started': 1}]}})
        self.assertEqual(len(pending_issues([batch], [])), 1)

    def test_earlier_pass_cannot_close_repeated_later_failure_or_review_replace_test(self):
        check = {'name': 'test-1', 'command': ['npm', 'test'], 'status': 'fail', 'reason': '测试失败'}
        batch = self.validation_batch([check])
        batch['receipts'].append({'call_id': 'repeat', 'action': 'validate_release', 'at': 4,
            'result': {'status': 'fail', 'head_sha': 'new', 'checks': [check]}})
        batch['receipts'].append({'call_id': 'earlier', 'action': 'validate_release', 'at': 6,
            'result': {'status': 'pass', 'head_sha': 'new', 'checks': [check | {'status': 'pass', 'started': 3}]}})
        issues = pending_issues([batch], [row('review', 'review_release', 'pass', 7)])
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['detected_at'], 4)

    def test_legacy_receipt_without_call_id_and_null_start_is_supported(self):
        check = {'name': 'test-1', 'command': ['npm', 'test'], 'status': 'fail', 'reason': '测试失败', 'started': None}
        batch = self.validation_batch([check])
        del batch['receipts'][0]['call_id']
        self.assertEqual(len(pending_issues([batch], [])), 1)
        batch['receipts'].append({'action': 'validate_release', 'at': 4,
            'result': {'status': 'pass', 'head_sha': 'new', 'checks': [check | {'status': 'pass'}]}})
        self.assertEqual(pending_issues([batch], []), [])

    def test_traceback_check_has_specific_title_and_preserves_sanitized_evidence(self):
        check = present_errors({'name': 'test-1', 'command': ['npm', 'test'], 'status': 'fail',
            'reason': "Traceback\nKeyError: 'state_root'\napi_key=private-key", 'exit_code': 1})
        issue = pending_issues([self.validation_batch([check])], [])[0]
        self.assertEqual(issue['title'], "test-1：KeyError: 'state_root'")
        self.assertNotIn('private-key', json.dumps(issue))

    def test_localized_defect_links_to_original_requirement_and_completion(self):
        batch, requirement = self.finding_fixture()
        reason = 'RepositoryScans.tsx:29 缺少 identity.isError 判断'
        proof = batch['receipts'][0]['result']['checks'][0]['evidence']
        proof.update(reason='本次操作未能完成', diagnostic={'original': {'reason': reason}},
                     error_info={'code': 'UNKNOWN', 'message': '本次操作未能完成', 'suggestion': ''})
        requirement['verification_occurrences'][0]['verification'] = {'status': 'fail', 'reason': reason}
        issue = pending_issues([batch], [], requirements=[requirement])[0]
        self.assertEqual(issue['title'], reason)
        self.assertEqual(issue['requirement']['id'], 'finding')
        self.assertEqual(pending_issues([batch], [], requirements=[requirement | {'status': 'online'}]), [])

    def test_successful_checks_and_reviews_cannot_close_open_backlog_requirement(self):
        batch, requirement = self.finding_fixture()
        batch['receipts'].append({'call_id': 'pass', 'action': 'validate_release', 'at': 5,
            'result': {'status': 'pass', 'head_sha': 'new', 'checks': [{'name': '业务验收', 'status': 'pass'}]}})
        issues = pending_issues([batch], [row('review', 'review_release', 'pass', 6)], requirements=[requirement])
        self.assertEqual([i['requirement']['id'] for i in issues], ['finding'])

    def test_execution_failure_never_becomes_code_issue_even_with_fail_status(self):
        self.assertEqual(pending_issues([], [row('error', 'review', 'fail', 1,
            result={'failure_kind': 'agent_execution', 'reason': '模型离线'})]), [])
        for failure in ('agent_execution', 'acceptance_infrastructure'):
            batch = self.validation_batch([{'name': '业务验收', 'status': 'fail',
                'evidence': {'status': 'fail', 'failure_kind': failure, 'reason': '验收环境启动失败'}}])
            self.assertEqual(pending_issues([batch], []), [])
        batch = self.validation_batch([{'name': '扫描操作链路', 'status': 'fail',
            'failure_kind': 'ui_acceptance', 'reason': '应禁止扫描，但仍能提交'}])
        self.assertEqual(pending_issues([batch], [])[0]['issue_kind'], 'check_failure')

    def finding_fixture(self):
        proof = {'status': 'fail', 'reason': '能力确认前允许提交扫描', 'evidence': '/proof'}
        batch = {'id': 'batch', 'product_id': 'project', 'status': 'online', 'pr_url': URL, 'updated': 8,
                 'receipts': [{'call_id': 'verify', 'action': 'validate_release', 'at': 2,
                              'result': {'status': 'fail', 'reason': '交付必需检查未通过', 'head_sha': 'head',
                                         'checks': [{'name': '业务验收', 'status': 'fail', 'evidence': proof}]}}]}
        requirement = {'id': 'finding', 'product_id': 'project', 'source': 'verification_finding',
                       'title': '修复扫描能力预检', 'status': 'queued', 'updated': 9,
                       'verification_occurrences': [{'delivery_id': 'batch', 'commit': 'head', 'verification': proof}]}
        return batch, requirement

    def test_deferred_finding_follows_requirement_instead_of_merged_batch(self):
        batch, requirement = self.finding_fixture()
        for status in ('queued', 'developing', 'blocked', 'cancelled', 'rejected', 'accepted'):
            issue = pending_issues([batch], [], requirements=[requirement | {'status': status}])[0]
            self.assertEqual(issue['status'], 'backlog')
            self.assertEqual(issue['requirement']['status'], status)
            self.assertEqual(issue['title'], '能力确认前允许提交扫描')
            self.assertEqual(issue['updated'], 9)
            self.assertFalse(issue['retryable'])
        for status in ('online', 'completed'):
            self.assertEqual(pending_issues([batch], [], requirements=[requirement | {'status': status}]), [])
        self.assertEqual(batch['receipts'][0]['result']['status'], 'fail')

    def test_finding_requires_matching_project_batch_commit_and_reason(self):
        batch, requirement = self.finding_fixture()
        occurrence = requirement['verification_occurrences'][0]
        unrelated = [requirement | {'product_id': 'other'}, requirement | {'source': 'manual'}]
        unrelated += [requirement | {'verification_occurrences': [occurrence | change]} for change in (
            {'delivery_id': 'other'}, {'commit': 'other'}, {'verification': {'reason': '另一个缺陷'}})]
        for other in unrelated:
            issue = pending_issues([batch], [], requirements=[other | {'status': 'completed'}])[0]
            self.assertNotIn('requirement', issue)

    def test_resolved_finding_does_not_hide_other_failed_checks(self):
        batch, requirement = self.finding_fixture()
        batch['receipts'][0]['result']['checks'].append({'name': '构建', 'status': 'fail', 'reason': '编译失败'})
        result = pending_issues([batch], [], requirements=[requirement | {'status': 'online'}])
        self.assertEqual([i['title'] for i in result], ['编译失败'])

    def test_nonblocking_backlog_finding_remains_visible_after_validation_passes(self):
        batch, requirement = self.finding_fixture()
        result = batch['receipts'][0]['result']
        result['status'] = 'pass'
        result['checks'][0].update(required=False, disposition='backlog', requirement_id='finding')
        self.assertEqual(pending_issues([batch], [], requirements=[requirement])[0]['status'], 'backlog')

    def test_later_failure_cannot_be_closed_by_an_older_requirement_completion(self):
        batch, requirement = self.finding_fixture()
        batch['receipts'][0]['at'] = 10
        result = pending_issues([batch], [], requirements=[requirement | {'status': 'completed'}])
        self.assertEqual(len(result), 1)

    def test_current_execution_overlays_history_without_mutating_or_resolving_it(self):
        rounds = [row('review', 'review_feature', 'fail', 1, result={'reason': '输入校验缺失'}),
                  row('repair', 'repair_feature', 'blocked', 3, reason='旧提交已变化')]
        batch = {'id': 'batch', 'status': 'syncing_feature', 'feature_pr_url': URL, 'version': 7,
                 'updated': 10, 'reason': '等待同步新提交'}
        issue = pending_issues([batch], rounds)[0]
        self.assertEqual(issue['status'], 'waiting')
        self.assertEqual(issue['last_attempt']['reason'], '旧提交已变化')
        self.assertEqual(issue['reason'], '等待同步新提交')
        self.assertFalse(issue['retryable'])
        self.assertEqual(issue['delivery_version'], 7)
        running = batch | {'status': 'code_review', 'call': {'id': 'new', 'action': 'review_feature', 'started': 11, 'path': 'private'}}
        issue = pending_issues([running], rounds)[0]
        self.assertEqual(issue['status'], 'running')
        self.assertEqual(issue['reason'], '')
        self.assertNotIn('path', issue['call'])
        self.assertEqual(rounds[-1]['reason'], '旧提交已变化')
        self.assertEqual(len(pending_issues([running], rounds)), 1)

    def test_update_wait_retry_eligibility_and_other_pr_isolation(self):
        rounds = [row('review', 'review_feature', 'fail', 1, result={'reason': '缺少校验'})]
        batch = {'id': 'batch', 'status': 'syncing_feature', 'feature_pr_url': URL, 'updated': 9}
        gate = {'status': 'observing', 'reason': '平台健康观察中'}
        self.assertEqual(pending_issues([batch], rounds, gate)[0]['status'], 'waiting_update')
        blocked = batch | {'status': 'blocked', 'reason': '新的阻塞'}
        self.assertTrue(pending_issues([blocked], rounds)[0]['retryable'])
        self.assertFalse(pending_issues([blocked], rounds, gate)[0]['retryable'])
        self.assertFalse(pending_issues([blocked | {'uncertain': True}], rounds)[0]['retryable'])
        other = batch | {'feature_pr_url': URL+'2', 'call': {'action': 'repair_feature'}}
        issue = pending_issues([other], rounds)[0]
        self.assertEqual(issue['status'], 'pending')
        self.assertEqual(issue['call'], {})

    def test_dispatch_marker_reports_unknown_until_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            self.assertIsNone(dispatch_state(state))
            root = state/'autopilot'; root.mkdir()
            (root/'update-drain.json').write_text('{')
            self.assertEqual(dispatch_state(state)['status'], 'unknown')
            (root/'updates').mkdir()
            job = root/'updates/test.json'
            job.write_text(json.dumps({'status': 'observing', 'healthy_since': 10}))
            (root/'update-drain.json').write_text(json.dumps({'job': str(job)}))
            self.assertEqual(dispatch_state(state)['status'], 'observing')

    def test_review_workflow_tracks_current_commit_and_repair_completion(self):
        pr = {'pr_url':URL,'status':'open','head_sha':'head','base_sha':'base'}
        passed = row('r1','review_release','pass',1,result={'head_sha':'head','base_sha':'base'})
        self.assertEqual(review_status(pr, [], []), 'unreviewed')
        self.assertEqual(review_status(pr, [passed], []), 'approved')
        self.assertEqual(review_status(pr | {'head_sha':'new'}, [passed], []), 'unreviewed')
        self.assertEqual(review_status(pr | {'base_sha':'new'}, [passed], []), 'unreviewed')
        issues = [{'pr_url':URL}]
        self.assertEqual(review_status(pr, [passed], issues), 'fixing')
        self.assertEqual(review_status(pr | {'status':'merged'}, [passed], issues), 'merged')
        self.assertEqual(review_status(pr, [passed,row('fix','repair_release','pass',3)], []), 'unreviewed')

    def test_multiple_issues_dedup_and_failed_repair_retains_them(self):
        issues = [{'path':'a.ts','content':'输入校验缺失'}, {'path':'b.ts','content':'错误处理缺失'}]
        rounds = [row('r1','review_feature','fail',1,result={'issues':issues}),
                  row('r2','review_feature','fail',3,result={'issues':issues}),
                  row('fix','repair_feature','blocked',5,reason='模型离线')]
        result = pending_issues([], rounds)
        self.assertEqual(len(result), 2)
        self.assertTrue(all(i['status']=='blocked' for i in result))
        rounds.append(row('fix2','repair_feature','pass',7))
        self.assertEqual(pending_issues([],rounds), [])
        rounds.append(row('r3','review_feature','fail',9,result={'issues':[issues[0]]}))
        self.assertEqual(len(pending_issues([],rounds)), 1)

    def test_pr_isolation_and_success_does_not_close_later_findings(self):
        rounds = [row('r1','review','fail',1,result={'reason':'问题一'}),
                  row('r2','review','fail',2,url=URL+'0',result={'reason':'问题二'}),
                  row('fix','repair','pass',3,finished_at=10),
                  row('r3','review','fail',5,result={'reason':'后来发现的问题'})]
        result = pending_issues([],rounds)
        self.assertEqual({i['title'] for i in result}, {'问题二','后来发现的问题'})

    def test_running_repair_and_release_validation(self):
        batch = {'id':'batch','title':'每日交付','pr_url':URL,'receipts':[
            {'call_id':'verify','action':'validate_release','at':2,'result':{'status':'fail','reason':'测试失败'}}]}
        result = pending_issues([batch],[row('fix','repair_release','running',4)])
        self.assertEqual([(i['title'],i['status']) for i in result],[('测试失败','repairing')])
        self.assertEqual(pending_issues([batch],[row('fix','repair_release','pass',4)]),[])

    def test_agent_failure_is_not_a_code_issue(self):
        self.assertEqual(pending_issues([], [row('error','review','blocked',1,result={'failure_kind':'agent_execution'})]),[])


    def test_requirements_and_release_prs_are_separate_and_history_is_truthful(self):
        feature={'id':URL,'pr_url':URL,'updated':1}
        release={'id':URL+'1','pr_url':URL+'1','updated':2,'head_branch':'release-day','base_branch':'master'}
        unrelated={'id':URL+'2','pr_url':URL+'2','updated':3}
        batches=[{'id':'batch','status':'validating','pr_url':release['pr_url'],'branch':'release-day','base_branch':'master',
            'feature_prs':[{'pr_url':URL,'run_ids':['r1','r2']}], 'run_ids':['r1','r2','r3'],'integrated_ids':['r1','r2']}]
        runs=[{'id':'r1','requirement_id':'q1'},{'id':'r2','requirement_id':'q2'},{'id':'r3','requirement_id':'q3'}]
        requirements=[{'id':f'q{n}','title':f'需求 {n}'} for n in (1,2,3)]
        prs, releases=separate_prs([feature,release,unrelated],batches,runs,requirements)
        self.assertEqual({p['requirement_title'] for p in prs},{'需求 1','需求 2'})
        self.assertTrue(all(p['shared_pr'] and p['pr_url']==URL for p in prs))
        self.assertEqual([p['pr_url'] for p in releases],[release['pr_url']])
        self.assertNotIn('需求 3',[p['requirement_title'] for p in prs])


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.control = Control(Store(Path(self.tmp.name), {'home':self.tmp.name}))
        self.ledger = self.control.ledger
        self.product = self.ledger.create('products', {'git':{'url':'https://github.com/example/repo'}})

    def tearDown(self):
        self.tmp.cleanup()

    def pr(self, number, state='open', merged=None):
        return {'number':number,'html_url':URL.rsplit('/',1)[0]+'/'+str(number),'title':f'PR {number}',
            'state':state,'merged_at':merged,'updated_at':'2026-10-09T02:00:00Z','head':{'ref':'feat'},'base':{'ref':'master'}}

    def associate(self, number):
        requirement = self.ledger.create('requirements', {'product_id':self.product['id'],'title':f'需求 {number}'})
        self.ledger.create('runs', {'product_id':self.product['id'],'requirement_id':requirement['id'],
            'feature_pr_url':URL.rsplit('/',1)[0]+'/'+str(number)})

    @patch('autopilot.delivery_board.GitHub')
    def test_all_pages_merged_closed_and_dedup(self, gh):
        for number in (1,101,102): self.associate(number)
        self.ledger.create('code_reviews', {'product_id':self.product['id'],'delivery_id':'batch','pr_url':URL,'phase':'review'},'running')
        self.ledger.create('code_reviews', {'product_id':self.product['id'],'delivery_id':'batch','pr_url':URL,'phase':'review'},'blocked')
        gh.return_value.api.side_effect=[[self.pr(n) for n in range(1,101)], [self.pr(101,'closed','2026-10-09'),self.pr(102,'closed')]]
        result=self.control.get(f"/products/{self.product['id']}/delivery-board")
        self.assertEqual(len(result['prs']),3)
        states={p['number']:p['git_status'] for p in result['prs']}
        self.assertEqual(states[101],'merged'); self.assertEqual(states[102],'closed')
        self.control.delivery_board.get(self.product)
        self.assertEqual(gh.return_value.api.call_count,2)
        self.control.delivery_board.attempted.clear()
        gh.return_value.api.side_effect=OSError('offline')
        result=self.control.delivery_board.get(self.product)
        self.assertEqual(len(result['prs']),3)
        self.assertTrue(result['sync_error'])

    @patch('autopilot.delivery_board.GitHub')
    def test_unconfirmed_pr_is_unknown_and_products_are_isolated(self, gh):
        self.associate(1)
        self.ledger.create('code_reviews', {'product_id':self.product['id'],'delivery_id':'batch','pr_url':URL,'phase':'review'},'running')
        self.ledger.create('code_reviews', {'product_id':'other','delivery_id':'batch','pr_url':URL+'9','phase':'review'},'running')
        gh.return_value.api.side_effect=OSError('offline')
        result=self.control.delivery_board.get(self.product)
        self.assertEqual([(p['pr_url'],p['git_status']) for p in result['prs']],[(URL,'unknown')])
