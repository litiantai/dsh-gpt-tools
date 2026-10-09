"""仓库命令的独立环境、受限文件访问、网络策略及进程组回收。"""
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time

from .store import redact


def environment(root, port=0):
    root = Path(root).resolve()
    for name in ('home', 'tmp', 'cache', 'state'):
        (root/name).mkdir(parents=True, exist_ok=True)
    tools=root/'tools'; tools.mkdir(exist_ok=True)
    # /bin/ps is set-id and cannot be launched from Seatbelt. Use the public
    # nonprivileged process API, preserving child identity and cleanup checks.
    if sys.platform=='darwin':
        shutil.copyfile(Path(__file__).resolve().parents[1]/'scripts/isolated-ps.py', tools/'ps'); (tools/'ps').chmod(0o755)
    env = {'PATH': str(tools)+os.pathsep+os.environ.get('PATH', '/opt/homebrew/bin:/usr/bin:/bin'),
            'HOME': str(root/'home'), 'TMPDIR': str(root/'tmp'), 'XDG_CACHE_HOME': str(root/'cache'),
            'npm_config_cache': str(root/'cache/npm'), 'npm_config_update_notifier': 'false',
            'PIP_CACHE_DIR': str(root/'cache/pip'), 'PYTHONDONTWRITEBYTECODE': '1',
            'PYTHONUNBUFFERED': '1', 'DSH_PROJECT_ISOLATED': '1', 'CI': '1', 'HOST': '127.0.0.1', 'PORT': str(port),
            'DSH_HOME': str(root/'home/dsh'), 'DSH_SUPERVISOR_STATE': str(root/'state'),
            'PLAYWRIGHT_BROWSERS_PATH': str(Path.home()/'Library/Caches/ms-playwright'),
            'GIT_TERMINAL_PROMPT': '0', 'LANG': 'en_US.UTF-8'}
    runtime=independent_runtime()
    if runtime:
        env['DSH_RUNTIME_NODE_MODULES']=str(runtime/'node_modules')
    return env


def independent_runtime():
    path=Path.home()/'.dsh/supervisor/autopilot/runtime'
    manifest=path/'package.json'
    if manifest.exists() and json.loads(manifest.read_text()).get('name')=='autopilot-independent-runtime':
        return path
    return None


def command(argv, root, workspace, *, install=False, ports=()):
    if sys.platform != 'darwin':
        raise ValueError('当前隔离执行仅支持 macOS；未配置隔离后端，禁止无沙箱启动')
    root, workspace = Path(root).resolve(), Path(workspace).resolve()
    # Read toolchains but never the user's app data, credentials or other repositories.
    read = [root, workspace, Path('/usr'), Path('/bin'), Path('/sbin'), Path('/System'),
            Path('/Library'), Path('/opt'), Path('/private/var/db'), Path('/dev'),
            Path.home()/'.nvm/versions', Path.home()/'Library/Caches/ms-playwright']
    read += [Path('/private/etc')]
    runtime=independent_runtime()
    if runtime:
        read.append(runtime)
    exceptions = ' '.join('(require-not (subpath '+json.dumps(str(p))+'))' for p in read)
    writes = ' '.join('(require-not (subpath '+json.dumps(str(p))+'))' for p in (root, workspace, Path('/dev')))
    rules = ['(version 1)', '(allow default)', '(deny file-write* (require-all '+writes+'))']
    for private in (Path('/Users'), Path('/Applications'), Path('/private/var/folders'), Path('/private/tmp')):
        rules.append('(deny file-read-data (require-all (subpath '+json.dumps(str(private))+') '+exceptions+'))')
    if install:
        rules += ['(deny network-bind)', '(deny network-outbound (remote tcp "localhost:*"))']
    else:
        outbound = ['localhost:*'] if 'test-loopback' in ports else ['localhost:'+str(int(p)) for p in ports]
        rules += ['(deny network-outbound)', '(deny network-bind)']
        for address in outbound:
            rules += ['(allow network-outbound (remote tcp '+json.dumps(address)+'))',
                      '(allow network-bind (local tcp '+json.dumps(address)+'))']
        for blocked in (13081,13083,13084):
            rules.append(f'(deny network-outbound (remote tcp "localhost:{blocked}"))')
    profile = root/('install.sb' if install else 'execute.sb')
    profile.write_text('\n'.join(rules)+'\n')
    return ['/usr/bin/sandbox-exec', '-f', str(profile), *argv]


def stop(proc):
    """回收隔离进程组，包括启动命令遗留的后台子进程。"""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        raise ValueError('隔离进程尚未停止')


def run(argv, workspace, root, name, *, install=False, ports=(), timeout=600):
    root = Path(root); root.mkdir(parents=True, exist_ok=True)
    started = time.time()
    log = root/(name+'.log')
    proc = None
    code = None
    status = 'blocked'
    reason = ''
    try:
        wrapped = command(argv, root, workspace, install=install, ports=ports)
        with log.open('w') as stream:
            proc = subprocess.Popen(wrapped, cwd=workspace, env=environment(root), stdout=stream,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            code = proc.wait(timeout=timeout)
            status = 'pass' if code == 0 else 'fail'
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        reason = str(exc)
    finally:
        if proc:
            stop(proc)
    content = log.read_text(errors='replace') if log.exists() else ''
    log.write_text(''.join(redact(line) for line in content.splitlines(keepends=True)))
    return {'name': name, 'command': argv, 'status': status, 'required': True, 'exit_code': code,
            'started': started, 'elapsed': time.time()-started, 'log': str(log),
            'log_tail': redact(content[-6000:]), 'reason': reason or ('' if status == 'pass' else redact(content[-2000:]))}
