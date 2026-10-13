"""按授权轮次补足执行预算；保留累计耗时与单次调用超时。"""
import time

from .store import DEFAULTS


def snapshot(run, product):
    """返回真实累计耗时、已追加额度和下一次调用的时间上限。"""
    policy = DEFAULTS | product.get('policy', {})
    used = run.get('execution_seconds', 0)
    extra = run.get('extra_execution_seconds', 0)
    limit = policy['execution_seconds'] + extra
    remaining = max(0, limit - used)
    return {'used_seconds': used, 'initial_seconds': policy['execution_seconds'],
            'extra_seconds': extra, 'limit_seconds': limit, 'remaining_seconds': remaining,
            'call_seconds': min(policy['execution_call_seconds'], remaining)}


def renew(run, product, grant_id, source):
    """一次有效授权仅补足一轮预算，重复消费同一授权不增加额度。"""
    grants = run.get('execution_grants', [])
    if any(grant['grant_id'] == grant_id for grant in grants):
        return {}
    budget = snapshot(run, product)
    added = max(0, budget['used_seconds'] + budget['initial_seconds'] - budget['limit_seconds'])
    grant = {'grant_id': grant_id, 'source': source, 'at': time.time(),
             'used_seconds': budget['used_seconds'], 'added_seconds': added}
    return {'extra_execution_seconds': budget['extra_seconds'] + added,
            'execution_grants': grants + [grant]}


def pending_changes(run, product):
    """预计算已批准协调返修的预算，正式恢复时随状态原子保存。"""
    pending = run.get('coordination_pending') or {}
    if pending.get('action') != 'repair' or not pending.get('decision_id'):
        return {}
    from .coordinator import enabled, config
    if not enabled(product) or pending.get('requires_human_authority') and not config(product)['human_authority']:
        return {}
    return renew(run, product, 'watch:' + pending['decision_id'], 'watch')
