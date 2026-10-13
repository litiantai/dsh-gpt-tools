"""多业务线巡检路线、不可变版本和逐步取证的调度入口。"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

from review_core import Conflict, Store
from .store import Ledger, DEFAULTS, redact
from .workspace import git

DEFAULT_ROLE = {'provider': 'codex', 'model': 'gpt-6.1-sol'}
KINDS = ('inspection_plans', 'inspection_routes', 'inspection_route_versions', 'inspection_rounds')


def role(product):
    return product.get('agents', {}).get('inspection_design') or DEFAULT_ROLE


def fingerprint(product):
    value={k:product.get(k) for k in ('goal','project_config')}
    value.update(source_commit=product.get('master_environment',{}).get('commit'), role=role(product))
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


def validate_routes(routes, product):
    if not isinstance(routes, list) or not 1 <= len(routes) <= 50:
        raise ValueError('巡检需包含 1–50 条业务路线')
    ids = set()
    for route in routes:
        if not all(isinstance(route.get(k), str) and route[k].strip() for k in ('key', 'title', 'business_line')):
            raise ValueError('路线标识、标题和业务线必填')
        if route['key'] in ids or route.get('environment') not in ('master', 'isolated'):
            raise ValueError('路线标识重复或执行环境无效')
        ids.add(route['key'])
        if not isinstance(route.get('steps'), list) or not 1 <= len(route['steps']) <= 100:
            raise ValueError('路线需包含 1–100 个检查点')
        for step in route['steps']:
            if not all(isinstance(step.get(k), str) and step[k].strip() for k in ('title', 'expected')):
                raise ValueError('检查点标题和预期必填')
            if step.get('action') not in ('navigate', 'click', 'fill', 'press', 'read_api', 'request_api', 'observe'):
                raise ValueError('巡检动作不受支持')
            if route['environment'] == 'master' and step['action'] not in ('navigate', 'read_api', 'observe'):
                raise ValueError('主实例仅允许查看页面和登记的只读接口')
            if step['action'] in ('navigate', 'read_api', 'request_api'):
                path = step.get('path', '')
                if not path.startswith('/') or path.startswith('//') or '\\' in path or any(ord(x) < 32 for x in path):
                    raise ValueError('巡检路径必须为同源绝对路径')
                if step['action'] == 'read_api' and route['environment'] == 'master':
                    allowed = product.get('project_config', {}).get('readonly_paths', [])
                    if path not in allowed:
                        raise ValueError('主实例接口未登记为只读路径')
            if step['action'] in ('click', 'fill', 'press') and not step.get('selector'):
                raise ValueError('交互步骤缺少定位器')
            if step['action']=='request_api':
                if route['environment']!='isolated' or step.get('api_method') not in ('POST','PUT','PATCH','DELETE'):
                    raise ValueError('接口写入仅允许在隔离实例执行')
                if step.get('api_body'):
                    json.loads(step['api_body'])
            wait = step.get('wait_seconds', 45)
            if type(wait) is not int or not 1 <= wait <= 1800:
                raise ValueError('步骤等待时间须为 1–1800 秒')
    return redact(routes)


def enqueue(ledger, product, *, force=False, db=None):
    if db is None:
        with ledger.store.transaction() as connection:
            return enqueue(ledger, product, force=force, db=connection)
    plans = ledger.scoped('inspection_plans', product['id'], db)
    active = next((p for p in plans if p['status'] in ('queued', 'running')), None)
    if active:
        return active
    mark = fingerprint(product)
    same = next((p for p in reversed(plans) if p.get('input_fingerprint') == mark), None)
    if same and not force:
        return same
    return ledger.create('inspection_plans', {'product_id':product['id'], 'title':'业务线巡检路线设计',
        'input_fingerprint':mark, 'selection':role(product), 'receipts':[]}, 'queued', db=db)


def finish_plan(ledger, plan, result):
    with ledger.store.transaction() as db:
        plan = ledger.get('inspection_plans', plan['id'], db)
        changes = {'result':result, 'processed_call':plan['receipts'][-1]['call_id']}
        try:
            if result.get('status') != 'pass':
                raise ValueError(result.get('reason', '路线设计未完成'))
            product = ledger.get('products', plan['product_id'], db)
            routes = validate_routes(result.get('routes'), product)
            if not result.get('source_commit'):
                raise ValueError('路线缺少源码版本依据')
            existing = {r['key']:r for r in ledger.scoped('inspection_routes', product['id'], db)}
            for definition in routes:
                route = existing.get(definition['key']) or ledger.create('inspection_routes', {
                    'product_id':product['id'], 'key':definition['key'], 'enabled':True}, 'ready', db=db)
                version = ledger.create('inspection_route_versions', {'product_id':product['id'], 'route_id':route['id'],
                    'definition':definition, 'source_commit':result['source_commit'], 'plan_id':plan['id']}, 'ready', db=db)
                ledger.update('inspection_routes', route['id'], route['version'], {'title':definition['title'],
                    'business_line':definition['business_line'], 'environment':definition['environment'],
                    'current_version_id':version['id'], 'coverage_reason':''}, 'ready', db)
            for key, route in existing.items():
                if key not in {r['key'] for r in routes}:
                    ledger.update('inspection_routes', route['id'], route['version'],
                        {'coverage_reason':'新设计未覆盖该路线，请核对业务是否已移除'}, 'stale', db)
            return ledger.update('inspection_plans', plan['id'], plan['version'], changes, 'completed', db)
        except (ValueError, KeyError) as exc:
            return ledger.update('inspection_plans', plan['id'], plan['version'], changes | {'reason':str(exc)}, 'blocked', db)


def api(control, parts, body=None):
    ledger = control.ledger
    product = ledger.get('products', parts[1])
    if body is None:
        routes = ledger.scoped('inspection_routes', product['id'])
        return {'design_tokens':ledger.budget_used(product['id'],'inspection_design_tokens'),'inspection_tokens':ledger.budget_used(product['id'],'inspection_tokens'),'routes':[r | {'definition':ledger.get('inspection_route_versions', r['current_version_id'])['definition']}
                          for r in routes if r.get('current_version_id')],
                'plans':ledger.scoped('inspection_plans', product['id'])[-10:],
                'rounds':ledger.scoped('inspection_rounds', product['id'])[-20:]}
    if parts[3:] == ['generate']:
        return enqueue(ledger, product, force=True)
    if parts[3:] == ['rescan']:
        from .inspection_rescan import enqueue as rescan
        return rescan(ledger, product)
    if len(parts) == 5 and parts[4] == 'toggle':
        item = ledger.get('inspection_routes', parts[3])
        if item['product_id'] != product['id']:
            raise KeyError('路线不属于此项目')
        if type(body.get('enabled')) is not bool:
            raise ValueError('启用状态必须为布尔值')
        return ledger.update('inspection_routes', item['id'], body['version'], {'enabled':body['enabled']})
    raise KeyError('巡检路线接口不存在')


def tick(scheduler):
    ledger = scheduler.ledger
    command = [sys.executable, str(Path(__file__).resolve().parents[1]/'scripts/autopilot-adapter.py'), 'inspection']
    for product in ledger.list('products'):
        if product.get('automation_disabled') or product['status'] not in ('active','observing') or not product.get('adapter'):
            continue
        for kind in ('inspection_plans', 'inspection_rounds'):
            for item in ledger.scoped(kind, product['id']):
                if item.get('call'):
                    result = scheduler.call_result(item)
                    if result is None:
                        continue
                    item = scheduler.consume(kind, item, result)
                if item.get('receipts') and item['receipts'][-1]['call_id'] != item.get('processed_call'):
                    result = item['receipts'][-1]['result']
                    if kind == 'inspection_plans':
                        finish_plan(ledger, item, result)
                    else:
                        from .inspection_rescan import finish
                        finish(ledger, item, result)
                        if item.get('legacy_inspection'):
                            for signal_data in result.get('signals', []):
                                ledger.signal(product['id'], signal_data)
                        scheduler.change(kind, item, {'processed_call':item['receipts'][-1]['call_id'],
                            'result':result,'reason':result.get('reason',''), 'finished_at':time.time()},
                            'completed' if result['status']=='pass' else 'blocked')
        from .inspection_rescan import schedule
        schedule(ledger, product, time.time())
        # 主实例尚未登记版本时仍可人工生成路线，自动执行等待可信运行身份。
        plans = ledger.scoped('inspection_plans', product['id'])
        plan = enqueue(ledger, product) if product.get('master_environment', {}).get('commit') else next((p for p in plans if p['status']=='queued'), None)
        if plan and plan['status'] == 'queued' and not plan.get('call'):
            if not scheduler.wait_for_off_peak('inspection_plans', plan, product, 'inspection_design', selected=plan['selection']):
                started = scheduler.start_call('inspection_plans', plan, product, 'inspection_design', command, timeout=900)
                if started.get('call'):
                    scheduler.change('inspection_plans', started, {}, 'running')
        rounds = ledger.scoped('inspection_rounds', product['id'])
        if any(r['status']=='running' or r.get('call') for r in rounds):
            continue
        if (product.get('call') or {}).get('action') == 'inspect':
            continue
        pending = next((r for r in rounds if r['status']=='queued' and not r.get('call')), None)
        if pending:
            legacy = pending.get('legacy_inspection')
            if pending.get('rescan') and not legacy:
                # 排队期间可能更新主实例或停用路线；以实际派发时的版本冻结本轮。
                if not product.get('master_environment', {}).get('commit'):
                    continue
                current_routes = [r for r in ledger.scoped('inspection_routes', product['id'])
                    if r.get('enabled') and r['status']=='ready' and r.get('current_version_id')]
                if not current_routes:
                    continue
                snapshots = [ledger.get('inspection_route_versions', r['current_version_id']) for r in current_routes]
                changes = {'target_commit':product['master_environment']['commit'], 'snapshots':snapshots}
                if any(pending.get(k) != v for k,v in changes.items()):
                    pending = scheduler.change('inspection_rounds', pending, changes)
            started = scheduler.start_call('inspection_rounds', pending, product, 'inspect' if legacy else 'inspection_run',
                product['adapter'] if legacy else command,
                timeout=600 if legacy else sum(sum(s.get('wait_seconds',45)+60 for s in v['definition']['steps']) for v in pending['snapshots'])+1800)
            if started.get('call'):
                scheduler.change('inspection_rounds', started, {}, 'running')
        if any(r['status'] in ('queued','running') or r.get('call') for r in rounds):
            continue
        if not product.get('master_environment', {}).get('commit'):
            continue
        interval = (DEFAULTS | product.get('policy', {}))['inspection_seconds']
        if rounds and time.time() - rounds[-1]['created'] < interval:
            continue
        routes = [r for r in ledger.scoped('inspection_routes', product['id']) if r.get('enabled') and r['status']=='ready']
        if not routes:
            continue
        snapshots = [ledger.get('inspection_route_versions', r['current_version_id']) for r in routes]
        item = ledger.create('inspection_rounds', {'product_id':product['id'], 'title':'多业务线巡检',
            'snapshots':snapshots, 'target_commit':product['master_environment']['commit'], 'receipts':[], 'route_results':[]}, 'queued')
        started = scheduler.start_call('inspection_rounds', item, product, 'inspection_run', command,
            timeout=sum(sum(s.get('wait_seconds',45)+60 for s in v['definition']['steps']) for v in snapshots)+1800)
        if started.get('call'):
            scheduler.change('inspection_rounds', started, {}, 'running')


def design(request):
    from .intelligence_worker import run_model
    from .role_skills import output_schema
    from .master import source_workspace
    product = request['product']
    workspace = source_workspace(request) if product.get('git', {}).get('enabled') else product.get('repository') or product['source']
    commit = git(workspace, 'rev-parse', 'HEAD')
    ledger = Ledger(Store(Path(request['state_root']).parent))
    material = {'goal':product.get('goal'), 'project_config':product.get('project_config', {}),
        'source_commit':commit, 'history':[{k:r.get(k) for k in ('title','method','judgement','environment')}
                                        for r in ledger.scoped('inspections', product['id'])[-20:]],
        'existing_routes':[ledger.get('inspection_route_versions', r['current_version_id'])['definition']
            for r in ledger.scoped('inspection_routes', product['id']) if r.get('current_version_id')],
        'requirements':[{k:r.get(k) for k in ('title','scope','acceptance','status')}
                        for r in ledger.scoped('requirements', product['id'])[-50:]]}
    result = run_model('inspection_design', request | {'record':request['record'] | {'workspace':workspace},
                       'budget_kind':'inspection_design_tokens'}, request['record'].get('selection') or role(product), material,
        '只读源码并设计覆盖各业务线的真实巡检路线。保留已有路线 key；只输出可由浏览器定位器及受控接口执行的步骤；接口写入必须在隔离环境。',
        output_schema('inspection_design')['properties'])
    return result | {'source_commit':commit}


@contextmanager
def isolated_instance(request, root):
    product = request['product']; commit = request['record']['target_commit']
    from .project import generic
    from .local_testing import verification
    from .inspection_instance import desktop_instance
    workspace=root/'workspace';branch='feat-inspection-'+request['record']['id']
    repo=product.get('delivery_repository') or product['repository']
    git(repo,'worktree','add','-b',branch,str(workspace),commit)
    try:
        if generic(product):
            record={'workspace':str(workspace),'branch':branch,'commit':commit}
            with verification(request | {'record':record},root/'instance',inspection=True) as checked:
                if checked['status']!='pass':
                    raise ValueError(checked.get('reason','隔离巡检环境不可用'))
                yield checked['instance'] | {'instance_role':'isolated'}
        else:
            with desktop_instance(request,workspace,root/'instance') as instance:
                yield instance
    finally:
        git(repo,'worktree','remove','--force',str(workspace))
        git(repo,'branch','-D',branch)


def api_evidence(instance, step):
    """纯后端取证使用受管实例 GET，不自动跟随重定向。"""
    import http.client
    import re
    origin=instance['origin']
    if not re.fullmatch(r'http://(?:127\.0\.0\.1|localhost):\d+',origin):
        raise ValueError('接口取证只能访问受管本机实例')
    url=urlsplit(origin)
    connection=http.client.HTTPConnection(url.hostname,url.port,timeout=step.get('wait_seconds',45))
    try:
        method=step.get('api_method') if step['action']=='request_api' else 'GET'
        body=step.get('api_body') if step['action']=='request_api' else None
        connection.request(method,step['path'],body=body.encode() if body else None,headers={'Content-Type':'application/json'} if body else {})
        response=connection.getresponse()
        content=response.read(1024*1024+1)
        if len(content)>1024*1024:
            raise ValueError('接口证据超过 1 MB')
        return {'ok':200<=response.status<300,'status':response.status,'text':content.decode('utf-8',errors='replace')}
    finally:
        connection.close()


def execute_route(ledger, product, round_record, snapshot, instance, root, driver_factory=None):
    from .computer_use import BrowserDriver
    from .evidence import Recorder
    definition = snapshot['definition']
    validate_routes([definition],product)
    root=Path(root); root.mkdir(parents=True,exist_ok=True)
    recorder = Recorder(ledger.store, product['id'], definition['title'],
        '逐检查点实际操作，操作前后及加载完成截图，每步即时入账', definition['environment'])
    recorder.inspection = ledger.update('inspections', recorder.inspection['id'], recorder.inspection['version'], {
        'business_line':definition['business_line'],'route_id':snapshot['route_id'],'route_version_id':snapshot['id'],
        'round_id':round_record['id'],'source_commit':round_record['target_commit'],
        'collection_status':'running','judgement_status':'pending'})
    api_only=all(s['action'] in ('read_api','request_api') for s in definition['steps'])
    driver = None if api_only else (driver_factory or BrowserDriver)(root)
    config = dict(product.get('computer_use') or {})
    config.update(entry_url=instance.get('entry_url'),full_page=True, read_only=definition['environment']=='master', readonly_paths=product.get('project_config', {}).get('readonly_paths', []))
    status = 'observed'; reason = '巡检证据已保存，等待独立归因'; asserted = False
    def capture(step, phase, state='observed', actual=None):
        path = root/(str(time.time_ns())+'.png')
        screen = driver.screenshot(path)
        return recorder.step(step['title'], step['action'], step['expected'], actual or screen.get('text',''),
            status=state, screenshot=path, details={'route_id':snapshot['route_id'],'route_version_id':snapshot['id'],
                'checkpoint':step['title'],'phase':phase,'instance':instance,'screen':screen})
    try:
        if instance.get('commit') != round_record['target_commit']:
            raise ValueError('巡检实例与当轮提交不一致')
        if definition.get('requires_auth') and not config.get('storage_state') and not instance.get('auth_ready'):
            raise ValueError('前置条件不足：请在项目隔离配置中提供测试登录状态')
        if driver:
            driver.open(instance['origin'], config)
        for step in definition['steps']:
            if step['action'] in ('read_api','request_api'):
                result = api_evidence(instance,step) if api_only else driver.send({'command':'inspect_api','step':step,'path':step['path'],'method':step.get('api_method','GET') if step['action']=='request_api' else 'GET','body':step.get('api_body')})
                passed = not step.get('expected_text') or step['expected_text'] in result['text']
                state = 'fail' if not result['ok'] or not passed else 'pass' if step.get('expected_text') else 'observed'
                recorder.step(step['title'],(step.get('api_method','GET') if step['action']=='request_api' else 'GET')+' '+step['path'],step['expected'],result['text'],status=state,
                    details={'phase':'response','instance':instance,'route_version_id':snapshot['id'],'request':{'method':step.get('api_method','GET') if step['action']=='request_api' else 'GET','url':instance['origin']+step['path'],'body':json.loads(step['api_body']) if step.get('api_body') else None},'response':result})
                asserted = asserted or state=='pass'
                if state=='fail':
                    status='fail'; reason='接口结果不符合预期'
                continue
            capture(step, 'before')
            if step['action'] != 'observe':
                driver.send({'command':'inspect_action','step':step})
                if step.get('wait_selector') or step.get('wait_text'):
                    capture(step, 'after')
            result = driver.send({'command':'inspect_wait','step':step})
            state = 'blocked' if not result['settled'] else 'fail' if not result['matched'] else 'pass' if step.get('expected_text') else 'observed'
            capture(step, ('settled' if step.get('wait_selector') or step.get('wait_text') else 'after') if result['settled'] else 'timeout', state)
            asserted = asserted or state=='pass'
            if state in ('fail','blocked'):
                status=state; reason='检查点未通过：'+step['title']
                break
        if status=='observed' and asserted:
            status='pass'; reason='本轮全部业务断言通过'
    except BaseException as exc:
        status='blocked'; reason=str(exc)
        try:
            capture({'title':'巡检中断','action':'观察现场','expected':'完成本路线'}, 'interrupted','blocked',reason)
        except Exception:
            recorder.step('巡检中断','执行路线','取得现场证据',reason,status='blocked')
        if isinstance(exc, (KeyboardInterrupt, SystemExit, InterruptedError)):
            raise
    finally:
        try:
            if driver:
                driver.close()
        finally:
            current = ledger.get('inspections',recorder.inspection['id'])
            recorder.inspection=ledger.update('inspections',current['id'],current['version'],{'collection_status':'blocked' if status=='blocked' else 'completed',
                'judgement_status':status if status in ('pass','fail') else 'pending'})
            recorder.finish(status, reason)
    return {'route_id':snapshot['route_id'],'inspection_id':recorder.inspection['id'],'status':status,'reason':reason}


def execute(action, request):
    if action == 'inspection_design':
        return design(request)
    if action != 'inspection_run':
        raise ValueError('未知巡检动作')
    from .master import stable_instance
    return stable_instance(execute_round)(request)


def execute_round(request):
    ledger = Ledger(Store(Path(request['state_root']).parent)); product=request['product']; record=request['record']
    root = Path(request['state_root'])/'inspection-rounds'/record['id']; root.mkdir(parents=True,exist_ok=True)
    results=[]
    from contextlib import ExitStack
    with ExitStack() as stack:
        isolated = None
        isolation_error = None
        for snapshot in record['snapshots']:
            folder=root/snapshot['id'];folder.mkdir(exist_ok=True)
            try:
                if snapshot['definition']['environment']=='master':
                    from .master import identity
                    instance=identity(product,record['target_commit'])
                else:
                    if isolated is None:
                        if isolation_error:
                            raise ValueError('共享隔离实例准备失败：'+isolation_error)
                        try:
                            isolated=stack.enter_context(isolated_instance(request,root))
                        except Exception as exc:
                            isolation_error = str(exc)
                            raise
                    instance=isolated
                result=execute_route(ledger, product, record, snapshot, instance, folder)
                if snapshot['definition']['environment']=='master' and identity(product,record['target_commit'])!=instance:
                    result.update(status='blocked',reason='巡检期间主实例身份变化，证据无效')
                    current=ledger.get('inspections',result['inspection_id'])
                    ledger.update('inspections',current['id'],current['version'],{'judgement':result['reason'],'judgement_status':'pending','collection_status':'blocked'},'blocked')
            except Exception as exc:
                from .evidence import Recorder
                recorder=Recorder(ledger.store,product['id'],snapshot['definition']['title'],'核对巡检前置条件',snapshot['definition']['environment'])
                recorder.inspection=ledger.update('inspections',recorder.inspection['id'],recorder.inspection['version'],{'business_line':snapshot['definition']['business_line'],'route_id':snapshot['route_id'],'route_version_id':snapshot['id'],'round_id':record['id'],'source_commit':record['target_commit'],'collection_status':'blocked','judgement_status':'pending'})
                recorder.step('前置条件','准备运行实例','实例身份和测试数据可用',str(exc),status='blocked')
                recorder.finish('blocked',str(exc))
                result={'route_id':snapshot['route_id'],'inspection_id':recorder.inspection['id'],'status':'blocked','reason':str(exc)}
            results.append(result)
            current=ledger.get('inspection_rounds',record['id'])
            ledger.update('inspection_rounds',current['id'],current['version'],{'route_results':results})
            ledger.signal(product['id'],{'source':'inspection','code':'BUSINESS_ROUTE','component':snapshot['route_id'],
                'version':record['id'], 'summary':snapshot['definition']['title'],'inspection_id':result['inspection_id'],
                'evidence':result | {'route_version_id':snapshot['id'],'source_commit':record['target_commit']}})
    return {'status':'pass' if all(r['status'] in ('pass','observed') for r in results) else 'blocked',
            'reason':'全部业务路线已执行，逐路线结果见巡检记录','routes':results}
