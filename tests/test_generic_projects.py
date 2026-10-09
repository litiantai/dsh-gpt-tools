"""通用接入、隔离执行、兼容迁移和独立更新恢复的行为测试。"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT), str(ROOT/'dsh-gpt-supervisor/scripts')]
from review_core import Store, Conflict
from autopilot.api import Control
from autopilot.scheduler import Scheduler
from autopilot.project import migrate, manifest_hash
from autopilot.onboarding import detect, execute, startup, onboard
from autopilot.workspace import git, snapshot
from autopilot.isolated import environment, run
from autopilot.probes import validate

spec=importlib.util.spec_from_file_location('updater', ROOT/'scripts/platform-updater.py')
updater=importlib.util.module_from_spec(spec);spec.loader.exec_module(updater)


class GenericTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.repo=self.root/'repo';self.repo.mkdir()
        git(self.repo,'init');git(self.repo,'config','user.name','Test');git(self.repo,'config','user.email','test@localhost')
        (self.repo/'README.md').write_text('fixture')
        git(self.repo,'add','.');git(self.repo,'commit','-m','fixture')
        self.store=Store(self.root/'state');self.control=Control(self.store);self.ledger=self.control.ledger
        self.p=self.ledger.create('products',{'name':'fixture','source':str(self.repo),'goal':'fixture'},'paused')

    def tearDown(self):
        self.tmp.cleanup()

    def test_detection_node_and_ambiguous_start(self):
        (self.repo/'package.json').write_text(json.dumps({'scripts':{'build':'tsc','test':'test'}}))
        (self.repo/'package-lock.json').write_text('{}')
        config=detect(self.repo)
        self.assertEqual(config['commands']['install'][0][1], 'ci')
        self.assertEqual(config['commands']['test'], [['npm','run','test']])
        self.assertTrue(config['blockers'])

    def test_python_and_jvm_are_detected_without_guessing(self):
        for name,stack in [('pyproject.toml','python'),('pom.xml','java')]:
            (self.repo/name).write_text('')
            self.assertIn(stack,detect(self.repo)['stacks'])
        self.assertTrue(detect(self.repo)['blockers'])

    def test_scan_retry_keeps_history_and_does_not_apply_config(self):
        scan=self.control.mutate('/scans',{'source':str(self.repo),'product_id':self.p['id']})
        scan=self.ledger.update('scans',scan['id'],scan['version'],{'reason':'missing dependencies'},'blocked')
        newer=self.control.mutate('/scans/'+scan['id']+'/retry',{'version':scan['version']})
        self.assertNotEqual(scan['id'],newer['id'])
        self.assertEqual(newer['previous_scan_id'],scan['id'])
        self.assertEqual(self.ledger.get('scans',scan['id'])['reason'],'missing dependencies')
        self.assertNotIn('project_config',self.ledger.get('products',self.p['id']))

    def test_onboard_persists_project_link_and_rejects_cross_project_apply(self):
        scan=self.control.mutate('/scans',{'source':str(self.repo)})
        scan=self.ledger.update('scans',scan['id'],scan['version'],{'result':{'configuration':detect(self.repo),'workspace':str(self.repo),'snapshot_commit':git(self.repo,'rev-parse','HEAD')}},'pass')
        linked=onboard(self.ledger,scan,{})
        product=self.ledger.get('products',linked['product_id'])
        self.assertEqual(product['config_scan_id'],scan['id'])
        self.assertEqual(self.ledger.get('scans',scan['id'])['product_id'],product['id'])
        other=self.ledger.create('products',{k:v for k,v in product.items() if k not in ('id','product_id')},'observing')
        with self.assertRaises(Conflict):
            self.control.mutate('/scans/'+scan['id']+'/apply',{'version':linked['version'],'product_id':other['id'],'product_version':other['version']})
        remote=self.ledger.create('scans',{'source':'https://example.com/other.git','result':scan['result']},'pass')
        for target in (self.p,product):
            with self.assertRaises(Conflict):
                self.control.mutate('/scans/'+remote['id']+'/apply',{'version':remote['version'],'product_id':target['id'],'product_version':target['version']})

    def test_model_provider_result_is_attributed_without_fallback(self):
        from autopilot.codex_executor import execute as model
        fake=self.root/'model'
        fake.write_text('#!'+sys.executable+'\nimport sys,json\nfrom pathlib import Path\nPath(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps({"status":"pass","reason":"","summary":"verified","plan":"implement"}))\n')
        fake.chmod(0o700)
        product=self.p|{'repository':str(self.repo),'agents':{'implementation':{'provider':'codex','model':'fixture','bin':str(fake)}}}
        result=model('plan',{'product':product,'record':{},'state_root':str(self.store.state/'autopilot')})
        self.assertEqual(result['provider'],'codex');self.assertEqual(result['plan'],'implement')

    def test_generic_codex_uses_outer_sandbox_for_tool_execution(self):
        from autopilot.codex_executor import execute as model
        fake=self.root/'codex-model'
        fake.write_text('#!'+sys.executable+'\nimport sys,json\nfrom pathlib import Path\nassert sys.argv[sys.argv.index("--sandbox")+1]=="danger-full-access"\nPath(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps({"status":"pass","plan":"outer policy"}))\n')
        fake.chmod(0o700)
        product=self.p|{'repository':str(self.repo),'agents':{'implementation':{'provider':'codex','model':'fixture','bin':str(fake)}},'adapter_spec':{'kind':'command'}}
        with patch('autopilot.sandbox.restrict',side_effect=lambda argv,*a,**kw: argv) as sandbox:
            result=model('plan',{'product':product,'record':{},'state_root':str(self.store.state/'autopilot')})
        self.assertEqual(result['status'],'pass')
        self.assertTrue(sandbox.call_args.kwargs['deny_local'])
        self.assertNotIn(str(self.repo),[str(p) for p in sandbox.call_args.args[1]])

    def test_python_project_scan_is_recorded_and_source_remains_unchanged(self):
        if sys.platform!='darwin' or os.environ.get('DSH_PROJECT_ISOLATED'):
            self.skipTest('隔离启动由外层验证运行')
        config={'version':1,'commands':{'test':[[sys.executable,'-c','assert 2+2==4']],
                'start':[[sys.executable,'-m','http.server','{port}','--bind','127.0.0.1']]},'web':False,'blockers':[]}
        (self.repo/'pyproject.toml').write_text('[project]\nname="fixture"\nversion="1.0.0"\n')
        (self.repo/'.autopilot.json').write_text(json.dumps(config))
        before=git(self.repo,'status','--porcelain')
        scan=self.control.mutate('/scans',{'source':str(self.repo)})
        result=execute('scan',{'record':scan,'state_root':str(self.store.state/'autopilot')})
        self.assertEqual(result['status'],'pass',result)
        self.assertEqual(before,git(self.repo,'status','--porcelain'))
        self.assertEqual(result['business_acceptance'],'pending')

    def test_migration_preserves_ids_evidence_and_is_idempotent(self):
        old=self.ledger.create('runs',{'product_id':self.p['id'],'receipts':[{'evidence':'original'}]},'cancelled')
        migrate(self.ledger);first=self.ledger.get('products',self.p['id'])
        migrate(self.ledger)
        self.assertEqual(first,self.ledger.get('products',self.p['id']))
        self.assertEqual(old,self.ledger.get('runs',old['id']))

    def test_retired_project_has_no_background_dispatch(self):
        self.p=self.ledger.update('products',self.p['id'],self.p['version'],{'automation_disabled':True,'daily_report_enabled':True,'daily_report_enabled_at':1},'active')
        self.ledger.create('requirements',{'product_id':self.p['id'],'classification':'development','in_scope':True},'pending')
        scheduler=Scheduler(self.store)
        with patch.object(scheduler,'start_call') as start:
            scheduler.tick();start.assert_not_called()
        self.assertEqual(self.ledger.list('daily_reports'),[])
        self.assertEqual(self.ledger.list('runs'),[])

    def test_drain_reconciles_unknown_launch_without_relaunch(self):
        import time
        scheduler=Scheduler(self.store)
        directory=scheduler.root/'calls'/'interrupted';directory.mkdir(parents=True)
        request=directory/'request.json';request.write_text('{}')
        self.ledger.create('runs',{'product_id':self.p['id'],'call':{'path':str(request),'started':time.time()-60}},'planning')
        self.assertTrue(updater.busy(self.store.state))
        (scheduler.root/'update-drain.json').write_text('{}')
        with patch.object(scheduler,'start_call') as launch:
            scheduler.tick();launch.assert_not_called()
        self.assertTrue(json.loads((directory/'result.json').read_text())['uncertain'])
        self.assertFalse(updater.busy(self.store.state))

    def test_generic_probe_allowlist_rejects_unknown_and_encoded_paths(self):
        product={'project_config':{'readonly_paths':['/api/status']}}
        validate(['/api/status'],product)
        for path in ['/api/admin','https://example.com','//evil','/api/%2e%2e','/api/status?write=1']:
            with self.assertRaises(ValueError):validate([path],product)

    def test_snapshot_excludes_info_and_old_evidence(self):
        (self.repo/'info').mkdir();(self.repo/'info/private.txt').write_text('private')
        (self.repo/'old.patch').write_text('old')
        snapshot(self.repo,self.root/'snapshot',exclude=['info','old.patch'])
        self.assertFalse((self.root/'snapshot/info').exists());self.assertFalse((self.root/'snapshot/old.patch').exists())
        self.assertTrue((self.repo/'info/private.txt').exists())

    def test_fresh_environment_does_not_inherit_credentials(self):
        with patch.dict('os.environ',{'AWS_SECRET_ACCESS_KEY':'secret','DSH_HOME':'/private/user'}):
            env=environment(self.root/'execution')
        self.assertNotIn('AWS_SECRET_ACCESS_KEY',env)
        self.assertTrue(env['DSH_HOME'].startswith(str(self.root)))

    @unittest.skipUnless(sys.platform=='darwin' and not os.environ.get('DSH_PROJECT_ISOLATED'), '实际 Seatbelt 边界测试由外层验证运行，macOS 不允许嵌套沙箱')
    def test_real_isolated_python_start_and_stop(self):
        config={'commands':{'start':[[sys.executable,'-m','http.server','{port}','--bind','127.0.0.1']]},'web':False,'startup_timeout':5}
        result=startup(self.repo,self.root/'execution',config)
        self.assertEqual(result['status'],'pass',result)
        self.assertTrue(result['stopped'])

    @unittest.skipUnless(sys.platform=='darwin' and not os.environ.get('DSH_PROJECT_ISOLATED'), '实际 Seatbelt 边界测试由外层验证运行，macOS 不允许嵌套沙箱')
    def test_sandbox_denies_private_reads_and_production_ports(self):
        secret=self.root/'private.txt';secret.write_text('private')
        script='from pathlib import Path;Path('+repr(str(secret))+').read_text()'
        result=run([sys.executable,'-c',script],self.repo,self.root/'execution','read')
        self.assertEqual(result['status'],'fail')
        script='import socket;socket.create_connection(("127.0.0.1",13084),timeout=1)'
        result=run([sys.executable,'-c',script],self.repo,self.root/'execution2','network',ports=['test-loopback'])
        self.assertEqual(result['status'],'fail')

    def test_generic_business_probe_requires_assertion(self):
        from autopilot.probes import check
        product={'project_config':{'readonly_paths':['/api/status']}}
        with self.assertRaises(ValueError):check(['/api/status'],lambda _: {'ok':False},product)
        assertion={'path':'/api/status','pointer':'/ready','operator':'equals','expected':True}
        self.assertFalse(check([assertion],lambda _: {'ready':False},product))

    def test_pending_self_update_keeps_release_and_run(self):
        run=self.ledger.create('runs',{'product_id':self.p['id'],'release_id':'release'},'deploying')
        result=Scheduler(self.store).complete_action(run,self.p,'publish',{'status':'deferred','update':'job.json','reason':'waiting'})
        self.assertEqual(result['status'],'deploying');self.assertEqual(result['release_id'],'release')
        self.assertEqual(result['update'],'job.json')

    def test_updater_waits_then_switches_and_completes(self):
        config,path=self.update_fixture()
        with patch.object(updater,'busy',return_value=True),patch.object(updater,'stop_services') as stopped:
            updater.step(config,path);stopped.assert_not_called()
        self.assertEqual(json.loads(path.read_text())['status'],'draining')
        with patch.object(updater,'busy',return_value=False),patch.object(updater,'stop_services'),patch.object(updater,'restart'),patch.object(updater,'healthy',return_value=True):
            updater.step(config,path);updater.step(config,path)
        self.assertEqual(json.loads(path.read_text())['status'],'completed')
        self.assertFalse((self.store.state/'autopilot/update-drain.json').exists())
        with patch.object(updater,'restart') as restart:
            updater.step(config,path);restart.assert_not_called()

    def test_updater_rolls_back_without_replacing_database(self):
        config,path=self.update_fixture()
        with patch.object(updater,'busy',return_value=False),patch.object(updater,'stop_services'),patch.object(updater,'restart'),patch.object(updater,'healthy',return_value=True):
            updater.step(config,path)
        self.ledger.create('signals',{'product_id':self.p['id'],'summary':'new history'})
        with patch.object(updater,'stop_services'),patch.object(updater,'restart'),patch.object(updater,'healthy',return_value=False):
            updater.step(config,path)
        self.assertEqual(json.loads(path.read_text())['status'],'rollback_check')
        with patch.object(updater,'healthy',return_value=True):updater.reconcile_rollback(config,path)
        self.assertEqual(json.loads(path.read_text())['status'],'rolled_back')
        self.assertEqual(len(self.ledger.list('signals')),1)
        self.assertEqual(Path(config['current']).resolve(),self.root/'old')

    def test_updater_rejects_tampering_before_stopping(self):
        config,path=self.update_fixture()
        manifest=json.loads(Path(json.loads(path.read_text())['manifest']).read_text())
        (Path(manifest['artifact'])/'bad').write_text('tampered')
        with patch.object(updater,'stop_services') as stop_service:
            updater.step(config,path);stop_service.assert_not_called()
        self.assertEqual(json.loads(path.read_text())['status'],'blocked')

    def test_updater_retries_failed_rollback_and_cleans_completed_drain(self):
        config,path=self.update_fixture()
        with patch.object(updater,'busy',return_value=False),patch.object(updater,'stop_services'),patch.object(updater,'restart',side_effect=OSError('temporarily unavailable')):
            updater.step(config,path)
        self.assertEqual(json.loads(path.read_text())['status'],'rollback_retry')
        job=json.loads(path.read_text());path.write_text(json.dumps(job|{'retry_at':0}))
        with patch.object(updater,'stop_services'),patch.object(updater,'restart'):
            updater.reconcile_rollback(config,path)
        with patch.object(updater,'healthy',return_value=True):updater.reconcile_rollback(config,path)
        self.assertEqual(json.loads(path.read_text())['status'],'rolled_back')
        drain=self.store.state/'autopilot/update-drain.json'
        drain.write_text(json.dumps({'job':str(path)}))
        updater.step(config,path)
        self.assertFalse(drain.exists())

    def test_updater_recovers_switch_intent_after_interruption(self):
        config,path=self.update_fixture()
        job=json.loads(path.read_text())|{'status':'switching','previous':str(self.root/'old')}
        path.write_text(json.dumps(job))
        with patch.object(updater,'restart'),patch.object(updater,'healthy',return_value=True):
            updater.step(config,path);updater.step(config,path)
        self.assertEqual(json.loads(path.read_text())['status'],'completed')

    def update_fixture(self):
        root=self.store.state/'autopilot/candidates/fixture';artifact=root/'artifact';artifact.mkdir(parents=True)
        marker={'product_id':self.p['id'],'commit':'new','release_id':'new'}
        (artifact/'autopilot-release.json').write_text(json.dumps(marker))
        manifest=root/'manifest.json';manifest.write_text(json.dumps(marker|{'artifact':str(artifact),'artifact_hash':manifest_hash(artifact),
            'database_compatibility':'backward-compatible','checks':[{'status':'pass','required':True}]}))
        old=self.root/'old';old.mkdir();(old/'autopilot-release.json').write_text(json.dumps(marker|{'commit':'old'}))
        current=self.root/'current';current.symlink_to(old,target_is_directory=True)
        path=self.store.state/'autopilot/updates/job.json';path.parent.mkdir();path.write_text(json.dumps({'status':'queued','manifest':str(manifest),'product_id':self.p['id'],'expected_commit':'new'}))
        return {'state':str(self.store.state),'current':str(current),'observation_seconds':0,'restart_commands':[],'stop_commands':[]},path


if __name__=='__main__':unittest.main()
