"""应用自动恢复：端口归属、持久化退避和操作互斥。"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parents[1]/'dsh-gpt-supervisor/scripts')]
from autopilot.runtime_recovery import processes,current_endpoint,RuntimeFault,step,busy
from autopilot.api import Control
from autopilot.scheduler import Scheduler
from review_core import Store,Conflict


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.app=self.root/'Sample App.app';self.app.mkdir()
        self.product={'id':'fixture','name':'fixture','source':str(self.root),'goal':'test','status':'active',
            'application':str(self.app),'app_support':str(self.root/'support'),'runtime':str(self.root/'runtime'),
            'runtime_recovery':{'enabled':True},'adapter':['fixture']}

    def tearDown(self):self.tmp.cleanup()

    def test_process_matching_excludes_isolated_instances_and_command_substrings(self):
        p=self.product
        entry=p['runtime']+'/node_modules/@thsoctop/host/lib/index.js'
        output=f"1 {self.app}/Contents/MacOS/thsoctop\n2 /usr/bin/node {entry} --port 57201 --app-support {p['app_support']} --runtime-dir {p['runtime']} --exit-on-stdin-close\n3 /usr/bin/node {entry} --app-support {p['app_support']}-candidate --runtime-dir {p['runtime']} --exit-on-stdin-close\n4 /bin/echo {self.app}/Contents/MacOS/thsoctop\n"
        with patch('autopilot.runtime_recovery.subprocess.check_output',return_value=output):
            self.assertEqual(processes(p),{'apps':[1],'hosts':[2]})

    def test_endpoint_uses_current_owned_port_and_identity_without_credentials(self):
        current={'apps':[1],'hosts':[2]}
        with patch('autopilot.runtime_recovery.processes',return_value=current),patch('autopilot.runtime_recovery.listening_ports',return_value=[57203]),patch('autopilot.thsoctop.http',return_value={'ok':True,'data':{'product':'thsoctop','version':'1'}}) as request:
            self.assertEqual(current_endpoint(self.product),'http://127.0.0.1:57203')
            request.assert_called_once_with('http://127.0.0.1:57203/ths-octop/api/status')
        with patch('autopilot.runtime_recovery.processes',return_value=current),patch('autopilot.runtime_recovery.listening_ports',return_value=[57203]),patch('autopilot.thsoctop.http',return_value={'ok':True,'data':{'product':'other','version':'1'}}):
            with self.assertRaises(RuntimeFault):current_endpoint(self.product)
        with patch('autopilot.runtime_recovery.processes',side_effect=[current,{'apps':[1],'hosts':[9]}]),patch('autopilot.runtime_recovery.listening_ports',return_value=[57203]),patch('autopilot.thsoctop.http',return_value={'ok':True,'data':{'product':'thsoctop','version':'1'}}):
            with self.assertRaises(RuntimeFault):current_endpoint(self.product)

    def test_stopped_application_never_uses_historical_port(self):
        with patch('autopilot.runtime_recovery.processes',return_value={'apps':[],'hosts':[]}),patch('autopilot.thsoctop.http') as request:
            with self.assertRaisesRegex(RuntimeFault,'APP_STOPPED'):current_endpoint(self.product)
            request.assert_not_called()

    def test_start_wait_backoff_and_cooldown_survive_new_scheduler(self):
        with patch('autopilot.thsoctop.probe',return_value={'status':'blocked','reason':'应用未运行'}),patch('autopilot.runtime_recovery.processes',return_value={'apps':[],'hosts':[]}),patch('autopilot.runtime_recovery.subprocess.run') as launch:
            product=dict(self.product)
            def advance(now):
                result=step(product,now);product['runtime_recovery_state']=json.loads(json.dumps(result['runtime_recovery_state']));return result
            self.assertEqual(advance(100)['runtime_recovery_state']['phase'],'starting')
            self.assertEqual(advance(105)['runtime_recovery_state']['phase'],'starting');self.assertEqual(launch.call_count,1)
            self.assertEqual(advance(190)['runtime_recovery_state']['next_attempt_at'],250)
            advance(249);self.assertEqual(launch.call_count,1)
            advance(250);self.assertEqual(launch.call_count,2)
            self.assertEqual(advance(340)['runtime_recovery_state']['next_attempt_at'],460)
            advance(460);self.assertEqual(launch.call_count,3)
            state=advance(550)['runtime_recovery_state'];self.assertEqual(state['phase'],'cooldown');self.assertEqual(state['next_attempt_at'],1450)
            advance(1449);self.assertEqual(launch.call_count,3)
            self.assertEqual(advance(1450)['runtime_recovery_state']['attempts'],1)

    def test_health_not_process_existence_confirms_recovery_and_resets_after_five_minutes(self):
        product=self.product|{'runtime_recovery_state':{'phase':'starting','started_at':100,'attempts':2}}
        with patch('autopilot.thsoctop.probe',return_value={'status':'blocked','reason':'应用服务尚未就绪'}),patch('autopilot.runtime_recovery.processes',return_value={'apps':[1],'hosts':[]}),patch('autopilot.runtime_recovery.subprocess.run') as launch:
            self.assertEqual(step(product,120)['runtime_recovery_state']['phase'],'starting')
            self.assertEqual(step(product,190)['runtime_recovery_state']['phase'],'backoff');launch.assert_not_called()
        with patch('autopilot.thsoctop.probe',return_value={'status':'pass','monitor_mode':'basic','release_monitor_available':False}):
            result=step(product,200);self.assertFalse(result['release_monitor_available'])
            product['runtime_recovery_state']=result['runtime_recovery_state']
            self.assertEqual(step(product,499)['runtime_recovery_state']['attempts'],2)
            self.assertEqual(step(product,500)['runtime_recovery_state']['attempts'],0)

    def test_pause_and_disabled_do_not_launch_or_probe(self):
        with patch('autopilot.thsoctop.probe') as probe,patch('autopilot.runtime_recovery.subprocess.run') as launch:
            for product in [self.product|{'status':'paused'},self.product|{'runtime_recovery':{'enabled':False}}]:
                self.assertEqual(step(product,100)['runtime_recovery_state']['phase'],'disabled')
            probe.assert_not_called();launch.assert_not_called()

    def test_manual_requests_are_serialized_and_publication_blocks_recovery(self):
        control=Control(Store(self.root/'state'))
        product=control.ledger.create('products',self.product,'active')
        path=f'/products/{product["id"]}/recover-runtime'
        requested=control.mutate(path,{'version':product['version']})
        with self.assertRaises(Conflict):control.mutate(path,{'version':requested['version']})
        product=control.ledger.update('products',product['id'],requested['version'],{'runtime_recovery_state':{}})
        control.ledger.create('releases',{'product_id':product['id']},'rollback_pending')
        self.assertTrue(busy(control.ledger,product['id']))
        with self.assertRaises(Conflict):control.mutate(path,{'version':product['version']})

    def test_launch_failure_is_reported_and_backed_off(self):
        with patch('autopilot.thsoctop.probe',return_value={'status':'blocked'}),patch('autopilot.runtime_recovery.processes',return_value={'apps':[],'hosts':[]}),patch('autopilot.runtime_recovery.subprocess.run',side_effect=subprocess.CalledProcessError(1,['open'])):
            result=step(self.product,100)
            self.assertEqual(result['runtime_recovery_state']['next_attempt_at'],160)
            self.assertIn('error_info',result)
