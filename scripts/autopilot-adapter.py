#!/usr/bin/env python3
"""使用确定的模块路径启动产品或执行器适配器。"""
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'dsh-gpt-supervisor/scripts')]
try:
    if sys.argv[1]=='thsoctop':
        from autopilot.thsoctop import main
    elif sys.argv[1]=='daily':
        from autopilot.daily import execute as main
    elif sys.argv[1]=='executor':
        from autopilot.executor import execute as main
    elif sys.argv[1]=='delivery':
        from autopilot.delivery_worker import execute as main
    else:
        raise ValueError('未知适配器')
    result=main(sys.argv[2],json.load(sys.stdin))
except Exception as exc:
    result={'status':'blocked','reason':str(exc)}
print(json.dumps(result,ensure_ascii=False))
