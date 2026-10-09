"""仓库接入扫描：独立源码快照、配置识别、启动验证和持久化回执。"""
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler

from .call import atomic
from .isolated import environment, command, run, stop
from .store import redact
from .workspace import git, snapshot

ROOT = Path(__file__).resolve().parents[1]


def detect(workspace):
    root = Path(workspace)
    config = {'version': 1, 'commands': {}, 'evidence': [], 'stacks': [], 'subprojects': [],
              'environment_variables': [], 'external_services': [], 'readonly_paths': [], 'blockers': []}
    commands = config['commands']
    package = root/'package.json'
    if package.is_file():
        data = json.loads(package.read_text())
        scripts = data.get('scripts', {})
        manager = 'pnpm' if (root/'pnpm-lock.yaml').exists() else 'yarn' if (root/'yarn.lock').exists() else 'npm'
        config.update(package_manager=manager, subprojects=data.get('workspaces', []))
        config['stacks'].append('node')
        config['evidence'].append('package.json')
        if manager == 'npm':
            commands['install'] = [['npm', 'ci' if (root/'package-lock.json').exists() else 'install', '--ignore-scripts', '--no-audit', '--no-fund']]
        elif manager == 'pnpm':
            commands['install'] = [['pnpm', 'install', '--frozen-lockfile', '--ignore-scripts']]
        else:
            config['blockers'].append('Yarn 版本与脚本隔离策略需显式配置')
        for phase, names in [('build', ['build']), ('test', ['typecheck', 'test']), ('browser', ['test:e2e'])]:
            commands[phase] = [[manager, 'run', n] for n in names if n in scripts]
        if 'start' in scripts:
            commands['start'] = [[manager, 'run', 'start']]
        elif 'dev' in scripts:
            commands['start'] = [[manager, 'run', 'dev', '--', '--host', '127.0.0.1', '--port', '{port}']]
    if (root/'pyproject.toml').exists() or (root/'requirements.txt').exists():
        config['stacks'].append('python')
        config['evidence'].extend(p for p in ('pyproject.toml', 'requirements.txt') if (root/p).exists())
        if not commands.get('install'):
            config['blockers'].append('Python 依赖需提供可复现的隔离安装命令，不能执行未审核的构建钩子')
    if (root/'pom.xml').exists() or (root/'build.gradle').exists() or (root/'build.gradle.kts').exists():
        config['stacks'].append('java')
        config['evidence'].extend(p for p in ('pom.xml', 'build.gradle', 'build.gradle.kts') if (root/p).exists())
        config['blockers'].append('JVM 项目需配置依赖下载与离线构建命令及启动入口')
    names = git(root, 'ls-files').splitlines()
    variables = set()
    for name in names:
        p = root/name
        if p.suffix not in ('.py', '.js', '.ts', '.mjs', '.yaml', '.yml', '.md') or p.stat().st_size > 100000:
            continue
        text = p.read_text(errors='replace')
        variables.update(re.findall(r'(?:process\.env\.|os\.environ\[\s*[\'"]|os\.environ\.get\([\'"])([A-Z][A-Z0-9_]+)', text))
        if p.name in ('compose.yaml', 'compose.yml', 'docker-compose.yml', 'docker-compose.yaml'):
            config['external_services'].append({'source': name, 'reason': '需要显式隔离服务配置'})
    config['environment_variables'] = sorted(variables)
    path = root/'.autopilot.json'
    if path.exists():
        override = json.loads(path.read_text())
        if override.get('version') != 1:
            raise ValueError('不支持的 .autopilot.json 版本')
        config.update(override)
        config['evidence'].append('.autopilot.json')
    if not config['commands'].get('start'):
        config['blockers'].append('未识别唯一启动命令；请补充 .autopilot.json')
    if len(config['commands'].get('start', [])) > 1:
        config['blockers'].append('多个启动入口，需明确项目服务组合')
    if config['external_services']:
        config['blockers'].append('外部服务未配置隔离实例')
    from .project import validate
    validate({'adapter_spec': {'version': 1, 'kind': 'command', 'capabilities': []}, 'project_config': config})
    return config


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('隔离健康检查不允许重定向')


def health(origin, path='/'):
    parsed = urlsplit(origin)
    if parsed.hostname != '127.0.0.1' or parsed.scheme != 'http' or parsed.username or parsed.path:
        raise ValueError('隔离健康地址必须为本机 HTTP 源')
    from .probes import safe_path
    if not safe_path(path):
        raise ValueError('健康检查路径无效')
    with build_opener(NoRedirect()).open(origin+path, timeout=2) as response:
        return response.status == 200


def startup(workspace, root, config):
    root = Path(root); root.mkdir(parents=True, exist_ok=True)
    port = free_port(); origin = f'http://127.0.0.1:{port}'
    argv = [x.replace('{port}', str(port)).replace('{state}', str(root/'state')) for x in config['commands']['start'][0]]
    proc = None; started = time.time(); result = {'name': 'startup', 'command': argv, 'required': True,
                                                  'status': 'blocked', 'origin': origin}
    log = root/'startup.log'
    try:
        with log.open('w') as stream:
            proc = subprocess.Popen(command(argv, root, workspace, ports=[port]), cwd=workspace,
                                    env=environment(root, port), stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic()+config.get('startup_timeout', 30)
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    result['reason'] = '启动进程提前退出'; break
                try:
                    if health(origin, config.get('health_path', '/')):
                        result.update(status='pass', reason='隔离启动与健康检查通过；业务验收尚未执行')
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(.25)
            else:
                result['reason'] = '启动超时或未使用分配的隔离端口'
            if result['status'] == 'pass' and config.get('web', True):
                image = root/'startup.png'
                script=root/'screenshot.mjs'
                script.write_text((ROOT/'scripts/project-screenshot.mjs').read_text().replace("from '@playwright/test'", "from "+json.dumps(str(Path(workspace)/'node_modules/@playwright/test/index.mjs'))))
                shot = run([config.get('node', 'node'), str(script), origin, str(image)],
                           workspace, root, 'screenshot', ports=[port], timeout=30)
                result['screenshot_check'] = shot
                if shot['status'] != 'pass':
                    result.update(status='blocked', reason='服务已启动，但网页截图未完成')
                else:
                    result['screenshot'] = str(image)
                    result['screenshot_sha256'] = __import__('hashlib').sha256(image.read_bytes()).hexdigest()
    finally:
        if proc:
            stop(proc)
            result.update(exit_code=proc.returncode, stopped=True)
        result.update(started=started, elapsed=time.time()-started, log=str(log))
        if log.exists():
            log.write_text(redact(log.read_text(errors='replace')))
            result['log_tail'] = log.read_text()[-6000:]
    return result


def verify(workspace, root, config):
    checks = []
    if config.get('blockers'):
        return {'status': 'blocked', 'reason': '；'.join(config['blockers']), 'checks': checks}
    for phase in ('install', 'build', 'test', 'browser'):
        for index, argv in enumerate(config['commands'].get(phase, [])):
            if phase == 'install' and not (len(argv)>2 and argv[0] in ('npm','pnpm') and argv[1] in ('ci','install') and '--ignore-scripts' in argv and not any(x.startswith('--ignore-scripts=') for x in argv)):
                return {'status': 'blocked', 'reason': '依赖安装必须禁用生命周期脚本；请配置受支持的隔离安装器', 'checks': checks}
            entry = run(argv, workspace, root, phase+'-'+str(index), install=phase == 'install',
                        ports=config.get('test_ports', []), timeout=config.get('command_timeout', 900))
            checks.append(entry)
            if entry['status'] != 'pass':
                return {'status': entry['status'], 'reason': phase+' 检查未通过', 'checks': checks}
    checks.append(startup(workspace, root, config))
    return {'status': checks[-1]['status'], 'reason': checks[-1].get('reason', ''), 'checks': checks,
            'startup_passed': checks[-1]['status'] == 'pass', 'business_acceptance': 'pending'}


def execute(action, request):
    if action != 'scan':
        raise ValueError('未知仓库扫描操作')
    scan = request['record']; root = Path(request['state_root'])/'scans'/scan['id']
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace = root/'workspace'
    # A retry gets a new ID; existing workspace means the interrupted call must be reconciled.
    if workspace.exists():
        raise ValueError('扫描工作区已存在；请创建关联的新扫描，不重复执行未知副作用')
    source = scan['source']
    if source.startswith('https://'):
        parsed = urlsplit(source)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('仓库 URL 不允许内嵌凭据或查询参数')
        clone = root/'clone'
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', 'clone', '--depth=1', '--', source, str(clone)],
                       check=True, env=environment(root), capture_output=True, timeout=120)
        original = git(clone, 'rev-parse', 'HEAD')
        captured = snapshot(clone, workspace, tracked_only=True)
    else:
        path = Path(source).resolve()
        original = git(path, 'rev-parse', 'HEAD')
        if (path/'.autopilot.json').is_symlink():
            raise ValueError('项目配置不能是符号链接')
        options=json.loads((path/'.autopilot.json').read_text()) if (path/'.autopilot.json').exists() else {}
        captured = snapshot(path, workspace, exclude=options.get('exclude', ['info']))
    config = detect(workspace)
    result = verify(workspace, root/'execution', config)
    result.update(source_commit=original, snapshot_commit=captured['commit'], workspace=str(workspace),
                  configuration=config, configuration_digest=__import__('hashlib').sha256(json.dumps(config, sort_keys=True).encode()).hexdigest())
    atomic(root/'result.json', redact(result))
    return result


def tick(scheduler):
    """扫描沿用外部调用台账，重启仅核对原回执；重试始终建立新记录。"""
    ledger = scheduler.ledger
    current = next((s for s in reversed(ledger.list('scans')) if s['status'] in ('queued', 'running')), None)
    if not current:
        return
    if current.get('call'):
        result = scheduler.call_result(current)
        if result is None:
            return
        current = scheduler.consume('scans', current, result)
        scheduler.change('scans', current, {'result': result, 'reason': result.get('reason', '')}, result['status'])
        return
    if current['status'] == 'running':
        scheduler.change('scans', current, {'reason': '扫描中断且缺少调用回执，请核对后重试'}, 'blocked')
        return
    current = scheduler.change('scans', current, {}, 'running')
    product = {'id': current['product_id'], 'source': str(ROOT)}
    scheduler.start_call('scans', current, product, 'scan', [sys.executable, str(ROOT/'scripts/autopilot-adapter.py'), 'scan'], timeout=7200)


def onboard(ledger, scan, body):
    """登记独立扫描基线，默认仅观察；模型及部署能力必须另行配置。"""
    result = scan['result']; config = result['configuration']
    source = scan['source'] if Path(scan['source']).is_absolute() else result['workspace']
    name = body.get('name') or Path(scan['source']).name.removesuffix('.git')
    product = {'name': name, 'source': source, 'goal': body.get('goal') or '依据真实证据完善项目功能、可靠性和验证流程。',
               'repository': result['workspace'], 'repository_source': scan['source'], 'baseline': result['snapshot_commit'], 'schema_version': 1,
               'adapter_spec': {'version': 1, 'kind': 'command', 'capabilities': ['probe','inspect','verify','investigate']},
               'adapter': [sys.executable, str(ROOT/'scripts/autopilot-adapter.py'), 'command'],
               'executor': [sys.executable, str(ROOT/'scripts/autopilot-adapter.py'), 'executor'],
               'project_config': config, 'config_scan_id': scan['id'], 'runtime_recovery': {'enabled': False}}
    from .store import DEFAULTS
    with ledger.store.transaction() as db:
        latest = ledger.get('scans', scan['id'], db)
        if latest['version'] != scan['version'] or latest.get('product_id'):
            from review_core import Conflict
            raise Conflict('扫描记录已关联项目或已更新')
        created = ledger.create('products', product | {'policy': DEFAULTS | {'inspection_seconds':3600, 'runs_per_day':5, 'tokens_per_day':10000000}}, 'observing', db=db)
        return ledger.update('scans', scan['id'], scan['version'], {'product_id': created['id']}, db=db)
