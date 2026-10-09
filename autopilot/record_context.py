"""按显式引用补全记录的直接关联，避免把同项目或同批次的其他任务当作证据。"""
import json

from .delivery_tasks import project_records
from .store import redact


KINDS = ('signals', 'requirements', 'runs', 'releases', 'deliveries',
         'code_reviews', 'inspections', 'evidence', 'evaluations', 'daily_reports')
REFERENCES = {
    'signal_id': 'signals', 'signal_ids': 'signals',
    'requirement_id': 'requirements', 'requirement_ids': 'requirements',
    'run_id': 'runs', 'run_ids': 'runs', 'integrated_ids': 'runs', 'pending_ids': 'runs',
    'release_id': 'releases', 'delivery_id': 'deliveries',
    'inspection_id': 'inspections', 'inspection_ids': 'inspections',
    'evidence_id': 'evidence', 'evidence_ids': 'evidence',
    'evaluation_id': 'evaluations', 'daily_report_id': 'daily_reports',
    'review_id': 'reviews', 'acceptance_review_id': 'reviews',
    'review_ids': 'reviews', 'code_review_id': 'code_reviews',
    'round_id': 'code_reviews',
}


def references(value, kind):
    """支持结构化字段和旧 JSON 文本；不按摘要、时间或路径猜测关联。"""
    if isinstance(value, str) and value.lstrip().startswith(('{', '[')):
        try:
            value = json.loads(value)
        except ValueError:
            return
    if isinstance(value, dict):
        for key, item in value.items():
            target = REFERENCES.get(key) or ('evidence' if key == 'steps' and kind == 'inspections' else None)
            if target:
                for ident in item if isinstance(item, list) else [item]:
                    if isinstance(ident, str) and ident:
                        yield target, ident
            yield from references(item, kind)
    elif isinstance(value, list):
        for item in value:
            yield from references(item, kind)


def record_context(ledger, kind, ident, record=None):
    if kind not in (*KINDS, 'reviews'):
        raise KeyError('不支持的关联记录类型')
    record = record if record is not None else ledger.get(kind, ident)
    if kind == 'reviews':
        try:
            run = ledger.get('runs', record.get('session_id'))
        except KeyError:
            return {'record': redact(record), 'related': [], 'missing': []}
        record = record | {'product_id': run['product_id'], 'run_id': run['id']}
    candidates = {(k, r['id']): r for k in KINDS
                  for r in project_records(ledger, k, record['product_id'])}
    run_ids = {i for k, i in candidates if k == 'runs'}
    with ledger.store.connect() as db:
        for row in db.execute('SELECT * FROM reviews'):
            if row['session_id'] in run_ids:
                review = ledger.store.decode(row)
                candidates['reviews', review['id']] = review | {'run_id': review['session_id']}
    wanted = set(references(record, kind))
    # 回执保留数量有限，持久化证据仍可通过任务或 call_id 找回。
    call_ids = {r.get('call_id') for r in record.get('receipts', []) if isinstance(r, dict)} - {None}
    if record.get('call_id'):
        call_ids.add(record['call_id'])
    related = []
    for (target, target_id), item in candidates.items():
        if (target, target_id) == (kind, ident):
            continue
        forward = (target, target_id) in wanted
        reverse = (kind, ident) in set(references(item, target))
        same_call = target == 'evidence' and item.get('call_id') in call_ids
        if forward or reverse or same_call:
            related.append({'kind': target, 'record': item})
    missing = [{'kind': k, 'id': i} for k, i in sorted(wanted) if (k, i) not in candidates]
    return {'record': redact(record), 'related': redact(related), 'missing': missing}
