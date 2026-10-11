"""只读对话和竞品研究执行器，复用配置的模型并保留真实能力回执。"""
from .role_skills import rule as skill_rule, output_schema
import json
import shutil
import subprocess
import time
from pathlib import Path

from .store import Ledger, redact


def selected_role(product, role):
    return product.get('intelligence', {}).get(role) or product.get('agents', {}).get('discovery', {})


def capabilities(role):
    provider = role.get('provider')
    binary = role.get('bin') or {'codex':'codex','claude':'claude','harness':'dsh'}.get(provider, '')
    found = shutil.which(binary) if binary else None
    result = {'provider': provider, 'model': role.get('model'), 'available': bool(found),
              'search': False, 'images': False, 'reason': '尚未配置或未找到执行器', 'checked_at': time.time()}
    if not found:
        return result
    try:
        help_text = subprocess.run([found, '--help'], capture_output=True, text=True, timeout=5).stdout
        if provider == 'codex':
            child = subprocess.run([found, 'exec', '--help'], capture_output=True, text=True, timeout=5).stdout
            result.update(search='--search' in help_text, images='--image' in child,
                          reason='已检测 CLI 接口；模型或账户实际能力以本次调用回执为准')
        elif provider == 'claude':
            result.update(search='--tools' in help_text, images='--tools' in help_text,
                          reason='通过 Read 读取图片、WebSearch 搜索；实际可用性以工具回执为准')
        else:
            result['reason'] = '当前隔离 Harness 配置仅开放只读文本工具，未检测到搜索或图片通道'
    except (OSError, subprocess.TimeoutExpired) as exc:
        result['reason'] = '执行器能力检测失败：' + str(exc)
    return result


def object_schema(properties):
    return {'type':'object', 'additionalProperties':False, 'properties':properties, 'required':list(properties)}


TEXT = {'type':'string'}
STRINGS = {'type':'array','items':TEXT}
DRAFT = object_schema({k: STRINGS if k in ('acceptance','questions') else TEXT
                       for k in ('title','goal','scenario','scope','acceptance','evidence','impact','questions')})
BASE = {'status':{'type':'string','enum':['pass','fail','blocked']}, 'reason':TEXT, 'summary':TEXT}


def run_model(action, request, role, material, instruction, properties, images=None, search=False):
    from .codex_executor import execute
    schema = json.loads(json.dumps(object_schema(BASE | properties)))
    if action == 'collaborate':
        schema['properties']['status']['enum'] = ['pass','fail','blocked','waiting_for_reply']
    result = execute(action, request | {'_intelligence': {
        'role':role, 'material':redact(material), 'instruction':instruction,
        'schema':schema, 'images': images or [], 'search':search}})
    return result | {'snapshot_at':material.get('snapshot_at',time.time())}


def search_receipt(path):
    def found(value):
        if isinstance(value,dict):
            if value.get('type') in ('web_search','web_search_call') and value.get('status') not in ('failed','error'):
                return True
            if value.get('type') == 'tool_use' and value.get('name') == 'WebSearch':
                return True
            return any(found(v) for v in value.values())
        return isinstance(value,list) and any(found(v) for v in value)
    for line in Path(path).read_text().splitlines():
        try:
            if found(json.loads(line)):
                return True
        except ValueError:
            continue
    return False


def execute(action, request):
    if action == 'coordinate':
        from .coordinator import evaluate
        return evaluate(request)
    from review_core import Store
    ledger = Ledger(Store(Path(request['state_root']).parent))
    product, record = request['product'], request['record']
    pid = product['id']
    if action == 'collaborate':
        role = product.get('agents', {}).get(record['to_role'], {})
    else:
        role = selected_role(product, 'chat' if action == 'chat' else 'research')
    if not role.get('model'):
        return {'status':'blocked','reason':'请先配置执行器与模型'}
    material = {'goal':product.get('goal'), 'project_config':product.get('project_config', {}),
                'snapshot_at':time.time(), 'known_requirements':[
                    {k:r.get(k) for k in ('id','title','status','scope','goal','scenario','acceptance','evidence','questions','confirmation_status','source','run_id')}
                    for r in ledger.scoped('requirements', pid)]}
    request = request | {'budget_kind': 'chat_tokens' if action=='chat' else
                        record.get('budget_kind','tokens') if action=='collaborate' else 'discovery_tokens'}
    if action == 'chat':
        messages = ledger.scoped('chat_messages', pid, conversation_id=record['conversation_id'])
        material['messages'] = [{k:m.get(k) for k in ('id','role','content','attachment_ids','created','requirement_ids')} for m in messages]
        material['runs'] = [{k:r.get(k) for k in ('id','requirement_id','title','status','reason','updated','summary','review_id','acceptance_review_id')}
                            for r in ledger.scoped('runs', pid)]
        material['deliveries'] = [{k:r.get(k) for k in ('id','status','reason','updated','pr_url')} for r in ledger.scoped('deliveries', pid)]
        material['attachments'], images = [], []
        from .attachments import read
        for ident in dict.fromkeys(i for m in messages for i in m.get('attachment_ids', [])):
            item, path = read(ledger, pid, ident)
            if item['status'] != 'ready':
                return {'status':'blocked','reason':item.get('reason','附件不可读取')}
            if item['mime'].startswith('image/'):
                if not capabilities(role)['images']:
                    return {'status':'blocked','reason':'当前执行器不支持图片输入，请在智能设置选择具备图片通道的执行器；未阅读图片'}
                images.append(str(path))
            material['attachments'].append({k:item.get(k) for k in ('id','name','text','mime')})
        if len(images) > 20:
            return {'status':'blocked','reason':'当前对话累计图片超过 20 张，请建立新对话'}
        return run_model(action, request, role, material,
            skill_rule('intelligence_worker-research-1'),
            output_schema('find_competitors')['properties'], images=images)
    if action == 'collaborate':
        from .collaboration import REQUEST_SCHEMA
        run = ledger.get('runs', record['run_id'])
        material.update(message=record, requirement=ledger.get('requirements', run['requirement_id']),
                        plan=run.get('plan'), summary=run.get('summary'), checks=run.get('checks'),
                        discussion=ledger.scoped('agent_messages', pid, run_id=run['id']))
        return run_model(action, request, role, material,
            skill_rule('intelligence_worker-collaboration-1'),
            output_schema('collaborate')['properties'])
    from .research import collect, fetch
    competitors = [r for r in ledger.scoped('competitors', pid) if r['status']=='active']
    material['competitors'] = competitors
    if action == 'find_competitors':
        capability = capabilities(role)
        if not capability['search']:
            return {'status':'blocked','reason':'搜索能力不可用：'+capability['reason']}
        material['excluded_competitors'] = [r for r in ledger.scoped('competitors', pid) if r['status']!='active']
        result = run_model(action, request, role, material,
            skill_rule('intelligence_worker-research-2'),
            output_schema('analyze_competitors')['properties'], search=True)
        if result.get('status') != 'pass':
            return result
        verified, failures = [], []
        for candidate in result.get('competitors', [])[:5]:
            sources = []
            for url in candidate.get('urls', [])[:10]:
                try:
                    sources.append(fetch(url))
                except Exception as exc:
                    failures.append({'url':url,'reason':str(exc)})
            if sources:
                verified.append(candidate | {'urls':[s['url'] for s in sources], 'verified_sources':sources})
        return result | {'competitors':verified,'fetch_failures':failures,
                         **({'status':'blocked','reason':'候选竞品均缺少可读取来源'} if result.get('competitors') and not verified else {})}
    if action != 'analyze_competitors':
        raise ValueError('未知研究动作')
    snapshots = collect(competitors)
    previous = ledger.scoped('source_snapshots', pid)
    known = {r['url']:r for r in previous if r['status']=='pass' and r.get('analyzed')}
    changed = [s for s in snapshots if s['status']=='pass' and s['fingerprint'] != known.get(s['url'], {}).get('fingerprint')]
    if not changed:
        failed = any(s['status']!='pass' for s in snapshots)
        return {'status':'blocked' if failed else 'pass','summary':'没有可分析的新内容' if failed else '来源无变化，未生成新需求',
                'reason':'部分来源采集失败，详见来源快照' if failed else '', 'snapshots':snapshots,'drafts':[]}
    material['sources'] = changed
    material['previous_sources'] = [known[s['url']] for s in changed if s['url'] in known]
    result = run_model(action, request, role, material,
        skill_rule('intelligence_worker-clarification-1'),
        output_schema('chat')['properties'])
    return result | {'snapshots':snapshots}
