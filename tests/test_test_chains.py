"""版本基准、持续观测、取消及必要验收阻断的行为回归。"""
import base64
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'dsh-gpt-supervisor/scripts')]
from review_core import Store, Conflict
from autopilot.api import Control
from autopilot.test_chains import enqueue_generation, revise, make_run, settings
from autopilot.test_chain_worker import finish_generation, verification_checks
from autopilot.computer_use import execute_run

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
DEFINITION = {'title':'异步提交', 'steps':[{'id':'submit','goal':'提交一次生成报告','expected':'出现完成报告',
    'loop':True, 'observation_action':'none','acceptance_indices':[0]}]}


class Clock:
    def __init__(self): self.value = 0
    def now(self): return self.value
    def sleep(self, seconds): self.value += seconds


class Driver:
    instances = []
    def __init__(self, root): self.actions=[]; self.closed=False; self.__class__.instances.append(self)
    def open(self, origin, config): pass
    def screenshot(self, path): Path(path).write_bytes(PNG); return {'url':'http://127.0.0.1:12345'}
    def act(self, action): self.actions.append(action)
    def close(self): self.closed=True


class TestChainsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.control=Control(Store(self.root/'state')); self.ledger=self.control.ledger
        self.product=self.ledger.create('products', {'name':'测试', 'source':str(self.root),'goal':'验收',
            'computer_use':{'enabled':True, 'interval_seconds':1, 'timeout_seconds':60, 'max_observations':5, 'max_actions':5},
            'agents':{'verification':{'provider':'codex','model':'test'}}}, 'active')
        self.req=self.ledger.create('requirements', {'product_id':self.product['id'],'title':'报告','acceptance':['报告完成'],
            'confirmation_required':True,'confirmation_status':'confirmed','in_scope':True}, 'queued')
        self.chain=enqueue_generation(self.ledger,self.product,self.req)
        self.chain=finish_generation(self.ledger,self.chain,{'status':'pass','definition':copy.deepcopy(DEFINITION)})
        self.record={'id':'task','workspace':str(self.root),'branch':'feat-test','commit':'abc','requirement_id':self.req['id'],'title':'报告验收'}
        self.request={'state_root':str(self.root/'state/autopilot'),'product':self.product,'record':self.record,'requirement':self.req}
        self.instance={'origin':'http://127.0.0.1:12345','commit':'abc','branch':'feat-test','stopped':False}
        self.prefix='/products/'+self.product['id']+'/test-chains'

    def tearDown(self): self.temp.cleanup()

    def run_chain(self, decisions, **kwargs):
        run=make_run(self.ledger,self.product,[self.chain],self.record)
        clock=Clock(); iterator=iter(decisions)
        def judge(request, material, images):
            item=next(iterator)
            if callable(item): return item(run)
            return {'status':'pass','decision':item,'actual':'截图中的实际结果', 'action':{'type':'click','x':10,'y':10}}
        result=execute_run(self.request,run,self.instance,driver_factory=Driver,judge=judge,sleep=clock.sleep,clock=clock.now,**kwargs)
        return self.ledger.get('test_chain_runs',run['id']),result

    def test_generation_is_idempotent_and_covers_acceptance(self):
        same=enqueue_generation(self.ledger,self.product,self.req)
        self.assertEqual(same['id'],self.chain['id']); self.assertEqual(len(self.ledger.list('test_chains')),1)
        other=self.ledger.create('requirements',self.req | {'title':'另一需求','acceptance':['一','二']},'queued')
        chain=enqueue_generation(self.ledger,self.product,other)
        result=finish_generation(self.ledger,chain,{'status':'pass','definition':DEFINITION})
        self.assertEqual(result['status'],'blocked')

    def test_versions_preserve_expectations_and_require_explicit_replacement(self):
        original=self.ledger.get('test_chain_versions',self.chain['current_version_id'])
        changed=copy.deepcopy(DEFINITION);changed['steps'][0]['expected']='另一个结果'
        with self.assertRaisesRegex(ValueError,'替代'):
            revise(self.ledger,self.chain,changed,self.req['id'])
        updated=revise(self.ledger,self.chain,changed,self.req['id'],{'submit':'submit'})
        self.assertNotEqual(updated['current_version_id'],original['id'])
        self.assertEqual(self.ledger.get('test_chain_versions',original['id'])['definition'],DEFINITION)
        with self.assertRaises(Conflict): revise(self.ledger,self.chain,DEFINITION,self.req['id'])

    def test_loop_submits_once_then_observes_and_keeps_evidence(self):
        run,result=self.run_chain(['act','observe','observe','pass'])
        self.assertEqual(result['status'],'pass');self.assertEqual(run['observations'],2)
        self.assertEqual(len(Driver.instances[-1].actions),1);self.assertTrue(Driver.instances[-1].closed)
        self.assertEqual(len(run['evidence_ids']),5)
        confirmed=self.control.mutate(self.prefix+'/runs/'+run['id']+'/confirm-baseline',{'version':run['version']})
        self.assertTrue(confirmed['baseline_confirmed']);self.assertEqual(len(self.ledger.list('test_chain_baselines')),1)
        self.control.mutate(self.prefix+'/runs/'+run['id']+'/confirm-baseline',{'version':confirmed['version']})
        self.assertEqual(len(self.ledger.list('test_chain_baselines')),1)

    def test_loop_rejects_second_action(self):
        run,result=self.run_chain(['act','observe','act'])
        self.assertEqual(result['status'],'blocked');self.assertEqual(len(Driver.instances[-1].actions),1)

    def test_loop_can_only_open_explicit_progress_page(self):
        value=copy.deepcopy(DEFINITION)
        value['steps'][0].update(observation_action='navigate',observation_url='/reports/progress')
        self.chain=revise(self.ledger,self.chain,value,self.req['id'])
        run,result=self.run_chain(['act','observe','pass'])
        self.assertEqual(result['status'],'pass')
        self.assertEqual(Driver.instances[-1].actions,[{'type':'click','x':10,'y':10},{'type':'navigate','url':'/reports/progress'}])

    def test_progress_page_cannot_navigate_outside_instance(self):
        for url in ['https://example.com','//example.com','/\\example.com','/\n/example.com']:
            value=copy.deepcopy(DEFINITION);value['steps'][0].update(observation_action='navigate',observation_url=url)
            with self.subTest(url=url), self.assertRaises(ValueError):
                revise(self.ledger,self.chain,value,self.req['id'])

    def test_timeout_and_failure_cannot_confirm(self):
        for decisions,expected in [(['observe']*5,'timeout'),(['fail'],'fail')]:
            with self.subTest(expected=expected):
                run,result=self.run_chain(decisions)
                self.assertEqual(result['status'],expected)
                with self.assertRaises(ValueError):
                    self.control.mutate(self.prefix+'/runs/'+run['id']+'/confirm-baseline',{'version':run['version']})

    def test_cancellation_stops_without_action(self):
        def cancel(run):
            item=self.ledger.get('test_chain_runs',run['id'])
            self.control.mutate(self.prefix+'/runs/'+run['id']+'/cancel',{'version':item['version']})
            return {'decision':'act','actual':'准备提交','action':{'type':'click','x':10,'y':10}}
        run,result=self.run_chain([cancel]);self.assertEqual(result['status'],'cancelled')
        self.assertEqual(Driver.instances[-1].actions,[])

    def test_wrong_commit_is_blocked(self):
        self.instance['commit']='different'
        run,result=self.run_chain([]);self.assertEqual(result['status'],'blocked');self.assertEqual(run['evidence_ids'],[])

    def test_model_timeout_is_environment_block_not_business_failure(self):
        run=make_run(self.ledger,self.product,[self.chain],self.record)
        def judge(*args): raise TimeoutError('model timeout')
        result=execute_run(self.request,run,self.instance,driver_factory=Driver,judge=judge)
        self.assertEqual(result['status'],'blocked');self.assertIn('模型判断超时',result['reason'])

    def test_lost_instance_blocks_before_business_action(self):
        self.instance['pid']=99999999
        run,result=self.run_chain([])
        self.assertEqual(result['status'],'blocked');self.assertIn('失联',result['reason'])
        self.assertEqual(Driver.instances[-1].actions,[])

    def test_wrong_branch_or_project_is_blocked(self):
        for field, value in [('branch','feat-other'),('product_id','other')]:
            with self.subTest(field=field):
                original=copy.deepcopy(self.instance);self.instance[field]=value
                run,result=self.run_chain([])
                self.assertEqual(result['status'],'blocked');self.assertEqual(run['evidence_ids'],[])
                self.instance=original

    def test_confirmed_actuals_are_available_but_do_not_replace_current_evidence(self):
        run,_=self.run_chain(['pass'])
        self.control.mutate(self.prefix+'/runs/'+run['id']+'/confirm-baseline',{'version':run['version']})
        chain=self.ledger.get('test_chains',self.chain['id'])
        current=make_run(self.ledger,self.product,[chain],self.record)
        def judge(request,material,images):
            self.assertEqual(material['baseline_observations'][0]['actual'],'截图中的实际结果')
            self.assertEqual(len(images),1)
            return {'decision':'fail','actual':'本次截图结果错误'}
        result=execute_run(self.request,current,self.instance,driver_factory=Driver,judge=judge)
        self.assertEqual(result['status'],'fail')
        self.assertEqual(self.ledger.get('test_chain_runs',run['id'])['status'],'pass')

    def test_mutated_screenshot_cannot_confirm_baseline(self):
        run,_=self.run_chain(['pass'])
        evidence=self.ledger.get('evidence',run['evidence_ids'][0]);Path(evidence['screenshot']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'校验'):
            self.control.mutate(self.prefix+'/runs/'+run['id']+'/confirm-baseline',{'version':run['version']})

    def test_project_boundaries(self):
        other=self.ledger.create('products', {'name':'other'})
        with self.assertRaisesRegex(ValueError,'当前项目'):
            self.control.mutate('/products/'+other['id']+'/test-chains/'+self.chain['id']+'/link',{'version':self.chain['version'],'requirement_id':self.req['id']})

    def test_failed_chain_is_required_even_with_backlog_policy(self):
        request=self.request | {'product':self.product | {'functional_findings_policy':'backlog'}}
        with patch('autopilot.test_chain_worker.execute_run',return_value={'status':'fail','reason':'截图显示错误'}):
            checks=verification_checks(request,self.instance)
        self.assertTrue(checks[0]['required']);self.assertEqual(checks[0]['status'],'fail')
        self.assertFalse(checks[0]['retryable'])

    def test_run_keeps_original_version_after_edit(self):
        run=make_run(self.ledger,self.product,[self.chain],self.record)
        changed=copy.deepcopy(DEFINITION);changed['steps'].append({'id':'new','goal':'查看详情','expected':'显示详情','loop':False,'acceptance_indices':[]})
        revise(self.ledger,self.chain,changed,self.req['id'])
        self.assertEqual(len(self.ledger.get('test_chain_runs',run['id'])['snapshots'][0]['definition']['steps']),1)

    def test_ui_only_confirm_waits_for_chain_then_queues_development(self):
        from autopilot.intake import draft
        value={'title':'纯界面','goal':'显示按钮','scope':'主页','scenario':'打开主页','impact':'可用性','evidence':'用户需求','acceptance':['显示按钮'],'questions':[]}
        with self.ledger.store.transaction() as db:
            requirement=draft(self.ledger,self.product['id'],value,'chat','ui-case',db)
        confirmed=self.control.mutate('/requirements/'+requirement['id']+'/confirm',{'version':requirement['version']})
        self.assertEqual(confirmed['status'],'awaiting_ui_chain')
        chain=next(c for c in self.ledger.list('test_chains') if requirement['id'] in c['requirement_ids'])
        ready=finish_generation(self.ledger,chain,{'status':'pass','definition':DEFINITION})
        self.assertEqual(ready['status'],'ready')
        requirement=self.ledger.get('requirements',requirement['id'])
        self.assertEqual(requirement['status'],'queued');self.assertTrue(requirement['ui_acceptance'])
        self.assertEqual(len([r for r in self.ledger.list('runs') if r.get('requirement_id')==requirement['id']]),1)

    def test_shared_auxiliary_slot_prevents_parallel_model_dispatch(self):
        from autopilot.scheduler import Scheduler
        from autopilot.test_chain_worker import tick
        from autopilot.coordinator import auxiliary_busy
        other=self.ledger.create('requirements',{'product_id':self.product['id'],'title':'另一个','acceptance':['另一结果']},'queued')
        enqueue_generation(self.ledger,self.product,other)
        self.ledger.create('chat_messages',{'product_id':self.product['id'],'call':{'id':'busy'}},'running')
        self.assertTrue(auxiliary_busy(self.ledger,self.product['id']))
        scheduler=Scheduler(self.ledger.store)
        with patch.object(scheduler,'start_call') as start:
            tick(scheduler);start.assert_not_called()

    def test_generation_receipt_recovered_once_after_restart(self):
        from autopilot.scheduler import Scheduler
        from autopilot.test_chain_worker import tick
        other=self.ledger.create('requirements',{'product_id':self.product['id'],'title':'恢复','acceptance':['结果']},'queued')
        chain=enqueue_generation(self.ledger,self.product,other)
        self.ledger.update('test_chains',chain['id'],chain['version'],{'receipts':[{'call_id':'receipt','result':{'status':'pass','definition':DEFINITION}}]},'running')
        scheduler=Scheduler(self.ledger.store);tick(scheduler);tick(scheduler)
        recovered=self.ledger.get('test_chains',chain['id'])
        self.assertEqual(recovered['status'],'ready');self.assertEqual(recovered['processed_call'],'receipt')
        self.assertEqual(len([v for v in self.ledger.list('test_chain_versions') if v['chain_id']==chain['id']]),1)

    def test_lost_session_is_blocked_without_replaying(self):
        from autopilot.scheduler import Scheduler
        from autopilot.test_chain_worker import tick
        run=make_run(self.ledger,self.product,[self.chain],self.record)
        self.ledger.update('test_chain_runs',run['id'],run['version'],{'pid':99999999},'observing')
        scheduler=Scheduler(self.ledger.store)
        with patch.object(scheduler,'start_call') as start:
            tick(scheduler);start.assert_not_called()
        self.assertEqual(self.ledger.get('test_chain_runs',run['id'])['status'],'blocked')

    def test_new_contracts_load_with_skill_snapshot(self):
        from autopilot.role_skills import output_schema,rule,bind
        for action,name in [('computer_step','computer_use-verification-1'),('computer_generate','test_chain_worker-verification-1')]:
            schema=output_schema(action);instruction=rule(name)
            folder=self.root/action;folder.mkdir()
            text,manifest=bind(action,folder,{'provider':'codex','model':'test'},instruction,schema)
            self.assertEqual(manifest['role'],'verification')
            self.assertTrue((folder/'role-skill/output.schema.json').exists())
            self.assertTrue(any(f['path'].endswith(action+'.json') for f in manifest['files']))

if __name__=='__main__': unittest.main()
