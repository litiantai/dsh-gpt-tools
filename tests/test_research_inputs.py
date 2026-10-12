"""竞品配置快照、Fake-IP 受控解析与独立重试回归。"""
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'dsh-gpt-supervisor/scripts')]
from review_core import Store
from autopilot.api import Control
from autopilot.scheduler import Scheduler
from autopilot.intelligence_scheduler import finish
from autopilot.research import public_addresses, fetch, normalize_url
from autopilot.role_skills import output_schema

class ResearchInputs(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'state');self.control=Control(self.store);self.ledger=self.control.ledger
        self.product=self.ledger.create('products',{'name':'测试','source':self.temp.name,'intelligence':{},'policy':{'deepseek_off_peak_only':False}},'paused')
        self.prefix='/products/'+self.product['id']+'/intelligence/'
        self.scheduler=Scheduler(self.store)

    def test_optional_inputs_save_without_research_and_frozen_snapshot(self):
        self.control.mutate(self.prefix+'settings',{'version':self.product['version'],'config':{'competitor_names':[' Example ','Example'],'comparison_focus':'','analysis_goal':'补齐报告'}})
        self.assertEqual(self.ledger.list('research_jobs'),[])
        job=self.control.mutate(self.prefix+'research_jobs',{'action':'find_competitors'})
        self.assertEqual(job['input_snapshot'],{'competitor_names':['Example'],'comparison_focus':'','analysis_goal':'补齐报告'})
        p=self.ledger.get('products',self.product['id']);self.control.mutate(self.prefix+'settings',{'version':p['version'],'config':{'competitor_names':[],'comparison_focus':'','analysis_goal':''}})
        self.assertEqual(self.ledger.get('research_jobs',job['id'])['input_snapshot'],job['input_snapshot'])
        self.assertIn('competitors',output_schema('find_competitors')['properties']);self.assertIn('drafts',output_schema('analyze_competitors')['properties'])

    def test_network_retry_is_new_attempt_and_old_receipt_not_replayed(self):
        job=self.control.mutate(self.prefix+'research_jobs',{'action':'find_competitors'})
        for index,delay in ((0,60),(1,300),(2,None)):
            result={'status':'blocked','reason':'执行器未能完成调用','diagnostic':'workspace routing discovery timed out'}
            current=self.ledger.get('research_jobs',job['id'])
            current=self.ledger.update('research_jobs',job['id'],current['version'],{'receipts':current.get('receipts',[])+[{'call_id':str(index),'result':result}]},'running')
            with patch('autopilot.intelligence_scheduler.time.time',return_value=1000):finish(self.scheduler,'research_jobs',current,result)
            saved=self.ledger.get('research_jobs',job['id']);self.assertEqual(len(saved['attempts']),index+1)
            self.assertEqual(saved['next_attempt_at'],1000+delay if delay else None)
            finish(self.scheduler,'research_jobs',saved,result)
            self.assertEqual(self.ledger.get('research_jobs',job['id'])['version'],saved['version'])
        retried=self.control.mutate(self.prefix+'research_jobs/'+job['id']+'/retry',{'version':saved['version']})
        self.assertEqual(retried['processed_call'],'2');self.assertEqual(retried['status'],'queued')

    def test_repaired_but_unmerged_requirement_receives_new_signal_evidence(self):
        requirement=self.ledger.create('requirements',{'product_id':self.product['id'],'title':'报告错误','signal_ids':[],'acceptance':['可读报告']},'delivered')
        signal=self.ledger.signal(self.product['id'],{'source':'inspection','code':'report','summary':'报告仍然错误','evidence':'新截图'})
        result={'requirements':[{'title':'报告错误的新描述','existing_requirement_id':requirement['id'],'signal_ids':[signal['id']],'evidence':'本轮新截图','reproduction':'打开报告','acceptance':['报告可读'],'impact':'结果不可用'}]}
        self.scheduler.accept_discovery(self.product,result)
        self.assertEqual(len(self.ledger.list('requirements')),1)
        current=self.ledger.get('requirements',requirement['id'])
        self.assertEqual(current['status'],'delivered');self.assertEqual(current['signal_ids'],[signal['id']])
        self.assertEqual(current['additional_evidence'][0]['evidence'],'本轮新截图')

    def test_authentication_failure_never_network_retries(self):
        job=self.control.mutate(self.prefix+'research_jobs',{'action':'find_competitors'})
        result={'status':'blocked','reason':'authentication failed HTTP 401'}
        job=self.ledger.update('research_jobs',job['id'],job['version'],{'receipts':[{'call_id':'auth','result':result}]},'running')
        finish(self.scheduler,'research_jobs',job,result)
        saved=self.ledger.get('research_jobs',job['id']);self.assertEqual(saved['status'],'failed');self.assertEqual(saved['result']['failure_kind'],'authentication')

    def test_fake_ip_doh_is_pinned_tls_and_rejects_private_answer(self):
        dns=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('198.18.0.1',443))]
        connection=Mock();response=connection.getresponse.return_value;response.status=200
        response.read.return_value=json.dumps({'Status':0,'Answer':[{'type':1,'data':'93.184.216.34'}]}).encode()
        context=Mock()
        with patch('autopilot.research.socket.getaddrinfo',return_value=dns),patch('autopilot.research.http.client.HTTPConnection',return_value=connection),patch('autopilot.research.socket.create_connection') as connect,patch('autopilot.research.ssl.create_default_context',return_value=context):
            self.assertEqual(public_addresses('example.com',443),['93.184.216.34'])
            self.assertEqual(connect.call_args.args[0],('1.1.1.1',443))
            self.assertEqual(context.wrap_socket.call_args.kwargs['server_hostname'],'cloudflare-dns.com')
            response.read.return_value=json.dumps({'Status':0,'Answer':[{'type':1,'data':'127.0.0.1'}]}).encode()
            with self.assertRaises(ValueError):public_addresses('example.com',443)
        with patch('autopilot.research.socket.getaddrinfo',return_value=[(2,1,6,'',('10.0.0.1',443))]),patch('autopilot.research.http.client.HTTPConnection') as connection:
            with self.assertRaises(ValueError):public_addresses('private.example',443)
            connection.assert_not_called()

    def test_multicast_and_reserved_addresses_are_not_public_sources(self):
        for url in ('http://224.0.0.1/','http://240.0.0.1/','http://[ff00::1]/','http://169.254.169.254/'):
            with self.assertRaises(ValueError):normalize_url(url)

    def test_redirect_to_private_network_is_rejected(self):
        connection=Mock();response=connection.getresponse.return_value;response.status=302;response.getheader.return_value='http://127.0.0.1/admin'
        with patch('autopilot.research.public_addresses',return_value=['93.184.216.34']),patch('autopilot.research.http.client.HTTPConnection',return_value=connection),patch('autopilot.research.socket.create_connection'):
            with self.assertRaises(ValueError):fetch('http://example.com')
            connection.close.assert_called_once()
