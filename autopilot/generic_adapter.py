"""通用命令项目适配器：隔离验证、版本产物及独立更新器交接。"""
import hashlib
import json
from contextlib import nullcontext
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
    from .acceptance_scope import pre_release
    value['acceptance_scope'] = pre_release(request.get('requirement'), record)
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
                    runtime = (product.get('worker_runtime') or independent_runtime()) if product.get('test_execution') == 'local' else independent_runtime(request.get('state_root'))
                    root.mkdir(parents=True)
                    git(repo, 'worktree', 'add', '--detach', str(workspace), expected)
                    config=product['project_config']
                    try:
                        if product.get('test_execution') == 'local':
                            from .local_testing import environment, run as local_run
                            env = environment(root/'execution', runtime=product.get('worker_runtime'))
                            for index, argv in enumerate(config['commands'].get('install', [])):
                                installed = local_run(argv, workspace, root/'execution', 'install-'+str(index), env=env)
                                if installed['status'] != 'pass':
                                    return installed
                            return local_run(config['acceptance_checks'][path[6:]], workspace, root/'execution', 'assertion', env=env)
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
        # 与 delivery_review 对齐：存在导入基线摘要时，控制器先自算工作区摘要并核对；
        # 不一致立即 blocked，绝不启动验证模型，也不用字符串匹配伪造通过。
        baseline_digest = record.get('baseline_source_digest')
        if baseline_digest and before != baseline_digest:
            return {'status': 'blocked',
                'reason': '工作区源码摘要与导入基线不一致，不能以基线验收条件启动独立验证',
                'source_digest': before, 'baseline_source_digest': baseline_digest, 'checks': []}
        from .test_chains import settings as computer_settings
        if product.get('test_execution') == 'local' or computer_settings(product)['enabled']:
            from .local_testing import verification
            session = verification(request, root/'checks')
        else:
            session = nullcontext(verify(record['workspace'], root/'checks', product['project_config'],
                        runtime=independent_runtime(request.get('state_root'))))
        with session as result:
            if result['status'] != 'pass':
                from .failures import verification_failure
                return verification_failure(result)
            if digest(record['workspace']) != before:
                return result | {'status': 'fail', 'reason': '验证期间源码发生变化', 'checks': result['checks']}
            from .test_chain_worker import verification_checks
            ui_checks = verification_checks(request, result.get('instance'))
            result['checks'].extend(ui_checks)
            if any(c['status'] != 'pass' for c in ui_checks):
                failed = next(c for c in ui_checks if c['status'] != 'pass')
                return result | {'status':failed['status'], 'reason':failed['reason'], 'failure_kind':failed['failure_kind'], 'retryable':False}
            from .codex_executor import execute as evaluate, verification_material
            payload = request
            supplied = request.get('verification')
            if not (isinstance(supplied, dict) and supplied.get('diff_file')):
                # 独立验证必须以控制器侧预生成的 base..commit 差异为自包含事实证据。
                # 不再依赖下游按 adapter_spec.kind 决定是否生成；缺差异时直接阻断，
                # 绝不让验证模型在无差异证据的情况下空转。
                try:
                    facts_record = record
                    if baseline_digest:
                        facts_record = record | {'source_digest': before, 'baseline_source_digest': baseline_digest}
                    material = verification_material(record['workspace'], facts_record, root)
                    if baseline_digest:
                        material['source_digest'] = before
                        material['baseline_source_digest'] = baseline_digest
                        material['baseline_digest_verified'] = True
                    payload = request | {'verification': material}
                except RuntimeError as exc:
                    return result | {'status': 'blocked', 'reason': str(exc), 'checks': result['checks']}
            from .quality import validate as validate_quality
            if result.get('quality') or product['project_config'].get('workflow_version') == 1:
                validate_quality(result.get('quality'), record['workspace'], product['project_config'], trusted_root=request['state_root'])
            judged = evaluate('validate', payload | {'checks': result['checks'], 'quality': result.get('quality'), 'test_instance': result.get('instance')})
            # 失败/阻塞回执必须可离线核对：把控制器落盘的自包含差异事实并入
            # 「独立业务验证」的 evidence，与 delivery_review.verify 的字段保持一致。
            # 保留 judged 的 status/reason/summary/provider/model，字段缺失用空值占位。
            material = payload.get('verification') if isinstance(payload.get('verification'), dict) else {}
            diff_path = Path(material.get('diff_file') or '')
            diff_hash = hashlib.sha256(diff_path.read_bytes()).hexdigest() if diff_path.is_file() else ''
            enrichment = {'diff_file': material.get('diff_file', ''), 'facts_file': material.get('facts_file', ''),
                          'diff_sha256': diff_hash, 'changed_files': material.get('changed_files') or [],
                          'merge_base': material.get('merge_base'), 'source': material.get('source'),
                          'source_digest': material.get('source_digest'),
                          'baseline_source_digest': material.get('baseline_source_digest')}
            checks = result['checks'] + [{'name': '独立业务验证', 'status': judged['status'], 'required': True,
                                          'evidence': judged | enrichment}]
            if digest(record['workspace']) != before:
                return result | {'status': 'fail', 'reason': '业务验收期间源码发生变化', 'checks': result['checks']}
            from .verification_findings import defer
            finding = defer(request, judged)
            if finding:
                checks[-1].update(required=False, disposition='backlog', requirement_id=finding['id'])
                result.update(reason='必需测试通过；功能缺陷已进入高优先级需求池', findings=[finding['id']])
            if judged['status'] != 'pass' and not finding:
                return result | {'status': judged['status'], 'reason': judged.get('reason', '独立验证未通过'), 'checks': checks,
                        'collaboration_requests': judged.get('collaboration_requests', [])}
            from .acceptance_scope import pre_release
            return result | {'checks': checks, 'manifest': package(request, checks, root),
                             'acceptance_scope': pre_release(request.get('requirement'), record, result.get('instance'))}
    if action == 'inspect':
        import uuid
        from .role_skills import bind
        from .onboarding import health
        folder = Path(request['state_root'])/'watch'/uuid.uuid4().hex
        folder.mkdir(parents=True)
        _, snapshot = bind('discover', folder, {}, '只读运行巡检；不执行编译、测试或源码体检。')
        checked = probe(product)
        checks = [{'name': '运行身份', 'step_id': 'identity', 'status': checked['status'], 'reason': checked.get('reason', '')}]
        if checked['status'] == 'pass':
            try:
                origin = product['deployment']['origin']
                path = product.get('project_config', {}).get('health_path', '/')
                healthy = health(origin, path)
                checks.append({'name': '运行健康', 'step_id': 'signals', 'status': 'pass' if healthy else 'fail'})
                for path in product.get('project_config', {}).get('readonly_paths', []):
                    checks.append({'name': path, 'step_id': 'signals', 'status': 'pass', 'evidence': http(product, path)})
            except (OSError, ValueError) as exc:
                checks.append({'name': '运行信号', 'step_id': 'signals', 'status': 'blocked', 'reason': str(exc)})
        failed = next((c for c in checks if c['status'] != 'pass'), None)
        if failed:
            checked.update(status=failed['status'], reason=failed.get('reason') or '运行巡检未通过')
        checked['checks'] = checks
        if snapshot.get('workflow'):
            checked['workflow'] = snapshot['workflow'] | {'status': checked['status'], 'steps': [
                step | {'status': checks[0]['status'] if step['id'] == 'identity' else
                        ('pass' if not failed else 'blocked') if step['id'] == 'signals' else
                        'pass' if step['id'] == 'evidence' else 'pending'}
                for step in snapshot['workflow']['steps']]}
        checked['role_skill'] = snapshot
        from review_core import Store
        from .evidence import Recorder
        recorder = Recorder(Store(Path(request['state_root']).parent), product['id'], '项目运行巡检', '核对运行实例和业务信号', environment='master')
        for check in checks:
            recorder.step(check['name'], '读取登记的只读接口', '取得真实运行证据', check.get('reason', ''), status=check['status'], details=check)
        recorder.finish(checked['status'], checked.get('reason', ''))
        atomic(folder/'result.json', checked)
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
