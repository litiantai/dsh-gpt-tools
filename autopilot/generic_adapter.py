"""通用命令项目适配器：隔离验证、版本产物及独立更新器交接。"""
import json
from pathlib import Path
import shutil
import subprocess
import time
from urllib.request import build_opener

from .call import atomic
from .onboarding import verify, NoRedirect
from .project import manifest_hash
from .workspace import git, digest, source_files


def http(product, path):
    from .probes import validate
    validate([path], product)
    origin = product.get('deployment', {}).get('origin', '')
    from urllib.parse import urlsplit
    parsed = urlsplit(origin)
    if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.path or parsed.username:
        raise ValueError('运行实例必须为登记的本机 HTTP 源')
    with build_opener(NoRedirect()).open(origin+path, timeout=5) as response:
        return json.load(response)


def identity(product, expected=None):
    path = product.get('project_config', {}).get('identity_path')
    if not path:
        raise ValueError('项目尚未配置运行版本身份接口')
    data = http(product, path)
    if data.get('product_id') != product['id'] or not data.get('commit'):
        raise ValueError('运行实例身份不匹配')
    if expected and data['commit'] != expected:
        raise ValueError('运行实例尚未更新到目标提交')
    return {'instance_role': 'master', 'commit': data['commit'], 'release_id': data.get('release_id'),
            'origin': product['deployment']['origin']}


def probe(product):
    try:
        return {'status': 'pass', 'instance': identity(product), 'reason': '运行身份检查通过'}
    except (OSError, ValueError) as exc:
        return {'status': 'blocked', 'reason': str(exc)}


def validate_manifest(path):
    manifest = json.loads(Path(path).read_text())
    if manifest.get('version') != 1 or not manifest.get('checks') or any(c.get('status') != 'pass' for c in manifest['checks'] if c.get('required', True)):
        raise ValueError('候选缺少完整验收记录')
    if manifest_hash(manifest['artifact']) != manifest['artifact_hash']:
        raise ValueError('验收后产物发生变化')
    return manifest


def package(request, checks, root):
    product, record = request['product'], request['record']
    workspace = Path(record['workspace']); artifact = root/'artifact'
    if artifact.exists():
        raise ValueError('候选产物已存在，禁止覆盖已验收版本')
    artifact.mkdir()
    excluded = product.get('project_config', {}).get('exclude', [])
    for rel in source_files(workspace):
        if any(str(rel) == p or str(rel).startswith(p.rstrip('/')+'/') for p in excluded):
            continue
        dest = artifact/rel; dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(workspace/rel, dest)
    for name in product.get('project_config', {}).get('artifacts', []):
        rel = Path(name)
        if rel.is_absolute() or '..' in rel.parts or not (workspace/rel).resolve().is_relative_to(workspace.resolve()):
            raise ValueError('发布产物必须位于候选工作区内')
        src, dest = workspace/rel, artifact/rel
        if not src.exists():
            raise ValueError('构建产物缺失：'+name)
        if dest.exists():
            shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
        if src.is_dir():
            shutil.copytree(src, dest, symlinks=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(src, dest)
    marker = {'product_id': product['id'], 'commit': record['commit'], 'release_id': record['id']}
    atomic(artifact/'autopilot-release.json', marker)
    value = {'version': 1, 'kind': 'command', **marker, 'root': str(root), 'artifact': str(artifact),
             'artifact_hash': manifest_hash(artifact), 'checks': checks, 'source_digest': digest(workspace),
             'source_tree': git(workspace, 'rev-parse', record['commit']+'^{tree}'),
             'database_compatibility': product.get('project_config', {}).get('database_compatibility', 'unknown')}
    atomic(root/'manifest.json', value)
    return str(root/'manifest.json')


def acceptance(request):
    from .master import target
    from .probes import check
    product = request['product']; expected = target(product)
    instance = identity(product, expected)
    audits = []
    for req in request.get('requirements', []):
        probes = req.get('resolution_probes', [])
        passed = bool(probes) and bool(req.get('merge_sha'))
        if passed:
            passed = subprocess.run(['git', '-C', product['delivery_repository'], 'merge-base', '--is-ancestor', req['merge_sha'], expected], capture_output=True).returncode == 0
        checks = []
        for item in probes:
            try:
                def fetch(path):
                    if not path.startswith('check:'):
                        return http(product, path)
                    from .isolated import run, independent_runtime
                    from .workspace import snapshot
                    import uuid
                    root = Path(request['state_root'])/'final-checks'/str(uuid.uuid4())
                    workspace = root/'workspace'
                    repo = product['delivery_repository']
                    runtime = independent_runtime(request.get('state_root'))
                    root.mkdir(parents=True)
                    git(repo, 'worktree', 'add', '--detach', str(workspace), expected)
                    config=product['project_config']
                    try:
                        for index, argv in enumerate(config['commands'].get('install', [])):
                            installed=run(argv, workspace, root/'execution', 'install-'+str(index), install=True, runtime=runtime)
                            if installed['status']!='pass':
                                return installed
                        return run(config['acceptance_checks'][path[6:]], workspace, root/'execution', 'assertion', ports=config.get('test_ports', []), runtime=runtime)
                    finally:
                        git(repo, 'worktree', 'remove', '--force', str(workspace))
                ok = check([item], fetch, product)
                checks.append({'name': item['path'], 'status': 'pass' if ok else 'fail'})
                passed = passed and ok
            except (ValueError, OSError) as exc:
                passed = False; checks.append({'name': item.get('path'), 'status': 'blocked', 'reason': str(exc)})
        audits.append({'requirement_id': req['id'], 'status': 'pass' if passed else 'blocked', 'checks': checks,
                       'reason': '' if passed else '缺少原需求的有效只读效果断言或检查未通过'})
    if identity(product, expected) != instance or target(product) != expected:
        return {'status': 'blocked', 'reason': '复验期间版本发生变化', 'audits': audits}
    return {'status': 'pass' if audits and all(a['status'] == 'pass' for a in audits) else 'blocked',
            'instance': instance, 'audits': audits}


def request_update(request, manifest_path):
    manifest = validate_manifest(manifest_path)
    product = request['product']; root = Path(request['state_root'])/'updates'
    if product.get('deployment', {}).get('kind') != 'self':
        return {'status': 'blocked', 'reason': '项目未配置部署适配器'}
    root.mkdir(parents=True, exist_ok=True)
    path = root/(manifest['release_id']+'.json')
    if not path.exists():
        atomic(path, {'status': 'queued', 'manifest': manifest_path, 'product_id': product['id'],
                      'requested_at': time.time(), 'expected_commit': manifest['commit']})
    job = json.loads(path.read_text())
    if job['status'] in ('completed', 'rolled_back', 'blocked'):
        return {'status': 'pass' if job['status'] == 'completed' else 'blocked', 'reason': job.get('reason', ''), 'update': str(path)}
    return {'status': 'deferred', 'reason': '独立更新器正在处理版本切换', 'update': str(path)}


def master_sync(request):
    from .master import target
    from review_core import Store
    from .store import Ledger
    product = request['product']; expected = target(product)
    try:
        return {'status': 'pass', 'instance': identity(product, expected)}
    except (ValueError, OSError):
        pass
    repo = product['delivery_repository']; tree = git(repo, 'rev-parse', expected+'^{tree}')
    ledger = Ledger(Store(Path(request['state_root']).parent))
    for batch in ledger.list('deliveries'):
        if batch['product_id'] != product['id'] or batch['status'] != 'online':
            continue
        proof = batch.get('validation_pass', {})
        for receipt in reversed(batch.get('receipts', [])):
            result = receipt['result']
            if receipt['action'] not in ('validate','validate_release') or result.get('status') != 'pass' or not result.get('manifest'):
                continue
            if result.get('head_sha') != proof.get('head_sha') or result.get('base_sha') != proof.get('base_sha'):
                continue
            manifest = validate_manifest(result['manifest'])
            if manifest['source_tree'] != tree:
                continue
            # Merge commits can differ while the verified source tree remains identical.
            root = Path(request['state_root'])/'master-builds'/product['id']/expected
            dest = root/'manifest.json'
            if not dest.exists():
                root.mkdir(parents=True, exist_ok=True)
                artifact = root/'artifact'
                shutil.copytree(manifest['artifact'], artifact, symlinks=True)
                atomic(artifact/'autopilot-release.json', {'product_id': product['id'], 'commit': expected, 'release_id': 'master-'+expected})
                atomic(dest, manifest | {'commit': expected, 'release_id': 'master-'+expected, 'artifact': str(artifact),
                                         'artifact_hash': manifest_hash(artifact), 'root': str(root)})
            result=request_update(request, str(dest))
            return result | {'status':'busy'} if result['status']=='deferred' else result
    return {'status': 'blocked', 'reason': '主分支缺少内容一致且验收通过的产物'}


def execute(action, request):
    product, record = request['product'], request['record']
    if action not in product.get('adapter_spec', {}).get('capabilities', []):
        return {'status': 'blocked', 'reason': '项目不支持 '+action}
    if action in ('probe', 'investigate'):
        return probe(product)
    if action == 'verify':
        from .isolated import independent_runtime
        root = Path(request['state_root'])/'candidates'/product['id']/record['id']
        root.mkdir(parents=True, exist_ok=True)
        before = digest(record['workspace'])
        result = verify(record['workspace'], root/'checks', product['project_config'],
                        runtime=independent_runtime(request.get('state_root')))
        if result['status'] != 'pass':
            return result
        if digest(record['workspace']) != before:
            return {'status': 'fail', 'reason': '验证期间源码发生变化', 'checks': result['checks']}
        from .codex_executor import execute as evaluate
        judged = evaluate('validate', request | {'checks': result['checks']})
        checks = result['checks'] + [{'name': '独立业务验证', 'status': judged['status'], 'required': True, 'evidence': judged}]
        if judged['status'] != 'pass':
            return {'status': judged['status'], 'reason': judged.get('reason', '独立验证未通过'), 'checks': checks}
        return result | {'checks': checks, 'manifest': package(request, checks, root)}
    if action == 'inspect':
        checked = probe(product)
        from review_core import Store
        from .evidence import Recorder
        recorder = Recorder(Store(Path(request['state_root']).parent), product['id'], '项目运行巡检', '核对运行实例版本', environment='master')
        recorder.step('版本身份', '读取登记的只读接口', '身份与项目一致', checked.get('reason', ''), status=checked['status'], details=checked)
        recorder.finish(checked['status'], checked.get('reason', ''))
        return checked | {'inspection_id': recorder.inspection['id'], 'signals': [{'source': 'inspection', 'code': 'PROJECT_HEALTH',
            'component': 'runtime', 'version': str(int(time.time()//3600)), 'summary': '项目运行巡检', 'evidence': checked}]}
    if action == 'master_sync':
        return master_sync(request)
    if action == 'final_acceptance':
        return acceptance(request)
    if action == 'publish':
        return request_update(request, record['manifest'])
    if action == 'idle':
        return {'status': 'pass', 'reason': '实际进程退出由独立更新器切换前核对'}
    if action == 'observe':
        from .probes import check
        instance=identity(product, record['commit'])
        probes=request.get('requirement',{}).get('resolution_probes',[])
        if any(not isinstance(p,dict) or p.get('path','').startswith('check:') for p in probes):
            return {'status':'blocked','reason':'直接发布的上线观察需提供运行实例的只读业务字段断言；命令断言使用 Git 主分支复验'}
        passed=check(probes,lambda path:http(product,path),product)
        return {'status':'pass' if passed else 'fail','instance':instance,'problem_resolved':passed,'reason':'原需求效果断言通过' if passed else '原需求效果断言未通过'}
    return {'status': 'blocked', 'reason': '项目能力尚未配置实现：'+action}
