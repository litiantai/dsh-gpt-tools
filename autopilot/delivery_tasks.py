"""交付批次内的任务、发现证据与可归属的模型用量。"""
import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .store import redact


def project_records(ledger, kind, product_id):
    with ledger.store.connect() as db:
        return [ledger.decode(row) for row in db.execute(
            f'SELECT * FROM {ledger._table(kind)} WHERE product_id=? ORDER BY created', (product_id,))]


def strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, str):
        yield value


def delivery_tasks(ledger, batch):
    product_id = batch['product_id']
    runs = project_records(ledger, 'runs', product_id)
    requirements = {r['id']: r for r in project_records(ledger, 'requirements', product_id)}
    signals = project_records(ledger, 'signals', product_id)
    inspections = project_records(ledger, 'inspections', product_id)
    evidence = project_records(ledger, 'evidence', product_id)
    ids = set(batch.get('run_ids', []) + batch.get('integrated_ids', []))
    result = []
    for run in runs:
        if run['id'] not in ids and run.get('delivery_id') != batch['id']:
            continue
        requirement = requirements.get(run.get('requirement_id'), {})
        sources = [s for s in signals if s['id'] in requirement.get('signal_ids', []) or
                   requirement.get('id') and (s.get('requirement_id') == requirement['id'] or
                                             requirement['id'] in s.get('requirement_ids', []))]
        references = set(strings([requirement, sources, run]))
        linked_evidence = [e for e in evidence if e['id'] in references or e.get('run_id') == run['id']]
        inspection_ids = {e.get('inspection_id') for e in linked_evidence}
        found = [i for i in inspections if i['id'] in references or i['id'] in inspection_ids]
        found_ids = {i['id'] for i in found}
        linked_ids = {e['id'] for e in linked_evidence}
        step_ids = {step for inspection in found for step in inspection.get('steps', []) if isinstance(step, str)}
        steps = [e for e in evidence if e['id'] in linked_ids | step_ids or e.get('inspection_id') in found_ids]
        prs = [{'label': '代码评审 MR', **p} for p in batch.get('feature_prs', []) if run['id'] in p.get('run_ids', [])]
        if run.get('feature_pr_url'):
            prs.append({'label': '代码评审 MR', 'pr_url': run['feature_pr_url']})
        pending = batch.get('pending_ids', [rid for rid in batch.get('run_ids', []) if rid not in batch.get('integrated_ids', [])])
        if run['id'] in pending and batch.get('feature_pr_url'):
            prs.append({'label': '代码评审 MR', 'pr_url': batch['feature_pr_url']})
        for receipt in batch.get('receipts', []):
            merged = receipt.get('result', {}).get('merged_feature') or {}
            if run['id'] in merged.get('run_ids', []) and merged.get('pr_url'):
                prs.append({'label': '代码评审 MR', **merged})
        if batch.get('pr_url'):
            prs.append({'label': '上线 PR', 'pr_url': batch['pr_url']})
        prs = list({p['pr_url']: p for p in prs if p.get('pr_url')}.values())
        result.append({'run': run, 'requirement': requirement, 'signals': sources,
                       'inspections': found, 'evidence': steps, 'prs': prs})
    return {'tasks': redact(result), 'day': batch.get('day'), 'missing_ids': sorted(ids - {r['id'] for r in runs})}


def task_usage(ledger, run):
    """优先使用显式任务归属，兼容旧回执路径；不将共享交付用量摊到单个任务。"""
    product_id = run['product_id']
    evidence = [e for e in project_records(ledger, 'evidence', product_id) if e.get('run_id') == run['id']]
    references = set(strings([run.get('receipts', []), evidence]))
    references |= {str(Path(value).resolve()) for value in references if value.startswith('/') and '\n' not in value}
    with ledger.store.connect() as db:
        reviews = [ledger.store.decode(r) for r in db.execute('SELECT * FROM reviews WHERE session_id=?', (run['id'],))]
        sources = db.execute('SELECT * FROM auto_token_sources WHERE product_id=?', (product_id,)).fetchall()
    review_paths = {str((ledger.store.state / 'reviews' / r['id'] / 'trace.jsonl').resolve()) for r in reviews}
    entries = []
    for source in sources:
        path = Path(source['path'])
        if source['record_id'] != run['id'] and str(path) not in review_paths and str(path) not in references and str(path.parent) not in references:
            continue
        entries.append({'id': source['id'], 'action': source['action'] or '历史执行',
                        'tokens': source['tokens'], 'collected': source['size'] >= 0,
                        'budget_kind': source['budget_kind']})
    product = ledger.get('products', product_id)
    from .quota import snapshot
    quota = snapshot(ledger, product)
    return {'tokens': sum(e['tokens'] for e in entries if e['collected']), 'sources': entries,
            'day': datetime.datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat(),
            'project_tokens_used': quota['tokens_used'],
            'project_tokens_limit': quota['tokens_limit'],
            'execution_seconds': run.get('execution_seconds'),
            'receipts': redact(run.get('receipts', [])), 'evidence': redact(evidence),
            'reviews': redact([{'title': r.get('phase'), 'status': r['status'], 'created': r['created'],
                                'result': r.get('result'), 'provider': (r.get('reviewer') or {}).get('provider'),
                                'model': (r.get('reviewer') or {}).get('model')} for r in reviews])}
