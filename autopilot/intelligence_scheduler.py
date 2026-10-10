"""独立的对话、研究和邮箱任务调度，沿用调用回执与项目运行边界。"""
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import sys
import time

from review_core import Conflict
from .intelligence import enqueue_research, save_competitor
from .intelligence_worker import selected_role
from .intake import draft

KINDS = ('chat_messages','research_jobs','agent_messages')


def schedule(ledger, product, now=None):
    config = product.get('intelligence', {})
    if not config.get('research_enabled') or not config.get('enabled',True) or product['status']=='paused':
        return
    local = datetime.fromtimestamp(now or time.time(), ZoneInfo('Asia/Shanghai'))
    week = (local-timedelta(days=local.weekday())).replace(hour=1,minute=0,second=0,microsecond=0)
    if week > local:
        week -= timedelta(days=7)
    day = local.replace(hour=2,minute=0,second=0,microsecond=0)
    if day > local:
        day -= timedelta(days=1)
    with ledger.store.transaction() as db:
        for action, when in [('find_competitors',week),('analyze_competitors',day)]:
            enqueue_research(ledger, product['id'], action, db, action+':'+when.isoformat())


def finish(scheduler, kind, item, result):
    if kind == 'agent_messages':
        from .collaboration import finish as collaboration_finish
        return collaboration_finish(scheduler,item,result)
    ledger = scheduler.ledger
    with ledger.store.transaction() as db:
        item = ledger.get(kind,item['id'],db)
        call_id = item['receipts'][-1]['call_id']
        if item.get('processed_call') == call_id:
            return
        ok = result.get('status')=='pass'
        created = []
        if kind=='research_jobs':
            for index, snapshot in enumerate(result.get('snapshots',[])):
                ledger.create('source_snapshots', snapshot | {'product_id':item['product_id'],'research_job_id':item['id'],
                    'analyzed':ok}, snapshot['status'], ident=call_id+'-'+str(index), db=db)
            if ok and item['action']=='find_competitors':
                for candidate in result.get('competitors',[]):
                    if candidate.get('verified_sources'):
                        competitor = save_competitor(ledger,item['product_id'],candidate,db,automatic=True)
                        for index, source in enumerate(candidate['verified_sources']):
                            ledger.create('source_snapshots',source | {'product_id':item['product_id'], 'competitor_id':competitor['id'],
                                'research_job_id':item['id'],'analyzed':False},source['status'],db=db)
        if ok:
            for value in result.get('drafts',[])[:10]:
                rows = ledger.scoped('requirements',item['product_id'],db)
                target = value.get('target_requirement_id')
                match = next((r for r in rows if (r['id']==target if target else
                              r.get('title','').strip().casefold()==str(value.get('title','')).strip().casefold())
                              and r['status']!='rejected'),None)
                if kind=='chat_messages' and match and target:
                    # Only revisions explicitly returned against an existing, displayed requirement.
                    messages = ledger.scoped('chat_messages',item['product_id'],db,conversation_id=item['conversation_id'])
                    if not any(match['id'] in m.get('requirement_ids',[]) for m in messages):
                        raise ValueError('只能修改当前对话已展示的需求')
                    if match['status']=='pending_confirmation':
                        from .intake import mutate as requirement_action
                        from .api import Control
                        match = requirement_action(Control(ledger.store),match['id'],'edit',
                            {'version':match['version'],'draft':value},db=db)
                    else:
                        match = draft(ledger,item['product_id'],value,'chat',item['id'],db,parent_id=match['id'])
                created.append((match or draft(ledger,item['product_id'],value,
                    'chat' if kind=='chat_messages' else 'competitor',item['id'],db))['id'])
        requirements = [ledger.get('requirements',ident,db) for ident in dict.fromkeys(created)]
        versions = {r['id']:r['version'] for r in requirements if r['status']=='pending_confirmation'}
        content = (result.get('summary') or '') if ok else result.get('reason','模型未完成回复')
        for index, requirement in enumerate(requirements,1):
            if requirement['status'] != 'pending_confirmation':
                continue
            content += '\n\n'+str(index)+'. '+requirement['title']+'\n目标：'+str(requirement.get('goal',''))
            content += '\n场景：'+str(requirement.get('scenario',''))+'\n范围：'+str(requirement.get('scope',''))
            content += '\n验收：'+'；'.join(requirement.get('acceptance',[]))
            content += '\n证据：'+str(requirement.get('evidence',''))+'\n价值：'+str(requirement.get('impact',''))
            if requirement.get('questions'):
                content += '\n待澄清：'+'；'.join(requirement['questions'])
        if versions:
            content += '\n\n可以直接告诉我需要修改的地方。核对并解决待澄清事项后，回复“确认”'+('，多项需求请回复“确认全部”或“确认第1项”' if len(versions)>1 else '')+'。'
        conversations = [item['conversation_id']] if kind=='chat_messages' else item.get('conversation_ids',[])
        for conversation_id in conversations:
            ledger.create('chat_messages',{'product_id':item['product_id'],'conversation_id':conversation_id,
                'role':'assistant','content':content, 'reply_to':item['id'] if kind=='chat_messages' else None,
                'research_job_id':item['id'] if kind=='research_jobs' else None,
                'requirement_ids':created,'requirement_versions':versions,'snapshot_at':result.get('snapshot_at',item['created']),
                'provider':result.get('provider'),'model':result.get('model')},'completed' if ok else 'failed',
                ident='answer-'+call_id+('-'+conversation_id if kind=='research_jobs' else ''),db=db)
        ledger.update(kind,item['id'],item['version'],{'processed_call':call_id,'result':result,
            'requirement_ids':created,'reason':result.get('reason','')},'completed' if ok else 'failed',db)


def notify_progress(ledger, pid):
    with ledger.store.transaction() as db:
        for requirement in ledger.scoped('requirements',pid,db):
            if requirement.get('source')!='chat' or requirement['status']=='pending_confirmation':
                continue
            source = ledger.get('chat_messages',requirement['source_id'],db)
            run = ledger.get('runs',requirement['run_id'],db) if requirement.get('run_id') else None
            status = run['status'] if run else requirement['status']
            if status not in ('queued','blocked','waiting_for_reply','accepted','delivered','online','completed','rejected') or requirement.get('last_chat_status')==status:
                continue
            labels={'queued':'已排队','blocked':'已阻塞','waiting_for_reply':'等待协作回复','accepted':'业务验收通过','delivered':'已交付未上线','online':'已上线','completed':'已完成','rejected':'已拒绝'}
            ledger.create('chat_messages',{'product_id':pid,'conversation_id':source['conversation_id'],'role':'system',
                'content':requirement['title']+'：'+labels[status]+('\n'+run.get('reason','') if run else ''),
                'requirement_ids':[requirement['id']],'run_id':run['id'] if run else None,'snapshot_at':time.time()},'completed',db=db)
            ledger.update('requirements',requirement['id'],requirement['version'],{'last_chat_status':status},db=db)


def tick(scheduler):
    ledger = scheduler.ledger
    for product in ledger.list('products'):
        pid = product['id']
        if product.get('automation_disabled'):
            continue
        config = product.get('intelligence',{})
        notify_progress(ledger,pid)
        schedule(ledger,product)
        for kind in KINDS:
            for item in ledger.scoped(kind,pid):
                if item.get('call'):
                    result = scheduler.call_result(item)
                    if result is None:
                        continue
                    item = scheduler.consume(kind,item,result)
                if item.get('receipts') and item['receipts'][-1]['call_id']!=item.get('processed_call'):
                    try:
                        finish(scheduler,kind,item,item['receipts'][-1]['result'])
                    except (ValueError,KeyError,TypeError) as exc:
                        scheduler.change(kind,item,{'processed_call':item['receipts'][-1]['call_id'],'reason':'回执处理失败：'+str(exc)},'failed')
                    continue
                if item['status']!='queued' or not config.get('enabled',True):
                    continue
                if kind!='chat_messages' and product['status']=='paused':
                    continue
                if kind=='chat_messages':
                    action, role = 'chat', selected_role(product,'chat')
                elif kind=='research_jobs':
                    if item.get('schedule_key') and not config.get('research_enabled'):
                        continue
                    action, role = item['action'], selected_role(product,'research')
                    if action=='analyze_competitors' and any(r['action']=='find_competitors' and r['status'] in ('queued','running') for r in ledger.scoped(kind,pid)):
                        continue
                else:
                    if not config.get('collaboration_enabled') and not item.get('from_role') == 'coordinator':
                        continue
                    action, role = 'collaborate',product.get('agents',{}).get(item['to_role'],{})
                    run = ledger.get('runs',item['run_id'])
                    if run['status']!='waiting_for_reply':
                        continue
                    if not scheduler.tokens_available(product):
                        continue
                # A project has one auxiliary model call at a time. The primary development slot stays independent.
                from .coordinator import auxiliary_busy
                if auxiliary_busy(ledger, pid):
                    continue
                if scheduler.wait_for_off_peak(kind,item,product,action,selected=role):
                    continue
                command = [sys.executable, str(Path(__file__).resolve().parents[1]/'scripts/intelligence-worker.py')]
                item = scheduler.start_call(kind,item,product,action,command,timeout=600)
                if item.get('call'):
                    scheduler.change(kind,item,{'transitions':item.get('transitions',[])+[{'status':'delivered','at':time.time()}]}
                        if kind=='agent_messages' else {},'delivered' if kind=='agent_messages' else 'running')
