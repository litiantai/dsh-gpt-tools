"""异常阻塞按小时恢复；保留额度、人工控制和副作用核对约束。"""
import datetime
import json
import re
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from .store import DEFAULTS

INTERVAL = 3600
FAULT = re.compile(r'timed?\s*out|timeout|connection|network|temporar|ECONN|ENOTFOUND|EAI_AGAIN|超时|退出码|执行失败|运行异常|回执.*(?:读取|解析)失败|网络|连接失败|连接中断|服务不可用|rate.limit|限流|429|502|503|504', re.I)


def abnormal(item):
    receipts = item.get('receipts') or []
    result = (receipts[-1].get('result') or {}) if receipts else {}
    if result.get('retryable') is False:
        return False
    if item.get('uncertain') or item.get('control') or result.get('uncertain'):
        return False
    reason = item.get('reason', '')
    if re.search(r'Insufficient Balance|余额不足|MODEL_BALANCE_INSUFFICIENT', reason, re.I):
        return False
    if any(word in reason for word in ('用户', '人工', '暂停证据', '核对副作用', '回执失败：', '额度', '上限', '已用尽', '源码发生变化')):
        return False
    return result.get('retryable') is True or result.get('failure_kind') == 'agent_execution' or bool(FAULT.search(reason))


def quota_reason(scheduler, item, product, kind):
    if not scheduler.tokens_available(product):
        return '每日 Token 额度不足，等待额度恢复'
    phase = item.get('resume_status')
    policy = DEFAULTS | product.get('policy', {})
    if kind == 'runs' and phase in ('queued', 'planning', 'developing'):
        if item.get('execution_seconds', 0) >= policy['execution_seconds']:
            return '累计开发执行时间额度不足'
    if kind == 'runs' and phase == 'queued' and not item.get('budget_reserved'):
        evaluation = scheduler.ledger.get('evaluations', item['evaluation_id']) if item.get('evaluation_id') else None
        budget_kind = 'evaluation:' + evaluation['id'] if evaluation else 'development'
        limit = evaluation['target_count'] if evaluation else policy['runs_per_day']
        if limit and scheduler.ledger.budget_used(product['id'], budget_kind) >= limit:
            return '每日需求额度不足，等待额度恢复'
    if kind == 'runs' and phase in ('plan_review', 'acceptance_review'):
        quota = scheduler.store.state / 'quota.json'
        if quota.exists():
            value = json.loads(quota.read_text())
            today = datetime.datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
            if value.get('day') == today and value.get('count', 0) >= scheduler.store.settings()['max_per_day']:
                return '平台每日审查额度不足，等待额度恢复'
    return ''


def recover(scheduler, kind, item, product, now=None):
    now = time.time() if now is None else now
    if item['status'] != 'blocked' or product['status'] != 'active' or not abnormal(item):
        return item
    if item.get('call') or item.get('pending_result'):
        return item
    for receipt in (item.get('receipts') or [])[-1:]:
        call_id = receipt.get('call_id')
        if not call_id or Path(str(call_id)).name != call_id:
            continue
        directory = scheduler.root / 'calls' / call_id
        process = directory / 'process.json'
        if process.exists() and scheduler.process_alive(json.loads(process.read_text()), str(directory / 'request.json')):
            return item
    phase = item.get('resume_status')
    if kind == 'runs' and phase not in ('queued', 'planning', 'developing', 'verifying', 'plan_review', 'acceptance_review', 'awaiting_release'):
        return item
    if not phase:
        return item
    due = item.get('next_auto_retry_at') or item['updated'] + INTERVAL
    if not item.get('next_auto_retry_at'):
        item = scheduler.change(kind, item, {'next_auto_retry_at': due})
    if now < due:
        return item
    reason = quota_reason(scheduler, item, product, kind)
    if reason:
        if item.get('auto_retry_wait_reason') != reason:
            item = scheduler.change(kind, item, {'auto_retry_wait_reason': reason})
        return item
    changes = {'reason': '异常阻塞已满一小时，自动重试原阶段', 'last_retry_reason': item.get('reason', ''),
               'last_auto_retry_at': now, 'next_auto_retry_at': None, 'auto_retry_wait_reason': None,
               'auto_retry_count': item.get('auto_retry_count', 0) + 1}
    if kind == 'runs' and phase in ('plan_review', 'acceptance_review'):
        ident = item.get('review_id') or (item.get('review_packet') or {}).get('request_id')
        if not ident:
            return item
        try:
            review = scheduler.store.get(ident)
        except KeyError:
            return item
        if review.get('human') or not review.get('execution_done') or review['status'] != 'blocked' or review['packet']['session_id'] != item['id']:
            return item
        changes.update(review_id=None, review_packet=None, last_failed_review_id=ident,
                       review_retries=item.get('review_retries', 0) + 1)
    if kind == 'deliveries':
        changes['next_attempt'] = 0
    return scheduler.change(kind, item, changes, phase)
