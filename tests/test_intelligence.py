"""智能需求入口、研究和角色消息的隔离回归，不调用真实模型。"""
import base64
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'dsh-gpt-supervisor/scripts')]
from review_core import Store, Conflict
from autopilot.api import Control
from autopilot.scheduler import Scheduler
from autopilot.intake import draft
from autopilot.intelligence import save_competitor
from autopilot.intelligence_scheduler import finish, schedule
from autopilot.collaboration import wait_run, finish as finish_message
from autopilot.intelligence_worker import execute, capabilities

VALUE = {'title':'改善需求输入','goal':'手机提需求','scenario':'手机打开需求页面','scope':'需求输入页面',
         'impact':'缩短反馈时间','evidence':'用户反馈与输入截图','acceptance':['手机可输入需求'],'questions':[]}


class IntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root/'state')
        self.control = Control(self.store)
        self.ledger = self.control.ledger
        self.product = self.ledger.create('products', {'name':'项目','source':str(self.root),'repository':str(self.root),
            'goal':'改善研发体验','policy':{'deepseek_off_peak_only':False},
            'agents':{k:{'provider':'codex','model':'test'} for k in ('discovery','implementation','verification','acceptance')},
            'intelligence':{'enabled':True,'research_enabled':True,'collaboration_enabled':True}}, 'active')
        self.prefix = '/products/'+self.product['id']+'/intelligence'
        self.scheduler = Scheduler(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def act(self,path,body=None):
        return self.control.mutate(self.prefix+'/'+path,body or {})

    def make_draft(self, **values):
        with self.store.transaction() as db:
            return draft(self.ledger,self.product['id'],VALUE|values,'chat','original',db)

    def test_confirm_gate_and_immutable_amendment(self):
        item = self.make_draft()
        with self.assertRaises(Conflict):
            self.control.queue(item)
        self.product = self.ledger.update('products',self.product['id'],self.product['version'],{},'paused')
        result = self.control.mutate('/requirements/'+item['id']+'/confirm',{'version':item['version']})
        self.assertEqual(result['classification'],'investigation')
        self.assertEqual(result['confirmed_snapshot']['scope'],VALUE['scope'])
        self.assertEqual(self.ledger.list('runs'),[])
        again = self.control.mutate('/requirements/'+item['id']+'/confirm',{'version':item['version']})
        self.assertEqual(again['version'],result['version'])
        with self.assertRaises(Conflict):
            self.control.mutate('/requirements/'+item['id']+'/edit',{'version':result['version'],'draft':{'scope':'changed'}})
        amended = self.control.mutate('/requirements/'+item['id']+'/amend',{'version':result['version'],'draft':{'scope':'new scope'}})
        self.assertEqual(amended['status'],'pending_confirmation')
        self.assertEqual(amended['parent_requirement_id'],item['id'])
        self.assertEqual(self.ledger.get('requirements',item['id'])['scope'],VALUE['scope'])

    def test_confirm_development_atomic_duplicate(self):
        item = self.make_draft(resolution_probes=[{'path':'/safe','pointer':'/ok','operator':'equals','expected':True}])
        with patch('autopilot.probes.validate'):
            result = self.control.mutate('/requirements/'+item['id']+'/confirm',{'version':item['version']})
        self.assertEqual(result['status'],'queued')
        self.control.mutate('/requirements/'+item['id']+'/confirm',{'version':item['version']})
        self.assertEqual(len(self.ledger.list('runs')),1)

    def test_unresolved_questions_and_concurrent_edits(self):
        item = self.make_draft(questions=['适用用户是谁？'])
        with self.assertRaises(ValueError):
            self.control.mutate('/requirements/'+item['id']+'/confirm',{'version':item['version']})
        edited = self.control.mutate('/requirements/'+item['id']+'/edit',{'version':item['version'],'draft':{'questions':[]}})
        with self.assertRaises(Conflict):
            self.control.mutate('/requirements/'+item['id']+'/confirm',{'version':item['version']})
        self.assertEqual(edited['questions'],[])

    def test_queue_rejects_corrupted_pending_unconfirmed(self):
        item = self.make_draft()
        item = self.ledger.update('requirements',item['id'],item['version'],{},'pending')
        with self.assertRaisesRegex(Conflict,'确认'):
            self.control.queue(item)
        with patch.object(self.scheduler,'start_call') as call:
            from autopilot.progress import investigate
            investigate(self.scheduler)
            call.assert_not_called()

    def test_discovery_and_nightly_keep_confirmation_gate(self):
        signal = self.ledger.signal(self.product['id'], {'source':'competitor','code':'new','summary':'需求差距','evidence':'https://example.com/docs'})
        value = VALUE | {'signal_ids':[signal['id']],'reproduction':'打开手机','classification':'development','in_scope':True}
        for report in (None,'nightly-report'):
            rows=self.scheduler.accept_discovery(self.product,{'requirements':[value]},signal_ids=[signal['id']],report_id=report)
            self.assertEqual(rows[0]['status'],'pending_confirmation')
        self.assertEqual(self.ledger.list('runs'),[])

    def test_attachment_isolation_types_and_docx(self):
        text = self.act('attachments',{'name':'需求.md','data':base64.b64encode('手机截图需求'.encode()).decode()})
        self.assertEqual(text['text'],'手机截图需求')
        archive=io.BytesIO()
        with zipfile.ZipFile(archive,'w') as z:
            z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>文档需求</w:t></w:r></w:p></w:document>')
        document=self.act('attachments',{'name':'需求.docx','data':base64.b64encode(archive.getvalue()).decode()})
        self.assertEqual(document['text'],'文档需求')
        with self.assertRaises(ValueError):
            self.act('attachments',{'name':'bad.png','data':base64.b64encode(b'not an image').decode()})
        failed=self.act('attachments',{'name':'bad.txt','data':base64.b64encode(b'\xff\xff').decode()})
        self.assertEqual(failed['status'],'parse_failed')
        other=self.ledger.create('products',{'name':'另一个项目'},'paused')
        with self.assertRaises(KeyError):
            self.control.get('/products/'+other['id']+'/intelligence/attachments/'+text['id'])
        conversation=self.act('conversations')
        with self.assertRaises(ValueError):
            self.act('chat_messages',{'conversation_id':conversation['id'],'content':'test','attachment_ids':[failed['id']]})
        with self.assertRaises(ValueError):
            self.act('chat_messages',{'conversation_id':conversation['id'],'content':'test','attachment_ids':[str(x) for x in range(6)]})

    def test_chat_receipt_recovery_does_not_duplicate_drafts(self):
        conversation=self.act('conversations')
        item=self.act('chat_messages',{'conversation_id':conversation['id'],'content':'手机需求'})
        with self.assertRaises(Conflict):
            self.act('chat_messages',{'conversation_id':conversation['id'],'content':'并发消息'})
        result={'status':'pass','summary':'请确认需求卡','drafts':[VALUE]}
        item=self.ledger.update('chat_messages',item['id'],item['version'],{'receipts':[{'call_id':'chat-1','result':result}]},'running')
        restarted=Scheduler(self.store)
        finish(restarted,'chat_messages',item,result)
        finish(restarted,'chat_messages',item,result)
        self.assertEqual(len(self.ledger.list('requirements')),1)
        self.assertEqual(len(self.ledger.scoped('chat_messages',self.product['id'])),2)
        self.assertEqual(self.ledger.list('requirements')[0]['status'],'pending_confirmation')

    def answer(self, conversation, text, drafts):
        item=self.act('chat_messages',{'conversation_id':conversation['id'],'content':text})
        result={'status':'pass','summary':'已整理修改内容','drafts':drafts}
        item=self.ledger.update('chat_messages',item['id'],item['version'],{'receipts':[{'call_id':item['id'],'result':result}]},'running')
        finish(self.scheduler,'chat_messages',item,result)
        return self.ledger.get('chat_messages','answer-'+item['id'])

    def test_chat_confirm_displayed_version_and_revision(self):
        conversation=self.act('conversations')
        answer=self.answer(conversation,'手机需求',[VALUE])
        ident=answer['requirement_ids'][0]
        updated=self.answer(conversation,'范围改为抽屉',[VALUE|{'scope':'两侧抽屉','target_requirement_id':ident}])
        self.assertEqual(self.ledger.get('requirements',ident)['scope'],'两侧抽屉')
        with self.assertRaises(Conflict):
            self.act('chat_messages',{'conversation_id':conversation['id'],'content':'确认','reply_to':answer['id']})
        self.act('chat_messages',{'conversation_id':conversation['id'],'content':'确认','reply_to':updated['id']})
        frozen=self.ledger.get('requirements',ident)
        self.assertEqual(frozen['confirmed_snapshot']['scope'],'两侧抽屉')
        amendment=self.answer(conversation,'增加新范围',[VALUE|{'scope':'新的范围','target_requirement_id':ident}])
        child=self.ledger.get('requirements',amendment['requirement_ids'][0])
        self.assertNotEqual(child['id'],ident)
        self.assertEqual(child['parent_requirement_id'],ident)
        self.assertEqual(child['status'],'pending_confirmation')
        self.assertEqual(self.ledger.get('requirements',ident)['confirmed_snapshot']['scope'],'两侧抽屉')

    def test_chat_multiple_confirmation_needs_explicit_selection(self):
        conversation=self.act('conversations')
        answer=self.answer(conversation,'两项需求',[VALUE,VALUE|{'title':'其他需求'}])
        self.act('chat_messages',{'conversation_id':conversation['id'],'content':'确认','reply_to':answer['id']})
        self.assertTrue(all(r['status']=='pending_confirmation' for r in self.ledger.list('requirements')))
        prompt=next(r for r in self.ledger.scoped('chat_messages',self.product['id']) if r.get('content','').startswith('这次有多项'))
        self.act('chat_messages',{'conversation_id':conversation['id'],'content':'确认第 2 项','reply_to':prompt['id']})
        self.assertEqual(self.ledger.get('requirements',answer['requirement_ids'][1])['confirmation_status'],'confirmed')
        self.assertEqual(self.ledger.get('requirements',answer['requirement_ids'][0])['confirmation_status'],'pending')
        other=self.act('conversations')
        with self.assertRaises(ValueError):
            self.act('chat_messages',{'conversation_id':other['id'],'content':'确认','reply_to':answer['id']})

    def test_research_returns_to_chat_and_respects_manual_disable(self):
        conversation=self.act('conversations')
        self.act('chat_messages',{'conversation_id':conversation['id'],'content':'发现竞品'})
        job=self.ledger.list('research_jobs')[0]
        self.assertIn(conversation['id'],job['conversation_ids'])
        result={'status':'blocked','reason':'没有搜索能力'}
        job=self.ledger.update('research_jobs',job['id'],job['version'],{'receipts':[{'call_id':'research-chat','result':result}]},'running')
        finish(self.scheduler,'research_jobs',job,result)
        self.assertTrue(any(r.get('content')=='没有搜索能力' for r in self.ledger.list('chat_messages')))
        competitor=self.act('competitors',{'name':'Example','reason':'same users','urls':['https://example.com']})
        self.act('chat_messages',{'conversation_id':conversation['id'],'content':'停用竞品 Example'})
        self.assertEqual(self.ledger.get('competitors',competitor['id'])['status'],'disabled')

    def test_human_collaboration_reply_in_pure_chat(self):
        run=self.run_record()
        wait_run(self.scheduler,run,'develop',self.question('human'),'human-call')
        question=self.ledger.list('chat_messages')[0]
        self.act('chat_messages',{'conversation_id':question['conversation_id'],'content':'使用390px验收','reply_to':question['id']})
        self.assertEqual(self.ledger.get('runs',run['id'])['status'],'developing')
        self.assertEqual(self.ledger.list('agent_messages')[0]['reply'],'使用390px验收')

    def test_pdf_text_extraction_when_dependency_installed(self):
        try:
            from pypdf import PdfWriter
            from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        except ImportError:
            self.skipTest('PDF 正文提取需要 requirements.txt 中的 pypdf')
        writer=PdfWriter()
        page=writer.add_blank_page(width=300,height=300)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        content=DecodedStreamObject();content.set_data(b'BT /F1 12 Tf 20 200 Td (Mobile requirement) Tj ET')
        page[NameObject('/Contents')]=writer._add_object(content)
        data=io.BytesIO();writer.write(data)
        row=self.act('attachments',{'name':'requirement.pdf','data':base64.b64encode(data.getvalue()).decode()})
        self.assertEqual(row['status'],'ready')
        self.assertIn('Mobile requirement',row['text'])

    def test_scoped_cursor_includes_records_beyond_global_limit(self):
        with self.store.transaction() as db:
            for i in range(1005):
                self.ledger.create('conversations',{'product_id':self.product['id'],'title':str(i)},db=db)
        all_ids=[];cursor=''
        while True:
            page=self.control.get(self.prefix+'/conversations?limit=100&cursor='+cursor)
            all_ids += [r['id'] for r in page['items']]
            cursor=page['next_cursor']
            if not cursor:break
        self.assertEqual(len(set(all_ids)),1005)
        self.assertEqual(len(self.ledger.list('conversations')),1000)

    def test_competitor_disabled_not_reenabled_and_merge(self):
        one=self.act('competitors',{'name':'One','reason':'same audience','urls':['https://example.com/docs']})
        self.act('competitors/'+one['id']+'/disable',{'version':one['version']})
        with self.store.transaction() as db:
            found=save_competitor(self.ledger,self.product['id'],{'name':'One','reason':'found again','urls':['https://example.com/docs']},db,automatic=True)
        self.assertEqual(found['status'],'disabled')
        two=self.act('competitors',{'name':'Two','reason':'same audience','urls':['https://example.org/docs']})
        merged=self.act('competitors/'+found['id']+'/merge',{'version':found['version'],'target_id':two['id']})
        self.assertEqual(merged['status'],'merged')
        self.assertEqual(len(self.ledger.get('competitors',two['id'])['urls']),2)

    def test_research_schedule_latest_only_pause_and_order(self):
        now=1791507600
        schedule(self.ledger,self.product,now)
        schedule(self.ledger,self.product,now)
        self.assertEqual(len(self.ledger.list('research_jobs')),2)
        schedule(self.ledger,self.product|{'status':'paused'},now+86400)
        self.assertEqual(len(self.ledger.list('research_jobs')),2)

    def test_failed_search_and_unchanged_sources_do_not_invent_requirements(self):
        item=self.act('research_jobs',{'action':'find_competitors'})
        request={'product':self.product,'record':item,'state_root':str(self.store.state/'autopilot')}
        with patch('autopilot.intelligence_worker.capabilities',return_value={'search':False,'reason':'missing'}),patch('autopilot.intelligence_worker.run_model') as model:
            result=execute('find_competitors',request)
            self.assertEqual(result['status'],'blocked');model.assert_not_called()
        self.act('competitors',{'name':'One','reason':'related','urls':['https://example.com']})
        snapshot={'url':'https://example.com/','status':'pass','fingerprint':'abc','content':'page','competitor_id':'one'}
        self.ledger.create('source_snapshots',snapshot|{'product_id':self.product['id'],'analyzed':True},'pass')
        with patch('autopilot.research.collect',return_value=[snapshot]),patch('autopilot.intelligence_worker.run_model') as model:
            result=execute('analyze_competitors',request)
            self.assertEqual(result['drafts'],[]);model.assert_not_called()

    def test_research_failures_do_not_advance_analyzed_baseline(self):
        item=self.act('research_jobs',{'action':'analyze_competitors'})
        result={'status':'blocked','reason':'model unavailable','snapshots':[{'url':'https://example.com','status':'pass','fingerprint':'a'}]}
        item=self.ledger.update('research_jobs',item['id'],item['version'],{'receipts':[{'call_id':'research-1','result':result}]},'running')
        finish(self.scheduler,'research_jobs',item,result)
        self.assertFalse(self.ledger.list('source_snapshots')[0]['analyzed'])

    def run_record(self):
        req=self.make_draft()
        return self.ledger.create('runs',{'product_id':self.product['id'],'requirement_id':req['id'],'workspace':str(self.root)},'developing')

    def question(self,to='verification'):
        return {'status':'waiting_for_reply','collaboration_requests':[{'to_role':to,'topic':'验收条件','content':'请核实移动端验收条件','evidence_ids':[]}]}

    def test_developer_question_reply_resumes_original_stage_once(self):
        run=self.run_record()
        wait_run(self.scheduler,run,'develop',self.question(),'develop-call')
        message=self.ledger.list('agent_messages')[0]
        result={'status':'pass','summary':'应在 390px 宽度验收输入与附件'}
        message=self.ledger.update('agent_messages',message['id'],message['version'],{'receipts':[{'call_id':'reply-call','result':result}]},'delivered')
        finish_message(Scheduler(self.store),message,result)
        finish_message(Scheduler(self.store),message,result)
        self.assertEqual(self.ledger.get('runs',run['id'])['status'],'developing')
        self.assertEqual(self.ledger.get('agent_messages',message['id'])['status'],'replied')
        self.assertEqual(len(self.ledger.list('agent_messages')),1)

    def test_cycle_goes_to_human_and_nested_reply_resumes_parent(self):
        run=self.run_record()
        wait_run(self.scheduler,run,'develop',self.question(),'develop-call')
        message=self.ledger.list('agent_messages')[0]
        result=self.question('implementation')
        message=self.ledger.update('agent_messages',message['id'],message['version'],{'receipts':[{'call_id':'nested-call','result':result}]},'delivered')
        finish_message(self.scheduler,message,result)
        child=next(r for r in self.ledger.list('agent_messages') if r['id']!=message['id'])
        self.assertEqual(child['status'],'needs_human')
        self.assertIn('环',child['reason'])
        self.assertEqual(len(self.ledger.list('conversations')),1)
        self.act('agent_messages/'+child['id']+'/reply',{'version':child['version'],'content':'采用 390px'} )
        self.assertEqual(self.ledger.get('agent_messages',message['id'])['status'],'queued')
        self.assertEqual(self.ledger.get('runs',run['id'])['status'],'waiting_for_reply')

    def test_review_wait_does_not_approve(self):
        run=self.run_record()
        run=self.ledger.update('runs',run['id'],run['version'],{'review_id':'old-review'},'acceptance_review')
        result=wait_run(self.scheduler,run,'review',self.question('human'),'review-call')
        self.assertEqual(result['status'],'waiting_for_reply')
        self.assertIsNone(result['review_id'])
        self.assertNotIn('accepted_at',result)
        question=self.ledger.list('agent_messages')[0]
        self.act('agent_messages/'+question['id']+'/reply',{'version':question['version'],'content':'请重新执行验证'})
        self.assertEqual(self.ledger.get('runs',run['id'])['status'],'acceptance_review')

    def test_real_chat_worker_and_restart_without_model_service(self):
        from autopilot.intelligence_scheduler import tick
        import os
        binary = self.root/'fake-codex'
        binary.write_text('#!'+sys.executable+'\n'+"""
import json,sys
from pathlib import Path
if '--help' in sys.argv:
 print('--search --image');sys.exit(0)
prompt=sys.stdin.read()
result={'status':'pass','reason':'','summary':'独立进程完成对话','drafts':[]}
Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps(result))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':5,'output_tokens':3}}))
""")
        binary.chmod(0o700)
        product=self.ledger.get('products',self.product['id'])
        workspace=self.root/'workspace';workspace.mkdir()
        self.ledger.update('products',product['id'],product['version'],{'source':str(workspace),'repository':str(workspace),'intelligence':{'enabled':True,'chat':{'provider':'codex','model':'fake','bin':str(binary)}}},'paused')
        conversation=self.act('conversations')
        item=self.act('chat_messages',{'conversation_id':conversation['id'],'content':'项目怎样了'})
        tick(self.scheduler)
        launched=self.ledger.get('chat_messages',item['id'])
        self.assertTrue(launched.get('call'),launched)
        restarted=Scheduler(self.store)
        deadline=time.time()+15
        while time.time()<deadline:
            tick(restarted)
            current=self.ledger.get('chat_messages',item['id'])
            if current['status'] in ('completed','failed'):
                break
            time.sleep(.05)
        for child in self.scheduler.children:
            child.wait(timeout=5)
        self.assertEqual(current['status'],'completed',current)
        from autopilot.usage import collect
        collect(self.ledger);collect(self.ledger)
        self.assertEqual(self.ledger.budget_used(product['id'],'chat_tokens'),8)
        self.assertEqual(len(self.ledger.scoped('chat_messages',product['id'])),2)

    def test_research_rejects_private_sources(self):
        from autopilot.research import normalize_url,fetch
        for url in ('file:///etc/passwd','http://127.0.0.1/api','https://localhost/docs','http://10.0.0.1/a','https://user:pass@example.com'):
            with self.assertRaises(ValueError):normalize_url(url)
        with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
            with self.assertRaisesRegex(ValueError,'非公网'):fetch('https://example.com')


if __name__=='__main__':
    unittest.main()
