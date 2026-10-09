#!/usr/bin/env python3
"""巡查脚本的本机证据写入入口；每一步立即出现在监控平台。"""
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'dsh-gpt-supervisor/scripts')]
from review_core import Store
from autopilot.evidence import Recorder

request=json.load(sys.stdin)
store=Store(Path(request['state']))
if request['action']=='start':
    rec=Recorder(store,request['product_id'],request['title'],request['method'])
    result=rec.inspection
else:
    rec=Recorder.resume(store,request['inspection_id'])
    if request['action']=='step':
        result=rec.step(**request['step'])
    elif request['action']=='finish':
        result=rec.finish(request['status'],request['judgement'])
    else:
        raise ValueError('未知记录动作')
print(json.dumps({'id':result['id']}))
