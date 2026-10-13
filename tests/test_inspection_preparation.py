"""真实隔离管理接口的场景准备、纯接口会话及响应引用回归。"""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'dsh-gpt-supervisor/scripts')]
from dashboard_server import Dashboard, ThreadingHTTPServer, handler_for
from autopilot.inspection_session import LocalApiSession
from autopilot.inspection_preparation import prepare, resolve, bind, configuration_for
from autopilot.inspection_routes import execute_route
from autopilot.store import Ledger
from review_core import Store


class PreparationTests(unittest.TestCase):
    def setUp(self):
        from test_dashboard import Fixture
        self.fixture=Fixture();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.app=Dashboard(self.fixture.state)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(self.app,0,ROOT/'dashboard/dist'))
        self.server.RequestHandlerClass=handler_for(self.app,self.server.server_port,ROOT/'dashboard/dist')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.stop)
        self.origin='http://127.0.0.1:'+str(self.server.server_port)
        self.spec={'path':'/api/bootstrap','probe_path':'/api/products'}
        (self.root/'.autopilot.json').write_text((ROOT/'.autopilot.json').read_text())

    def stop(self):
        self.server.shutdown();self.server.server_close();self.thread.join()

    def test_preparation_creates_real_paused_records_with_public_bindings(self):
        result=prepare(self.root,{}, {'origin':self.origin,'instance_role':'isolated'},self.root)
        receipt=json.loads(Path(result['receipt']).read_text())
        self.assertEqual(receipt['status'],'pass')
        client=LocalApiSession(self.origin);client.initialize(self.spec)
        product=json.loads(client.request('/api/products/'+result['bindings']['inspection_project_id'])['text'])
        self.assertEqual(product['status'],'paused');self.assertTrue(product['automation_disabled'])
        requirement=json.loads(client.request('/api/requirements/'+result['bindings']['inspection_requirement_id'])['text'])
        self.assertEqual(requirement['product_id'],product['id']);self.assertEqual(requirement['status'],'pending')
        self.assertEqual(json.loads(client.request('/api/runs')['text']),[])
        self.assertNotIn(self.app.csrf,Path(result['receipt']).read_text())
        self.assertNotIn(self.app.browser_key,Path(result['receipt']).read_text())
        self.assertTrue(all(step['status']=='completed' for step in receipt['steps']))
        with self.assertRaises(ValueError):prepare(self.root,{}, {'origin':self.origin,'instance_role':'isolated'},self.root)
        self.assertEqual(len(json.loads(client.request('/api/products')['text'])),1)

    def test_api_only_route_initializes_cookie_and_creates_then_reads_actual_record(self):
        prepared=prepare(self.root,{}, {'origin':self.origin,'instance_role':'isolated'},self.root)
        ledger=Ledger(Store(self.root/'evidence'));product=ledger.create('products',{'name':'巡查主项目'},'active')
        steps=[{'title':'创建待处理需求','action':'request_api','path':'/api/requirements','api_method':'POST',
            'api_body':json.dumps({'product_id':'{inspection_project_id}','title':'实际新增需求','evidence':'接口回执','acceptance':'可追溯','impact':'隔离验证'}),
            'response_bindings':[{'name':'new_requirement_id','pointer':'/id'}],'expected':'创建实际pending需求'},
            {'title':'核对实际创建结果','action':'read_api','path':'/api/requirements/{new_requirement_id}',
             'expected':'返回创建的需求并保留pending状态','expected_text':'实际新增需求'}]
        definition={'key':'actual-requirement','title':'需求接口闭环','business_line':'需求','environment':'isolated','requires_auth':True,'steps':steps}
        with patch('autopilot.computer_use.BrowserDriver') as browser:
            result=execute_route(ledger,product,{'id':'round','target_commit':'c'}, {'id':'v','route_id':'r','definition':definition},
                {'origin':self.origin,'commit':'c','session_bootstrap':self.spec,'inspection_bindings':prepared['bindings']},self.root/'route')
            browser.assert_not_called()
        self.assertEqual(result['status'],'pass',result)
        evidence=ledger.scoped('evidence',product['id'])
        self.assertEqual(evidence[0]['details']['session']['probe_status'],200)
        self.assertEqual(evidence[-1]['details']['response']['status'],200)
        request=next(x for x in evidence if (x.get('details') or {}).get('phase')=='request')
        self.assertTrue(request['details']['request']['body']['operation_id'])
        self.assertNotIn(self.app.csrf,json.dumps(evidence));self.assertNotIn(self.app.browser_key,json.dumps(evidence))

    def test_browser_route_uses_actual_prepared_project_and_requirement(self):
        prepared=prepare(self.root,{}, {'origin':self.origin,'instance_role':'isolated'},self.root)
        ledger=Ledger(Store(self.root/'evidence'));product=ledger.create('products',{'name':'巡查主项目'},'active')
        definition={'key':'prepared-requirement-ui','title':'实际隔离需求列表','business_line':'需求','environment':'isolated','requires_auth':True,
            'steps':[{'title':'进入实际创建项目的需求池','action':'navigate',
                'path':'/autopilot?project={inspection_project_id}&view=requirements&tab=requirements',
                'expected':'显示通过本轮接口实际创建的待处理需求','wait_text':'巡查准备需求','expected_text':'巡查准备需求','wait_seconds':10}]}
        result=execute_route(ledger,product,{'id':'round','target_commit':'c'}, {'id':'v','route_id':'r','definition':definition},
            {'origin':self.origin,'commit':'c','session_bootstrap':self.spec,'inspection_bindings':prepared['bindings']},self.root/'browser')
        self.assertEqual(result['status'],'pass',result)
        evidence=ledger.scoped('evidence',product['id']);screens=[x for x in evidence if x.get('screenshot')]
        self.assertTrue(screens);self.assertTrue(all(Path(x['screenshot']).stat().st_size for x in screens))
        self.assertIn(prepared['bindings']['inspection_project_id'],screens[-1]['details']['screen']['url'])

    def test_preparation_rejects_master_and_background_task_paths(self):
        with self.assertRaises(ValueError):prepare(self.root,{}, {'origin':self.origin,'instance_role':'master'},self.root)
        for path in ['/api/scans','/api/products/id/resume','//remote/api/products']:
            (self.root/'.autopilot.json').write_text(json.dumps({'inspection_setup':[{'title':'bad','path':path,'method':'POST'}]}))
            with self.subTest(path=path),self.assertRaises(ValueError):configuration_for(self.root)
        client=LocalApiSession(self.origin);client.initialize(self.spec)
        self.assertEqual(json.loads(client.request('/api/products')['text']),[])

    def test_failed_probe_is_rejected(self):
        client=LocalApiSession(self.origin)
        with self.assertRaises(ValueError):client.initialize({'path':'/api/bootstrap','probe_path':'/api/missing'})
        with self.assertRaises(ValueError):LocalApiSession('https://example.com')
        client.initialize(self.spec)
        self.assertNotIn(self.app.csrf,client.request('/api/bootstrap')['text'])

    def test_response_bindings_preserve_types_escape_json_and_reject_unknown_or_secret(self):
        bindings={};bind({'response_bindings':[{'name':'version','pointer':'/version'}]}, {'text':'{"version":3}'},bindings)
        self.assertEqual(json.loads(resolve({'api_body':'{"version": {version}}'},bindings)['api_body']),{'version':3})
        self.assertEqual(json.loads(resolve({'api_body':'{"title":"{name}"}'},{'name':'a"b'})['api_body']),{'title':'a"b'})
        with self.assertRaises(ValueError):resolve('/api/products/{missing}',bindings)
        with self.assertRaises(ValueError):bind({'response_bindings':[{'name':'secret','pointer':'/csrf'}]}, {'text':'{}'},bindings)
        with self.assertRaises(ValueError):bind({'response_bindings':[{'name':'version','pointer':'/version'}]}, {'text':'{"version":4}'},bindings)


if __name__=='__main__':unittest.main()
