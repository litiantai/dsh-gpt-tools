#!/usr/bin/env python3
"""独立智能协作执行器入口；仅返回结构化结果。"""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'dsh-gpt-supervisor/scripts')]
from autopilot.intelligence_worker import execute

if __name__ == '__main__':
    try:
        result = execute(sys.argv[1], json.load(sys.stdin))
    except Exception as exc:
        result = {'status':'blocked','reason':str(exc)}
    print(json.dumps(result, ensure_ascii=False))
