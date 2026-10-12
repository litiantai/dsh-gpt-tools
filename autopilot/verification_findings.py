"""将合并验收发现的功能缺陷持久化为高优先级需求，保留原失败结论。"""
import hashlib
import json
from pathlib import Path
import time

from .store import Ledger, redact


def acceptance_targets(request):
    """由控制器为本次需求的每条验收条件分配稳定标识。"""
    requirements = request.get('requirements') or [request.get('requirement') or {}]
    return [{'criterion_id': f"{r.get('id') or 'candidate'}:{index}",
             'requirement_id': r.get('id') or 'candidate', 'criterion': criterion}
            for r in requirements for index, criterion in enumerate(r.get('acceptance', []))] + [
            {'criterion_id': f"{r.get('id') or 'candidate'}:probe:{index}",
             'requirement_id': r.get('id') or 'candidate', 'criterion': probe}
            for r in requirements for index, probe in enumerate(r.get('resolution_probes', []))]


def acceptance_passed(request, judged):
    """核对逐项证据完整、唯一且全部通过。"""
    targets = acceptance_targets(request)
    results = judged.get('acceptance_results')
    requirements = request.get('requirements') or [request.get('requirement') or {}]
    if not isinstance(results, list) or any(not r.get('acceptance') and not r.get('resolution_probes') for r in requirements):
        return False
    expected = {item['criterion_id'] for item in targets}
    if len(expected) != len(targets) or len(results) != len(expected):
        return False
    seen = set()
    for item in results:
        if (not isinstance(item, dict) or not isinstance(item.get('criterion_id'), str)
                or item['criterion_id'] not in expected or item['criterion_id'] in seen
                or item.get('status') != 'pass' or not isinstance(item.get('evidence'), str)
                or not item['evidence'].strip()):
            return False
        seen.add(item['criterion_id'])
    return True


def inconsistent_pass(request, judged, *, check_coverage=True):
    """带逐项结论的新回执不得用顶层 pass 掩盖失败或遗漏。"""
    if judged.get('status') != 'pass':
        return False
    if 'findings' in judged and judged['findings'] != []:
        return True
    if 'acceptance_results' not in judged:
        return False
    results = judged['acceptance_results']
    if not isinstance(results, list) or any(
            not isinstance(item, dict) or item.get('status') != 'pass'
            or not isinstance(item.get('evidence'), str) or not item['evidence'].strip() for item in results):
        return True
    return check_coverage and not acceptance_passed(request, judged)


def unrelated_failure(request, judged):
    """全部本次条件有通过证据，且所有发现明确无关时，才允许延期处理。"""
    findings = judged.get('findings')
    if (judged.get('status') != 'fail' or judged.get('failure_kind')
            or not acceptance_targets(request) or not acceptance_passed(request, judged)
            or not isinstance(findings, list) or not findings):
        return False
    return all(isinstance(item, dict) and item.get('scope') == 'unrelated'
               and item.get('requirement_ids') == []
               and all(isinstance(item.get(key), str) and item[key].strip() for key in ('reason', 'evidence'))
               for item in findings)


def defer(request, judged):
    """仅处理已完成的功能失败；运行异常、未完成验证和开发验收仍阻断。"""
    product, record = request['product'], request['record']
    if (product.get('functional_findings_policy') != 'backlog'
            or record.get('flow') != 'review_before_release'
            or record.get('review_target') == 'feature'
            or judged.get('status') != 'fail' or judged.get('failure_kind')
            or not str(judged.get('reason', '')).strip()
            or not unrelated_failure(request, judged)):
        return None
    from review_core import Store
    ledger = Ledger(Store(Path(request['state_root']).parent))
    reason = redact(judged['reason'].strip())
    fingerprint = hashlib.sha256(json.dumps([product['id'], ' '.join(reason.split())], ensure_ascii=False).encode()).hexdigest()
    ident = 'verification-finding-'+fingerprint[:32]
    evidence = redact({'branch': record.get('branch'), 'commit': record.get('commit'),
        'base_commit': record.get('base_commit'), 'delivery_id': record.get('delivery_id') or record.get('id'),
        'verification': judged, 'at': time.time()})
    with ledger.store.transaction() as db:
        try:
            item = ledger.get('requirements', ident, db)
        except KeyError:
            item = None
        if item and item['status'] in ('completed', 'online', 'cancelled', 'rejected'):
            ident += '-'+str(record.get('commit', 'unknown'))[:12]
            try:
                item = ledger.get('requirements', ident, db)
            except KeyError:
                item = None
        if item:
            occurrences = item.get('verification_occurrences', [])
            if not any(e.get('commit') == evidence['commit'] for e in occurrences):
                item = ledger.update('requirements', ident, item['version'],
                    {'verification_occurrences': occurrences+[evidence]}, db=db)
            return item
        return ledger.create('requirements', {
            'product_id': product['id'], 'title': '修复验收发现的功能缺陷：'+reason[:100],
            'source': 'verification_finding', 'source_id': fingerprint, 'priority': 0,
            'queue_first': True, 'queue_first_at': time.time(), 'classification': 'development',
            'in_scope': True, 'confirmation_required': False,
            'goal': reason, 'scope': reason, 'reproduction': reason,
            'impact': '本机测试通过后独立业务验收发现的功能缺陷，按用户策略优先修复。',
            'acceptance': ['复现并修复该功能缺陷：'+reason, '为缺陷补充回归测试，并在对应 feat 分支的本机实例验证通过。'],
            'evidence': json.dumps(evidence, ensure_ascii=False),
            'verification_occurrences': [evidence], 'merge_blocking': False,
        }, 'pending', ident=ident, db=db)
