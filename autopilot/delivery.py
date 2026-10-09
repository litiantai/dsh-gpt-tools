"""北京时间每日交付编排；源码上线与客户端安装使用独立状态。"""
import copy
import datetime as dt
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

from review_core import Conflict
from .store import TERMINAL

ZONE = ZoneInfo('Asia/Shanghai')
FINISHED = {'online', 'cancelled'}


def day_at(now):
    return dt.datetime.fromtimestamp(now, ZONE).strftime('%Y%m%d')


def enabled(product):
    return bool(product.get('git', {}).get('enabled'))


def configured(product):
    return bool(product.get('git', {}).get('url'))


def ready(product):
    return not configured(product) or product.get('git_migration', {}).get('status') == 'completed'


def config(product):
    agents = product.get('agents', {})
    return {'reviewer': copy.deepcopy(agents.get('acceptance')), 'fixer': copy.deepcopy(agents.get('implementation')),
            'max_revisions': 0} | copy.deepcopy(product.get('code_review', {}))


def repair_queue(scheduler, batch, product, now):
    """修复额度只延后新修复；评审、验证和合并不受累计修复次数限制。"""
    from .retry import recover
    batch = recover(scheduler, 'deliveries', batch, product, now)
    repair_states = {'repairing', 'review_failed', 'release_failed', 'repairing_feature', 'repairing_release'}
    if (batch['status'] == 'blocked' and not batch.get('call') and not batch.get('pending_result')
            and batch.get('reason') == '自动修复轮数已用尽；可调整上限后重试'
            and batch.get('resume_status') in repair_states):
        batch = scheduler.change('deliveries', batch, {'reason': '', 'next_attempt': 0}, batch['resume_status'])
    if batch['status'] not in repair_states:
        return batch, False
    limit = config(product)['max_revisions']
    midnight = dt.datetime.fromtimestamp(now, ZONE).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = (midnight + dt.timedelta(days=1)).timestamp()
    # 按持久化轮次计数，避免重启重置额度或累计历史次数导致永久阻塞。
    with scheduler.store.connect() as db:
        rows = db.execute('SELECT * FROM auto_code_reviews WHERE product_id=?', (product['id'],)).fetchall()
    rounds = [scheduler.ledger.decode(row) for row in rows]
    used = sum(r.get('delivery_id') == batch['id'] and r.get('phase') in ('repair', 'repair_feature', 'repair_release')
               and r['id'] not in batch.get('excluded_repair_rounds', [])
               and midnight.timestamp() <= r.get('started_at', r['created']) < tomorrow for r in rounds)
    if limit > 0 and used >= limit:
        reason = '当日交付修复额度已用尽，排队等待北京时间次日零点；已通过检查的成果继续上线'
        if batch.get('repair_deferred_until') != tomorrow or batch.get('reason') != reason:
            batch = scheduler.change('deliveries', batch, {'repair_deferred_until': tomorrow, 'reason': reason})
        return batch, True
    if batch.get('repair_deferred_until'):
        batch = scheduler.change('deliveries', batch, {'repair_deferred_until': None, 'reason': ''})
    return batch, False


def create_batch(ledger, product, now, bootstrap=False):
    day = day_at(now)
    ident = product['id'] + '-' + day
    with ledger.store.transaction() as db:
        try:
            return ledger.get('deliveries', ident, db)
        except KeyError:
            staged = product.get('delivery_flow', 'review_before_release') == 'review_before_release'
            cutoff = dt.datetime.fromtimestamp(now, ZONE).replace(hour=23, minute=30, second=0, microsecond=0).timestamp() if staged else (dt.datetime.fromtimestamp(now, ZONE).replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(days=1)).timestamp()
            return ledger.create('deliveries', {'product_id': product['id'], 'title': day + (' 首次源码基线' if bootstrap else ' 每日代码交付'),
                'day': day, 'cutoff': cutoff, 'feat_branch': 'feat-' + day, 'branch': 'release-' + day,
                'base_branch': product['git'].get('base_branch', 'master'), 'git_url': product['git']['url'],
                'flow': 'review_before_release' if staged else 'legacy',
                'bootstrap': bootstrap, 'run_ids': [], 'integrated_ids': [], 'revisions': 0, 'receipts': [],
                'reason': '', 'frozen': False}, 'preparing', ident, db)


def start_migration(ledger, product):
    if not enabled(product):
        raise Conflict('请先保存并启用 Git 交付配置')
    if any(r['product_id'] == product['id'] and r['status'] not in TERMINAL | {'queued', 'blocked'} for r in ledger.list('runs')):
        raise Conflict('当前任务仍在执行；请等待其结束后迁移，原工作区将保留')
    accepted = {r['id']: r for r in ledger.list('runs') if r['product_id'] == product['id'] and r['status'] == 'accepted' and not r.get('delivery_id')}
    ordered, visiting = [], set()
    def include(run):
        if run['id'] in ordered:
            return
        if run['id'] in visiting:
            raise Conflict('历史验收任务依赖存在循环，不能自动交付')
        visiting.add(run['id'])
        for dependency in run.get('dependencies', []):
            item = ledger.get('runs', dependency['run_id'])
            if item['product_id'] != product['id'] or item.get('commit') != dependency['commit']:
                raise Conflict('历史验收任务依赖版本不一致')
            if item['id'] in accepted:
                include(item)
            elif item['status'] != 'online':
                raise Conflict('历史验收任务包含未上线且不能交付的依赖')
        visiting.remove(run['id'])
        ordered.append(run['id'])
    for run in reversed(list(accepted.values())):
        include(run)
    batch = create_batch(ledger, product, time.time(), bootstrap=True)
    if not batch.get('bootstrap'):
        raise Conflict('当日已有普通交付批次，不能覆盖为基线迁移')
    with ledger.store.transaction() as db:
        batch = ledger.get('deliveries', batch['id'], db)
        if ordered and (batch.get('frozen') or batch.get('call') or batch['status'] != 'preparing'):
            raise Conflict('基线已开始执行，不能追加历史任务')
        if ordered:
            batch = ledger.update('deliveries', batch['id'], batch['version'], {'run_ids': list(dict.fromkeys(batch['run_ids'] + ordered))}, db=db)
            for ident in ordered:
                run = ledger.get('runs', ident, db)
                ledger.update('runs', ident, run['version'], {'delivery_id': batch['id']}, db=db)
        current = ledger.get('products', product['id'], db)
        ledger.update('products', current['id'], current['version'], {'git_migration': {'status': 'pending', 'delivery_id': batch['id']}}, db=db)
        return batch


def attach_accepted(ledger, product, now):
    if not ready(product):
        return
    candidates = [r for r in reversed(ledger.list('runs')) if r['product_id'] == product['id'] and r['status'] == 'accepted' and not r.get('delivery_id')]
    for run in candidates:
        batch = create_batch(ledger, product, now)
        if batch.get('call') or batch['status'] in FINISHED | {'blocked', 'repairing'} or batch.get('frozen'):
            return
        with ledger.store.transaction() as db:
            run = ledger.get('runs', run['id'], db)
            batch = ledger.get('deliveries', batch['id'], db)
            if run.get('delivery_id'):
                continue
            ledger.update('deliveries', batch['id'], batch['version'], {'run_ids': batch['run_ids'] + [run['id']],
                'review_pass': None, 'validation_pass': None, 'reason': ''}, 'preparing', db)
            ledger.update('runs', run['id'], run['version'], {'delivery_id': batch['id']}, db=db)


def mark_online(ledger, batch, result):
    """只消费 GitHub 已确认合并的回执，并原子更新全部关联业务记录。"""
    if not result.get('merged') or not result.get('merge_sha'):
        raise ValueError('缺少 GitHub 合并确认，禁止标记已上线')
    with ledger.store.transaction() as db:
        current = ledger.get('deliveries', batch['id'], db)
        ledger.update('deliveries', current['id'], current['version'], {'merge_sha': result['merge_sha'], 'merged_at': time.time(), 'reason': ''}, 'online', db)
        for ident in current['integrated_ids']:
            run = ledger.get('runs', ident, db)
            ledger.update('runs', ident, run['version'], {'merge_sha': result['merge_sha'], 'online_at': time.time()}, 'online', db)
            req = ledger.get('requirements', run['requirement_id'], db)
            ledger.update('requirements', req['id'], req['version'], {'delivery_id': current['id'], 'merge_sha': result['merge_sha']}, 'online', db)
        if current.get('bootstrap'):
            product = ledger.get('products', current['product_id'], db)
            ledger.update('products', product['id'], product['version'], {'git_migration': {'status': 'completed', 'delivery_id': current['id'], 'merge_sha': result['merge_sha']},
                'delivery_repository': current['repository']}, db=db)


def complete(scheduler, batch, result, action):
    if batch.get('flow') == 'review_before_release':
        from .staged_delivery import complete as staged_complete
        return staged_complete(scheduler, batch, result, action)
    ledger = scheduler.ledger
    if action == 'prepare' and 'integrated_ids' in result:
        values = {k: result[k] for k in ('repository', 'workspace', 'head_sha', 'base_sha', 'integrated_ids', 'pr_number', 'pr_url', 'backup') if k in result}
        batch = scheduler.change('deliveries', batch, values)
        for ident in result['integrated_ids']:
            run = ledger.get('runs', ident)
            if run['status'] == 'accepted':
                scheduler.change('runs', run, {'delivery_id': batch['id']}, 'delivered')
                req = ledger.get('requirements', run['requirement_id'])
                scheduler.change('requirements', req, {'delivery_id': batch['id']}, 'delivered')
    change = lambda values, status=None: scheduler.change('deliveries', batch, values, status)
    round_id = batch.get('round_id')
    if round_id and action in ('review', 'repair'):
        review = ledger.get('code_reviews', round_id)
        if review.get('phase') != action or review['status'] != 'running' and batch['status'] != {'review': 'code_review', 'repair': 'repairing'}[action]:
            return batch
        if review['status'] == 'running' and review.get('phase') == action:
            scheduler.change('code_reviews', review, {'result': result, 'reason': result.get('reason') or result.get('summary', ''), 'finished_at': time.time()}, result['status'])
    if result.get('stale') and action in ('review', 'validate'):
        return change({'review_pass': None, 'validation_pass': None, 'reason': 'PR 提交变化，自动同步后重新评审'}, 'syncing')
    if result['status'] == 'busy':
        return change({'reason': result.get('reason', ''), 'next_attempt': time.time() + 60})
    if result['status'] != 'pass':
        if result['status'] == 'fail' and action in ('prepare', 'sync', 'review', 'validate'):
            return change({'feedback': result, 'resume_action': action, 'reason': result.get('reason', '代码评审未通过')}, 'repairing')
        from .delivery_review import agent_error
        values = {'reason': result.get('reason', '交付执行失败'), 'resume_status': batch['status']}
        if action == 'repair' and agent_error(result):
            values |= {'revisions': max(0, batch['revisions'] - 1),
                'excluded_repair_rounds': list(dict.fromkeys(batch.get('excluded_repair_rounds', []) + [round_id]))}
        return change(values, 'blocked')
    if action == 'prepare':
        values = {k: result[k] for k in ('repository', 'workspace', 'head_sha', 'base_sha', 'integrated_ids', 'pr_number', 'pr_url', 'backup') if k in result}
        values.update(reason='', review_pass=None, validation_pass=None)
        updated = change(values, 'code_review')
        return updated
    if action == 'review':
        return change({'review_pass': {'head_sha': result['head_sha'], 'base_sha': result['base_sha'], 'round_id': round_id}, 'validation_pass': None, 'reason': ''}, 'validating')
    if action == 'validate':
        return change({'validation_pass': {'head_sha': result['head_sha'], 'base_sha': result['base_sha'], 'round_id': round_id}, 'reason': ''}, 'awaiting_merge')
    if action == 'repair':
        return change({'review_pass': None, 'validation_pass': None, 'reason': '', 'head_sha': result.get('head_sha')}, 'preparing' if batch.get('resume_action') == 'prepare' else 'syncing')
    if action == 'sync':
        changed = result['head_sha'] != batch.get('head_sha') or result['base_sha'] != batch.get('base_sha')
        return change({'head_sha': result['head_sha'], 'base_sha': result['base_sha'], 'reason': '',
            'review_pass': None if changed else batch.get('review_pass'),
            'validation_pass': None if changed else batch.get('validation_pass')},
            'code_review' if changed or not batch.get('review_pass') else 'validating' if not batch.get('validation_pass') else 'merging')
    if action == 'merge':
        if result.get('stale'):
            return change({'review_pass': None, 'reason': '目标版本变化，重新同步与评审'}, 'syncing')
        mark_online(ledger, batch, result)


def tick(scheduler, now=None):
    now = time.time() if now is None else now
    ledger = scheduler.ledger
    for product in ledger.list('products'):
        if not enabled(product):
            continue
        if product.get('delivery_flow', 'review_before_release') == 'review_before_release':
            from .staged_delivery import tick_product
            tick_product(scheduler, product, now)
            continue
        batches = sorted([b for b in ledger.list('deliveries') if b['product_id'] == product['id'] and b['status'] not in FINISHED], key=lambda b: b['day'])
        # Consume in-flight calls even if the project was paused while they ran.
        active = next((b for b in batches if b.get('call')), None)
        pending = next((b for b in batches if b.get('pending_result')), None)
        if pending:
            value = pending['pending_result']
            if pending.get('call'):
                pending = scheduler.consume('deliveries', pending, value['result'])
            complete(scheduler, pending, value['result'], value['action'])
            fresh = ledger.get('deliveries', pending['id'])
            scheduler.change('deliveries', fresh, {'pending_result': None})
            continue
        if active:
            result = scheduler.call_result(active)
            if result is not None:
                action = active['call']['action']
                active = scheduler.change('deliveries', active, {'pending_result': {'action': action, 'result': result}})
                active = scheduler.consume('deliveries', active, result)
                complete(scheduler, active, result, action)
                fresh = ledger.get('deliveries', active['id'])
                scheduler.change('deliveries', fresh, {'pending_result': None})
            continue
        if product['status'] != 'active':
            continue
        attach_accepted(ledger, product, now)
        batches = sorted([b for b in ledger.list('deliveries') if b['product_id'] == product['id'] and b['status'] not in FINISHED], key=lambda b: b['day'])
        if not batches:
            continue
        # 仅跳过额度等待的修复，不让它占住后续已完成成果的上线队列。
        batch = None
        for candidate in batches:
            candidate, waiting = repair_queue(scheduler, candidate, product, now)
            if not waiting:
                batch = candidate
                break
        if batch is None:
            continue
        if now >= batch['cutoff'] and not batch['frozen']:
            batch = scheduler.change('deliveries', batch, {'frozen': True})
        if batch['status'] == 'blocked' or now < batch.get('next_attempt', 0):
            continue
        if batch['status'] == 'awaiting_merge':
            if not batch['frozen']:
                continue
            batch = scheduler.change('deliveries', batch, {}, 'syncing')
        action = {'preparing': 'prepare', 'code_review': 'review', 'validating': 'validate', 'repairing': 'repair', 'syncing': 'sync', 'merging': 'merge'}[batch['status']]
        choice = config(product)
        if action in ('review', 'repair'):
            selected = choice['reviewer' if action == 'review' else 'fixer']
            from reviewers import selection, normalize, snapshot
            selection(selected)
            actual = snapshot(normalize(scheduler.store.settings()) | {'reviewer_mode': 'unified', 'reviewer_unified': selected}, 'acceptance')
            selected = selected | {'bin': selected.get('bin', actual['bin']), 'reasoning_effort': selected.get('reasoning_effort', 'medium')}
            import json
            skill_version = json.loads((Path(__file__).resolve().parents[1] / 'vendor/open-code-review/lock.json').read_text())
            review = ledger.create('code_reviews', {'product_id': product['id'], 'delivery_id': batch['id'], 'title': batch['title'] + (' · 代码评审' if action == 'review' else ' · 问题修复'),
                'phase': action, 'selection': selected, 'skill_version': skill_version, 'pr_url': batch.get('pr_url'), 'pr_number': batch.get('pr_number'), 'head_sha': batch.get('head_sha'), 'base_sha': batch.get('base_sha'), 'started_at': now}, 'running')
            batch = scheduler.change('deliveries', batch, {'selection': selected, 'round_id': review['id'], 'revisions': batch['revisions'] + (action == 'repair')})
        else:
            batch = scheduler.change('deliveries', batch, {'round_id': None})
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/autopilot-adapter.py'), 'delivery']
        scheduler.start_call('deliveries', batch, product, action, command,
            {'runs': [ledger.get('runs', ident) for ident in batch['run_ids']],
             'requirements': [ledger.get('requirements', ledger.get('runs', ident)['requirement_id']) for ident in batch['run_ids']]}, timeout=14400)
