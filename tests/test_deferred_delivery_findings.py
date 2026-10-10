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
from autopilot.delivery_board import pending_issues, review_status, separate_prs

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
