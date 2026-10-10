"""由本机控制器运行分支测试；测试实例、日志和回执绑定实际 Git 提交。"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import time

from .call import atomic
from .isolated import independent_runtime, stop
from .onboarding import free_port, health
from .store import redact
from .workspace import git


def identity(request):
    """核对 release/feat 工作区，拒绝以其他分支或过期提交代替验收。"""
    record = request['record']
    workspace = Path(record['workspace']).resolve()
    branch = git(workspace, 'symbolic-ref', '--quiet', '--short', 'HEAD')
    commit = git(workspace, 'rev-parse', 'HEAD')
    role = 'release' if record.get('flow') == 'review_before_release' and record.get('review_target') != 'feature' else 'feature'
    prefix = 'release-' if role == 'release' else 'feat-'
    if not branch.startswith(prefix) or (record.get('branch') and record['branch'] != branch):
        raise ValueError(f'{role} 测试必须使用对应 {prefix} 分支，当前为 {branch}')
    if commit != record.get('commit'):
        raise ValueError('本机测试提交与待验收提交不一致')
    if git(workspace, 'status', '--porcelain'):
        raise ValueError('本机测试工作区存在未提交改动')
    return {'instance_role': role, 'branch': branch, 'commit': commit, 'workspace': str(workspace), 'execution': 'local'}


def environment(root, *, port=0, origin='', runtime=None):
    """使用本机工具链和真实 HOME，测试状态与临时数据单独存放。"""
    if os.environ.get('DSH_PROJECT_ISOLATED'):
        raise ValueError('本机测试必须由控制器启动，不能在开发 Agent 沙箱内执行')
    root = Path(root).resolve()
    for name in ('tmp', 'state', 'cache'):
        (root/name).mkdir(parents=True, exist_ok=True)
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TERM', 'SYSTEMROOT') if key in os.environ}
    env.update(TMPDIR=str(root/'tmp'), PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1',
               CI='1', HOST='127.0.0.1', PORT=str(port), DSH_TEST_ORIGIN=origin,
               DSH_E2E_PORT=str(port), DSH_E2E_EXTERNAL_ORIGIN=origin,
               DSH_SUPERVISOR_STATE=str(root/'state'), DSH_HOME=str(root/'state/dsh'), npm_config_cache=str(root/'cache/npm'),
               npm_config_update_notifier='false', GIT_TERMINAL_PROMPT='0')
    if runtime:
        env['DSH_RUNTIME_NODE_MODULES'] = str(Path(runtime)/'node_modules')
    return env


def run(argv, workspace, root, name, *, env, timeout=900):
    """前台运行本机命令；超时终止进程组，保留真实退出码与完整日志。"""
    root = Path(root); root.mkdir(parents=True, exist_ok=True)
    log = root/(name+'.log'); proc = None; code = None
    started = time.time(); status = 'blocked'; reason = ''
    try:
        with log.open('w') as stream:
            proc = subprocess.Popen(argv, cwd=workspace, env=env, stdout=stream,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            code = proc.wait(timeout=timeout)
            status = 'pass' if code == 0 else 'fail'
    except (OSError, subprocess.TimeoutExpired) as exc:
        reason = str(exc)
    finally:
        if proc:
            stop(proc)
    content = ''.join(redact(line) for line in log.read_text(errors='replace').splitlines(keepends=True)) if log.exists() else ''
    log.write_text(content)
    return {'name': name, 'command': argv, 'status': status, 'required': True,
            'execution': 'local', 'exit_code': code, 'started': started,
            'elapsed': time.time()-started, 'log': str(log), 'log_tail': content[-6000:],
            'reason': reason or (content[-2000:] if status != 'pass' else '')}


@contextmanager
def verification(request, root):
    """按分支启动一个本机实例，测试和业务验收结束后统一回收。"""
    root = Path(root); root.mkdir(parents=True, exist_ok=True)
    result = {'status': 'blocked', 'checks': [], 'reason': ''}
    proc = None; instance = None
    try:
        instance = identity(request)
        config = request['product']['project_config']
        if config.get('blockers'):
            raise ValueError('；'.join(config['blockers']))
        port = free_port(); origin = f'http://127.0.0.1:{port}'
        instance.update(origin=origin, state=str(root/'state'), stopped=False)
        env = environment(root, port=port, origin=origin,
                          runtime=request['product'].get('worker_runtime') or independent_runtime())
        result['instance'] = instance
        atomic(root/'instance.json', instance)
        workspace = instance['workspace']
        for phase in ('install', 'build'):
            for i, argv in enumerate(config['commands'].get(phase, [])):
                check = run(argv, workspace, root, f'{phase}-{i}', env=env, timeout=config.get('command_timeout', 900))
                result['checks'].append(check)
                if check['status'] != 'pass':
                    result.update(status=check['status'], reason=f'{phase} 检查未通过：'+check['reason'])
                    break
            if result['reason']:
                break
        if not result['reason']:
            starts = config.get('test_start', config['commands'].get('start', []))
            if len(starts) != 1:
                raise ValueError('分支测试必须配置唯一启动命令')
            argv = [x.replace('{port}', str(port)).replace('{state}', str(root/'state')) for x in starts[0]]
            log = root/'startup.log'
            with log.open('w') as stream:
                proc = subprocess.Popen(argv, cwd=workspace, env=env, stdout=stream,
                                        stderr=subprocess.STDOUT, start_new_session=True)
            instance['pid'] = proc.pid
            deadline = time.monotonic()+config.get('startup_timeout', 30)
            ready = False
            while time.monotonic() < deadline and proc.poll() is None:
                try:
                    ready = health(origin, config.get('health_path', '/'))
                except (OSError, ValueError):
                    pass
                if ready:
                    break
                time.sleep(.1)
            check = {'name': 'startup', 'status': 'pass' if ready else 'blocked',
                     'required': True, 'execution': 'local', 'origin': origin, 'log': str(log)}
            result['checks'].append(check)
            if not ready:
                result['reason'] = '本机分支实例启动失败或健康检查超时'
            else:
                result.update(status='pass', reason='本机分支测试通过')
                for phase in ('test', 'browser'):
                    for i, argv in enumerate(config['commands'].get(phase, [])):
                        check = run(argv, workspace, root, f'{phase}-{i}', env=env, timeout=config.get('command_timeout', 900))
                        result['checks'].append(check)
                        if check['status'] != 'pass':
                            result.update(status=check['status'], reason=f'{phase} 检查未通过：'+check['reason'])
                            break
                    if result['status'] != 'pass':
                        break
                if proc.poll() is not None:
                    result.update(status='blocked', reason='分支测试期间实例已退出')
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        result.update(status='blocked', reason=str(exc))
    try:
        for check in result['checks']:
            if instance:
                check.update(branch=instance['branch'], commit=instance['commit'])
        yield result
    except BaseException as exc:
        result.update(status='blocked', reason='本机验收中断：'+str(exc))
        raise
    finally:
        if proc:
            stop(proc)
        if instance:
            instance['stopped'] = True
            atomic(root/'instance.json', instance)
        atomic(root/'local-verification.json', result)
