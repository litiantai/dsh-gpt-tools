#!/usr/bin/env python3
"""工作台分支同步终端的独立执行入口。"""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'dsh-gpt-supervisor/scripts')]
from autopilot.branch_sync import execute

if __name__ == '__main__':
    print(json.dumps(execute(json.load(sys.stdin)), ensure_ascii=False))
