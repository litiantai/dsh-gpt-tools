"""主分支实例的版本核对、只读巡检、合入后复验与受管更新。"""
import json
import fcntl
from functools import wraps
from pathlib import Path
import shutil
import subprocess
import time

from .call import atomic
from .workspace import git


def source_workspace(request):
    """需求、调查和复盘读取当前 master 提交，不复用 feat/release 候选源码。"""
    product = request['product']
    expected = target(product)
    root = Path(request['state_root'])/'master-source'/product['id']/expected
    root.parent.mkdir(parents=True, exist_ok=True)
    with (root.parent/'checkout.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not root.exists():
            git(product['delivery_repository'], 'worktree', 'add', '--detach', str(root), expected)
        if git(root, 'rev-parse', 'HEAD') != expected or git(root, 'status', '--porcelain'):
            raise ValueError('master 只读工作区提交不一致或源码发生变化')
    return str(root)

def enabled(product):
    from .project import generic
    if generic(product):
        return bool(product.get('git', {}).get('enabled') and product.get('delivery_repository') and product.get('deployment'))
    return bool(product.get('git', {}).get('enabled') and product.get('delivery_repository')
                and all(product.get(k) for k in ('application', 'runtime', 'app_support')))


def target(product):
    """从远端读取主分支，禁止把候选基线或过期本地分支当成 master。"""
    from .delivery_worker import network_git
    repo = product['delivery_repository']
    branch = product['git'].get('base_branch', 'master')
    network_git(repo, 'fetch', 'origin', '+refs/heads/'+branch+':refs/remotes/origin/'+branch)
    return git(repo, 'rev-parse', 'refs/remotes/origin/'+branch)


def identity(product, expected):
    """核对实际进程、认证监测与安装标记，旧测试实例不能充当主实例。"""
    from .project import generic
    if generic(product):
        from .generic_adapter import identity as identify
        return identify(product, expected)
    from .thsoctop import endpoint, monitor
    origin = endpoint(product)
    marker = json.loads((Path(product['runtime'])/'autopilot-release.json').read_text())
    health = monitor(product)
    if marker.get('commit') != expected or health.get('releaseId') != marker.get('id'):
        raise ValueError('主实例运行版本与 master 不一致，等待主实例更新后复验')
    if health.get('host') != 'ready':
        raise ValueError('主实例尚未就绪')
    return {'instance_role': 'master', 'commit': expected, 'release_id': marker['id'], 'origin': origin}


def stable_instance(function):
    """巡检与复验共享读锁；安装持有同一路径的排他锁。"""
    @wraps(function)
    def run(request, *args, **kwargs):
        path=Path(request['state_root'])/'runtime-locks'/(request['product']['id']+'.lock')
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('a') as lock:
            try:
                fcntl.flock(lock,fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'status':'busy','reason':'主实例正在更新，等待版本切换完成后检查'}
            return function(request,*args,**kwargs)
    return run


@stable_instance
def inspect(request, expected=None):
    """巡检只使用主实例的只读接口，结果不冒充完整业务验收。"""
    from .thsoctop import http
    product = request['product']
    expected = expected or target(product)
    instance = identity(product, expected)
    checks = []
    for path in ('/ths-octop/api/status', '/ths-octop-watchlist/api/list', '/ths-octop-market/api/overview'):
        try:
            response = http(instance['origin']+path)
            data = response.get('data') or {}
            checks.append({'name': path, 'status': 'pass' if response.get('ok') else 'blocked',
                'sections': {k: {f: v.get(f) for f in ('status', 'stale', 'errorCode', 'message')}
                    for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {},
                'reason': response.get('error') or ''})
        except Exception as exc:
            checks.append({'name': path, 'status': 'blocked', 'reason': str(exc)})
    changed = identity(product, expected) != instance or target(product) != expected
    result = {'status': 'pass' if all(c['status']=='pass' for c in checks) else 'blocked',
        'reason': 'master 实例只读接口巡检完成' if all(c['status']=='pass' for c in checks) else 'master 实例部分只读接口检查未通过',
        'instance': instance, 'source_commit': expected, 'checks': checks}
    if changed:
        result.update(status='blocked',reason='巡检期间主实例或 master 版本变化，本次证据无效')
    from review_core import Store
    from .evidence import Recorder
    recorder=Recorder(Store(Path(request['state_root']).parent),product['id'],'master 实例只读巡检',
        '核对运行提交并读取状态、自选和市场接口；不代替界面业务验收',environment='master')
    for item in checks:
        recorder.step(item['name'],'GET '+item['name'],'接口返回有效业务结果',
            item.get('reason') or '接口已响应',status=item['status'],details={'instance':instance,'check':item})
    recorder.finish(result['status'],result['reason'])
    result['inspection_id']=recorder.inspection['id']
    result['signals'] = [{'source': 'inspection', 'code': 'MASTER_INSPECTION', 'component': 'finance-assistant',
        'version': expected+':'+str(int(time.time()//product.get('policy', {}).get('inspection_seconds', 3600))),
        'summary': 'master 实例只读巡检', 'evidence': {'instance': instance, 'checks': checks}}]
    return result


@stable_instance
def acceptance(request):
    """合入 master 后在同一主实例逐项执行原需求效果断言。"""
    from .project import generic
    if generic(request['product']):
        from .generic_adapter import acceptance as accept
        return accept(request)
    from .probes import check
    from .thsoctop import http
    product = request['product']
    expected = target(product)
    instance = identity(product, expected)
    requirements = request.get('requirements', [])
    audits = []
    for requirement in requirements:
        merged = requirement.get('merge_sha')
        if not merged or subprocess.run(['git', '-C', product['delivery_repository'], 'merge-base',
                '--is-ancestor', merged, expected], capture_output=True).returncode:
            audits.append({'requirement_id': requirement['id'], 'title': requirement.get('title',requirement['id']), 'status': 'blocked', 'reason': '需求尚未合入当前 master'})
            continue
        probes = requirement.get('resolution_probes', [])
        if not probes or any(not isinstance(p, dict) for p in probes):
            audits.append({'requirement_id': requirement['id'], 'title': requirement.get('title',requirement['id']), 'status': 'blocked', 'reason': '缺少原需求的只读效果断言，不能以健康检查代替最终验收'})
            continue
        checks = []
        for probe in probes:
            try:
                passed = check([probe], lambda path: http(instance['origin']+path))
                checks.append({'name': probe['path'], 'assertion': probe, 'status': 'pass' if passed else 'fail'})
            except Exception as exc:
                checks.append({'name': probe.get('path'), 'status': 'blocked', 'reason': str(exc)})
        status = 'fail' if any(c['status']=='fail' for c in checks) else 'blocked' if any(c['status']=='blocked' for c in checks) else 'pass'
        audits.append({'requirement_id': requirement['id'], 'title': requirement.get('title',requirement['id']), 'status': status, 'checks': checks})
    if target(product) != expected or identity(product, expected) != instance:
        return {'status': 'blocked', 'reason': '最终复验期间 master 或运行实例变化，证据失效', 'audits': audits}
    status = 'fail' if any(a['status']=='fail' for a in audits) else 'blocked' if not audits or any(a['status']=='blocked' for a in audits) else 'pass'
    reason='；'.join(a['title']+'：'+a.get('reason','效果断言未通过，请查看检查回执') for a in audits if a['status']!='pass')
    return {'status': status, 'reason': 'master 实例最终复验通过' if status=='pass' else reason or '没有可执行最终复验的已合入需求',
        'instance': instance, 'audits': audits, 'checks': [c for a in audits for c in a.get('checks', [])]}


def prepare(request, expected):
    """仅复用与 master 内容完全相同的已验收 release 产物，保留原证据。"""
    from review_core import Store
    from .store import Ledger
    from .thsoctop import manifest_hash
    product = request['product']
    ledger = Ledger(Store(Path(request['state_root']).parent))
    repo = product['delivery_repository']
    tree = git(repo, 'rev-parse', expected+'^{tree}')
    selected = None
    for batch in ledger.list('deliveries'):
        if batch['product_id'] != product['id'] or batch['status'] != 'online':
            continue
        proof = batch.get('validation_pass') or {}
        for receipt in reversed(batch.get('receipts', [])):
            result = receipt['result']
            if receipt['action'] not in ('validate', 'validate_release') or result.get('status') != 'pass' or not result.get('manifest'):
                continue
            if result.get('head_sha') != proof.get('head_sha') or result.get('base_sha') != proof.get('base_sha'):
                continue
            manifest = json.loads(Path(result['manifest']).read_text())
            if manifest.get('commit') != result['head_sha'] or git(repo, 'rev-parse', manifest['commit']+'^{tree}') != tree:
                continue
            selected = manifest
            break
        if selected:
            break
    if not selected:
        raise ValueError('master 缺少内容一致且已通过业务验收的构建产物，不能安装未经验证的版本')
    source = Path(selected['root'])
    if not selected.get('checks') or any(c.get('status')!='pass' for c in selected['checks'] if c.get('required', True)):
        raise ValueError('master 对应构建缺少完整业务验收证据')
    if manifest_hash(source/'runtime') != selected['runtime_hash'] or manifest_hash(source/'thsoctop.app') != selected['app_hash']:
        raise ValueError('master 对应的验收产物发生变化')
    root = Path(request['state_root'])/'master-builds'/product['id']/expected
    path = root/'manifest.json'
    if not path.exists():
        root.mkdir(parents=True, exist_ok=True)
        for name in ('runtime', 'thsoctop.app'):
            dest = root/name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(source/name, dest, symlinks=True)
        profile = root/'support/dsh/profiles/octop/package.json'
        profile.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source/'support/dsh/profiles/octop/package.json', profile)
        ident = 'master-'+expected
        atomic(root/'runtime/autopilot-release.json', {'id': ident, 'commit': expected})
        atomic(path, selected | {'id': ident, 'commit': expected, 'root': str(root),
            'validated_commit': selected['commit'], 'source_tree': tree,
            'runtime_hash': manifest_hash(root/'runtime')})
    return str(path)


def sync(request):
    """主实例在持续空闲后原位更新，沿用发布日志与失败回滚。"""
    from .thsoctop import idle, main
    product = request['product']
    expected = target(product)
    try:
        return {'status': 'pass', 'instance': identity(product, expected), 'reason': 'master 实例版本已就绪'}
    except (OSError, ValueError):
        pass
    readiness = idle(product)
    if readiness['status'] != 'pass':
        return {'status': 'busy', 'reason': '等待主实例空闲后更新：'+readiness.get('reason', '')}
    since = product.get('master_idle_since')
    if not since or time.time()-since < product.get('policy', {}).get('idle_seconds', 300):
        return {'status': 'busy', 'reason': '等待主实例持续空闲后更新', 'master_idle_since': since or time.time()}
    manifest = prepare(request, expected)
    if target(product) != expected:
        return {'status': 'busy', 'reason': '准备更新期间 master 变化，等待重新核对'}
    record = {'id': 'master-'+expected, 'release_id': 'master-'+expected, 'commit': expected, 'manifest': manifest,
              'master_commit': expected}
    result = main('publish', request | {'record': record})
    if result['status'] == 'pass':
        result['instance'] = identity(product, expected)
    return result


def pending(ledger, product):
    """重启后从已合入的需求恢复最终验收队列，历史通过不重复执行。"""
    commit=(product.get('master_environment') or {}).get('commit')
    return [r for r in ledger.list('requirements') if r['product_id']==product['id'] and r['status']=='online'
            and r.get('merge_sha') and (not r.get('final_acceptance') or
            (commit and r['final_acceptance'].get('status')!='pass'
             and (r['final_acceptance'].get('instance') or {}).get('commit')!=commit))]


def consume(ledger, product, result):
    from .store import redact
    for audit in result.get('audits', []):
        requirement = ledger.get('requirements', audit['requirement_id'])
        if requirement['product_id'] != product['id']:
            raise ValueError('最终验收需求不属于当前项目')
        proof = redact(audit | {'instance': result.get('instance'), 'at': time.time()})
        if result.get('status') == 'blocked' and not result.get('instance'):
            proof.update(status='blocked', reason=result.get('reason'))
        ledger.update('requirements', requirement['id'], requirement['version'], {'final_acceptance': proof})
