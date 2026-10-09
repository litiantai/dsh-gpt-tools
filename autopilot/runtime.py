"""独立 Harness 运行时的完整性校验、互斥安装与原子发布。"""
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

PACKAGES = ('@deepseek-ai/dsh', '@deepseek-ai/dsh-base', '@deepseek-ai/dsh-headless')


def manifest(version):
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[a-zA-Z0-9.]+)?', version):
        raise ValueError('运行时需要固定的发布版本')
    return {'name':'autopilot-independent-runtime', 'private':True,
            'dependencies':{name:version for name in PACKAGES}}


def ready(path):
    """仅接受锁文件、固定版本和关键入口均完整的运行时，包括完整旧部署。"""
    path = Path(path)
    try:
        data = json.loads((path/'package.json').read_text())
        version = data['dependencies'][PACKAGES[0]]
        if data != manifest(version):
            return False
        lock = json.loads((path/'package-lock.json').read_text())
        if lock['packages']['']['dependencies'] != data['dependencies']:
            return False
        for name in PACKAGES:
            package = path/'node_modules'/name
            meta = json.loads((package/'package.json').read_text())
            if meta['version'] != version or lock['packages']['node_modules/'+name]['version'] != version:
                return False
            entry = meta.get('bin', {}).get('dsh') if name == PACKAGES[0] else meta.get('main')
            target = (package/entry).resolve() if isinstance(entry,str) else package/'missing-entry'
            if not target.is_relative_to(package.resolve()) or not target.is_file():
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def install(state, version):
    """锁覆盖完整安装；失败目录保留证据，正式路径仅发布完整版本。"""
    expected = manifest(version)
    auto = Path(state)/'autopilot'; auto.mkdir(parents=True, exist_ok=True)
    runtime = auto/'runtime'
    with (auto/'runtime-install.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (runtime/'package.json').exists():
            if json.loads((runtime/'package.json').read_text()) != expected:
                raise ValueError('独立运行时已使用其他配置，禁止原位覆盖')
        if ready(runtime):
            return {'status':'pass','runtime':str(runtime),'version':version,'reused':True}
        pending = Path(tempfile.mkdtemp(prefix='.runtime-install-', dir=auto))
        (pending/'package.json').write_text(json.dumps(expected, indent=2))
        try:
            with (pending/'install.log').open('w') as output:
                subprocess.run(['npm','install','--ignore-scripts','--no-audit','--no-fund'],
                               cwd=pending, check=True, stdout=output, stderr=subprocess.STDOUT, timeout=900)
            if not ready(pending):
                raise ValueError('依赖安装结束，但锁文件、包版本或关键入口不完整')
            # An invalid legacy directory never qualifies as a running runtime.
            # Preserve it for diagnosis; a crash here leaves the path unavailable,
            # never falsely ready. Retrying installs another complete candidate.
            if runtime.exists():
                runtime.rename(auto/('.runtime-incomplete-'+str(uuid.uuid4())))
            os.replace(pending, runtime)
        except Exception as exc:
            (pending/'failure.json').write_text(json.dumps({'status':'blocked','version':version,'reason':str(exc)}))
            raise RuntimeError('运行时安装未完成，原路径未发布；证据：'+str(pending)) from exc
    return {'status':'pass','runtime':str(runtime),'version':version,'reused':False}
