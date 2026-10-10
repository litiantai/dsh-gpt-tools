"""真实本机 Git 回归：同步方向、脏工作区保护、冲突方案和失败留证。"""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'dsh-gpt-supervisor/scripts')]
from review_core import Store, Conflict
from autopilot.store import Ledger
from autopilot.call import atomic
from autopilot.workspace import git
from autopilot import branch_sync as sync
from dashboard_server import Dashboard


class BranchSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.state = self.root/'state'; self.ledger = Ledger(Store(self.state))
        self.origin = self.root/'origin.git'; self.origin.mkdir(); git(self.origin,'init','--bare','--initial-branch=master')
        self.repo = self.root/'repo'; git(self.root,'clone',str(self.origin),str(self.repo))
        git(self.repo,'config','user.name','Test'); git(self.repo,'config','user.email','test@localhost')
        (self.repo/'shared.txt').write_text('base\n'); git(self.repo,'add','.'); git(self.repo,'commit','-m','base')
        git(self.repo,'push','origin','master')
        for branch in ('feat-one','release-one'):
            git(self.repo,'checkout','-b',branch)
            (self.repo/(branch+'.txt')).write_text(branch); git(self.repo,'add','.'); git(self.repo,'commit','-m',branch)
            git(self.repo,'push','origin',branch); git(self.repo,'checkout','master')
        (self.repo/'master.txt').write_text('latest'); git(self.repo,'add','.'); git(self.repo,'commit','-m','master update'); git(self.repo,'push','origin','master')
        self.master = git(self.repo,'rev-parse','master')
        self.product = self.ledger.create('products',{'name':'fixture','delivery_repository':str(self.repo),
            'git':{'enabled':True,'base_branch':'master'},'code_review':{'fixer':{'provider':'codex','model':'fixture'}}},'active')
        self.network = patch.object(sync,'network_git',side_effect=git); self.network.start(); self.addCleanup(self.network.stop)

    def tearDown(self): self.temp.cleanup()

    def run_job(self):
        ident = str(uuid.uuid4()); root = sync.directory(self.state,self.product['id'])/ident; root.mkdir(parents=True)
        atomic(root/'job.json',{'id':ident,'status':'queued','started':time.time(),'branches':[]})
        atomic(root.parent/'latest.json',{'id':ident})
        result = sync.execute({'product':self.product,'id':ident,'state_root':str(self.state/'autopilot')})
        return result, sync.snapshot(self.ledger,self.product)

    def conflict(self):
        git(self.repo,'checkout','feat-one'); (self.repo/'shared.txt').write_text('feature\n')
        git(self.repo,'commit','-am','feature conflicting change'); git(self.repo,'push','origin','feat-one')
        git(self.repo,'checkout','master'); (self.repo/'shared.txt').write_text('master\n')
        git(self.repo,'commit','-am','master conflicting change'); git(self.repo,'push','origin','master')
        self.master = git(self.repo,'rev-parse','master')

    def test_merges_master_into_feature_and_release_only(self):
        result, job = self.run_job(); self.assertEqual(result['status'],'pass')
        for branch in ('feat-one','release-one'):
            git(self.repo,'merge-base','--is-ancestor',self.master,branch)
            self.assertEqual(git(self.origin,'rev-parse',branch),git(self.repo,'rev-parse',branch))
            self.assertEqual(git(self.repo,'show',branch+':'+branch+'.txt'),branch)
        self.assertEqual(git(self.origin,'rev-parse','master'),self.master)
        self.assertEqual(git(self.repo,'rev-parse','master'),self.master)
        self.assertEqual(job['status'],'completed')

    def test_clean_checked_out_branch_updates_worktree_without_switching_branch(self):
        git(self.repo,'checkout','feat-one')
        result,job=self.run_job(); self.assertEqual(result['status'],'pass',job)
        self.assertEqual(git(self.repo,'symbolic-ref','--short','HEAD'),'feat-one')
        self.assertEqual((self.repo/'master.txt').read_text(),'latest')
        self.assertEqual(git(self.repo,'status','--porcelain'),'')

    def test_local_only_branch_is_not_published_and_remote_only_branch_is_synced(self):
        git(self.repo,'branch','feat-local','feat-one')
        git(self.repo,'branch','-D','release-one')
        result,job=self.run_job(); self.assertEqual(result['status'],'pass',job)
        self.assertEqual(git(self.origin,'for-each-ref','refs/heads/feat-local'),'')
        git(self.repo,'merge-base','--is-ancestor',self.master,'feat-local')
        git(self.repo,'merge-base','--is-ancestor',self.master,'release-one')

    def test_dirty_worktree_is_preserved_and_other_branch_still_syncs(self):
        git(self.repo,'checkout','feat-one'); before = git(self.repo,'rev-parse','HEAD')
        (self.repo/'shared.txt').write_text('uncommitted\n')
        _,job = self.run_job()
        self.assertEqual(job['branches'][0]['status'],'blocked')
        self.assertIn('未提交',job['branches'][0]['reason'])
        self.assertEqual(git(self.repo,'rev-parse','HEAD'),before)
        self.assertEqual((self.repo/'shared.txt').read_text(),'uncommitted\n')
        git(self.repo,'merge-base','--is-ancestor',self.master,'release-one')

    def test_running_task_is_skipped(self):
        self.ledger.create('runs',{'product_id':self.product['id'],'branch':'feat-one','call':{'id':'active'}},'developing')
        before=git(self.repo,'rev-parse','feat-one'); _,job=self.run_job()
        self.assertIn('任务执行',job['branches'][0]['reason']); self.assertEqual(git(self.repo,'rev-parse','feat-one'),before)

    def test_conflict_plan_is_logged_before_edit_and_original_branch_updated_after(self):
        self.conflict(); before=git(self.repo,'rev-parse','feat-one')
        def model(request,folder,prompt,**kwargs):
            if kwargs.get('write'):
                events=sync.snapshot(self.ledger,self.product)['events']
                self.assertTrue(any(e['kind']=='plan' and e['detail']['summary']=='保留双方内容' for e in events))
                self.assertEqual(git(self.repo,'rev-parse','feat-one'),before)
                (kwargs['workspace']/'shared.txt').write_text('master\nfeature\n')
            return {'status':'pass','summary':'保留双方内容'}
        with patch('autopilot.delivery_review.model',side_effect=model): result,job=self.run_job()
        self.assertEqual(result['status'],'pass',job)
        self.assertEqual(git(self.repo,'show','feat-one:shared.txt'),'master\nfeature')
        self.assertEqual(git(self.origin,'rev-parse','master'),self.master)

    def test_failed_plan_preserves_conflict_workspace_and_branch_head(self):
        self.conflict(); before=git(self.repo,'rev-parse','feat-one')
        with patch('autopilot.delivery_review.model',return_value={'status':'blocked','reason':'需人工核对'}) as model:
            _,job=self.run_job()
        self.assertEqual(model.call_count,1); row=job['branches'][0]
        self.assertEqual(row['status'],'blocked'); self.assertTrue(Path(row['workspace']).exists())
        self.assertTrue(git(row['workspace'],'diff','--name-only','--diff-filter=U'))
        self.assertEqual(git(self.repo,'rev-parse','feat-one'),before)

    def test_concurrent_remote_update_is_never_force_pushed(self):
        old=git(self.origin,'rev-parse','feat-one')
        def remote(repo,*args):
            if args[0]=='push' and args[-1].endswith(':refs/heads/feat-one'):
                # A valid concurrent remote commit, outside the fetched ancestry.
                tree=git(self.origin,'rev-parse',old+'^{tree}')
                extra=git(self.origin,'-c','user.name=Other','-c','user.email=other@localhost','commit-tree',tree,'-p',old,'-m','concurrent')
                git(self.origin,'update-ref','refs/heads/feat-one',extra,old)
                self.concurrent=extra
            return git(repo,*args)
        with patch.object(sync,'network_git',side_effect=remote): _,job=self.run_job()
        self.assertEqual(job['branches'][0]['status'],'blocked')
        self.assertEqual(git(self.origin,'rev-parse','feat-one'),self.concurrent)

    def test_api_double_click_reuses_job_and_drain_rejects_start(self):
        app=Dashboard(self.state); path='/products/'+self.product['id']+'/sync-master'
        with patch.object(sync.subprocess,'Popen') as popen:
            popen.return_value.pid=123
            first=app.operation(path,{'operation_id':str(uuid.uuid4())})
            second=app.operation(path,{'operation_id':str(uuid.uuid4())})
            self.assertEqual(first['id'],second['id']); self.assertEqual(popen.call_count,1)
        self.assertEqual(app.get(path)['status'],'queued')
        atomic(self.state/'autopilot/update-drain.json',{'job':'fixture'})
        with self.assertRaises(Conflict): sync.start(self.ledger,self.product)
