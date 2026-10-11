"""需求测试链路、不可变版本及人工确认基准；所有关联均限定在项目内。"""
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

from review_core import Conflict
from .store import redact

KINDS = ('test_chains', 'test_chain_versions', 'test_chain_runs', 'test_chain_baselines')
DEFAULTS = {'enabled': False, 'interval_seconds': 30, 'timeout_seconds': 1800, 'max_observations': 60, 'max_actions': 100}
TERMINAL = {'pass', 'fail', 'blocked', 'cancelled', 'timeout'}


def settings(product):
    return DEFAULTS | product.get('computer_use', {})


def validate_config(config):
    if not isinstance(config, dict) or set(config) - set(DEFAULTS) - {'model', 'allowed_origins', 'storage_state'}:
        raise ValueError('测试链路配置字段无效')
    if type(config.get('enabled', False)) is not bool:
        raise ValueError('启用状态必须为布尔值')
    for key, upper in [('interval_seconds', 3600), ('timeout_seconds', 7200), ('max_observations', 1000), ('max_actions', 1000)]:
        value = config.get(key, DEFAULTS[key])
        if type(value) is not int or not 1 <= value <= upper:
            raise ValueError(key + ' 超出允许范围')
    if config.get('model') and (not isinstance(config['model'], dict) or config['model'].get('provider') != 'codex' or not config['model'].get('model')):
        raise ValueError('测试链路需要 Codex 模型')
    origins = config.get('allowed_origins', [])
    if not isinstance(origins, list):
        raise ValueError('依赖地址必须为列表')
    for origin in origins:
        if not isinstance(origin, str):
            raise ValueError('依赖地址必须为文本')
        u = urlsplit(origin)
        if u.scheme not in ('http', 'https') or not u.netloc or u.username or u.password or u.query or u.fragment or u.path not in ('', '/'):
            raise ValueError('依赖地址必须是无凭据的 HTTP origin')
    if config.get('storage_state') and (not isinstance(config['storage_state'], str) or not Path(config['storage_state']).is_absolute()):
        raise ValueError('测试登录状态必须为本机绝对路径')


def scoped(ledger, kind, ident, pid, db=None):
    item = ledger.get(kind, ident, db)
    if item['product_id'] != pid:
        raise ValueError('记录不属于当前项目')
    return item


def requirement_hash(item):
    return hashlib.sha256(json.dumps({k:item.get(k) for k in ('title', 'goal', 'scope', 'scenario', 'acceptance')}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def enqueue_generation(ledger, product, requirement, db=None):
    if db is None:
        with ledger.store.transaction() as conn:
            return enqueue_generation(ledger, product, requirement, conn)
    from .intake import approved
    if not approved(requirement) or requirement['status'] == 'rejected':
        raise ValueError('请先确认需求')
    pid = product['id']
    if requirement['product_id'] != pid:
        raise ValueError('需求不属于当前项目')
    fingerprint = requirement_hash(requirement)
    ident = 'chain-' + hashlib.sha256((pid + requirement['id'] + fingerprint).encode()).hexdigest()[:32]
    try:
        return ledger.get('test_chains', ident, db)
    except KeyError:
        pass
    return ledger.create('test_chains', {'product_id':pid, 'title':requirement['title'],
        'requirement_ids':[requirement['id']], 'source_requirement_id':requirement['id'],
        'requirement_hash':fingerprint, 'requirement_snapshot':{k:requirement.get(k) for k in ('title','goal','scope','scenario','acceptance')},
        'enabled':True, 'receipts':[], 'current_version_id':None}, 'queued', ident=ident, db=db)


def validate_definition(value, acceptance=None):
    if not isinstance(value, dict) or not isinstance(value.get('title'), str) or not value['title'].strip():
        raise ValueError('链路标题不能为空')
    steps = value.get('steps')
    if not isinstance(steps, list) or not 1 <= len(steps) <= 100:
        raise ValueError('链路必须包含 1 到 100 个步骤')
    ids, covered = set(), set()
    for step in steps:
        if not isinstance(step, dict) or any(not isinstance(step.get(k), str) or not step[k].strip() for k in ('id','goal','expected')):
            raise ValueError('每步需要唯一 ID、操作目标和预期')
        if step['id'] in ids:
            raise ValueError('步骤 ID 重复')
        ids.add(step['id'])
        refs = step.get('acceptance_indices', [])
        if not isinstance(refs, list) or any(type(i) is not int or i < 0 for i in refs):
            raise ValueError('验收条件索引无效')
        covered.update(refs)
        loop = step.get('loop', False)
        if type(loop) is not bool or step.get('observation_action', 'none') not in ('none', 'reload', 'navigate'):
            raise ValueError('观测只能选择等待、刷新或查看指定进度页')
        if step.get('observation_action') == 'navigate':
            path = step.get('observation_url', '')
            if not isinstance(path, str) or not path.startswith('/') or path.startswith('//') or '\\' in path or any(ord(c) < 32 for c in path):
                raise ValueError('进度页必须是测试实例内的绝对路径')
    if acceptance is not None and (not acceptance or covered != set(range(len(acceptance)))):
        raise ValueError('链路必须覆盖每项需求验收条件')
    return redact(value)


def revise(ledger, chain, definition, requirement_id, replacements=None, db=None):
    if db is None:
        with ledger.store.transaction() as conn:
            return revise(ledger, chain, definition, requirement_id, replacements, conn)
    old = scoped(ledger, 'test_chains', chain['id'], chain['product_id'], db)
    if old['version'] != chain['version']:
        raise Conflict('链路已更新，请刷新')
    req = scoped(ledger, 'requirements', requirement_id, chain['product_id'], db)
    value = validate_definition(definition)
    replacements = replacements or {}
    if not isinstance(replacements, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k,v in replacements.items()):
        raise ValueError('替代关系必须为检查点标识映射')
    prior = ledger.get('test_chain_versions', old['current_version_id'], db) if old.get('current_version_id') else None
    if prior:
        current = {s['id']:s for s in value['steps']}
        for step in prior['definition']['steps']:
            same = current.get(step['id'])
            if same and same['expected'] == step['expected']:
                continue
            replacement = replacements.get(step['id'])
            if not replacement or replacement not in current:
                raise ValueError('删除或改变已有预期须明确指定替代步骤及变更需求')
        if replacements and req['id'] not in chain['requirement_ids']:
            raise ValueError('请先将变更需求关联到链路')
    version = ledger.create('test_chain_versions', {'product_id':chain['product_id'], 'chain_id':chain['id'],
        'number':(prior['number'] + 1) if prior else 1, 'definition':value, 'requirement_id':requirement_id,
        'replaces':replacements, 'parent_version_id':prior['id'] if prior else None}, 'ready', db=db)
    return ledger.update('test_chains', chain['id'], chain['version'],
        {'title':value['title'], 'current_version_id':version['id'], 'reason':''}, 'ready', db)


def linked(ledger, request):
    ids = {r['id'] for r in request.get('requirements', []) if r.get('id')}
    ids.update(x for x in (request.get('requirement', {}).get('id'), request['record'].get('requirement_id')) if x)
    extra = set(request['record'].get('test_chain_ids', []))
    return [c for c in ledger.scoped('test_chains', request['product']['id'])
            if c.get('enabled') and (ids.intersection(c.get('requirement_ids', [])) or c['id'] in extra)]


def make_run(ledger, product, chains, record, db=None):
    if db is None:
        with ledger.store.transaction() as conn:
            return make_run(ledger, product, chains, record, conn)
    chains = [scoped(ledger, 'test_chains', c['id'], product['id'], db) for c in chains]
    if not chains or any(c['status'] != 'ready' or not c.get('current_version_id') for c in chains):
        raise ValueError('链路尚未生成或存在阻塞')
    snapshots = []
    for chain in chains:
        version = scoped(ledger, 'test_chain_versions', chain['current_version_id'], product['id'], db)
        snapshots.append({'chain_id':chain['id'], 'version_id':version['id'], 'definition':version['definition'],
                          'baseline_id':chain.get('baseline_id')})
    config = settings(product)
    config['model'] = config.get('model') or product.get('agents', {}).get('verification', {})
    return ledger.create('test_chain_runs', {'product_id':product['id'], 'title':record.get('title', '测试链路'),
        'snapshots':snapshots, 'target':{k:record.get(k) for k in ('id','workspace','branch','commit','flow','review_target','requirement_id')},
        'config':config, 'receipts':[], 'evidence_ids':[], 'cancel_requested':False}, 'queued', db=db)


def confirm_baseline(ledger, pid, run, db):
    if run['status'] != 'pass' or not run.get('evidence_ids') or not run.get('instance', {}).get('commit'):
        raise ValueError('只有证据完整、绑定提交的通过结果可确认为基准')
    evidence = [scoped(ledger, 'evidence', ident, pid, db) for ident in run['evidence_ids']]
    root = (ledger.store.state/'autopilot/evidence').resolve()
    for item in evidence:
        if item.get('screenshot'):
            path = Path(item['screenshot']).resolve()
            if not path.is_relative_to(root) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item.get('sha256'):
                raise ValueError('截图缺失或校验失败，不能确认基准')
    for snapshot in run['snapshots']:
        for step in snapshot['definition']['steps']:
            checks = [e for e in evidence if e.get('details', {}).get('step_id') == step['id']
                      and e.get('details', {}).get('chain_id') == snapshot['chain_id'] and e['status'] == 'pass' and e.get('screenshot')]
            if not checks:
                raise ValueError('缺少检查点通过截图')
        ident = 'baseline-' + run['id'] + '-' + snapshot['chain_id']
        try:
            ledger.get('test_chain_baselines', ident, db)
            continue
        except KeyError:
            pass
        baseline = ledger.create('test_chain_baselines', {'product_id':pid, 'run_id':run['id'],
            'snapshot':snapshot, 'instance':run['instance'], 'evidence_ids':run['evidence_ids'],
            'confirmed_at':time.time()}, 'confirmed', ident=ident, db=db)
        chain = scoped(ledger, 'test_chains', snapshot['chain_id'], pid, db)
        # Confirmation of an older run preserves history without moving the latest baseline backwards.
        prior = ledger.get('test_chain_baselines', chain['baseline_id'], db) if chain.get('baseline_id') else None
        prior_run = ledger.get('test_chain_runs', prior['run_id'], db) if prior else None
        if prior_run is None or run['created'] >= prior_run['created']:
            ledger.update('test_chains', chain['id'], chain['version'], {'baseline_id':baseline['id']}, db=db)
    return ledger.update('test_chain_runs', run['id'], run['version'], {'baseline_confirmed':True}, db=db)


def api(control, path, body=None):
    ledger = control.ledger
    parts = path.strip('/').split('/')
    pid = parts[1]
    product = ledger.get('products', pid)
    tail = parts[3:]
    if body is None:
        if not tail:
            config = settings(product)
            return {'chains':ledger.scoped('test_chains', pid), 'runs':ledger.scoped('test_chain_runs', pid),
                    'versions':ledger.scoped('test_chain_versions', pid), 'baselines':ledger.scoped('test_chain_baselines', pid),
                    'config':config, 'product_version':product['version'], 'driver':'Codex + Playwright 浏览器'}
        if len(tail) == 2 and tail[0] == 'runs':
            run = scoped(ledger, 'test_chain_runs', tail[1], pid)
            return run | {'evidence':[scoped(ledger, 'evidence', e, pid) for e in run.get('evidence_ids', [])]}
        raise KeyError('测试链路接口不存在')
    with ledger.store.transaction() as db:
        if tail == ['configure']:
            product = ledger.get('products', pid, db)
            if body.get('version') != product['version']:
                raise Conflict('配置已更新，请刷新')
            config = body['config']
            validate_config(config)
            return ledger.update('products', pid, product['version'], {'computer_use':config}, db=db)
        if tail == ['generate']:
            if not settings(product)['enabled']:
                raise ValueError('请先启用测试链路')
            req = scoped(ledger, 'requirements', body['requirement_id'], pid, db)
            return enqueue_generation(ledger, product, req, db)
        if len(tail) == 3 and tail[0] == 'runs':
            run = scoped(ledger, 'test_chain_runs', tail[1], pid, db)
            if body.get('version') != run['version']:
                raise Conflict('运行状态已更新，请刷新')
            if tail[2] == 'cancel':
                if run['status'] in TERMINAL:
                    return run
                return ledger.update('test_chain_runs', run['id'], run['version'], {'cancel_requested':True},
                                     'cancelled' if run['status'] == 'queued' and not run.get('call') else 'cancelling', db)
            if tail[2] == 'confirm-baseline':
                return confirm_baseline(ledger, pid, run, db)
        if len(tail) != 2:
            raise KeyError('测试链路接口不存在')
        chain = scoped(ledger, 'test_chains', tail[0], pid, db)
        if body.get('version') != chain['version']:
            raise Conflict('链路已更新，请刷新')
        action = tail[1]
        if action == 'retry':
            if chain.get('call') or chain['status'] not in ('blocked', 'fail'):
                raise Conflict('当前链路不可重试')
            return ledger.update('test_chains', chain['id'], chain['version'], {'reason':''}, 'queued', db)
        if action == 'revise':
            return revise(ledger, chain, body['definition'], body['requirement_id'], body.get('replacements'), db)
        if action == 'link':
            req = scoped(ledger, 'requirements', body['requirement_id'], pid, db)
            return ledger.update('test_chains', chain['id'], chain['version'],
                                 {'requirement_ids':list(dict.fromkeys(chain['requirement_ids'] + [req['id']]))}, db=db)
        if action == 'run':
            if not settings(product)['enabled']:
                raise ValueError('请先启用测试链路')
            record = scoped(ledger, 'runs', body['run_id'], pid, db)
            if not record.get('workspace') or not record.get('commit'):
                raise ValueError('请选择已有工作区和提交的分支任务')
            if not isinstance(body.get('extra_chain_ids', []), list) or any(not isinstance(i, str) for i in body.get('extra_chain_ids', [])):
                raise ValueError('额外链路必须是标识列表')
            chains = [chain] + [scoped(ledger, 'test_chains', ident, pid, db) for ident in dict.fromkeys(body.get('extra_chain_ids', [])) if ident != chain['id']]
            return make_run(ledger, product, chains, record, db)
        raise KeyError('未知测试链路操作')
