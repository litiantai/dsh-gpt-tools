"""控制器执行语言质量门禁；只信任持久化且绑定源码的命令回执。"""
import hashlib
import json
import shutil
from pathlib import Path
import uuid

from .call import atomic
from .role_workflows import PHASES, load, modules, sha
from .workspace import digest, git


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source(workspace):
    return {'commit': git(workspace, 'rev-parse', 'HEAD'), 'source_digest': digest(workspace)}


def prepare(workspace, config, root, role='verification', action='verify', phases=PHASES, isolated=True):
    """冻结配置和规则，展开模块检查；已启用但不完整的配置直接阻塞。"""
    root = Path(root); root.mkdir(parents=True, exist_ok=True)
    from .project import validate as validate_config
    validate_config({'adapter_spec': {'version': 1, 'kind': 'command', 'capabilities': []}, 'project_config': config})
    workflow = load(role, action)
    if workflow is None:
        return None
    resolved = modules(workspace, config)
    planned = []
    for phase in phases:
        for module in resolved:
            profile = workflow['profiles'].get(module['stack'], workflow['profiles'].get('generic'))
            if profile is None:
                raise ValueError('工作流缺少通用语言标准')
            requirement = next((p for p in profile['phases'] if p['command'] == phase), None)
            if requirement is None:
                continue
            commands = module.get('commands', {}).get(phase, [])
            if requirement['required'] == 'always' and not commands:
                raise ValueError(f"{module['id']} ({module['stack']}) 缺少必需 {phase} 命令")
            for index, argv in enumerate(commands):
                if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
                    raise ValueError('质量检查命令必须为非空参数数组')
                if phase == 'install' and (isolated or module['stack'] == 'java'):
                    installation(argv, module.get('install_policy'))
                step = f"{module['id']}:{phase}:{index}"
                planned.append({'step_id': step, 'module': module['id'], 'stack': module['stack'],
                    'phase': phase, 'command': argv, 'workspace': module['workspace'],
                    'install_policy': module.get('install_policy'), 'required': True})
    identity = source(workspace)
    snapshot = {'version': 1, 'role': role, 'action': action, 'workflow_hash': workflow['workflow_hash'],
                'resources': workflow['resources'], 'config_hash': sha(config), 'phases': list(phases), 'steps': planned, **identity}
    folder = root / ('quality-' + uuid.uuid4().hex)
    folder.mkdir()
    atomic(folder/'plan.json', snapshot)
    return {'plan': snapshot, 'workflow': workflow, 'root': folder, 'workspace': str(workspace),
            'checks': [], 'config': config}


def installation(argv, policy):
    from .jvm import kind, install_allowed
    if kind(argv):
        if not install_allowed(argv, policy):
            raise ValueError('JVM 依赖准备需显式 install_policy=jvm-resolve 及专用依赖解析命令')
        return
    isolated = (len(argv) > 2 and argv[0] in ('npm', 'pnpm') and argv[1] in ('ci', 'install')
                and '--ignore-scripts' in argv and not any(x.startswith('--ignore-scripts=') for x in argv))
    if not isolated:
        raise ValueError('依赖安装必须禁用生命周期脚本；请配置受支持的隔离安装器')
    if argv[0] == 'npm' and argv[1] != 'ci' and policy != 'explicit-unpinned':
        raise ValueError('未固定依赖的 npm install 需显式声明 install_policy=explicit-unpinned')


def execute(session, phases, runner, *, isolated=True):
    """按声明顺序调用现有执行器；编译失败立即停止，不接收模型自报检查。"""
    from .jvm import prepare as jvm_command
    plan = session['plan']
    for step in plan['steps']:
        if step['phase'] not in phases:
            continue
        previous = next((c for c in session['checks'] if c['step_id'] == step['step_id']), None)
        if previous and previous['status'] == 'pass':
            continue
        name = step['phase'] + '-' + step['step_id'].rsplit(':', 1)[1]
        if step['module'] != 'root':
            name = step['module'] + '-' + name
        execution_root = session['root'] / step['module']
        execution_root.mkdir(parents=True, exist_ok=True)
        try:
            if step['phase'] == 'install' and (isolated or step['stack'] == 'java'):
                installation(step['command'], step.get('install_policy'))
            executable = step['command'][0]
            available = (Path(step['workspace']) / executable).is_file() if '/' in executable else shutil.which(executable)
            if not available:
                from .jvm import MissingRuntime
                raise MissingRuntime(Path(executable).name, '请安装项目要求的本机运行时，并加入 PATH 或配置命令绝对路径；安装后重试。')
            argv = jvm_command(step['command'], execution_root, step['workspace'], install=step['phase'] == 'install')
            check = runner(argv, step['workspace'], execution_root, name, step['phase'])
            from .jvm import kind, classify
            if kind(argv):
                check = classify(check)
        except (OSError, ValueError) as exc:
            check = {'name': name, 'command': step['command'], 'status': 'blocked', 'exit_code': None, 'reason': str(exc)}
            if hasattr(exc, 'hint'):
                check.update(failure_kind='environment', missing_tools=[exc.tool], install_hint=exc.hint)
        check.update({key: step[key] for key in ('step_id', 'module', 'stack', 'phase', 'required')})
        check.update(role=plan['role'], workflow_version=plan['version'], workflow_hash=plan['workflow_hash'],
                     commit=plan['commit'], source_digest=plan['source_digest'], declared_command=step['command'])
        if check.get('log') and Path(check['log']).is_file():
            check['log_sha256'] = file_hash(check['log'])
        if check.get('status') == 'pass' and (check.get('exit_code') != 0 or not check.get('log_sha256')):
            check.update(status='blocked', reason='命令缺少真实退出码或日志，不能通过质量检查')
        session['checks'].append(check)
        if check['status'] != 'pass':
            return check
    return None


def finish(session, *, complete=True):
    """保存控制器回执；源代码变化、漏执行或日志缺失均不得通过。"""
    if session is None:
        return None
    plan = session['plan']
    checks = session['checks']
    failed = next((c for c in checks if c['status'] != 'pass'), None)
    missing = [s['step_id'] for s in plan['steps'] if s['step_id'] not in {c['step_id'] for c in checks}]
    status = failed['status'] if failed else 'blocked' if complete and missing else 'pass' if complete else 'pending'
    reason = failed.get('reason', '') if failed else '缺少必需质量步骤：' + ', '.join(missing) if complete and missing else ''
    if source(session['workspace']) != {k: plan[k] for k in ('commit', 'source_digest')}:
        status, reason = 'blocked', '质量检查期间源码发生变化，检查证据失效'
    proof = {'version': 1, 'status': status, 'reason': reason, 'role': plan['role'],
             'workflow_hash': plan['workflow_hash'], 'config_hash': plan['config_hash'],
             'commit': plan['commit'], 'source_digest': plan['source_digest'], 'checks': checks,
             'plan_hash': file_hash(session['root']/'plan.json')}
    atomic(session['root']/'receipt.json', proof)
    return proof | {'receipt': str(session['root']/'receipt.json'), 'receipt_hash': file_hash(session['root']/'receipt.json')}


def validate(proof, workspace, config, *, trusted_root=None, phases=PHASES):
    """只复用控制器目录内的完整回执；模型 JSON、旧源码及被改动日志均拒绝。"""
    if not isinstance(proof, dict) or not proof.get('receipt') or not proof.get('receipt_hash'):
        raise ValueError('缺少控制器质量回执')
    path = Path(proof['receipt']).resolve()
    if trusted_root and not path.is_relative_to(Path(trusted_root).resolve()):
        raise ValueError('质量回执不在控制器证据目录内')
    if file_hash(path) != proof['receipt_hash']:
        raise ValueError('质量回执已被修改')
    saved = json.loads(path.read_text())
    plan_path = path.parent/'plan.json'
    if file_hash(plan_path) != saved['plan_hash']:
        raise ValueError('质量工作流快照已被修改')
    plan = json.loads(plan_path.read_text())
    if not set(phases) <= set(plan.get('phases', [])):
        raise ValueError('质量回执未覆盖当前阶段的全部检查')
    if sha(plan['resources']) != plan['workflow_hash'] or saved['workflow_hash'] != plan['workflow_hash']:
        raise ValueError('质量规则快照不一致')
    if saved['status'] != 'pass' or saved['config_hash'] != sha(config) or any(saved.get(k) != v for k, v in source(workspace).items()):
        raise ValueError('质量检查未通过或源码、配置已变化')
    steps = plan['steps']
    if len(steps) != len(saved['checks']):
        raise ValueError('质量回执缺少必需步骤')
    for step, check in zip(steps, saved['checks']):
        if (check.get('step_id') != step['step_id'] or check.get('declared_command') != step['command']
                or check.get('status') != 'pass' or check.get('exit_code') != 0
                or not check.get('log') or file_hash(check['log']) != check.get('log_sha256')):
            raise ValueError('质量步骤证据缺失或失效')
    return saved


def summary(session, proof):
    if session is None:
        return None
    workflow = session['workflow']
    checks = proof['checks']
    return {'version': workflow['version'], 'role': workflow['role'], 'workflow_hash': workflow['workflow_hash'],
            'status': proof['status'], 'reason': proof['reason'], 'steps': [
                s | next(({k:c.get(k) for k in ('status','reason','log','exit_code')} for c in checks if c['step_id'] == s['step_id']), {'status': 'pending'})
                for s in session['plan']['steps']]}


def run(workspace, config, root, *, role='code-review', action='review', local=False, runtime=None):
    """代码评审前执行质量检查，不启动服务；返回可供后续阶段复核的证据。"""
    try:
        session = prepare(workspace, config, root, role, action, phases=PHASES[:-1], isolated=not local)
        if session is None:
            return {'status': 'pass', 'checks': []}
        def runner(argv, cwd, folder, name, phase):
            if local:
                from .local_testing import environment, run as spawn
                env = environment(folder, runtime=runtime)
                from .jvm import kind, environment as jvm_environment
                if kind(argv):
                    env.update(jvm_environment(folder))
                return spawn(argv, cwd, folder, name, env=env, timeout=config.get('command_timeout', 900))
            from .onboarding import install_check
            from .isolated import run as spawn
            if phase == 'install':
                return install_check(argv, cwd, folder, name, timeout=config.get('command_timeout', 900), runtime=runtime)
            return spawn(argv, cwd, folder, name, ports=config.get('test_ports', []), timeout=config.get('command_timeout', 900), runtime=runtime)
        execute(session, PHASES[:-1], runner, isolated=not local)
        proof = finish(session)
        return {'status': proof['status'], 'reason': proof['reason'], 'checks': proof['checks'],
                'quality': proof, 'workflow': summary(session, proof)}
    except (OSError, ValueError) as exc:
        return {'status': 'blocked', 'reason': str(exc), 'checks': []}
