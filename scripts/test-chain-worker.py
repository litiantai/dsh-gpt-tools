#!/usr/bin/env python3
"""测试链路后台进程入口； stdout 仅输出结构化回执。"""
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'dsh-gpt-supervisor/scripts')]
from autopilot.test_chain_worker import execute
if __name__ == '__main__':
    try:
        result = execute(sys.argv[1], json.load(sys.stdin))
    except Exception as exc:
        result = {'status':'blocked', 'reason':str(exc), 'retryable':False}
    print(json.dumps(result, ensure_ascii=False))
