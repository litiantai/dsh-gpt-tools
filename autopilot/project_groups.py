"""关联项目需求组、UI 优先设计及跨仓库联合验收。"""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import re
import sys
import time

from review_core import Conflict, Store
from .store import Ledger

ROLES = ('frontend', 'backend', 'fullstack', 'other')


def project_role(product):
    explicit = product.get('project_role', 'auto')
    if explicit in ROLES:
        return explicit
    config = product.get('project_config', {})
    stacks=set(config.get('stacks',[])) | {config.get('stack','')} | {m.get('stack','') for m in config.get('modules',[])}
    if config.get('web') or stacks & {'react','vue'}:
        return 'fullstack'
    return 'backend' if stacks & {'java','python','node'} else 'other'


def validate_links(ledger, product, changes):
    links = changes.get('related_projects', [])
    if changes.get('project_role', 'auto') not in ('auto', *ROLES):
        raise ValueError('项目职责无效')
    if not isinstance(links, list) or len(links) > 10:
        raise ValueError('关联项目最多 10 个')
    seen = {product['id']}
    for link in links:
        if not isinstance(link, dict) or link.get('role') not in ROLES or link.get('project_id') in seen:
            raise ValueError('关联项目重复或职责无效')
        ledger.get('products', link['project_id'])
        seen.add(link['project_id'])


def create_group(ledger, product, requirement, db):
    links = product.get('related_projects', [])
    if not links or requirement.get('group_id'):
        return requirement
    participants = [{'project_id':product['id'], 'role':project_role(product)}] + links
    group = ledger.create('requirement_groups', {'product_id':product['id'], 'title':requirement['title'],
        'source_requirement_id':requirement['id'], 'participants':participants, 'members':[], 'receipts':[],
        'requirement_snapshot':{k:requirement.get(k) for k in ('title','goal','scope','scenario','acceptance','evidence','impact')},
        'design_version_ids':[], 'merge_results':[]}, 'scoping', db=db)
    return ledger.update('requirements', requirement['id'], requirement['version'], {'group_id':group['id']}, db=db)


def members(ledger, group):
    return [ledger.get('runs', m['run_id']) for m in group.get('members', [])]


def current_designs(ledger, run):
    ids = run.get('design_version_ids', [])
    if run.get('group_id'):
        ids = ledger.get('requirement_groups', run['group_id']).get('design_version_ids', [])
    return [ledger.get('design_versions', ident) for ident in ids]


def review_designs(ledger, run):
    """完整设计独立保存供审查读取，避免 HTML 挤占方案摘要导致契约被截断。"""
    from .call import atomic
    root=ledger.store.state/'autopilot/design-artifacts';root.mkdir(parents=True,exist_ok=True)
    result=[]
    for design in current_designs(ledger,run):
        path=root/(design['id']+'.json')
        if path.exists():
            if json.loads(path.read_text())!=design:
                raise ValueError('设计文件与不可变台账不一致')
        else:
            atomic(path,design)
        result.append({k:design.get(k) for k in ('id','kind','input_design_ids','content_hash')} | {'path':str(path)})
    return result


def role_for(ledger, run, product):
    if run.get('group_id'):
        group = ledger.get('requirement_groups', run['group_id'])
        return next((m['role'] for m in group.get('members', []) if m['run_id']==run['id']), project_role(product))
    return project_role(product)


def design_action(ledger, run, product):
    if not run.get('design_workflow'):
        return None
    own = [d for d in current_designs(ledger, run) if d['run_id']==run['id']]
    role = role_for(ledger, run, product)
    if role in ('frontend','fullstack') and not any(d['kind']=='ui' for d in own):
        return 'design_ui'
    if role in ('backend','fullstack') and not any(d['kind']=='api' for d in own):
        return 'design_api'
    return None


def runnable(ledger, run):
    if run.get('call') or run['status'] in ('cancelling','pausing'):
        return True
    if not run.get('group_id'):
        return True
    group=ledger.get('requirement_groups',run['group_id'])
    if group['status'] in ('scoping','scope_blocked','cancelled'):
        return False
    if run['status'] in ('cancelling','pausing','blocked','paused'):
        return True
    product=ledger.get('products',run['product_id'])
    if product['status']!='active':
        return False
    designs=current_designs(ledger,run)
    by_run={r['id']:r for r in members(ledger,group)}
    if run['status'] in ('queued','planning'):
        action=design_action(ledger,run,product)
        if action != 'design_ui':
            if any(not any(d['run_id']==m['run_id'] and d['kind']=='ui' for d in designs)
                   for m in group['members'] if m['role'] in ('frontend','fullstack')):
                return False
        if action is None:
            if any(design_action(ledger,r,ledger.get('products',r['product_id'])) for r in by_run.values()):
                return False
    if run['status']=='developing' and not all(r.get('plan_approved') for r in by_run.values()):
        return False
    return True


def save_design(ledger, run, action, result):
    kind='ui' if action=='design_ui' else 'api'
    data=result.get('design')
    if not isinstance(data,dict):
        raise ValueError('缺少结构化设计产物')
    if kind=='ui' and (not isinstance(data.get('html'),str) or not data['html'].strip() or not isinstance(data.get('fields'),list) or not data['fields'] or not all(isinstance(f,str) for f in data['fields']) or not isinstance(data.get('states'),list) or not set(('normal','loading','empty','error')) <= set(data['states'])):
        raise ValueError('UI 设计必须包含可预览 HTML、页面字段与正常/加载/空数据/错误状态')
    if kind=='api':
        try:
            contract=json.loads(data.get('contract',''))
        except (ValueError,TypeError):
            raise ValueError('接口契约必须为可解析 JSON')
        if not isinstance(contract,(dict,list)) or not contract:
            raise ValueError('接口设计缺少字段契约')
    with ledger.store.transaction() as db:
        run=ledger.get('runs',run['id'],db)
        group=ledger.get('requirement_groups',run['group_id'],db) if run.get('group_id') else None
        ids=(group or run).get('design_version_ids',[])
        if kind=='ui':
            ids=[i for i in ids if ledger.get('design_versions',i,db)['kind']!='api']
        inputs=[i for i in ids if ledger.get('design_versions',i,db)['run_id']!=run['id'] or ledger.get('design_versions',i,db)['kind']!=kind]
        item=ledger.create('design_versions',{'product_id':run['product_id'],'run_id':run['id'],'group_id':run.get('group_id'),
            'kind':kind,'content':data,'input_design_ids':inputs,'source_commit':run.get('base_commit'),
            'content_hash':hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()},'ready',db=db)
        own=[i for i in run.get('design_version_ids',[]) if ledger.get('design_versions',i,db)['kind']!=kind]+[item['id']]
        run=ledger.update('runs',run['id'],run['version'],{'design_version_ids':own},db=db)
        if group:
            for member in group.get('members',[]):
                other=ledger.get('runs',member['run_id'],db)
                versions=[i for i in other.get('design_version_ids',[]) if kind!='ui' or ledger.get('design_versions',i,db)['kind']!='api']
                changes={'plan_approved':False,'design_version_ids':versions}
                state='planning' if other.get('plan_approved') and not other.get('call') else None
                ledger.update('runs',other['id'],other['version'],changes,state,db)
            run=ledger.get('runs',run['id'],db)
            ledger.update('requirement_groups',group['id'],group['version'],{'design_version_ids':inputs+[item['id']],
                'joint_result':None},db=db)
        return run


def model_action(action, request):
    from .intelligence_worker import run_model
    from .role_skills import output_schema
    ledger=Ledger(Store(Path(request['state_root']).parent)); product=request['product'];record=request['record']
    material={'goal':product.get('goal'),'project_config':product.get('project_config',{}),
        'requirement':request.get('requirement',record.get('requirement_snapshot')), 'designs':current_designs(ledger,record) if action!='group_scope' else []}
    if action=='group_scope':
        material['participants']=[p | {'name':ledger.get('products',p['project_id'])['name'],
            'goal':ledger.get('products',p['project_id']).get('goal'),
            'project_config':ledger.get('products',p['project_id']).get('project_config',{})} for p in record['participants']]
    elif record.get('group_id'):
        material['participants']=ledger.get('requirement_groups',record['group_id'])['members']
    instruction={
        'group_scope':'分析需求涉及哪些已关联项目，必须包含发起项目；仅返回实际需要修改的项目、职责、具体范围和验收条件。不得新增名单外项目。',
        'design_ui':'先设计可预览的完整 HTML（内联样式和交互，无外部请求），包含正常、加载、空数据、错误状态与关键交互。fields 写页面字段需求。不要实现后端。',
        'design_api':'根据已提供 UI 设计与 fields 制定接口契约，逐项写请求参数、响应字段类型/可空性/错误/分页，映射 UI 字段。独立后端依据调用方场景。contract 使用 JSON 文本。joint_routes 使用巡检路线结构，目标为参与项目 ID、完整隔离交互和明确 expected_text 断言。bindings 声明真实联调时消费者需要读取的服务地址环境变量及提供者项目 ID，避免使用系统变量。后端必须兼容尚未更新的前端。'
    }[action]
    if record.get('group_id'):
        request=request | {'usage_product_id':ledger.get('requirement_groups',record['group_id'])['product_id']}
    return run_model(action, request, product['agents']['implementation'], material, instruction,
                     output_schema(action)['properties'])


def finish_scope(scheduler, group, result):
    ledger=scheduler.ledger
    if result.get('status')!='pass':
        return scheduler.change('requirement_groups',group,{'reason':result.get('reason','范围分析失败')},'scope_blocked')
    entries=result.get('members',[]);allowed={p['project_id']:p['role'] for p in group['participants']}
    ids=[m.get('project_id') for m in entries]
    if not entries or len(ids)!=len(set(ids)) or group['product_id'] not in ids or not set(ids)<=set(allowed):
        raise ValueError('协作范围必须包含发起项目且只能使用已关联项目')
    if len(entries)>1 and any(not ledger.get('products',pid).get('git',{}).get('enabled') for pid in ids):
        raise ValueError('跨项目联合交付需要各参与项目启用 Git 交付；配置后重试范围分析')
    with ledger.store.transaction() as db:
        group=ledger.get('requirement_groups',group['id'],db)
        source=ledger.get('requirements',group['source_requirement_id'],db)
        result_members=[]
        for entry in entries:
            if not entry.get('scope') or not entry.get('acceptance'):
                raise ValueError('各项目需要独立修改范围和验收条件')
            pid=entry['project_id']
            if pid==group['product_id']:
                requirement=source;run=ledger.get('runs',source['run_id'],db)
                requirement=ledger.update('requirements',source['id'],source['version'],{'scope':entry['scope'],'acceptance':entry['acceptance']},db=db)
            else:
                payload={k:v for k,v in source.items() if k not in ('id','version','status','created','updated','run_id','product_id','signal_ids','resolution_probes','evaluation_id')}
                requirement=ledger.create('requirements',payload | {'product_id':pid,'scope':entry['scope'],'acceptance':entry['acceptance'],
                    'source':'linked_project','source_id':source['id'],'group_id':group['id'],'confirmation_required':False,
                    'classification':'development','in_scope':True},'pending',db=db)
                run=scheduler.control.queue(requirement,db=db)
            result_members.append({'project_id':pid,'role':allowed[pid],'scope':entry['scope'],
                'requirement_id':requirement['id'],'run_id':run['id']})
        ledger.update('requirement_groups',group['id'],group['version'],{'members':result_members,'reason':''},'designing',db=db)


def candidates(ledger, group):
    """联合验证绑定实际待交付候选，评审修复或 release 同步后自动失效。"""
    from .delivery_worker import paths
    from .staged_delivery import feature_request
    result=[]
    for run in members(ledger,group):
        product=ledger.get('products',run['product_id'])
        record=run; workspace=run.get('workspace'); commit=run.get('commit')
        if run.get('delivery_id'):
            batch=ledger.get('deliveries',run['delivery_id'])
            if run['id'] in batch.get('integrated_ids',[]) and batch.get('head_sha'):
                record=batch;commit=batch['head_sha']
            elif batch.get('feature_requirement_id')==run['requirement_id'] and batch.get('feature_head_sha'):
                record=feature_request({'record':batch})['record'];commit=batch['feature_head_sha']
            if record is not run:
                workspace=str(paths({'state_root':str(ledger.store.state/'autopilot'),'product':product,'record':record})[3])
        result.append({'run_id':run['id'],'project_id':run['product_id'],'workspace':workspace,'commit':commit})
    return result


def joint_signature(ledger, group):
    return hashlib.sha256(json.dumps({'runs':candidates(ledger,group),
                                    'designs':group.get('design_version_ids',[])},sort_keys=True).encode()).hexdigest()


def project_order(ledger, group):
    """服务依赖决定启动与合并顺序，无依赖项目按职责稳定排序。"""
    participants={m['project_id']:m for m in group['members']}
    dependencies={pid:set() for pid in participants}
    for design in (ledger.get('design_versions',i) for i in group.get('design_version_ids',[])):
        for binding in design['content'].get('bindings',[]):
            consumer=binding['consumer_id'];provider=binding['provider_id']
            if consumer not in participants or provider not in participants or consumer==provider:
                raise ValueError('服务绑定必须指向需求组内不同项目')
            dependencies[consumer].add(provider)
    result=[]
    while dependencies:
        ready=sorted((pid for pid,needs in dependencies.items() if not needs),
            key=lambda pid:({'backend':0,'fullstack':1,'other':2,'frontend':3}[participants[pid]['role']],pid))
        if not ready:
            raise ValueError('服务依赖存在环，需调整接口兼容与部署顺序')
        for pid in ready:
            result.append(participants[pid]);dependencies.pop(pid)
        for needs in dependencies.values():
            needs.difference_update(ready)
    return result


def delivery_gate(ledger, runs, *, merging=False):
    for run in runs:
        if not run.get('group_id'):
            continue
        group=ledger.get('requirement_groups',run['group_id'])
        if len(group.get('members',[]))<=1:
            continue
        if any(r['status'] not in ('accepted','delivered','online') for r in members(ledger,group)):
            return '关联需求等待各项目独立验收完成'
        if any(r['status']!='online' and (ledger.get('products',r['product_id'])['status']!='active' or ledger.get('products',r['product_id']).get('automation_disabled')) for r in members(ledger,group)):
            return '关联项目已暂停，等待恢复后继续交付'
        result=group.get('joint_result') or {}
        if result.get('status')!='pass' or group.get('joint_signature')!=joint_signature(ledger,group):
            return '关联需求等待全部项目通过联合验收'
        if merging:
            ordered=project_order(ledger,group)
            for member in ordered:
                if member['run_id']==run['id']:
                    break
                if ledger.get('runs',member['run_id'])['status']!='online':
                    p=ledger.get('products',member['project_id'])
                    return '关联需求等待接口提供项目先合入：'+p['name']
    return ''


def check_merge_request(request, *, merging):
    # 旧单项目回执无需访问额外账本。
    if not any(r.get('group_id') for r in request.get('runs',[])):
        return ''
    ledger=Ledger(Store(Path(request['state_root']).parent))
    return delivery_gate(ledger,[ledger.get('runs',r['id']) for r in request['runs']],merging=merging)


def joint_execute(request):
    from .local_testing import verification
    from .inspection_routes import execute_route, validate_routes
    ledger=Ledger(Store(Path(request['state_root']).parent));group=request['record']
    root=Path(request['state_root'])/'joint-verification'/group['id']/str(time.time_ns());root.mkdir(parents=True)
    frozen=group.get('joint_candidates') or candidates(ledger,group)
    by_project={c['project_id']:c for c in frozen}
    designs=[ledger.get('design_versions',i) for i in group.get('design_version_ids',[])]
    bindings=[b for d in designs for b in d['content'].get('bindings',[])]
    routes=[r for d in designs for r in d['content'].get('joint_routes',[])]
    if not routes:
        return {'status':'blocked','reason':'接口设计缺少联合验收路线，无法证明跨项目交互'}
    participants={m['project_id'] for m in group['members']};instances={};results=[]
    consumers={b['consumer_id'] for b in bindings}
    frontend={m['project_id'] for m in group['members'] if m['role'] in ('frontend','fullstack')}
    if len(participants)>1 and (not bindings or not (consumers | frontend)<={r['project_id'] for r in routes}):
        return {'status':'blocked','reason':'联合路线必须覆盖各调用方，且声明真实服务地址绑定'}
    ordered=project_order(ledger,group)
    with ExitStack() as stack:
        for member in ordered:
            pid=member['project_id'];product=ledger.get('products',pid);run=ledger.get('runs',member['run_id'])
            product=product | {'project_config':dict(product.get('project_config',{}),test_start=product.get('project_config',{}).get('commands',{}).get('start',[]))}
            env={}
            for binding in bindings:
                if binding['consumer_id']!=pid:
                    continue
                provider=binding['provider_id'];key=binding['environment_variable']
                if provider not in participants or provider not in instances:
                    raise ValueError('联调服务依赖尚未启动或存在依赖环')
                if not re.fullmatch(r'(?:VITE_|NEXT_PUBLIC_|REACT_APP_|DSH_LINKED_|API_|BACKEND_|SERVICE_)[A-Z0-9_]+',key):
                    raise ValueError('联调变量必须为服务地址变量，不能覆盖系统环境')
                env[key]=instances[provider]['origin']
            from .workspace import git
            candidate=by_project[pid]
            checkout=root/(pid+'-source');branch='feat-joint-'+str(time.time_ns())
            git(candidate['workspace'],'worktree','add','-b',branch,str(checkout),candidate['commit'])
            def cleanup(repo=candidate['workspace'], path=checkout, name=branch):
                git(repo,'worktree','remove','--force',str(path))
                git(repo,'branch','-D',name)
            stack.callback(cleanup)
            run=run | {'workspace':str(checkout),'commit':candidate['commit'],'branch':branch}
            checked=stack.enter_context(verification({'state_root':request['state_root'],'product':product,
                'record':run,'joint_environment':env},root/pid))
            if checked['status']!='pass':
                return {'status':'blocked','reason':product['name']+'：'+checked.get('reason','联合实例未就绪'),'checks':checked['checks']}
            instances[pid]=dict(checked['instance'])
        for index,item in enumerate(routes):
            pid=item['project_id']
            if pid not in participants:
                raise ValueError('联合路线项目不属于需求组')
            definition=item['route']
            if definition.get('environment')!='isolated' or not any(s.get('expected_text') for s in definition.get('steps',[])):
                raise ValueError('联合验收必须使用隔离路线及业务断言')
            product=ledger.get('products',pid);validate_routes([definition],product)
            product=product | {'computer_use':dict(product.get('computer_use') or {},allowed_origins=[v['origin'] for v in instances.values()])}
            folder=root/('route-'+str(index));folder.mkdir()
            proof=execute_route(ledger,product,{'id':group['id'],'target_commit':instances[pid]['commit']},
                {'id':group['id']+'-'+str(index),'route_id':group['id']+'-'+str(index),'definition':definition},instances[pid],folder)
            results.append(proof)
    return {'status':'pass' if all(r['status']=='pass' for r in results) else 'blocked',
            'reason':'联合业务链路通过' if all(r['status']=='pass' for r in results) else '联合业务链路未全部通过',
            'routes':results,'instances':instances,'candidates':frozen}


def tick(scheduler):
    ledger=scheduler.ledger
    command=[sys.executable,str(Path(__file__).resolve().parents[1]/'scripts/autopilot-adapter.py'),'groups']
    for group in ledger.list('requirement_groups'):
        product=ledger.get('products',group['product_id'])
        if group.get('call'):
            result=scheduler.call_result(group)
            if result is None:
                continue
            group=scheduler.consume('requirement_groups',group,result)
        if group.get('receipts') and group['receipts'][-1]['call_id']!=group.get('processed_call'):
            receipt=group['receipts'][-1];result=receipt['result']
            group=scheduler.change('requirement_groups',group,{'processed_call':receipt['call_id']})
            if receipt['action']=='group_scope':
                try:
                    finish_scope(scheduler,group,result)
                except (ValueError,KeyError,Conflict) as exc:
                    scheduler.change('requirement_groups',ledger.get('requirement_groups',group['id']),{'reason':str(exc)},'scope_blocked')
            else:
                scheduler.change('requirement_groups',group,{'joint_result':result,'reason':result.get('reason','')},
                    'ready' if result['status']=='pass' else 'joint_blocked')
            continue
        if product['status']!='active' or product.get('automation_disabled'):
            continue
        if group['status']=='scoping':
            scheduler.start_call('requirement_groups',group,product,'group_scope',command,timeout=900)
            continue
        if not group.get('members') or group['status'] in ('scope_blocked','completed','cancelled'):
            continue
        runs=members(ledger,group)
        merged=[r for r in runs if r['status']=='online']
        if merged:
            proofs=[ledger.get('requirements',m['requirement_id']).get('final_acceptance',{}).get('status')=='pass' for m in group['members']]
            status='completed' if len(merged)==len(runs) and all(proofs) else 'awaiting_final_acceptance' if len(merged)==len(runs) else 'partially_merged'
            if group['status']!=status:
                scheduler.change('requirement_groups',group,{'merge_results':[{'project_id':r['product_id'],'run_id':r['id'],'merge_sha':r.get('merge_sha')} for r in merged]},status)
            if len(merged)==len(runs):
                continue
            group=ledger.get('requirement_groups',group['id'])
        if len(runs)==1:
            if group['status']!='awaiting_project_delivery':
                scheduler.change('requirement_groups',group,{},'awaiting_project_delivery')
            continue
        if any(ledger.get('products',r['product_id'])['status']!='active' for r in runs):
            continue
        if any(ledger.get('deliveries',r['delivery_id']).get('call') for r in runs if r.get('delivery_id')):
            continue
        signature=joint_signature(ledger,group)
        if all(r['status'] in ('accepted','delivered','online') for r in runs) and (group.get('joint_signature')!=signature or group['status'] in ('joint_queued','joint_verifying')):
            group=scheduler.change('requirement_groups',group,{'joint_signature':signature,'joint_candidates':candidates(ledger,group)},'joint_verifying')
            scheduler.start_call('requirement_groups',group,product,'joint_verify',command,timeout=14400)


def detail(ledger, group):
    result=[]
    for member in group.get('members',[]):
        product=ledger.get('products',member['project_id']);run=ledger.get('runs',member['run_id'])
        batch=ledger.get('deliveries',run['delivery_id']) if run.get('delivery_id') else {}
        urls=[p['pr_url'] for p in batch.get('feature_prs',[]) if run['id'] in p.get('run_ids',[]) and p.get('pr_url')]
        if run.get('feature_pr_url'):
            urls.append(run['feature_pr_url'])
        if batch.get('feature_requirement_id')==member['requirement_id'] and batch.get('feature_pr_url'):
            urls.append(batch['feature_pr_url'])
        result.append(member | {'project_name':product['name'],'repository':product.get('git',{}).get('url'),
            'status':run['status'],'reason':run.get('reason'),'prs':list(dict.fromkeys(urls)),'release_pr':batch.get('pr_url')})
    return group | {'members':result,'designs':[ledger.get('design_versions',i) for i in group.get('design_version_ids',[])]}


def api(control, parts, body=None):
    ledger=control.ledger
    if parts[0]=='products':
        pid=parts[1];ledger.get('products',pid)
        return [detail(ledger,g) for g in ledger.list('requirement_groups') if any(p['project_id']==pid for p in g['participants'])]
    group=ledger.get('requirement_groups',parts[1])
    if body is None:
        return detail(ledger,group)
    if parts[2:] == ['retry'] and not group.get('call') and group['status'] in ('scope_blocked','joint_blocked'):
        state='scoping' if group['status']=='scope_blocked' else 'joint_queued'
        return ledger.update('requirement_groups',group['id'],body['version'],{'reason':''},state)
    raise Conflict('当前需求组不能重试')


def execute(action, request):
    return joint_execute(request) if action=='joint_verify' else model_action(action,request)
