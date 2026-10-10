"""将合并验收发现的功能缺陷持久化为高优先级需求，保留原失败结论。"""
import hashlib
import json
from pathlib import Path
import time

from .store import Ledger, redact


def defer(request, judged):
    """仅处理已完成的功能失败；运行异常、未完成验证和开发验收仍阻断。"""
    product, record = request['product'], request['record']
    if (product.get('functional_findings_policy') != 'backlog'
            or record.get('flow') != 'review_before_release'
            or record.get('review_target') == 'feature'
            or judged.get('status') != 'fail' or judged.get('failure_kind')
            or not str(judged.get('reason', '')).strip()):
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
