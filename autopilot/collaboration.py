"""阶段边界的角色邮箱：持久化提问、回复、依赖与幂等续跑。"""
from .role_skills import rule as skill_rule
import hashlib
import json
import time

from review_core import Conflict
from .intelligence_worker import object_schema, TEXT
from .store import redact

ROLES = ('discovery', 'implementation', 'verification', 'acceptance', 'human')
REQUEST_SCHEMA = object_schema({'to_role':{'type':'string','enum':list(ROLES)}, 'topic':TEXT, 'content':TEXT,
                                'evidence_ids':{'type':'array','items':TEXT}})
INSTRUCTION = (skill_rule('collaboration-collaboration-1'))


def context(ledger, run):
    return [{k:r.get(k) for k in ('id','from_role','to_role','topic','content','reply','evidence_ids','status','updated')}
            for r in ledger.scoped('agent_messages', run['product_id'], run_id=run['id'])]


def request_reply(ledger, run, requests, from_role, call_id, db, parent=None):
    if not isinstance(requests, list) or len(requests) != 1:
        raise ValueError('每次等待必须提交一个协作问题')
    request = requests[0]
    if request.get('to_role') not in ROLES or not all(isinstance(request.get(k),str) and request[k].strip() for k in ('topic','content')):
        raise ValueError('协作接收角色、主题或内容无效')
    ident = 'message-' + hashlib.sha256((call_id+':question').encode()).hexdigest()
    existing = db.execute('SELECT * FROM auto_agent_messages WHERE id=?', (ident,)).fetchone()
    if existing:
        return ledger.decode(existing)
    messages = ledger.scoped('agent_messages', run['product_id'], db, run_id=run['id'])
    topic = parent['topic'] if parent else request['topic'].strip()[:200]
    previous = [m for m in messages if m['topic'] == topic]
    ancestors = [from_role]
    current = parent
    while current:
        ancestors.append(current['from_role'])
        current = next((m for m in messages if m['id'] == current.get('parent_message_id')), None)
    human = request['to_role'] == 'human' or request['to_role'] in ancestors or len(previous) >= 3
    reason = '协作依赖形成环，需要人工处理' if request['to_role'] in ancestors else '同一问题超过三轮，需要人工处理' if len(previous)>=3 else '等待用户回复' if human else '等待角色回复'
    ids = request.get('evidence_ids', [])
    if not isinstance(ids, list) or not all(isinstance(i,str) for i in ids):
        raise ValueError('证据引用无效')
    for evidence_id in ids:
        evidence = ledger.get('evidence', evidence_id, db)
        if evidence['product_id'] != run['product_id']:
            raise ValueError('协作证据必须属于同一项目')
    item = ledger.create('agent_messages', redact({'product_id':run['product_id'], 'run_id':run['id'],
        'requirement_id':run['requirement_id'], 'from_role':from_role, 'to_role':request['to_role'], 'topic':topic,
        'content':request['content'], 'evidence_ids':ids, 'parent_message_id':parent['id'] if parent else None,
        'workspace':run.get('workspace'), 'budget_kind':'tokens', 'reason':reason,
        'receipts':[], 'transitions':[{'status':'needs_human' if human else 'queued','at':time.time()}]}),
        'needs_human' if human else 'queued', ident=ident, db=db)
    if human:
        surface(ledger, item, db)
    return item


def surface(ledger, item, db):
    from .intelligence import create_conversation
    ident = 'collaboration-' + item['run_id']
    if not db.execute('SELECT 1 FROM auto_conversations WHERE id=?', (ident,)).fetchone():
        create_conversation(ledger, item['product_id'], '任务协作待答', db, ident)
    mid = 'question-' + item['id']
    if not db.execute('SELECT 1 FROM auto_chat_messages WHERE id=?', (mid,)).fetchone():
        ledger.create('chat_messages', {'product_id':item['product_id'], 'conversation_id':ident, 'role':'system',
            'content':item['topic']+'\n'+item['content'], 'agent_message_id':item['id'], 'run_id':item['run_id']}, 'completed', ident=mid, db=db)


def wait_run(scheduler, run, action, result, call_id):
    ledger = scheduler.ledger
    if not ledger.get('products',run['product_id']).get('intelligence',{}).get('collaboration_enabled',False):
        return scheduler.block(run, 'Agent 协作未启用，请启用后重试阶段')
    role = {'plan':'implementation','develop':'implementation','verify':'verification','review':'acceptance'}.get(action,'discovery')
    with ledger.store.transaction() as db:
        run = ledger.get('runs',run['id'],db)
        message = request_reply(ledger,run,result.get('collaboration_requests'),role,call_id,db)
        return ledger.update('runs',run['id'],run['version'],{'collaboration_wait':message['id'],
            'collaboration_resume_status':run['status'], 'collaboration_action':action, 'collaboration_processed_call':call_id,
            'reason':message['reason'], 'review_id':None if action=='review' else run.get('review_id')},'waiting_for_reply',db)


def reply(ledger, item, content, db, author=None):
    if not isinstance(content,str) or not content.strip() or len(content)>20000:
        raise ValueError('回复必须为 1–20000 字符')
    item = ledger.update('agent_messages',item['id'],item['version'],{'reply':redact(content),
        'reply_role':author or item['to_role'], 'replied_at':time.time(), 'reason':'',
        'transitions':item.get('transitions',[])+[{'status':'replied','at':time.time()}]},'replied',db)
    if item.get('parent_message_id'):
        parent = ledger.get('agent_messages',item['parent_message_id'],db)
        ledger.update('agent_messages',parent['id'],parent['version'],{'reason':'已收到补充回复，等待继续处理'},'queued',db)
    else:
        run = ledger.get('runs',item['run_id'],db)
        if run.get('collaboration_wait') == item['id'] and run['status']=='waiting_for_reply':
            ledger.update('runs',run['id'],run['version'],{'collaboration_wait':None,'reason':'','call':None,'coordinator_human':None,'coordination_no_progress':0 if author=='human' else run.get('coordination_no_progress',0)},run['collaboration_resume_status'],db)
    return item


def human_reply(ledger, pid, ident, body, db):
    from .intelligence import owned
    item = owned(ledger,'agent_messages',ident,pid,db)
    if body.get('version') != item['version'] or item['status']!='needs_human':
        raise Conflict('问题已变化或已被回答')
    result = reply(ledger,item,body.get('content'),db,'human')
    conversation_id = 'collaboration-' + item['run_id']
    ledger.create('chat_messages', {'product_id':pid,'conversation_id':conversation_id,'role':'user',
        'content':result['reply'],'agent_message_id':ident},'completed',db=db)
    return result


def finish(scheduler, item, result):
    ledger = scheduler.ledger
    with ledger.store.transaction() as db:
        item = ledger.get('agent_messages',item['id'],db)
        call_id = item['receipts'][-1]['call_id']
        if item.get('processed_call') == call_id:
            return
        item = ledger.update('agent_messages',item['id'],item['version'],{'processed_call':call_id,
            'transitions':item.get('transitions',[])+[{'status':'consumed','at':time.time()}]},'consumed',db)
        if result.get('status') == 'waiting_for_reply':
            run = ledger.get('runs',item['run_id'],db)
            child = request_reply(ledger,run,result.get('collaboration_requests'),item['to_role'],call_id,db,parent=item)
            ledger.update('agent_messages',item['id'],item['version'],{'waiting_on':child['id']},'waiting_for_reply',db)
        elif result.get('status') == 'pass' and result.get('summary'):
            reply(ledger,item,result['summary'],db)
        else:
            item = ledger.update('agent_messages',item['id'],item['version'],{'reason':result.get('reason') or '角色未提供回复'},'needs_human',db)
            surface(ledger,item,db)
