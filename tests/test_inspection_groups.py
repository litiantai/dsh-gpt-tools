"""多业务线证据、UI 依赖及真实双仓库联合实例回归。"""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'dsh-gpt-supervisor/scripts')]
from review_core import Store
from autopilot.store import Ledger
from autopilot.inspection_routes import validate_routes, execute_route, finish_plan, fingerprint
from autopilot.project_groups import save_design, runnable, delivery_gate, joint_signature, joint_execute
from autopilot.workspace import git


def route(action='navigate', **step):
    return {'key':'reports','title':'报告生成','business_line':'报告','environment':'isolated','preconditions':[],
            'source_evidence':['server.py'], 'steps':[{'title':'生成并查看报告','action':action,'path':'/',
            'expected':'报告包含真实业务结果','wait_seconds':2, **step}]}


class Cases(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.store=Store(self.root/'state');self.ledger=Ledger(self.store)
        self.product=self.ledger.create('products',{'name':'前端','project_role':'frontend','goal':'报告','project_config':{}},'active')

    def test_master_rejects_mutation_external_and_undeclared_api(self):
        for definition in (route('click',selector='button'),route('navigate',path='//remote/'),route('read_api',path='/logout')):
            definition['environment']='master'
            with self.assertRaises(ValueError):validate_routes([definition],self.product)
        valid=route('observe');valid['environment']='master';validate_routes([valid],self.product)

    def test_failed_design_keeps_previous_immutable_version(self):
        plan=self.ledger.create('inspection_plans',{'product_id':self.product['id'],'receipts':[{'call_id':'1'}]},'running')
        finish_plan(self.ledger,plan,{'status':'pass','source_commit':'abc','routes':[route()]})
        old=self.ledger.list('inspection_route_versions')
        broken=self.ledger.create('inspection_plans',{'product_id':self.product['id'],'receipts':[{'call_id':'2'}]},'running')
        finish_plan(self.ledger,broken,{'status':'blocked','reason':'模型超时'})
        self.assertEqual(old,self.ledger.list('inspection_route_versions'))
        self.assertEqual(self.ledger.list('inspection_routes')[0]['current_version_id'],old[0]['id'])
        self.assertEqual(fingerprint(self.product|{'master_environment':{'commit':'a','pid':1}}),fingerprint(self.product|{'master_environment':{'commit':'a','pid':2}}))

    def test_observation_is_not_pass_and_steps_survive_interruption(self):
        ledger=self.ledger;pid=self.product['id']
        class Driver:
            def __init__(self,root):pass
            def open(self,*args):pass
            def screenshot(self,path):
                path.write_bytes(b'fixture');return {'text':'页面加载中','url':'http://127.0.0.1:9/','errors':[]}
            def send(self,value):
                if value['command']=='inspect_action':
                    assert len(ledger.scoped('evidence',pid))>=1
                    return {}
                return {'settled':True,'matched':True}
            def close(self):pass
        snapshot={'id':'version','route_id':'route','definition':route()}
        result=execute_route(ledger,self.product,{'id':'round','target_commit':'c'},snapshot,{'origin':'http://127.0.0.1:9','commit':'c'},self.root/'proof',Driver)
        self.assertEqual(result['status'],'observed')
        saved=ledger.get('inspections',result['inspection_id']);self.assertEqual(saved['judgement_status'],'pending')
        self.assertEqual(len(saved['steps']),2)
        class Broken(Driver):
            def send(self,value):raise RuntimeError('中断')
        result=execute_route(ledger,self.product,{'id':'round','target_commit':'c'},snapshot,{'origin':'http://127.0.0.1:9','commit':'c'},self.root/'broken',Broken)
        self.assertEqual(result['status'],'blocked')
        self.assertGreaterEqual(len(ledger.get('inspections',result['inspection_id'])['steps']),2)

    def make_group(self):
        backend=self.ledger.create('products',{'name':'后端','project_role':'backend'},'active')
        group=self.ledger.create('requirement_groups',{'product_id':self.product['id'],'members':[],'design_version_ids':[]},'designing')
        runs=[]
        for p,role in ((self.product,'frontend'),(backend,'backend')):
            req=self.ledger.create('requirements',{'product_id':p['id'],'title':role},'pending')
            runs.append(self.ledger.create('runs',{'product_id':p['id'],'requirement_id':req['id'],'group_id':group['id'],'design_workflow':True,'commit':role},'planning'))
        group=self.ledger.update('requirement_groups',group['id'],group['version'],{'members':[{'run_id':r['id'],'project_id':r['product_id'],'requirement_id':r['requirement_id'],'role':role} for r,role in zip(runs,['frontend','backend'])]})
        return group,runs

    def test_ui_precedes_api_and_all_plans_precede_implementation(self):
        group,(front,back)=self.make_group()
        self.assertTrue(runnable(self.ledger,front));self.assertFalse(runnable(self.ledger,back))
        front=save_design(self.ledger,front,'design_ui',{'design':{'html':'<h1>报告</h1>','fields':['report.title'],'states':['normal','loading','empty','error']}})
        self.assertTrue(runnable(self.ledger,back));self.assertFalse(runnable(self.ledger,front))
        back=save_design(self.ledger,back,'design_api',{'design':{'contract':'{"title":"string"}'}})
        self.assertTrue(runnable(self.ledger,front))
        front=self.ledger.get('runs',front['id']);front=self.ledger.update('runs',front['id'],front['version'],{'plan_approved':True},'developing')
        self.assertFalse(runnable(self.ledger,front))
        back=self.ledger.update('runs',back['id'],back['version'],{'plan_approved':True},'developing')
        self.assertTrue(runnable(self.ledger,front))

    def test_changed_design_replans_before_development_and_review_artifact_is_complete(self):
        from autopilot.scheduler import Scheduler
        from autopilot.project_groups import review_designs
        group,(front,back)=self.make_group()
        html='<h1>'+('完整 UI 内容'*4000)+'</h1>'
        front=save_design(self.ledger,front,'design_ui',{'design':{'html':html,'fields':['title'],'states':['normal','loading','empty','error']}})
        front=self.ledger.update('runs',front['id'],front['version'],{'plan_design_ids':[],'plan_approved':True},'developing')
        scheduler=Scheduler(self.store)
        with patch.object(scheduler,'start_call') as start:
            result=scheduler.advance(front)
            start.assert_not_called()
        self.assertEqual(result['status'],'planning')
        references=review_designs(self.ledger,result)
        self.assertEqual(json.loads(Path(references[0]['path']).read_text())['content']['html'],html)
        self.assertNotIn('content',references[0])

    def test_candidate_change_invalidates_joint_proof_and_provider_merges_first(self):
        group,runs=self.make_group()
        runs=[self.ledger.update('runs',r['id'],r['version'],{},'accepted') for r in runs]
        mark=joint_signature(self.ledger,group)
        group=self.ledger.update('requirement_groups',group['id'],group['version'],{'joint_result':{'status':'pass'},'joint_signature':mark})
        self.assertEqual(delivery_gate(self.ledger,runs),'')
        self.assertIn('提供项目',delivery_gate(self.ledger,[runs[0]],merging=True))
        self.ledger.update('runs',runs[1]['id'],runs[1]['version'],{'commit':'repaired'})
        self.assertIn('联合验收',delivery_gate(self.ledger,runs))

    def test_legacy_desktop_lifecycle_keeps_login_entry_out_of_ledger(self):
        from contextlib import nullcontext
        from unittest.mock import Mock
        import plistlib
        from autopilot.inspection_instance import desktop_instance
        workspace=self.root/'legacy';app=workspace/'apps/desktop/src-tauri/target/release/bundle/macos/thsoctop.app'
        (app/'Contents/MacOS').mkdir(parents=True);(app/'Contents/Info.plist').write_bytes(plistlib.dumps({}))
        (app/'Contents/MacOS/thsoctop').write_text('fixture')
        root=self.root/'isolated';support=root/'support';support.mkdir(parents=True)
        home=support/'dsh';(home/'thsoctop').mkdir(parents=True);(home/'thsoctop/credentials.json').write_text('{}')
        ready=support/'host-ready.private.json'
        env={'THSOCTOP_APP_SUPPORT':str(support),'THSOCTOP_DSH_HOME':str(home),'THSOCTOP_RUNTIME_DIR':str(root/'runtime')}
        proc=Mock(pid=123);proc.poll.return_value=None
        def spawn(*args,**kwargs):
            ready.write_text(json.dumps({'url':'http://127.0.0.1:9999/?token=private-test-token'}));return proc
        request={'state_root':str(self.store.state/'autopilot'),'product':self.product,'record':{'id':'round','target_commit':'abc'}}
        with patch('autopilot.test_instance.slot',return_value=nullcontext()),patch('autopilot.thsoctop.command',return_value={'status':'pass'}),patch('autopilot.thsoctop.bind_runtime_sdk'),patch('autopilot.thsoctop.stage_runtime',return_value=env),patch('autopilot.inspection_instance.subprocess.run'),patch('autopilot.inspection_instance.subprocess.Popen',side_effect=spawn),patch('autopilot.sandbox.restrict',side_effect=lambda argv,*args:argv),patch('autopilot.inspection_instance.git',return_value='abc'),patch('autopilot.isolated.stop') as stop,patch('autopilot.test_instance.record') as record:
            with desktop_instance(request,workspace,root) as instance:
                self.assertEqual(instance['commit'],'abc');self.assertTrue(instance['auth_ready']);self.assertIn('token=',instance['entry_url'])
            stop.assert_called_once_with(proc)
            self.assertTrue(all('entry_url' not in call.args[2] for call in record.call_args_list))
            self.assertEqual(record.call_args.args[2]['status'],'stopped')

    def test_api_only_route_never_opens_browser_and_captures_response(self):
        definition=route('read_api',path='/report',expected_text='real report')
        with patch('autopilot.inspection_routes.api_evidence',return_value={'ok':True,'status':200,'text':'real report'}),patch('autopilot.computer_use.BrowserDriver') as browser:
            result=execute_route(self.ledger,self.product,{'id':'r','target_commit':'c'},{'id':'v','route_id':'r','definition':definition},{'origin':'http://127.0.0.1:9','commit':'c'},self.root/'api')
            browser.assert_not_called()
        self.assertEqual(result['status'],'pass')
        evidence=self.ledger.scoped('evidence',self.product['id'])[0]
        self.assertNotIn('screenshot',evidence);self.assertEqual(evidence['details']['response']['status'],200)

    def test_timeout_records_last_screen_without_resubmitting(self):
        actions=[]
        class Driver:
            def __init__(self,root):pass
            def open(self,*args):pass
            def screenshot(self,path):path.write_bytes(b'fixture');return {'text':'loading'}
            def send(self,value):
                actions.append(value['command'])
                return {'settled':False,'matched':False}
            def close(self):pass
        result=execute_route(self.ledger,self.product,{'id':'r','target_commit':'c'},{'id':'v','route_id':'r','definition':route('click',selector='button',wait_text='ready')},{'origin':'http://127.0.0.1:9','commit':'c'},self.root/'timeout',Driver)
        self.assertEqual(result['status'],'blocked');self.assertEqual(actions,['inspect_action','inspect_wait'])
        self.assertEqual(self.ledger.scoped('evidence',self.product['id'])[-1]['details']['phase'],'timeout')

    def test_one_route_failure_does_not_skip_other_routes(self):
        from autopilot.inspection_routes import execute_round
        from contextlib import nullcontext
        snapshots=[{'id':str(i),'route_id':str(i),'definition':route()} for i in range(2)]
        record=self.ledger.create('inspection_rounds',{'product_id':self.product['id'],'target_commit':'c','snapshots':snapshots},'running')
        results=[{'route_id':str(i),'inspection_id':str(i),'status':'blocked' if i==0 else 'pass'} for i in range(2)]
        with patch('autopilot.inspection_routes.isolated_instance',return_value=nullcontext({'commit':'c'})),patch('autopilot.inspection_routes.execute_route',side_effect=results) as run:
            execute_round({'state_root':str(self.store.state/'autopilot'),'product':self.product,'record':record})
        self.assertEqual(run.call_count,2);self.assertEqual(len(self.ledger.get('inspection_rounds',record['id'])['route_results']),2)

    def test_partial_merge_keeps_success_and_completes_only_after_final_acceptance(self):
        from autopilot.project_groups import tick
        from autopilot.scheduler import Scheduler
        group,runs=self.make_group();scheduler=Scheduler(self.store)
        for r in runs:
            self.ledger.update('runs',r['id'],r['version'],{},'accepted')
        group=self.ledger.update('requirement_groups',group['id'],group['version'],{'joint_signature':joint_signature(self.ledger,group),'joint_result':{'status':'pass'}})
        back=self.ledger.get('runs',runs[1]['id']);self.ledger.update('runs',back['id'],back['version'],{'merge_sha':'backend-merge'},'online')
        with patch.object(scheduler,'start_call') as start:tick(scheduler);start.assert_not_called()
        self.assertEqual(self.ledger.get('requirement_groups',group['id'])['status'],'partially_merged')
        self.assertEqual(delivery_gate(self.ledger,[runs[0]],merging=True),'')
        front=self.ledger.get('runs',runs[0]['id']);self.ledger.update('runs',front['id'],front['version'],{'merge_sha':'frontend-merge'},'online')
        tick(scheduler);self.assertEqual(self.ledger.get('requirement_groups',group['id'])['status'],'awaiting_final_acceptance')
        for r in runs:
            req=self.ledger.get('requirements',r['requirement_id']);self.ledger.update('requirements',req['id'],req['version'],{'final_acceptance':{'status':'pass'}})
        tick(scheduler);self.assertEqual(self.ledger.get('requirement_groups',group['id'])['status'],'completed')

    def test_real_two_repositories_and_async_browser_evidence(self):
        group,runs=self.make_group()
        for run in runs:
            repo=self.root/run['product_id'];repo.mkdir();backend=run is runs[1]
            code='''import os,sys,json,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  if self.path=="/data":
   time.sleep(.25); data=b'{"title":"real-backend-report"}'
  else:
   origin=os.environ.get('BACKEND_ORIGIN','')
   data=("<html><body><div id='result'>loading</div><button onclick=\\\"document.querySelector('#result').textContent='generating';fetch('"+origin+"/data').then(r=>r.json()).then(r=>document.querySelector('#result').textContent=r.title)\\\">Generate</button><script>setTimeout(()=>document.querySelector('#result').textContent='ready',250)</script></body></html>").encode()
  self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Access-Control-Allow-Origin','*');self.end_headers();self.wfile.write(data)
ThreadingHTTPServer(('127.0.0.1',int(sys.argv[1])),Handler).serve_forever()
'''
            (repo/'server.py').write_text(code)
            git(repo,'init','-b','feat-fixture');git(repo,'config','user.name','Fixture');git(repo,'config','user.email','test@localhost');git(repo,'add','.');git(repo,'commit','-m','fixture')
            current=self.ledger.get('runs',run['id']);self.ledger.update('runs',run['id'],current['version'],{'workspace':str(repo),'branch':'feat-fixture','commit':git(repo,'rev-parse','HEAD')},'accepted')
            product=self.ledger.get('products',run['product_id']);self.ledger.update('products',product['id'],product['version'],{'project_config':{'commands':{'start':[[sys.executable,'server.py','{port}']]},'startup_timeout':5},'test_execution':'local'})
        definition=route();definition['steps']=[{'title':'加载数据','action':'navigate','path':'/','expected':'数据就绪','wait_text':'ready','wait_seconds':3,'expected_text':'ready'}, {'title':'生成报告','action':'click','selector':'button','expected':'后端真实报告','wait_text':'real-backend-report','expected_text':'real-backend-report','wait_seconds':3}]
        design=self.ledger.create('design_versions',{'product_id':runs[1]['product_id'],'kind':'api','content':{'bindings':[{'consumer_id':runs[0]['product_id'],'provider_id':runs[1]['product_id'],'environment_variable':'BACKEND_ORIGIN'}],'joint_routes':[{'project_id':runs[0]['product_id'],'route':definition}]}},'ready')
        group=self.ledger.update('requirement_groups',group['id'],group['version'],{'design_version_ids':[design['id']]})
        result=joint_execute({'state_root':str(self.store.state/'autopilot'),'record':group})
        self.assertEqual(result['status'],'pass',result)
        evidence=self.ledger.scoped('evidence',runs[0]['product_id'])
        self.assertEqual([e['details']['phase'] for e in evidence],['before','after','settled','before','after','settled'])
        self.assertTrue(all(Path(e['screenshot']).stat().st_size>100 for e in evidence))
        self.assertIn('generating',evidence[4]['actual']);self.assertIn('real-backend-report',evidence[5]['actual'])
        self.assertEqual(len(result['instances']),2)

if __name__=='__main__':unittest.main()
