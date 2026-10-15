"""通过已鉴权管理端提供复盘报告及其固定证据文件。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


FILES = {'report.html': 'text/html; charset=utf-8',
         'evidence.json': 'application/json; charset=utf-8',
         'analysis.json': 'application/json; charset=utf-8',
         'conversion.json': 'application/json; charset=utf-8'}


def read_artifact(ledger, report_id, name):
    """核对台账、项目归属、受控目录和报告哈希后返回固定产物。"""
    if name not in FILES:
        raise KeyError('报告产物不存在')
    report = ledger.get('daily_reports', report_id)
    ledger.get('products', report['product_id'])
    artifact = report.get('html_report') or {}
    if not isinstance(artifact, dict) or not artifact.get('path'):
        raise KeyError('报告尚未生成 HTML 产物')
    path = Path(artifact['path']).resolve()
    root = (ledger.store.state / 'autopilot' / 'retrospectives').resolve()
    if not path.is_relative_to(root) or path.name != 'report.html':
        raise ValueError('报告产物路径无效')
    target = (path.parent / name).resolve()
    if target.parent != path.parent or not target.is_file():
        raise KeyError('报告产物不存在')
    if target.stat().st_size > 64 * 1024 * 1024:
        raise ValueError('报告产物超过大小上限')
    raw = target.read_bytes()
    if name == 'report.html' and hashlib.sha256(raw).hexdigest() != artifact.get('sha256'):
        raise ValueError('报告产物校验失败')
    if name.endswith('.json'):
        json.loads(raw)
    return raw, FILES[name]
