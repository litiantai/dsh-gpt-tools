"""项目对话、竞品研究与协作的管理接口。"""
import base64
import time
import uuid
from urllib.parse import urlsplit, parse_qs

from review_core import Conflict
from .store import redact
from .intake import draft

RESOURCES = {'conversations', 'chat_messages', 'competitors', 'research_jobs', 'source_snapshots', 'agent_messages', 'requirements'}


def route(control, path):
    url = urlsplit(path)
    parts = url.path.strip('/').split('/')
    if len(parts) < 3 or parts[0] != 'products' or parts[2] != 'intelligence':
        raise KeyError('接口不存在')
    product = control.ledger.get('products', parts[1])
    return product, parts[3:], {k: v[-1] for k, v in parse_qs(url.query).items()}


def owned(ledger, kind, ident, product_id, db=None):
    item = ledger.get(kind, ident, db)
    if item['product_id'] != product_id:
        raise KeyError('记录不属于此项目')
    return item


def get(control, path):
    product, parts, query = route(control, path)
    ledger, pid = control.ledger, product['id']
    if parts == ['settings']:
        from .intelligence_worker import capabilities, selected_role
        return {'config': product.get('intelligence', {}), 'version': product['version'],
                'capabilities': {role: capabilities(selected_role(product, role)) for role in ('chat', 'research')}}
    if len(parts) == 2 and parts[0] == 'attachments':
        from .attachments import read
        item, attachment_path = read(ledger, pid, parts[1])
        return item | {'data_url': 'data:' + item['mime'] + ';base64,' + base64.b64encode(attachment_path.read_bytes()).decode()}
    if len(parts) == 2 and parts[0] in RESOURCES:
        return owned(ledger, parts[0], parts[1], pid)
    if len(parts) == 1 and parts[0] in RESOURCES:
        filters = {k: query[k] for k in ('conversation_id', 'run_id', 'status') if k in query}
        return ledger.page(parts[0], pid, query.get('cursor', ''), query.get('limit', 50), **filters)
    raise KeyError('接口不存在')


def create_conversation(ledger, pid, title='新对话', db=None, ident=None):
    return ledger.create('conversations', {'product_id': pid, 'title': title[:100]}, 'open', ident=ident, db=db)


def enqueue_research(ledger, pid, action, db, schedule_key=None):
    if action not in ('find_competitors', 'analyze_competitors'):
        raise ValueError('研究动作无效')
    rows = ledger.scoped('research_jobs', pid, db)
    if schedule_key:
        old = next((r for r in rows if r.get('schedule_key') == schedule_key), None)
        if old:
            return old
    active = next((r for r in rows if r.get('action') == action and r['status'] in ('queued', 'running')), None)
    if active:
        return active
    return ledger.create('research_jobs', {'product_id': pid, 'action': action, 'schedule_key': schedule_key,
        'title': '发现新竞品' if action == 'find_competitors' else '竞品差距分析', 'receipts': []}, 'queued', db=db)


def save_competitor(ledger, pid, body, db, existing=None, automatic=False):
    from .research import normalize_url
    name, reason = str(body.get('name', '')).strip(), str(body.get('reason', '')).strip()
    urls = body.get('urls', [])
    if not name or not reason or not isinstance(urls, list) or not 1 <= len(urls) <= 10:
        raise ValueError('竞品名称、相关性说明和 1–10 个来源链接必填')
    urls = list(dict.fromkeys(normalize_url(url) for url in urls))
    key = urlsplit(urls[0]).hostname.removeprefix('www.') + '/' + name.casefold()
    match = next((r for r in ledger.scoped('competitors', pid, db) if r.get('identity') == key), None)
    if automatic:
        denied = next((r for r in ledger.scoped('competitors',pid,db) if r['status'] != 'active' and
                       (r.get('name','').casefold()==name.casefold() or set(r.get('urls',[])) & set(urls))),None)
        if denied:
            return denied
    if match and (not existing or match['id'] != existing['id']):
        if automatic:
            return match  # In particular, never revive a manually disabled or merged competitor.
        raise Conflict('该竞品已存在，请编辑或合并已有记录')
    value = {'product_id': pid, 'name': name, 'reason': reason, 'urls': urls, 'identity': key,
             'origin': 'ai' if automatic else 'human'}
    if existing:
        return ledger.update('competitors', existing['id'], existing['version'], value, db=db)
    return ledger.create('competitors', value, 'active', db=db)


def mutate(control, path, body):
    product, parts, _ = route(control, path)
    ledger, pid = control.ledger, product['id']
    if parts == ['attachments']:
        if not product.get('intelligence',{}).get('enabled',True):
            raise Conflict('项目已关闭智能协作')
        from .attachments import upload
        return upload(ledger, pid, body)
    with ledger.store.transaction() as db:
        if parts == ['settings']:
            product = ledger.get('products', pid, db)
            if body.get('version') != product['version']:
                raise Conflict('项目已更新，请刷新设置')
            config = body.get('config', {})
            if not isinstance(config, dict) or set(config) - {'enabled', 'research_enabled', 'collaboration_enabled', 'chat', 'research'}:
                raise ValueError('智能协作配置无效')
            for key in ('enabled', 'research_enabled', 'collaboration_enabled'):
                if key in config and type(config[key]) is not bool:
                    raise ValueError('开关必须为布尔值')
            from reviewers import selection
            for role in ('chat', 'research'):
                if config.get(role):
                    selection(config[role])
            result = ledger.update('products', pid, product['version'], {'intelligence': config}, db=db)
            # Seed only the matching repository, through the same project identity as onboarding.
            from pathlib import Path
            from .scan_identity import repository_key
            if repository_key(product['source']) == repository_key(str(Path(__file__).resolve().parents[1])):
                for name, url, reason in [('Devin', 'https://docs.devin.ai/work-with-devin/advanced-capabilities', '会话复盘、可复用流程与任务协调'),
                                         ('Claude Code', 'https://code.claude.com/docs/en/agent-teams', '角色邮箱、共享任务与协作追溯')]:
                    save_competitor(ledger, pid, {'name': name, 'reason': reason, 'urls': [url]}, db, automatic=True)
            return result
        if not product.get('intelligence', {}).get('enabled', True):
            raise Conflict('项目已关闭智能协作，历史记录可继续查看')
        if parts == ['conversations']:
            return create_conversation(ledger, pid, str(body.get('title') or '新对话'), db)
        if parts == ['chat_messages']:
            conversation = owned(ledger, 'conversations', body['conversation_id'], pid, db)
            text = str(body.get('content', '')).strip()
            ids = body.get('attachment_ids', [])
            if not isinstance(ids, list) or len(ids) > 5 or len(set(ids)) != len(ids):
                raise ValueError('每条消息最多五个不同附件')
            if (not text and not ids) or len(text) > 20000:
                raise ValueError('请输入内容或附件，文字最多 20000 字符')
            attachments = [owned(ledger, 'attachments', ident, pid, db) for ident in ids]
            if any(a['status'] != 'ready' for a in attachments):
                raise ValueError('请移除解析失败的附件后发送')
            if any(m['status'] in ('queued','running') for m in ledger.scoped('chat_messages', pid, db, conversation_id=conversation['id'])):
                raise Conflict('请等待当前回复完成后继续发送')
            handled = conversation_action(control, pid, conversation, text, ids, body.get('reply_to'), db)
            if handled is not None:
                return handled
            return ledger.create('chat_messages', {'product_id': pid, 'conversation_id': conversation['id'],
                'role': 'user', 'content': redact(text), 'attachment_ids': ids, 'receipts': []}, 'queued', db=db)
        if parts == ['research_jobs']:
            return enqueue_research(ledger, pid, body.get('action'), db)
        if parts == ['competitors']:
            return save_competitor(ledger, pid, body, db)
        if len(parts) == 3 and parts[0] == 'competitors':
            item = owned(ledger, 'competitors', parts[1], pid, db)
            if body.get('version') != item['version']:
                raise Conflict('竞品记录已更新')
            if parts[2] == 'edit':
                return save_competitor(ledger, pid, body, db, existing=item)
            if parts[2] in ('disable','enable'):
                return ledger.update('competitors', item['id'], item['version'], {'manual_status': True}, 'disabled' if parts[2]=='disable' else 'active', db)
            if parts[2] == 'merge':
                target = owned(ledger, 'competitors', body['target_id'], pid, db)
                if target['id'] == item['id'] or target['status'] != 'active':
                    raise ValueError('请选择其他启用的竞品作为合并目标')
                ledger.update('competitors', target['id'], target['version'], {'urls': list(dict.fromkeys(target['urls'] + item['urls']))[:10]}, db=db)
                return ledger.update('competitors', item['id'], item['version'], {'merged_into': target['id'], 'manual_status': True}, 'merged', db)
        if len(parts) == 3 and parts[0] == 'agent_messages' and parts[2] == 'reply':
            from .collaboration import human_reply
            return human_reply(ledger, pid, parts[1], body, db)
        if len(parts) == 3 and parts[0] in ('chat_messages','research_jobs') and parts[2] == 'retry':
            item = owned(ledger, parts[0], parts[1], pid, db)
            if item['version'] != body.get('version') or item['status'] != 'failed' or item.get('call'):
                raise Conflict('当前任务不能重试')
            return ledger.update(parts[0], item['id'], item['version'], {'reason': '', 'processed_call': None}, 'queued', db)
    raise KeyError('接口不存在')


def conversation_action(control, pid, conversation, text, attachments, reply_to, db):
    """将用户明确回复绑定到已展示版本，确认与排队仍使用同一事务。"""
    import re
    from .intake import mutate as requirement_action
    ledger = control.ledger
    if attachments:
        return None
    previous = owned(ledger, 'chat_messages', reply_to, pid, db) if reply_to else None
    if previous and (previous['conversation_id'] != conversation['id'] or previous['role'] == 'user'):
        raise ValueError('回复必须关联当前对话中的助手消息')
    if previous and previous.get('agent_message_id'):
        from .collaboration import human_reply
        question = owned(ledger, 'agent_messages', previous['agent_message_id'], pid, db)
        if question['status'] == 'needs_human':
            human_reply(ledger, pid, question['id'], {'version':question['version'], 'content':text}, db)
            return ledger.create('chat_messages', {'product_id':pid, 'conversation_id':conversation['id'],
                'role':'assistant', 'content':'已将回复交给负责角色，任务将从等待阶段继续。'}, 'completed', db=db)
    command = text.strip().rstrip('。！!')
    confirm = re.fullmatch(r'(确认|确认并排队|确认全部|拒绝|拒绝全部|确认第\s*([1-9][0-9]*)\s*项)', command)
    response = None
    if confirm:
        references = previous.get('requirement_versions', {}) if previous else {}
        if not references:
            response = '当前回复没有可确认的需求版本。请先说明要做的需求，核对完整内容后再确认。'
        elif len(references)>1 and command not in ('确认全部','拒绝全部') and not confirm.group(2):
            response = '这次有多项需求，请回复“确认全部”或“确认第1项”，明确要处理的范围。'
        else:
            ids = list(references)
            if confirm.group(2):
                index = int(confirm.group(2))-1
                if index >= len(ids):
                    raise ValueError('需求序号超出当前回复范围')
                ids = [ids[index]]
            results = []
            for ident in ids:
                owned(ledger, 'requirements', ident, pid, db)
                item = requirement_action(control, ident, 'reject' if command.startswith('拒绝') else 'confirm',
                                          {'version':references[ident]}, db=db)
                results.append(item['title']+'：'+('已拒绝' if item['status']=='rejected' else
                               '已确认，先调查补齐可执行验收证据' if item['classification']=='investigation' else '已确认并排队'))
            response = '\n'.join(results)
    elif command in ('发现竞品','分析竞品'):
        job = enqueue_research(ledger, pid, 'find_competitors' if command=='发现竞品' else 'analyze_competitors', db)
        subscribers = list(dict.fromkeys(job.get('conversation_ids', [])+[conversation['id']]))
        ledger.update('research_jobs', job['id'], job['version'], {'conversation_ids':subscribers}, db=db)
        response = '已排队'+job['title']+'，完成后会在这里回复。'
    elif command == '查看竞品':
        rows = ledger.scoped('competitors', pid, db)
        response = '\n\n'.join(r['name']+'（'+('启用' if r['status']=='active' else '停用或合并')+'）\n'+r['reason']+'\n'+'\n'.join(r['urls']) for r in rows) or '暂时没有竞品。回复“发现竞品”可开始研究。'
    elif command.startswith(('停用竞品 ', '启用竞品 ')):
        name = command[5:].strip()
        matches = [r for r in ledger.scoped('competitors',pid,db) if r['name']==name and r['status']!='merged']
        if len(matches)!=1:
            response = '未找到唯一匹配的竞品，请先回复“查看竞品”核对名称。'
        else:
            item = matches[0]
            ledger.update('competitors',item['id'],item['version'],{'manual_status':True},'disabled' if command.startswith('停用') else 'active',db)
            response = name+'已'+('停用' if command.startswith('停用') else '启用')+'。'
    if response is None:
        return None
    user = ledger.create('chat_messages', {'product_id':pid, 'conversation_id':conversation['id'],
        'role':'user', 'content':text, 'reply_to':reply_to, 'attachment_ids':[]}, 'completed', db=db)
    ledger.create('chat_messages', {'product_id':pid, 'conversation_id':conversation['id'],
        'role':'assistant', 'content':response, 'reply_to':user['id'],
        'requirement_versions':references if confirm and response.startswith('这次有多项') else {}}, 'completed', db=db)
    return user
