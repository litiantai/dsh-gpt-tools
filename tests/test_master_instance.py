"""主实例路由、真实版本约束、合入后验收与测试实例互斥。"""
import json
import fcntl
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'dsh-gpt-supervisor/scripts')]
from review_core import Store
from autopilot.store import Ledger
from autopilot.scheduler import Scheduler
from autopilot import master
from autopilot.workspace import git


class MasterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.repo=self.root/'repo';self.repo.mkdir()
        git(self.repo,'init','-b','master');git(self.repo,'config','user.name','Test');git(self.repo,'config','user.email','test@localhost')
        (self.repo/'file').write_text('one');git(self.repo,'add','.');git(self.repo,'commit','-m','one')
        self.commit=git(self.repo,'rev-parse','HEAD')
        self.store=Store(self.root/'state');self.ledger=Ledger(self.store)
        self.runtime=self.root/'runtime';self.runtime.mkdir()
        self.marker=self.runtime/'autopilot-release.json'
        self.marker.write_text(json.dumps({'id':'master-'+self.commit,'commit':self.commit}))
        self.product=self.ledger.create('products',{'name':'test','source':str(self.repo),'repository':str(self.repo),
            'delivery_repository':str(self.repo),'git':{'enabled':True,'base_branch':'master'},
            'application':str(self.root/'main.app'),'runtime':str(self.runtime),'app_support':str(self.root/'support'),
            'adapter':['fake'],'policy':{'idle_seconds':300},'test_environment':{'origin':'http://127.0.0.1:1'}},'active')
        self.instance={'instance_role':'master','commit':self.commit,'release_id':'master-'+self.commit,'origin':'http://127.0.0.1:9000'}
        self.request={'product':self.product,'record':self.product,'state_root':str(self.store.state/'autopilot')}

    def tearDown(self):
        self.temp.cleanup()

    def requirement(self, probes=True):
        data={'product_id':self.product['id'],'title':'效果验证','merge_sha':self.commit}
        if probes:
            data['resolution_probes']=[{'path':'/ths-octop/api/status','pointer':'/data/value','operator':'equals','expected':42}]
        return self.ledger.create('requirements',data,'online')

    def test_identity_rejects_old_commit_and_wrong_running_release(self):
        with patch('autopilot.thsoctop.endpoint',return_value=self.instance['origin']),patch('autopilot.thsoctop.monitor',return_value={'host':'ready','releaseId':'wrong'}):
            with self.assertRaisesRegex(ValueError,'master 不一致'):master.identity(self.product,self.commit)
        with patch('autopilot.thsoctop.endpoint',return_value=self.instance['origin']),patch('autopilot.thsoctop.monitor',return_value={'host':'ready','releaseId':self.instance['release_id']}):
            self.assertEqual(master.identity(self.product,self.commit),self.instance)
            with self.assertRaises(ValueError):master.identity(self.product,'different')

    def test_inspection_never_uses_test_origin(self):
        from autopilot.thsoctop import main
        with patch('autopilot.master.target',return_value=self.commit),patch('autopilot.master.identity',return_value=self.instance),patch('autopilot.thsoctop.http',return_value={'ok':True,'data':{}}) as fetch:
            result=main('inspect',self.request)
        self.assertEqual(result['status'],'pass')
        self.assertTrue(all(c.args[0].startswith(self.instance['origin']) for c in fetch.call_args_list))
        self.assertEqual(result['instance']['commit'],self.commit)

    def test_installation_excludes_inspection_and_final_acceptance(self):
        path=Path(self.request['state_root'])/'runtime-locks'/(self.product['id']+'.lock')
        path.parent.mkdir(parents=True)
        with path.open('a') as handle,patch('autopilot.master.target') as target:
            fcntl.flock(handle,fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(master.inspect(self.request)['status'],'busy')
            self.assertEqual(master.acceptance(self.request)['status'],'busy')
            target.assert_not_called()

    def test_final_acceptance_requires_effect_assertions_on_merged_master(self):
        requirement=self.requirement()
        with patch('autopilot.master.target',return_value=self.commit),patch('autopilot.master.identity',return_value=self.instance),patch('autopilot.thsoctop.http',return_value={'ok':True,'data':{'value':42}}) as fetch:
            result=master.acceptance(self.request | {'requirements':[requirement]})
            self.assertEqual(result['status'],'pass');fetch.assert_called_once_with(self.instance['origin']+'/ths-octop/api/status')
            self.assertEqual(master.acceptance(self.request | {'requirements':[self.requirement(False)]})['status'],'blocked')
            (self.repo/'file').write_text('future');git(self.repo,'commit','-am','future')
            future=requirement | {'merge_sha':git(self.repo,'rev-parse','HEAD')}
            self.assertEqual(master.acceptance(self.request | {'requirements':[future]})['status'],'blocked')

    def test_changed_master_invalidates_successful_probe(self):
        requirement=self.requirement()
        with patch('autopilot.master.target',side_effect=[self.commit,'new']),patch('autopilot.master.identity',return_value=self.instance),patch('autopilot.thsoctop.http',return_value={'ok':True,'data':{'value':42}}):
            result=master.acceptance(self.request | {'requirements':[requirement]})
        self.assertEqual(result['status'],'blocked')
        master.consume(self.ledger,self.product,result)
        self.assertEqual(self.ledger.get('requirements',requirement['id'])['final_acceptance']['status'],'blocked')

    def test_probe_failure_does_not_pass_and_consumed_work_is_not_repeated(self):
        requirement=self.requirement()
        with patch('autopilot.master.target',return_value=self.commit),patch('autopilot.master.identity',return_value=self.instance),patch('autopilot.thsoctop.http',return_value={'ok':True,'data':{'value':0}}):
            result=master.acceptance(self.request | {'requirements':[requirement]})
        self.assertEqual(result['status'],'fail')
        master.consume(self.ledger,self.product,result)
        self.assertEqual(master.pending(self.ledger,self.product),[])

    def test_sync_waits_for_continuous_idle_without_installing(self):
        with (patch('autopilot.master.target',return_value=self.commit),patch('autopilot.master.identity',side_effect=ValueError('old')),
                patch('autopilot.thsoctop.idle',return_value={'status':'busy','reason':'active session'}),patch('autopilot.master.prepare') as prepare):
            self.assertEqual(master.sync(self.request)['status'],'busy');prepare.assert_not_called()
        with (patch('autopilot.master.target',return_value=self.commit),patch('autopilot.master.identity',side_effect=ValueError('old')),
                patch('autopilot.thsoctop.idle',return_value={'status':'pass'}),patch('autopilot.master.prepare') as prepare):
            result=master.sync(self.request)
            self.assertEqual(result['status'],'busy');self.assertGreater(result['master_idle_since'],0);prepare.assert_not_called()

    def test_existing_live_master_does_not_reinstall(self):
        with patch('autopilot.master.target',return_value=self.commit),patch('autopilot.master.identity',return_value=self.instance),patch('autopilot.master.prepare') as prepare:
            self.assertEqual(master.sync(self.request)['status'],'pass');prepare.assert_not_called()

    def test_prepare_reuses_only_identical_validated_tree_and_preserves_original(self):
        from autopilot.thsoctop import manifest_hash
        candidate=self.root/'candidate'
        for name in ('runtime','thsoctop.app','support/dsh/profiles/octop'):
            (candidate/name).mkdir(parents=True)
        (candidate/'runtime/autopilot-release.json').write_text(json.dumps({'id':'candidate','commit':self.commit}))
        (candidate/'thsoctop.app/binary').write_text('verified')
        (candidate/'support/dsh/profiles/octop/package.json').write_text('{}')
        manifest={'id':'candidate','root':str(candidate),'commit':self.commit,'checks':[{'name':'business','status':'pass'}],
                  'runtime_hash':manifest_hash(candidate/'runtime'),'app_hash':manifest_hash(candidate/'thsoctop.app')}
        path=candidate/'manifest.json';path.write_text(json.dumps(manifest))
        proof={'head_sha':self.commit,'base_sha':self.commit}
        self.ledger.create('deliveries',{'product_id':self.product['id'],'validation_pass':proof,
            'receipts':[{'action':'validate_release','result':proof | {'status':'pass','manifest':str(path)}}]},'online')
        git(self.repo,'commit','--allow-empty','-m','merged')
        head=git(self.repo,'rev-parse','HEAD')
        copied=json.loads(Path(master.prepare(self.request,head)).read_text())
        self.assertEqual(copied['commit'],head)
        self.assertEqual(copied['validated_commit'],self.commit)
        self.assertEqual(json.loads((candidate/'runtime/autopilot-release.json').read_text())['commit'],self.commit)
        self.assertEqual(copied['runtime_hash'],manifest_hash(Path(copied['root'])/'runtime'))
        (self.repo/'file').write_text('not validated');git(self.repo,'commit','-am','unvalidated')
        with self.assertRaisesRegex(ValueError,'缺少内容一致'):
            master.prepare(self.request,git(self.repo,'rev-parse','HEAD'))
        (candidate/'thsoctop.app/binary').write_text('changed')
        with self.assertRaisesRegex(ValueError,'产物发生变化'):master.prepare(self.request,head)

    def test_scheduler_syncs_before_final_acceptance(self):
        self.requirement()
        scheduler=Scheduler(self.store)
        with patch.object(scheduler,'start_call') as start:
            self.assertTrue(scheduler.master_step(self.product));self.assertEqual(start.call_args.args[3],'master_sync')
            product=self.product | {'last_master_sync':__import__('time').time(),'last_master_sync_result':{'status':'pass'}}
            self.assertTrue(scheduler.master_step(product));self.assertEqual(start.call_args.args[3],'final_acceptance')

    def test_test_slot_is_exclusive_and_cannot_close_production(self):
        from autopilot.test_instance import slot
        root=Path(self.request['state_root'])
        with slot(root,self.product):
            with self.assertRaisesRegex(ValueError,'占用'):
                with slot(root,self.product):pass
        self.product=self.ledger.update('products',self.product['id'],self.product['version'],{'test_environment':{'pid':123,'application':self.product['application']}})
        with patch('autopilot.test_instance.os.kill') as kill:
            with self.assertRaisesRegex(ValueError,'不在隔离目录'):
                with slot(root,self.product):pass
            kill.assert_not_called()

    def test_daily_reacceptance_routes_to_master_and_rejects_unmerged_run(self):
        from autopilot.daily import execute
        request=self.request | {'audit_run':{'status':'accepted'},'requirement':self.requirement()}
        with patch('autopilot.master.acceptance',return_value={'status':'pass'}) as acceptance:
            self.assertEqual(execute('daily_acceptance',request)['status'],'blocked');acceptance.assert_not_called()
            request['audit_run']={'status':'online','merge_sha':self.commit}
            self.assertEqual(execute('daily_acceptance',request)['status'],'pass')
            self.assertEqual(acceptance.call_args.args[0]['requirements'][0]['merge_sha'],self.commit)


if __name__=='__main__':
    unittest.main()
