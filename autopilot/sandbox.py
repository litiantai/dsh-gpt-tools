"""macOS 工作进程写隔离；发布适配器不在开发进程内执行。"""
import json
from pathlib import Path
import sys


def restrict(command, allowed, profile, private_roots=(), read_allowed=(), deny_local=False):
    if sys.platform!='darwin':
        raise RuntimeError('第一版执行隔离只支持 macOS，不能无沙箱降级运行')
    roots=[str(Path(p).resolve()) for p in allowed]+['/dev/null']
    exceptions=' '.join('(require-not (subpath '+json.dumps(p)+'))' for p in roots)
    rules='(version 1)\n(allow default)\n(deny file-write* (require-all '+exceptions+'))\n'
    read_exceptions=' '.join('(require-not (subpath '+json.dumps(p)+'))' for p in roots+[str(Path(p).resolve()) for p in read_allowed])
    for private in private_roots:
        rules+='(deny file-read-data (require-all (subpath '+json.dumps(str(Path(private).resolve()))+') '+read_exceptions+'))\n'
    if deny_local:
        rules+='(deny network-outbound (remote ip "localhost:*"))\n'
    Path(profile).write_text(rules)
    return ['/usr/bin/sandbox-exec','-f',str(profile),*command]
