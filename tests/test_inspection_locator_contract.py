"""源码巡检契约及真实隔离浏览器回归，不读取生产凭据或派发模型任务。"""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests'),str(ROOT/'dsh-gpt-supervisor/scripts')]
import test_inspection_preparation as preparation
from autopilot.inspection_routes import source_routes, execute_route, validate_routes, json_assertions
from autopilot.inspection_preparation import prepare
from autopilot.store import Ledger
from review_core import Store


class LocatorContractTests(unittest.TestCase):
    setUp=preparation.PreparationTests.setUp
    stop=preparation.PreparationTests.stop
    def test_maintained_routes_execute_with_real_api_preparation_and_browser(self):
        prepared=prepare(ROOT,{}, {'origin':self.origin,'instance_role':'isolated'},self.root)
        ledger=Ledger(Store(self.root/'contract-evidence'))
        product=ledger.create('products',{'name':'巡检契约','project_config':{'readonly_paths':['/api/runtime-identity']}},'active')
        instance={'origin':self.origin,'commit':'c','session_bootstrap':self.spec,'inspection_bindings':prepared['bindings']}
        routes=source_routes(ROOT,product)
        results=[]
        for definition in routes:
            with self.subTest(route=definition['key']):
                snapshot={'id':'v-'+definition['key'],'route_id':definition['key'],'definition':definition}
                if definition['key']=='local-api-session-prepared-records':
                    with patch('autopilot.computer_use.BrowserDriver') as browser:
                        result=execute_route(ledger,product,{'id':'round','target_commit':'c'},snapshot,instance,self.root/definition['key'])
                        browser.assert_not_called()
                else:
                    result=execute_route(ledger,product,{'id':'round','target_commit':'c'},snapshot,instance,self.root/definition['key'])
                results.append(result)
                self.assertEqual(result['status'],'pass',result)
        evidence=ledger.scoped('evidence',product['id'])
        self.assertEqual(len(results),8)
        self.assertEqual(len([e for e in evidence if (e.get('details') or {}).get('phase')=='session']),7)
        self.assertTrue(any((e.get('details') or {}).get('assertions') for e in evidence))
        self.assertNotIn(self.app.csrf,json.dumps(evidence));self.assertNotIn(self.app.browser_key,json.dumps(evidence))
        from autopilot.inspection_session import LocalApiSession
        session=LocalApiSession(self.origin);session.initialize(self.spec)
        self.assertEqual(json.loads(session.request('/api/runs')['text']),[])
        self.assertTrue(all(p['status']=='paused' and p['automation_disabled'] for p in json.loads(session.request('/api/products')['text'])))

    def test_registration_rejects_fragile_selectors_without_rewriting_old_routes(self):
        definition=source_routes(ROOT,{'project_config':{'readonly_paths':['/api/runtime-identity']}})[1]
        definition['steps'][4]['selector']="button:text-is('接入仓库')"
        with self.assertRaisesRegex(ValueError,':text-is'):
            validate_routes([definition],{},registration=True)
        self.assertEqual(validate_routes([definition],{})[0]['steps'][4]['selector'],"button:text-is('接入仓库')")

    def test_source_route_path_cannot_escape_workspace(self):
        (self.root/'.autopilot.json').write_text(json.dumps({'inspection_routes_file':'../outside.json'}))
        with self.assertRaisesRegex(ValueError,'相对文件'):source_routes(self.root,{})

    def test_json_assertions_reject_wrong_type_missing_path_and_non_json(self):
        for body in ['{"enabled":1}','{}','not json']:
            self.assertFalse(json_assertions(body,'{"/enabled":true}')[0]['matched'])
        self.assertFalse(json_assertions('{"nested":{"enabled":1}}','{"/nested":{"enabled":true}}')[0]['matched'])
        self.assertTrue(json_assertions('{"a/b":[{"~key":false}]}','{"/a~1b/0/~0key":false}')[0]['matched'])
