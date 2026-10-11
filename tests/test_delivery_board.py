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

URL = 'https://github.com/example/repo/pull/1'


def row(ident, phase, status, at, url=URL, **extra):
    return dict(id=ident, delivery_id='batch', phase=phase, status=status, pr_url=url,
                created=at, started_at=at, updated=at + 1, **extra)


class IssueTests(unittest.TestCase):
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
