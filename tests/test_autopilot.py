"""Autonomous control behavior: isolated repositories, real subprocess receipts, fake model only."""
import concurrent.futures
import json
import os
import runpy
from pathlib import Path
import subprocess
import shlex
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'dsh-gpt-supervisor/scripts')]
from review_core import Store, Conflict
from autopilot.api import Control
from autopilot.scheduler import Scheduler
from autopilot.store import Ledger, redact
from autopilot.workspace import snapshot, git
from autopilot.thsoctop import manifest_hash, idle, publish, rollback, bind_runtime_sdk
from autopilot.sandbox import restrict
from autopilot.evidence import Recorder
from autopilot.scheduler import WorkerStore


class AutonomousTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.source=self.root/'source'
        self.source.mkdir()
        git(self.source,'init')
        git(self.source,'config','user.name','Test')
        git(self.source,'config','user.email','test@localhost')
        (self.source/'calc.py').write_text('value = 0\n')
        (self.source/'.gitignore').write_text('.env\nnode_modules/\n')
        git(self.source,'add','.')
        git(self.source,'commit','-m','initial')
        (self.source/'notes.txt').write_text('uncommitted')
        (self.source/'.env').write_text('SECRET=hidden')
        self.fake=self.root/'reviewer'
        self.fake.write_text(f'''#!{sys.executable}
import sys,json
from pathlib import Path
p=sys.stdin.read()
r={{"decision":"done" if '"phase": "acceptance"' in p else "approve","summary":"checked","instruction":"continue","checks":["verified"],"issues":[]}}
Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps(r))
''')
        self.fake.chmod(0o700)
        self.store=Store(self.root/'state',{'home':str(self.root/'home'),'codex_bin':str(self.fake),'review_timeout':5,'handoff_timeout':10})
        self.control=Control(self.store)
        self.ledger=self.control.ledger
        self.adapter=self.root/'adapter.py'
        self.adapter.write_text('''import json,sys
from pathlib import Path
r=json.load(sys.stdin); action=sys.argv[1]; record=r['record']
out={'status':'pass'}
if action=='plan':out['plan']='fix calc value and verify'
if action=='develop':
 Path(record['workspace'],'calc.py').write_text('value = 1\\n');out['summary']='fixed'
if action=='verify':
 assert 'value = 1' in Path(record['workspace'],'calc.py').read_text()
 p=Path(record['workspace']).parent/(record['id']+'.manifest.json');p.write_text('{}')
 out.update(checks=[{'name':'calc','status':'pass','required':True}],manifest=str(p))
if action=='observe':out['problem_resolved']=True
print(json.dumps(out))
''')
        base=snapshot(self.source,self.root/'baseline')
        self.product=self.control.mutate('/products',{'config':{'name':'test','source':str(self.source),'goal':'fix calculation',
            'repository':base['repository'],'adapter':[sys.executable,str(self.adapter)],'executor':[sys.executable,str(self.adapter)],
            'policy':{'idle_seconds':1,'observation_seconds':1,'probe_seconds':1}}})

    def tearDown(self):
        self.tmp.cleanup()

    def test_legacy_probe_reads_health_without_claiming_release_readiness(self):
        from autopilot.thsoctop import probe
        from urllib.error import HTTPError
        missing=HTTPError('http://localhost/status',404,'Not Found',{},None)
        old={'ok':True,'data':{'product':'thsoctop 桌宠工作台','version':'0.1.0','nickname':'private','userId':'private'}}
        with patch('autopilot.thsoctop.monitor',side_effect=missing),patch('autopilot.thsoctop.endpoint',return_value='http://127.0.0.1:1'),patch('autopilot.thsoctop.http',return_value=old):
            result=probe(self.product)
            self.assertEqual(result['status'],'pass')
            self.assertEqual(result['monitor_mode'],'basic')
            self.assertFalse(result['release_monitor_available'])
            self.assertFalse(result['health']['activity']['known'])
            self.assertNotIn('private',json.dumps(result))
            self.assertEqual(idle(self.product)['status'],'busy')

    def test_probe_does_not_fallback_on_auth_failure_or_wrong_product(self):
        from autopilot.thsoctop import probe
        from urllib.error import HTTPError
        with patch('autopilot.thsoctop.monitor',side_effect=HTTPError('http://localhost',401,'Unauthorized',{},None)),patch('autopilot.thsoctop.http') as read:
            self.assertEqual(probe(self.product)['status'],'blocked');read.assert_not_called()
        with patch('autopilot.thsoctop.monitor',side_effect=FileNotFoundError()),patch('autopilot.thsoctop.endpoint',return_value='http://localhost'),patch('autopilot.thsoctop.http',return_value={'ok':True,'data':{'product':'other','version':'1'}}):
            self.assertEqual(probe(self.product)['status'],'blocked')
        with patch('autopilot.thsoctop.monitor',side_effect=FileNotFoundError()),patch('autopilot.thsoctop.endpoint',return_value='http://localhost'),patch('autopilot.thsoctop.http',side_effect=ConnectionError('offline')):
            self.assertEqual(probe(self.product)['status'],'blocked')

    def test_preview_rejects_unaccepted_task_and_changed_artifact(self):
        start=runpy.run_path(str(ROOT/'scripts/autopilot-preview.py'))['start']
        run=self.control.queue(self.requirement())
        with self.assertRaisesRegex(ValueError,'已经通过业务验收'):
            start(self.store.state,run['id'])
        candidate=self.root/'candidate'
        for name in ('runtime','thsoctop.app'):
            (candidate/name).mkdir(parents=True)
            (candidate/name/'payload').write_text('verified')
        manifest={'root':str(candidate),'commit':'verified-commit','checks':[{'status':'pass','required':True}],
                  'runtime_hash':manifest_hash(candidate/'runtime'),'app_hash':manifest_hash(candidate/'thsoctop.app')}
        path=candidate/'manifest.json';path.write_text(json.dumps(manifest))
        self.ledger.update('runs',run['id'],run['version'],{'commit':manifest['commit'],'manifest':str(path)},'accepted')
        (candidate/'runtime/payload').write_text('changed after acceptance')
        with self.assertRaisesRegex(ValueError,'验收后产物发生改变'):
            start(self.store.state,run['id'])
        self.assertFalse((self.store.state/'autopilot/previews').exists())

    def requirement(self):
        return self.control.mutate('/requirements',{'product_id':self.product['id'],'title':'fix calc','evidence':'value is zero',
                'acceptance':['value equals one'],'impact':'wrong result'})

    def test_acceptance_target_stops_before_release(self):
        evaluation=self.ledger.create('evaluations',{'product_id':self.product['id'],'target_count':1,'run_ids':[],'target_phase':'acceptance'},'running')
        self.product=self.ledger.update('products',self.product['id'],self.product['version'],{'evaluation_id':evaluation['id']},'active')
        run=self.control.queue(self.requirement());scheduler=Scheduler(self.store)
        end=time.time()+20
        while time.time()<end:
            scheduler.tick();current=self.ledger.get('runs',run['id'])
            if current['status'] in ('accepted','blocked'):break
            time.sleep(.06)
        self.assertEqual(current['status'],'accepted',current)
        self.assertTrue(current['acceptance_review_id'])
        scheduler.tick()
        self.assertEqual(self.ledger.list('releases'),[])
        self.assertEqual(self.ledger.get('evaluations',evaluation['id'])['status'],'pass')

    def test_vendor_manifest_preserves_ignored_distribution_files(self):
        vendor=self.source/'vendor/demo/Package/dist';vendor.mkdir(parents=True)
        (vendor/'index.js').write_text('export const version=1;')
        (vendor.parent.parent/'manifest.json').write_text(json.dumps({'packages':{'Package':{'files':{'dist/index.js':'fixture'}}}}))
        (self.source/'.gitignore').write_text('dist/\nnode_modules/\n')
        snap=self.root/'vendor-baseline';snapshot(self.source,snap)
        self.assertTrue((snap/'vendor/demo/Package/dist/index.js').exists())
        self.assertEqual(git(snap,'show','HEAD:vendor/demo/Package/dist/index.js'),'export const version=1;')

    def test_guard_accepts_redirection_but_rejects_background(self):
        script="""
import {apply} from './scripts/autopilot-guard.mjs';
process.env.DSH_AUTOPILOT_WORKER='test';process.env.DSH_AUTOPILOT_PHASE='develop';
let guard;apply({tools:{guard:g=>guard=g}});
for(const command of ['pnpm install --frozen-lockfile 2>&1 | tail -25','node test.js && node check.js'])
 if(guard({name:'bash',arguments:{command}}))throw Error('foreground blocked');
for(const command of ['node test.js &','nohup node test.js','node test.js & node check.js'])
 if(!guard({name:'bash',arguments:{command}}))throw Error('background allowed');
"""
        subprocess.run(['node','--input-type=module','-e',script],cwd=ROOT,check=True,capture_output=True)

    def test_evaluation_aggregates_blockers_and_acceptance(self):
        run=self.control.queue(self.requirement())
        evaluation=self.ledger.create('evaluations',{'product_id':self.product['id'],'target_count':1,'run_ids':[run['id']],'target_phase':'acceptance'},'running')
        run=self.ledger.update('runs',run['id'],run['version'],{'reason':'missing prerequisite'},'blocked')
        scheduler=Scheduler(self.store);scheduler.reconcile_evaluations()
        self.assertEqual(self.ledger.get('evaluations',evaluation['id'])['status'],'blocked')
        self.ledger.update('runs',run['id'],run['version'],{'reason':''},'accepted')
        scheduler.reconcile_evaluations()
        self.assertEqual(self.ledger.get('evaluations',evaluation['id'])['passed'],1)
        self.assertEqual(self.ledger.get('evaluations',evaluation['id'])['status'],'pass')
        scheduler.tick()
        self.assertEqual(self.ledger.list('releases'),[])

    def test_snapshot_preserves_dirty_tree_and_excludes_credentials(self):
        self.assertEqual((self.source/'notes.txt').read_text(),'uncommitted')
        self.assertIn('?? notes.txt',git(self.source,'status','--porcelain'))
        self.assertTrue((self.root/'baseline/notes.txt').exists())
        self.assertFalse((self.root/'baseline/.env').exists())

    def test_worker_inherits_only_model_credential_references(self):
        source=self.root/'model-home'
        profile=source/'profiles/review'
        profile.mkdir(parents=True)
        (profile/'cordis.patch.yml').write_text('- id: agent-default-model\n  config:\n    provider: deepseek-official\n    model: inherited-model\n')
        secret=source/'.credentials.yaml'
        secret.write_text('version: 1\nrefs:\n  DEEPSEEK_API_KEY: fixture-model-secret\n  UNRELATED_KEY: fixture-private-secret\nrecords: {}\n')
        worker=self.root/'worker-home'
        subprocess.run(['node',str(ROOT/'scripts/autopilot-profile.mjs'),str(source),'review',str(worker),str(self.root/'runtime'),'guard.mjs'],check=True,capture_output=True)
        copied=(worker/'.credentials.yaml').read_text()
        self.assertIn('fixture-model-secret',copied)
        self.assertNotIn('fixture-private-secret',copied)
        self.assertEqual((worker/'.credentials.yaml').stat().st_mode & 0o777,0o600)
        self.assertIn('fixture-private-secret',secret.read_text())
        self.assertNotIn('fixture-model-secret',(worker/'profiles/autopilot-worker/cordis.patch.yml').read_text())
        self.assertIn('harness-sessions',(worker/'profiles/autopilot-worker/cordis.patch.yml').read_text())

    def test_isolated_build_uses_fixed_runtime_sdk(self):
        sdk=self.root/'fixed-runtime/node_modules/@deepseek-ai/dsh-tools'
        sdk.mkdir(parents=True)
        (sdk/'package.json').write_text('{"name":"fixture-sdk"}')
        bind_runtime_sdk(self.source,{'worker_runtime':str(self.root/'fixed-runtime')})
        bound=self.source/'node_modules/@deepseek-ai/dsh-tools'
        self.assertEqual(bound.resolve(),sdk.resolve())
        self.assertEqual((bound/'package.json').read_text(),'{"name":"fixture-sdk"}')

    def test_inspection_retains_steps_and_verifies_screenshot_integrity(self):
        picture=self.root/'scene.png';picture.write_bytes(b'fixture-image')
        rec=Recorder(self.store,self.product['id'],'watchlist','isolated browser')
        evidence=rec.step('open','click','quotes','timeout','blocked',picture,{'cookie':'credential-canary'})
        rec.finish('blocked','external service unavailable, investigate first')
        shown=self.control.get(f'/evidence/{evidence["id"]}/screenshot')
        self.assertTrue(shown['data_url'].startswith('data:image/png;base64,'))
        self.assertNotIn('credential-canary',json.dumps(self.ledger.get('evidence',evidence['id'])))
        Path(evidence['screenshot']).write_bytes(b'tampered')
        with self.assertRaises(ValueError):self.control.get(f'/evidence/{evidence["id"]}/screenshot')
        latest=self.ledger.get('evidence',evidence['id'])
        self.ledger.update('evidence',latest['id'],latest['version'],{'screenshot':str(picture)})
        with self.assertRaises(ValueError):self.control.get(f'/evidence/{evidence["id"]}/screenshot')

    def test_project_reviewer_does_not_change_global_configuration(self):
        previous=self.store.settings()['reviewer_unified']
        scoped=WorkerStore(self.store.state,self.root/'worker',{'provider':'codex','model':'fixture-model'})
        self.assertEqual(scoped.settings()['reviewer_unified']['model'],'fixture-model')
        self.assertEqual(self.store.settings()['reviewer_unified'],previous)

    @unittest.skipUnless(sys.platform=='darwin' and not os.environ.get('DSH_PROJECT_ISOLATED'),'由外层验证运行 macOS 沙箱边界测试，系统不允许嵌套 Seatbelt')
    def test_sandbox_denies_external_writes_and_private_data_but_allows_dev_null(self):
        allowed=self.root/'allowed';allowed.mkdir()
        private=self.root/'private';private.mkdir()
        sentinel=private/'sentinel';sentinel.write_text('unchanged')
        def run(command):
            return subprocess.run(restrict(['/bin/sh','-c',command],[allowed],self.root/'sandbox.sb',private_roots=[private]),capture_output=True)
        self.assertEqual(run('echo safe >/dev/null').returncode,0)
        self.assertEqual(run('echo safe >'+shlex.quote(str(allowed/'output'))).returncode,0)
        self.assertNotEqual(run('echo bad >'+shlex.quote(str(sentinel))).returncode,0)
        self.assertNotEqual(run('cat '+shlex.quote(str(sentinel))).returncode,0)
        self.assertEqual(sentinel.read_text(),'unchanged')

    def test_dedup_is_atomic_and_redacts_evidence(self):
        body={'source':'test','code':'FAIL','summary':'failure','evidence':{'authorization':'private','log':'Bearer secret'}}
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            records=list(pool.map(lambda _:self.ledger.signal(self.product['id'],body),range(10)))
        self.assertEqual(len({r['id'] for r in records}),1)
        signal=self.ledger.list('signals')[0]
        self.assertEqual(signal['count'],10)
        self.assertNotIn('private',json.dumps(signal))
        self.assertNotIn('secret',json.dumps(signal))

    def test_conflicting_versions_and_duplicate_queue_are_rejected(self):
        req=self.requirement()
        self.control.queue(req)
        with self.assertRaises(Conflict):self.control.queue(req)
        with self.assertRaises(Conflict):
            self.control.mutate(f'/products/{self.product["id"]}/pause',{'version':0})
        self.assertEqual(len(self.ledger.list('runs')),1)

    def test_daily_budget_and_lease_have_single_winner(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            results=list(pool.map(lambda _:self.ledger.budget(self.product['id'],'development',2),range(10)))
        self.assertEqual(sum(results),2)
        self.assertTrue(self.ledger.lease('scheduler','one'))
        self.assertFalse(self.ledger.lease('scheduler','two'))

    def test_settings_save_survives_monitor_updates_and_preserves_runtime(self):
        before=self.product
        latest=self.ledger.update('products',before['id'],before['version'],
                                  {'last_probe':123,'call':{'id':'monitor-call'}},'observing')
        saved=self.control.mutate(f'/products/{before["id"]}/configure',{
            'version':before['version'],'base_config':{'goal':before['goal']},'config':{'goal':'updated goal'}})
        self.assertEqual(saved['goal'],'updated goal')
        self.assertEqual(saved['version'],latest['version']+1)
        for key in ('call','last_probe','status','adapter','executor','repository','policy'):
            self.assertEqual(saved[key],latest[key])

    def test_settings_conflict_keeps_other_edit_and_legacy_version_check(self):
        before=self.product
        latest=self.ledger.update('products',before['id'],before['version'],{'goal':'another editor'})
        path=f'/products/{before["id"]}/configure'
        with self.assertRaisesRegex(Conflict,'项目配置已被其他操作修改'):
            self.control.mutate(path,{'version':before['version'],'base_config':{'goal':before['goal']},
                                      'config':{'goal':'my draft'}})
        with self.assertRaises(Conflict):
            self.control.mutate(path,{'version':before['version'],'config':{'goal':'my draft'}})
        self.assertEqual(self.ledger.get('products',before['id']),latest)

    def test_accepted_tasks_do_not_block_settings_save(self):
        runs=[self.ledger.create('runs',{'product_id':self.product['id'],'title':f'accepted task {i}'},'accepted')
              for i in range(3)]
        saved=self.control.mutate(f'/products/{self.product["id"]}/configure',{
            'version':self.product['version'],'base_config':{'goal':self.product['goal']},
            'config':{'goal':'next development goal'}})
        self.assertEqual(saved['goal'],'next development goal')
        for run in runs:
            self.assertEqual(self.ledger.get('runs',run['id']),run)

    def test_settings_baseline_cannot_bypass_execution_configuration_or_running_guard(self):
        path=f'/products/{self.product["id"]}/configure'
        for base,changes in [({}, {'goal':'changed'}),
                             ({'executor':self.product['executor']},{'executor':['other']})]:
            with self.assertRaises(ValueError):
                self.control.mutate(path,{'base_config':base,'config':changes})
        run=self.control.queue(self.requirement())
        self.ledger.update('runs',run['id'],run['version'],{},'developing')
        with self.assertRaisesRegex(Conflict,'有执行中的任务'):
            self.control.mutate(path,{'base_config':{'goal':self.product['goal']},'config':{'goal':'changed'}})

    def test_concurrent_settings_saves_have_one_winner(self):
        path=f'/products/{self.product["id"]}/configure'
        def save(goal):
            try:
                return self.control.mutate(path,{'base_config':{'goal':self.product['goal']},'config':{'goal':goal}})
            except Conflict:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(save,['first draft','second draft']))
        winners=[result for result in results if result]
        self.assertEqual(len(winners),1)
        self.assertEqual(self.ledger.get('products',self.product['id'])['goal'],winners[0]['goal'])

    def test_unknown_delivery_cannot_be_retried(self):
        run=self.control.queue(self.requirement())
        run=self.ledger.update('runs',run['id'],run['version'],{'uncertain':True},'blocked')
        with self.assertRaises(Conflict):
            self.control.mutate(f'/runs/{run["id"]}/retry',{'version':run['version']})

    def test_token_usage_counts_cache_once_and_survives_repeated_collection(self):
        from autopilot.usage import register,collect
        trace=self.root/'trace.jsonl'
        step={'type':'status','phase':'step_end','turn':1,'step':1,
              'usage':{'inputTokens':20,'outputTokens':10,'cacheReadTokens':70,'totalTokens':100}}
        trace.write_text(json.dumps(step)+'\n'+json.dumps(step)+'\n')
        register(self.ledger,self.product['id'],trace)
        collect(self.ledger);collect(self.ledger)
        self.assertEqual(self.ledger.budget_used(self.product['id'],'tokens'),100)
        with trace.open('a') as stream:
            stream.write(json.dumps({'type':'turn.completed','usage':{'input_tokens':100,'cached_input_tokens':70,'output_tokens':25,'reasoning_output_tokens':5}})+'\n')
        collect(self.ledger)
        self.assertEqual(self.ledger.budget_used(self.product['id'],'tokens'),225)

    def test_quota_pauses_queue_but_not_inspection_and_limit_raise_resumes(self):
        from autopilot.usage import register,collect
        product=self.ledger.update('products',self.product['id'],self.product['version'],
            {'policy':self.product['policy'] | {'tokens_per_day':100,'runs_per_day':5},'last_probe':time.time()},'active')
        trace=self.root/'trace.jsonl';trace.write_text(json.dumps({'type':'turn.completed','usage':{'input_tokens':100,'output_tokens':1}})+'\n')
        register(self.ledger,product['id'],trace);collect(self.ledger)
        run=self.control.queue(self.requirement());scheduler=Scheduler(self.store)
        scheduler.advance(run)
        self.assertEqual(self.ledger.get('runs',run['id'])['status'],'queued')
        self.assertIn('Token',self.ledger.get('runs',run['id'])['reason'])
        self.assertEqual(self.ledger.budget_used(product['id'],'development'),0)
        with patch.object(scheduler,'start_call') as call:
            scheduler.monitor(product)
            self.assertEqual(call.call_args.args[3],'inspect')
        saved=self.control.mutate(f'/products/{product["id"]}/configure',{'version':product['version'],
            'config':{'policy':product['policy'] | {'tokens_per_day':1000}}})
        with patch.object(scheduler,'start_call') as call:
            scheduler.advance(self.ledger.get('runs',run['id']))
            self.assertEqual(call.call_args.args[3],'plan')
        self.assertEqual(self.ledger.budget_used(product['id'],'development'),1)
        # While a task is active, limits remain editable but execution strategy stays protected.
        self.control.mutate(f'/products/{product["id"]}/configure',{'version':saved['version'],
            'config':{'policy':saved['policy'] | {'tokens_per_day':2000}}})

    def test_daily_task_limit_keeps_queue_and_midnight_restores_it(self):
        import datetime
        product=self.ledger.update('products',self.product['id'],self.product['version'],
            {'policy':self.product['policy'] | {'runs_per_day':1}},'active')
        self.assertTrue(self.ledger.budget(product['id'],'development',1))
        run=self.control.queue(self.requirement());scheduler=Scheduler(self.store)
        scheduler.advance(run)
        self.assertEqual(self.ledger.get('runs',run['id'])['status'],'queued')
        tomorrow=datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(days=1)
        with patch('autopilot.store.datetime.datetime') as date,patch.object(scheduler,'start_call'):
            date.now.return_value=tomorrow
            scheduler.advance(self.ledger.get('runs',run['id']))
        self.assertEqual(self.ledger.get('runs',run['id'])['status'],'planning')

    def test_zero_quotas_mean_unlimited_but_intervals_must_stay_positive(self):
        self.control.validate_product(self.product | {'policy':self.product['policy'] | {'runs_per_day':0,'tokens_per_day':0,'discovery_per_day':0}})
        for _ in range(3):self.assertTrue(self.ledger.budget(self.product['id'],'development',0))
        with self.assertRaises(ValueError):
            self.control.validate_product(self.product | {'policy':{'probe_seconds':0}})

    def test_completed_evaluation_can_switch_to_continuous_without_new_sample_limit(self):
        evaluation=self.ledger.create('evaluations',{'product_id':self.product['id'],'target_count':3,'run_ids':['old-1','old-2','old-3']},'pass')
        product=self.ledger.update('products',self.product['id'],self.product['version'],{'evaluation_id':evaluation['id']})
        with self.assertRaises(Conflict):self.control.queue(self.requirement())
        saved=self.control.mutate(f'/products/{product["id"]}/continuous',{'version':product['version']})
        self.assertIsNone(saved['evaluation_id'])
        run=self.control.queue(self.requirement())
        self.assertNotIn('evaluation_id',run)
        self.assertEqual(self.ledger.get('evaluations',evaluation['id']),evaluation)

    def test_missing_legacy_monitor_only_allows_fully_stopped_installation(self):
        product=self.product | {'application':'/fixture/App.app','runtime':'/fixture/runtime','app_support':'/fixture/support'}
        with patch('autopilot.thsoctop.monitor',side_effect=FileNotFoundError()),patch('autopilot.thsoctop.subprocess.check_output',return_value='/usr/bin/unrelated\n'):
            self.assertEqual(idle(product)['status'],'pass')
        with patch('autopilot.thsoctop.monitor',side_effect=FileNotFoundError()),patch('autopilot.thsoctop.subprocess.check_output',return_value='node /fixture/runtime/host.js\n'):
            self.assertEqual(idle(product)['status'],'busy')

    def test_resolution_probes_check_actual_fields_and_reject_empty_evidence(self):
        from autopilot.probes import check,validate
        probe={'path':'/ths-octop-market/api/overview','pointer':'/data/items/*/label','operator':'not_contains','expected':'seven_over'}
        self.assertTrue(check([probe],lambda _: {'ok':True,'data':{'items':[{'label':'七板以上'}]}}))
        self.assertFalse(check([probe],lambda _: {'ok':True,'data':{'items':[{'label':'seven_over'}]}}))
        self.assertFalse(check([probe],lambda _: {'ok':True,'data':{'items':[]}}))
        with self.assertRaises(ValueError):validate([probe | {'path':'https://other.invalid/write'}])

    def test_skipped_required_check_never_enters_acceptance(self):
        scheduler=Scheduler(self.store)
        run=self.control.queue(self.requirement())
        run=self.ledger.update('runs',run['id'],run['version'],{},'verifying')
        scheduler.complete_action(run,self.product,'verify',{'status':'pass','manifest':'fake','checks':[{'status':'skipped','required':True}]})
        self.assertEqual(self.ledger.get('runs',run['id'])['status'],'blocked')

    def test_missing_monitor_is_busy_not_idle(self):
        with patch('autopilot.thsoctop.monitor',side_effect=ValueError('offline')):
            self.assertEqual(idle(self.product)['status'],'busy')

    def test_artifact_tampering_changes_digest(self):
        directory=self.root/'artifact';directory.mkdir()
        (directory/'app').write_text('v1')
        first=manifest_hash(directory)
        (directory/'app').write_text('v2')
        self.assertNotEqual(first,manifest_hash(directory))

    def test_completed_pipeline_uses_real_reviews_and_promotes_once(self):
        self.product=self.control.mutate(f'/products/{self.product["id"]}/enable',{'version':self.product['version']})
        run=self.control.queue(self.requirement())
        scheduler=Scheduler(self.store)
        end=time.time()+25
        states=[]
        while time.time()<end:
            scheduler.tick()
            current=self.ledger.get('runs',run['id'])
            states.append(current['status'])
            if current['status'] in ('completed','blocked'):
                break
            time.sleep(.06)
        self.assertEqual(current['status'],'completed',current)
        self.assertIn('plan_review',states)
        self.assertIn('acceptance_review',states)
        self.assertEqual(len(self.ledger.list('releases')),1)
        self.assertEqual((self.source/'calc.py').read_text(),'value = 0\n')
        self.assertEqual(git(self.root/'baseline','show','codex/autonomous:calc.py'),'value = 1')

    def test_process_receipt_survives_scheduler_restart(self):
        scheduler=Scheduler(self.store)
        run=self.control.queue(self.requirement())
        started=scheduler.start_call('runs',run,self.product,'probe',[sys.executable,str(self.adapter)])
        restored=Scheduler(self.store)
        end=time.time()+5
        result=None
        while time.time()<end and result is None:
            result=restored.call_result(started);time.sleep(.05)
        self.assertEqual(result['status'],'pass')
        self.assertEqual(len(list((self.root/'state/autopilot/calls').iterdir())),1)
        for child in scheduler.children:
            child.wait(timeout=5)

    def release_fixture(self):
        support=self.root/'official';runtime=support/'dependencies/dsh';runtime.mkdir(parents=True)
        (runtime/'version').write_text('old')
        app=self.root/'installed.app';app.mkdir();(app/'version').write_text('old')
        profile=support/'dsh/profiles/octop';profile.mkdir(parents=True)
        (profile/'package.json').write_text('{"version":"old"}')
        (profile/'cordis.patch.yml').write_text('user-plugin-config')
        candidate=self.root/'candidate';(candidate/'runtime').mkdir(parents=True)
        (candidate/'runtime/version').write_text('new')
        (candidate/'thsoctop.app').mkdir();(candidate/'thsoctop.app/version').write_text('new')
        cp=candidate/'support/dsh/profiles/octop';cp.mkdir(parents=True)
        (cp/'package.json').write_text('{"version":"new"}')
        manifest=candidate/'manifest.json'
        manifest.write_text(json.dumps({'id':'candidate','commit':'abc','root':str(candidate),
          'checks':[{'name':'fixture acceptance','status':'pass','required':True}],
          'runtime_hash':manifest_hash(candidate/'runtime'),'app_hash':manifest_hash(candidate/'thsoctop.app')}))
        product={'runtime':str(runtime),'application':str(app),'app_support':str(support)}
        run={'id':'candidate','release_id':'release','manifest':str(manifest),'commit':'abc'}
        return {'product':product,'record':run,'state_root':str(self.root/'release-state')}

    def test_publish_and_rollback_keep_user_profile_and_restore_whole_version(self):
        request=self.release_fixture()
        with patch('autopilot.thsoctop.idle',return_value={'status':'pass','health':{'releaseId':'old'}}), \
             patch('autopilot.thsoctop.quit_app'),patch('autopilot.thsoctop.open_app'), \
             patch('autopilot.thsoctop.wait_health',return_value={'host':'ready'}):
            self.assertEqual(publish(request)['status'],'pass')
            self.assertEqual(Path(request['product']['runtime'],'version').read_text(),'new')
            self.assertEqual(Path(request['product']['application'],'version').read_text(),'new')
            self.assertEqual(rollback(request)['status'],'pass')
            self.assertEqual(rollback(request)['status'],'pass')
        self.assertEqual(Path(request['product']['runtime'],'version').read_text(),'old')
        self.assertEqual(Path(request['product']['application'],'version').read_text(),'old')
        self.assertEqual(Path(request['product']['app_support'],'dsh/profiles/octop/cordis.patch.yml').read_text(),'user-plugin-config')

    def test_failed_start_rolls_back_without_touching_user_data(self):
        request=self.release_fixture()
        user=Path(request['product']['app_support'])/'dsh/user-data.json';user.write_text('keep me')
        with patch('autopilot.thsoctop.idle',return_value={'status':'pass','health':{'releaseId':'old'}}), \
             patch('autopilot.thsoctop.quit_app'),patch('autopilot.thsoctop.open_app'), \
             patch('autopilot.thsoctop.wait_health',side_effect=[ValueError('new version failed'),{'host':'ready'}]):
            self.assertEqual(publish(request)['status'],'fail')
        self.assertEqual(Path(request['product']['application'],'version').read_text(),'old')
        self.assertEqual(user.read_text(),'keep me')

    def test_interrupted_switch_recovers_from_journal(self):
        request=self.release_fixture()
        root=Path(request['state_root'])/'deployments/release';root.mkdir(parents=True)
        root.joinpath('journal.json').write_text(json.dumps({'step':'switching','previous_release':'old'}))
        runtime=Path(request['product']['runtime']);runtime.rename(root/'previous-runtime')
        with patch('autopilot.thsoctop.quit_app'),patch('autopilot.thsoctop.open_app'), \
             patch('autopilot.thsoctop.wait_health',return_value={'host':'ready'}):
            self.assertEqual(publish(request)['status'],'fail')
        self.assertEqual(runtime.joinpath('version').read_text(),'old')

    def test_busy_before_switch_does_not_modify_installation(self):
        request=self.release_fixture()
        with patch('autopilot.thsoctop.idle',return_value={'status':'busy','reason':'recording'}):
            self.assertEqual(publish(request)['status'],'busy')
        self.assertEqual(Path(request['product']['application'],'version').read_text(),'old')

    def test_stale_release_and_failed_acceptance_never_modify_installation(self):
        request=self.release_fixture()
        request['product']['repository']=self.product['repository']
        request['record']['base_commit']='stale'
        self.assertEqual(publish(request)['status'],'blocked')
        request['record']['base_commit']=git(self.product['repository'],'rev-parse','HEAD')
        path=Path(request['record']['manifest']);manifest=json.loads(path.read_text())
        manifest['checks'][0]['status']='fail';path.write_text(json.dumps(manifest))
        self.assertEqual(publish(request)['status'],'blocked')
        self.assertEqual(Path(request['product']['application'],'version').read_text(),'old')


if __name__=='__main__':unittest.main()
