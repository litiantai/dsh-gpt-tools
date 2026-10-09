#!/usr/bin/env python3
"""隔离桌面验收的 Node 包装器；认证就绪地址仅写入本机私有文件。"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

child=subprocess.Popen([os.environ['THSOCTOP_REAL_NODE'],*sys.argv[1:]],stdin=sys.stdin,stdout=subprocess.PIPE)
signal.signal(signal.SIGTERM,lambda *_:child.terminate())
for line in iter(child.stdout.readline,b''):
    try:
        event=json.loads(line)
        if event.get('type')=='ready':
            dest=Path(os.environ['THSOCTOP_APP_SUPPORT'])/'host-ready.private.json'
            fd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(fd,'w') as stream:
                json.dump(event,stream)
    except (ValueError,KeyError):
        pass
    sys.stdout.buffer.write(line)
    sys.stdout.buffer.flush()
sys.exit(child.wait())
