"""交付返修以真实检查和评审结果判断进展，连续无改善时停止自动追加。"""
import json
import runpy
from pathlib import Path


def changes(batch, result, action):
    """每份新验证或评审回执只计一次，执行和网络异常不算代码无进展。"""
    if not action.startswith(('validate', 'review')) or result.get('retryable') is True or result.get('failure_kind') == 'agent_execution':
        return {}
    from .verification_control import outcome
    current = outcome(batch) if action.startswith('validate') else {
        'review_id': (batch.get('receipts') or [{}])[-1].get('call_id'),
        'checks': {}, 'issues': sorted(json.dumps(i, sort_keys=True, ensure_ascii=False) for i in result.get('issues', []))}
    if result.get('status') not in ('pass', 'fail'):
        return {}
    key = action + ':' + str(batch.get('feature_requirement_id') or batch.get('feat_branch') or '') if action.endswith('feature') else action
    previous = (batch.get('delivery_progress_baselines') or {}).get(key)
    baselines = dict(batch.get('delivery_progress_baselines') or {}, **{key: current})
    if not previous:
        return {'delivery_progress_baselines': baselines}
    marker = current.get('receipt_id') or current.get('review_id')
    if not marker or marker == previous.get('receipt_id') or marker == previous.get('review_id'):
        return {}
    path = Path(__file__).resolve().parents[1] / 'skills/dsh-role-coordinator/scripts/compare_progress.py'
    value = runpy.run_path(str(path))['compare'](previous, current)
    observed = value['fresh_evidence']
    count = 0 if result.get('status') == 'pass' or value['improved'] else batch.get('delivery_no_progress', 0) + int(observed)
    return {'delivery_progress_baselines': baselines, 'delivery_progress': value,
            'delivery_no_progress': count, 'delivery_progress_hold': count >= 2}


def hold(scheduler, batch):
    """无进展批次不占后续已完成成果的交付队列，须显式人工重试解除。"""
    if not batch.get('delivery_progress_hold'):
        return batch, False
    reason = '连续两轮交付验证或评审没有改善，停止自动返修；请补充诊断后人工重试'
    if batch['status'] != 'blocked' or batch.get('reason') != reason:
        batch = scheduler.change('deliveries', batch, {'reason': reason, 'resume_status': batch['status']}, 'blocked')
    return batch, True
