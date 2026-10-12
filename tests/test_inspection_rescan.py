"""固定时刻补扫、场景证据匹配及巡查串行调度回归。"""
import datetime as dt
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'dsh-gpt-supervisor/scripts')]
from review_core import Store, Conflict
from autopilot.api import Control
from autopilot.inspection_rescan import enqueue, schedule, finish
from autopilot.inspection_routes import tick
from autopilot.scheduler import Scheduler


class RescanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.control = Control(Store(Path(self.tmp.name)/'state')); self.ledger = self.control.ledger
        self.midnight = dt.datetime(2026,10,12,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        with patch('autopilot.store.time.time', return_value=self.midnight):
            self.product = self.ledger.create('products', {'name':'复扫','source':self.tmp.name,
                'adapter':['fixture'], 'master_environment':{'commit':'abc'}}, 'active')
        self.exception = self.ledger.signal(self.product['id'], {'source':'inspection','code':'ROUTE',
            'component':'route', 'summary':'登录场景','evidence':{'status':'blocked','reason':'超时'}})
        route = self.ledger.create('inspection_routes', {'product_id':self.product['id'],'enabled':True}, 'ready', 'route')
        version = self.ledger.create('inspection_route_versions', {'product_id':self.product['id'],'route_id':route['id'],
            'definition':{'title':'登录','environment':'master','steps':[{'wait_seconds':1}]}}, 'ready')
        self.ledger.update('inspection_routes',route['id'],route['version'],{'current_version_id':version['id']})

    def test_nine_am_once_late_recovery_and_new_day(self):
        self.assertIsNone(schedule(self.ledger,self.product,self.midnight+8*3600))
        record = schedule(self.ledger,self.product,self.midnight+9*3600)
        self.assertEqual(record['target_commit'],'abc')
        self.assertEqual(schedule(self.ledger,self.product,self.midnight+18*3600)['id'],record['id'])
        self.assertEqual(schedule(self.ledger,self.product,self.midnight+32*3600)['id'],record['id'])
        self.assertNotEqual(schedule(self.ledger,self.product,self.midnight+33*3600)['id'],record['id'])
        self.assertEqual(len(self.ledger.scoped('inspection_rounds',self.product['id'])),2)

    def test_manual_rescan_reuses_active_request_and_protects_disabled_project(self):
        first = self.control.mutate(f"/products/{self.product['id']}/inspection-routes/rescan",{})
        self.assertEqual(self.control.mutate(f"/products/{self.product['id']}/inspection-routes/rescan",{})['id'],first['id'])
        self.ledger.update('products',self.product['id'],self.product['version'],{'automation_disabled':True})
        with self.assertRaises(Conflict):enqueue(self.ledger,self.product)

    def test_classification_does_not_stop_retrying_unresolved_environment_issue(self):
        source = self.ledger.update('signals',self.exception['id'],self.exception['version'],
            {'requirement_id':'environment-req','attribution_pending':False},'classified')
        record = schedule(self.ledger,self.product,self.midnight+9*3600)
        self.assertIn(source['id'],record['exception_signal_ids'])
        finish(self.ledger,record,{'status':'blocked','routes':[{'route_id':'route','status':'blocked','reason':'仍无法登录'}]})
        current = self.ledger.get('signals',source['id'])
        self.assertEqual(current['status'],'classified')
        self.assertEqual(current['requirement_id'],'environment-req')
        self.assertTrue(current['attribution_pending'])

    def test_update_drain_defers_without_losing_daily_retry(self):
        drain = self.ledger.store.state/'autopilot/update-drain.json'
        drain.parent.mkdir(parents=True,exist_ok=True);drain.write_text('{}')
        result = schedule(self.ledger,self.product,self.midnight+9*3600)
        self.assertEqual(result['status'],'deferred')
        self.assertEqual(self.ledger.scoped('inspection_rounds',self.product['id']),[])
        drain.unlink()
        self.assertEqual(schedule(self.ledger,self.product,self.midnight+10*3600)['status'],'queued')

    def test_each_exception_requires_its_own_route_evidence(self):
        unrelated = self.ledger.signal(self.product['id'],{'source':'inspection','code':'ROUTE','component':'missing',
            'summary':'覆盖缺口','evidence':{'status':'fail'}})
        record = enqueue(self.ledger,self.product)
        finish(self.ledger,record,{'status':'pass','routes':[{'route_id':'route','status':'observed','inspection_id':'new'}]})
        source = self.ledger.get('signals',self.exception['id'])
        self.assertEqual(source['rescan_status'],'collected')
        self.assertTrue(source['attribution_pending'])
        self.assertEqual(source['evidence']['reason'],'超时')
        self.assertEqual(self.ledger.get('signals',unrelated['id'])['rescan_status'],'blocked')
        finish(self.ledger,record,{'status':'pass','routes':[{'route_id':'route','status':'pass'}]})
        self.assertEqual(self.ledger.get('signals',self.exception['id'])['rescan_status'],'recovered')

    def test_queued_rescan_does_not_overlap_running_round(self):
        running = self.ledger.create('inspection_rounds',{'product_id':self.product['id'],'receipts':[]},'running')
        queued = enqueue(self.ledger,self.product)
        scheduler = Scheduler(self.ledger.store)
        with patch('autopilot.inspection_routes.enqueue',return_value=None),patch.object(scheduler,'start_call') as start:
            tick(scheduler);start.assert_not_called()
            self.ledger.update('inspection_rounds',running['id'],running['version'],{},'completed')
            start.side_effect = lambda kind,item,*args,**kwargs:item
            tick(scheduler)
            self.assertEqual(start.call_args.args[1]['id'],queued['id'])
            self.assertEqual(start.call_count,1)

    def test_legacy_rescan_runs_adapter_and_persists_result(self):
        legacy = self.ledger.create('products',{'name':'旧项目','source':self.tmp.name,'adapter':['fixture']},'active')
        source = self.ledger.signal(legacy['id'],{'source':'inspection','code':'HEALTH','summary':'旧巡查',
            'evidence':{'status':'blocked'}})
        record = enqueue(self.ledger,legacy)
        self.assertTrue(record['legacy_inspection'])
        scheduler = Scheduler(self.ledger.store)
        with patch('autopilot.inspection_routes.enqueue',return_value=None),patch.object(scheduler,'start_call',side_effect=lambda kind,item,*args,**kwargs:item) as start:
            tick(scheduler)
            call = next(c for c in start.call_args_list if c.args[1]['id']==record['id'])
            self.assertEqual(call.args[3:5],('inspect',['fixture']))
        finish(self.ledger,record,{'status':'pass','checks':[{'status':'pass'}]})
        self.assertEqual(self.ledger.get('signals',source['id'])['rescan_status'],'recovered')

    def test_queued_rescan_uses_runtime_version_at_dispatch(self):
        record = enqueue(self.ledger,self.product)
        self.ledger.update('products',self.product['id'],self.product['version'],{'master_environment':{'commit':'updated'}})
        scheduler = Scheduler(self.ledger.store)
        with patch('autopilot.inspection_routes.enqueue',return_value=None),patch.object(scheduler,'start_call',side_effect=lambda kind,item,*args,**kwargs:item) as start:
            tick(scheduler)
        call = next(c for c in start.call_args_list if c.args[1]['id']==record['id'])
        self.assertEqual(call.args[1]['target_commit'],'updated')

    def test_shared_instance_failure_is_recorded_without_rebuilding_for_every_route(self):
        from autopilot.inspection_routes import execute_round
        snapshots = [{'id':str(i),'route_id':str(i),'definition':{'title':str(i),'business_line':'业务','environment':'isolated'}} for i in range(3)]
        record = self.ledger.create('inspection_rounds',{'product_id':self.product['id'],'target_commit':'abc','snapshots':snapshots},'running')
        with patch('autopilot.inspection_routes.isolated_instance',side_effect=ValueError('启动测试失败')) as start:
            result = execute_round({'state_root':str(self.ledger.store.state/'autopilot'),'product':self.product,'record':record})
        start.assert_called_once()
        self.assertEqual(len(result['routes']),3)
        self.assertTrue(all(r['status']=='blocked' for r in result['routes']))


if __name__ == '__main__':unittest.main()
