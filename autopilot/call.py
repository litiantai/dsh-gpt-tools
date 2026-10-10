"""独立子进程回执写入器；调度器退出不会丢失调用结果。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1]/'dsh-gpt-supervisor/scripts')]
from error_messages import present_errors, failure_fields


def atomic(path, value):
    path = Path(path)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False))
    temp.chmod(0o600)
    os.replace(temp,path)


def main(request_path):
    request_path=Path(request_path).resolve()
    request=json.loads(request_path.read_text())
    root=request_path.parent
    atomic(root/'process.json',{'pid':os.getpid(),'started':time.time(),'request':str(request_path)})
    start=time.monotonic()
    child=None
    try:
        with (root/'stdout.log').open('w') as stdout, (root/'stderr.log').open('w') as stderr:
            child=subprocess.Popen(request['command'],cwd=request.get('cwd'),stdin=subprocess.PIPE,
                                   stdout=stdout,stderr=stderr,text=True,env=os.environ | request.get('env',{}))
            try:
                child.communicate(json.dumps(request['input'],ensure_ascii=False),timeout=request['timeout'])
            except subprocess.TimeoutExpired:
                # Whole invocation has its own process group, including tools spawned by the model.
                atomic(root/'result.json',{'status':'blocked','failure_kind':'agent_execution','reason':'执行超时，进程组正在终止','elapsed':time.monotonic()-start})
                os.killpg(os.getpgrp(),signal.SIGTERM)
                return
        content=(root/'stdout.log').read_text()
        if len(content)>2_000_000:
            raise ValueError('执行器输出超过限制')
        result=json.loads(content)
        if not isinstance(result,dict) or result.get('status') not in ('pass','fail','blocked','busy','stale','deferred','waiting_for_reply'):
            raise ValueError('执行器缺少结构化状态')
        if child.returncode and result['status']=='pass':
            result=result | {'status':'blocked','failure_kind':'agent_execution','reason':f'执行器异常退出，退出码 {child.returncode}'}
        result['elapsed']=time.monotonic()-start
        result['timing']='monotonic'
        result['adapter_exit_code']=child.returncode
        result.setdefault('exit_code',child.returncode)
        # Preserve retry classification before replacing legacy machine text.
        if result['status']=='blocked' and 'retryable' not in result:
            from autopilot.retry import abnormal
            result['retryable']=abnormal({'reason':result.get('reason',''),'receipts':[{'result':result}]})
        atomic(root/'result.json',present_errors(result))
    except Exception as exc:
        atomic(root/'result.json',{'status':'blocked','failure_kind':'agent_execution',**failure_fields(exc),'elapsed':time.monotonic()-start})


if __name__=='__main__':
    main(sys.argv[1])
