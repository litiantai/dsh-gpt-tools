"""macOS 工作进程写隔离；发布适配器不在开发进程内执行。"""
import json
import os
from pathlib import Path
import sys


def model_environment(extra=None):
    """仅继承工具链与区域设置，模型凭据由选定执行器显式提供。"""
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'TERM') if key in os.environ}
    env.update(extra or {})
    return env


def restrict(command, allowed, profile, private_roots=(), read_allowed=(), deny_local=False, readonly_roots=()):
    if sys.platform!='darwin':
        raise RuntimeError('第一版执行隔离只支持 macOS，不能无沙箱降级运行')
    roots=[str(Path(p).resolve()) for p in allowed]+['/dev/null']
    exceptions=' '.join('(require-not (subpath '+json.dumps(p)+'))' for p in roots)
    rules='(version 1)\n(allow default)\n(deny file-write* (require-all '+exceptions+'))\n'
    for readonly in readonly_roots:
        rules+='(deny file-write* (subpath '+json.dumps(str(Path(readonly).resolve()))+'))\n'
    read_exceptions=' '.join('(require-not (subpath '+json.dumps(p)+'))' for p in roots+[str(Path(p).resolve()) for p in read_allowed])
    for private in private_roots:
        rules+='(deny file-read-data (require-all (subpath '+json.dumps(str(Path(private).resolve()))+') '+read_exceptions+'))\n'
    if deny_local:
        rules+='(deny network-outbound (remote ip "localhost:*"))\n'
    Path(profile).write_text(rules)
    return ['/usr/bin/sandbox-exec','-f',str(profile),*command]
