"""项目协调实例：只读模型建议经过版本、资源和人工控制校验后原子执行。"""
import hashlib
import json
from pathlib import Path
import runpy
import sys
import time

from review_core import Conflict, ACTIVE
from .store import DEFAULTS, TERMINAL, redact

AUXILIARY = ('chat_messages', 'research_jobs', 'agent_messages', 'coordination_decisions', 'test_chains', 'test_chain_runs')
PHASES = ('planning', 'developing', 'verifying', 'plan_review', 'acceptance_review')
ACTIONS = ('wait', 'repair', 'verify', 'assist', 'reprioritize', 'human')


def config(product):
    return {'enabled': True, 'model': None} | product.get('coordinator', {})


def selected(product):
    return config(product).get('model') or product.get('agents', {}).get('acceptance', {})


def enabled(product):
    return config(product)['enabled'] and product['status'] == 'active' and not product.get('automation_disabled')


def validate(value):
    if not isinstance(value, dict) or set(value) - {'enabled', 'model'} or type(value.get('enabled', True)) is not bool:
        raise ValueError('协调配置无效')
    if value.get('model') is not None:
        from reviewers import selection
        selection(value['model'])


def instance(ledger, product, db):
    ident = 'coordinator-' + product['id']
    try:
        return ledger.get('coordinators', ident, db)
    except KeyError:
        return ledger.create('coordinators', {'product_id': product['id'], 'last_check': 0,
            'fingerprint': '', 'force': False}, 'idle', ident=ident, db=db)


def auxiliary_busy(ledger, pid):
    return any(item.get('call') for kind in AUXILIARY for item in ledger.scoped(kind, pid))


def protected(run):
    return bool(run.get('control') or run.get('uncertain') or run.get('pending_result') or run.get('call') or
                run['status'] in TERMINAL | {'pausing', 'paused', 'cancelling', 'deploying', 'observing', 'awaiting_release'} or
                run.get('resume_status') in ('deploying', 'observing', 'awaiting_release') or run.get('coordinator_human'))


def checks_snapshot(run):
    """只采用控制器验证回执；开发总结和模型声称修复不能进入进展判断。"""
    from .role_skills import ROOT
    collect = runpy.run_path(str(ROOT/'dsh-role-coordinator/scripts/collect_evidence.py'))['collect']
    return collect(run)


def progress(run):
    baseline = run.get('coordination_baseline')
    if not baseline:
        return None
    current = checks_snapshot(run)
    # Count each finished repair once, including a repair blocked before new tests.
    marker = [run.get('revisions', 0), current.get('receipt_id'), current.get('review_id'), (run.get('receipts') or [{}])[-1].get('call_id')]
    if run.get('coordination_progress_marker') == marker:
        return None
    from .role_skills import ROOT
    compare = runpy.run_path(str(ROOT/'dsh-role-coordinator/scripts/compare_progress.py'))['compare']
    value = compare(baseline, current)
    return {'coordination_progress': value, 'coordination_progress_marker': marker,
            'coordination_no_progress': 0 if value['improved'] else run.get('coordination_no_progress', 0) + 1,
            'coordination_baseline': None}


def snapshot(ledger, product):
    from .quota import snapshot as quota
    tasks = []
    for run in ledger.scoped('runs', product['id']):
        if run['status'] in TERMINAL:
            continue
        tasks.append({k: run.get(k) for k in ('id', 'version', 'title', 'status', 'resume_status', 'reason',
            'commit', 'revisions', 'extra_revisions', 'execution_seconds', 'feedback', 'coordination_no_progress',
            'coordination_progress', 'coordination_pending', 'coordinator_human', 'requirement_id')} |
            {'protected': protected(run), 'checks': checks_snapshot(run),
             'diagnostic': run.get('last_revision_diagnostic'),
             'recent_results': [r.get('result') for r in run.get('receipts', [])[-3:]],
             'evidence_ids': [r['evidence_id'] for r in run.get('receipts', [])[-5:] if r.get('evidence_id')]})
    usage = quota(ledger, product)
    # Credential redaction treats keys containing "token" as secret; supply numeric
    # resource facts under unambiguous quota names to the reasoning model.
    resources = {'day': usage['day'], 'development_used': usage['tokens_used'],
                 'development_limit': usage['tokens_limit'], 'exhausted': usage['tokens_exhausted']}
    quota_file = ledger.store.state / 'quota.json'
    review_usage = json.loads(quota_file.read_text()) if quota_file.is_file() else {}
    resources.update(review_used=review_usage.get('count', 0) if review_usage.get('day') == usage['day'] else 0,
                     review_limit=ledger.store.settings()['max_per_day'],
                     main_capacity=1, auxiliary_capacity=1,
                     auxiliary_in_use=auxiliary_busy(ledger, product['id']))
    return {'product_id': product['id'], 'tasks': tasks, 'quota': resources,
            'policy': DEFAULTS | product.get('policy', {}), 'model': selected(product),
            'messages': [{k:m.get(k) for k in ('id','status','run_id','topic','reply')}
                         for m in ledger.scoped('agent_messages', product['id'])[-20:]]}


def fingerprint(material):
    value = json.loads(json.dumps(material))
    # Local coordinator bookkeeping and quota reset timestamp must not trigger a loop.
    for task in value['tasks']:
        task.pop('version', None)
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def status(ledger, product):
    items = ledger.scoped('coordinators', product['id'])
    return {'config': config(product), 'model': selected(product), 'version': product['version'],
            'instance': items[0] if items else {'status': 'idle', 'reason': '等待首次检查'},
            'decisions': list(reversed(ledger.scoped('coordination_decisions', product['id'])))[0:100],
            'coordination_tokens': ledger.budget_used(product['id'], 'coordination_tokens'),
            'enabled': enabled(product)}


def api(control, parts, body=None):
    ledger = control.ledger
    product = ledger.get('products', parts[1])
    if body is None and len(parts) == 3:
        return status(ledger, product)
    if body is None or len(parts) != 4 or parts[3] not in ('evaluate', 'settings'):
        raise KeyError('协调接口不存在')
    with ledger.store.transaction() as db:
        product = ledger.get('products', product['id'], db)
        if body.get('version') != product['version']:
            raise Conflict('项目已更新，请刷新后重试')
        if parts[3] == 'settings':
            value = body.get('config')
            validate(value)
            ledger.update('products', product['id'], product['version'], {'coordinator': value}, db=db)
        else:
            if not enabled(product):
                raise Conflict('请先启用项目自主运行和项目协调')
            current = instance(ledger, product, db)
            ledger.update('coordinators', current['id'], current['version'], {'force': True}, db=db)
    return status(ledger, ledger.get('products', product['id']))


def human(ledger, run, reason, db):
    from .collaboration import request_reply
    message = request_reply(ledger, run, [{'to_role': 'human', 'topic': '项目协调需要帮助',
        'content': reason, 'evidence_ids': []}], 'coordinator', 'coordinator-human-' + run['id'] + '-' + str(run.get('revisions', 0)), db)
    return ledger.update('runs', run['id'], run['version'], {'coordinator_human': message['id'],
        'collaboration_wait': message['id'], 'collaboration_resume_status': 'blocked',
        'reason': reason, 'coordination_pending': None}, 'waiting_for_reply', db)


def apply(scheduler, decision, result):
    ledger = scheduler.ledger
    with ledger.store.transaction() as db:
        decision = ledger.get('coordination_decisions', decision['id'], db)
        if decision['status'] not in ('running', 'queued'):
            return decision
        product = ledger.get('products', decision['product_id'], db)
        changes = {'result': redact(result), 'processed_at': time.time()}
        def finish(state, reason):
            result = ledger.update('coordination_decisions', decision['id'], decision['version'], changes | {'reason': reason}, state, db)
            owner = instance(ledger, product, db)
            if owner.get('last_decision_id') == decision['id']:
                ledger.update('coordinators', owner['id'], owner['version'],
                              {'reason': reason, 'force': state == 'stale'}, 'idle' if state == 'applied' else state, db)
            return result
        if not enabled(product) or product.get('coordinator', {}) != decision.get('config', {}) or selected(product) != decision.get('selection'):
            return finish('stale', '项目运行模式或协调配置已变化')
        if result.get('status') != 'pass':
            return finish('blocked', result.get('reason') or '协调模型未返回有效建议')
        action = result.get('action')
        if action not in ACTIONS:
            return finish('blocked', '协调动作无效')
        if action == 'wait':
            return finish('applied', result.get('reason') or result.get('strategy') or '等待条件变化')
        target = next((t for t in decision['material']['tasks'] if t['id'] == result.get('run_id')), None)
        if not target or result.get('run_version') != target['version']:
            return finish('stale', '建议没有引用本次任务版本')
        run = ledger.get('runs', target['id'], db)
        if run['product_id'] != product['id'] or run['version'] != target['version']:
            return finish('stale', '任务已变化，等待重新评估')
        if protected(run) or run.get('coordination_pending'):
            return finish('blocked', '任务受人工控制、仍在执行或结果待核对')
        if run.get('review_id'):
            review = scheduler.store.get(run['review_id'])
            if review['status'] in ACTIVE or not review.get('execution_done') or review.get('human'):
                return finish('blocked', '原审查尚未结束或由人工接管')
        strategy = result.get('strategy', '')
        if not isinstance(strategy, str) or not strategy.strip():
            return finish('blocked', '缺少具体处理策略')
        if action == 'reprioritize':
            priority = result.get('priority')
            if run['status'] != 'queued' or type(priority) is not int or not 0 <= priority <= 10:
                return finish('blocked', '只能调整未开始任务的队列优先级')
            ledger.update('runs', run['id'], run['version'], {'coordination_priority': priority,
                'coordination_reason': strategy}, db=db)
        elif action == 'human':
            human(ledger, run, strategy, db)
        elif action == 'assist':
            role = result.get('to_role')
            if role not in ('discovery','implementation','verification','acceptance') or run['status'] != 'blocked':
                return finish('blocked', '只能为阻塞任务派发已配置角色协助')
            from .collaboration import request_reply
            msg = request_reply(ledger, run, [{'to_role': role, 'topic': '协调协助：' + run.get('title', run['id']),
                'content': strategy, 'evidence_ids': target['evidence_ids']}], 'coordinator', decision['id'], db)
            ledger.update('runs', run['id'], run['version'], {'collaboration_wait': msg['id'],
                'collaboration_resume_status': 'blocked', 'coordinator_assist': True,
                'reason': msg['reason']}, 'waiting_for_reply', db)
        else:
            phase = 'verifying' if action == 'verify' else result.get('resume_phase')
            if run['status'] != 'blocked' or phase not in ('planning','developing','verifying') or (action == 'repair' and phase == 'verifying'):
                return finish('blocked', '当前任务不能恢复到建议阶段')
            if run.get('coordination_no_progress', 0) >= 2:
                human(ledger, run, '连续两轮没有可验证进展：' + strategy, db)
                return finish('needs_human', '停止自动追加，等待人工回复')
            ledger.update('runs', run['id'], run['version'], {'coordination_pending': {
                'decision_id': decision['id'], 'phase': phase, 'strategy': strategy, 'action': action},
                'coordination_reason': strategy}, db=db)
        return finish('applied', strategy)


def resume_pending(scheduler):
    """在主研发槽可用时恢复一项授权，额度仍由原调度器执行。"""
    ledger = scheduler.ledger
    for product in ledger.list('products'):
        if not enabled(product):
            continue
        for run in ledger.scoped('runs', product['id']):
            pending = run.get('coordination_pending')
            if not pending or run['status'] != 'blocked' or protected(run):
                continue
            from .retry import quota_reason
            candidate = run | {'resume_status': pending['phase']}
            reason = quota_reason(scheduler, candidate, product, 'runs')
            from .off_peak import waiting
            action = {'planning':'plan','developing':'develop','verifying':'verify'}[pending['phase']]
            wait = waiting(product, action)
            reason = reason or (wait or {}).get('reason', '')
            if reason:
                if run.get('coordination_wait') != reason:
                    scheduler.change('runs', run, {'coordination_wait': reason})
                continue
            with ledger.store.transaction() as db:
                current = ledger.get('runs', run['id'], db)
                if current['version'] != run['version']:
                    continue
                ledger.update('runs', run['id'], run['version'], {'feedback': pending['strategy'] + '\n原失败原因：' + run.get('reason', ''),
                    'review_id': None, 'review_packet': None, 'reason': '', 'coordination_wait': '',
                    'next_auto_retry_at': None, 'coordination_baseline': checks_snapshot(run)}, pending['phase'], db)
            return True
    return False


def consume_grant(scheduler, run):
    pending = run.get('coordination_pending')
    if not pending:
        return run
    product = scheduler.ledger.get('products', run['product_id'])
    if not enabled(product):
        raise Conflict('协调已停用，未消费的授权保持等待')
    revisions = run.get('revisions', 0)
    limit = (DEFAULTS | product.get('policy', {}))['max_revisions']
    changes = {'coordination_pending': None, 'coordination_last_decision': pending['decision_id']}
    if pending['action'] == 'repair':
        changes.update(revisions=revisions + 1, extra_revisions=run.get('extra_revisions', 0) + int(revisions >= limit))
    return scheduler.change('runs', run, changes)


def runnable(scheduler, run):
    """资源等待不独占主槽；已经运行的进程和审查必须先核对结束。"""
    if run.get('call') or run.get('review_id') or run['status'] in ('pausing', 'cancelling'):
        return True
    product = scheduler.ledger.get('products', run['product_id'])
    if product['status'] != 'active' and run['status'] in PHASES:
        return False
    if run.get('coordination_pending') and not enabled(product):
        return False
    if run['status'] not in PHASES:
        return True
    from .retry import quota_reason
    from .off_peak import waiting
    reason = quota_reason(scheduler, run | {'resume_status': run['status']}, product, 'runs')
    # An exhausted execution-time budget must reach advance() to become blocked.
    if reason == '累计开发执行时间额度不足':
        return True
    action = {'planning':'plan', 'developing':'develop', 'verifying':'verify'}.get(run['status'], run['status'])
    wait = waiting(product, action, selected=product.get('agents', {}).get('acceptance') if 'review' in run['status'] else None)
    reason = reason or (wait or {}).get('reason', '')
    if reason and run.get('reason') != reason:
        scheduler.change('runs', run, {'reason': reason})
    return not reason


def evaluate(request):
    from .intelligence_worker import run_model
    from .role_skills import read, output_schema
    schema = output_schema('coordinate')
    fields = schema['properties']
    from .role_skills import ROOT
    validator = runpy.run_path(str(ROOT/'dsh-role-coordinator/scripts/validate_output.py'))['validate']
    result = run_model('coordinate', request | {'budget_kind': 'coordination_tokens'},
        request['record']['selection'], request['record']['material'],
        read('dsh-role-coordinator/references/decisions.md'), fields)

    if result.get('status') == 'pass':
        try:
            validator({k:v for k,v in result.items() if k not in ('evidence','provider','model','role_skill','snapshot_at')}, schema)
        except ValueError as exc:
            return result | {'status':'blocked','reason':'协调结果不符合契约：' + str(exc)}
    return result


def tick(scheduler, now=None):
    now = time.time() if now is None else now
    ledger = scheduler.ledger
    # Reconcile calls even if a project was disabled while the model was running.
    for product in ledger.list('products'):
        pid = product['id']
        decisions = ledger.scoped('coordination_decisions', pid)
        for decision in decisions:
            if decision.get('call'):
                result = scheduler.call_result(decision)
                if result is None:
                    continue
                decision = scheduler.consume('coordination_decisions', decision, result)
            if decision['status'] in ('running', 'queued') and not decision.get('call') and decision.get('receipts'):
                apply(scheduler, decision, decision['receipts'][-1]['result'])
        if not enabled(product):
            continue
        if any(d['status'] in ('queued', 'running') for d in ledger.scoped('coordination_decisions', pid)):
            continue
        with ledger.store.transaction() as db:
            state = instance(ledger, product, db)
            if not state.get('force') and now - state.get('last_check', 0) < 60:
                continue
            state = ledger.update('coordinators', state['id'], state['version'], {'last_check': now}, db=db)
        for run in ledger.scoped('runs', pid):
            if run['status'] == 'blocked' and not protected(run):
                changes = progress(run)
                if changes:
                    run = scheduler.change('runs', run, changes)
                if run.get('coordination_no_progress', 0) >= 2:
                    with ledger.store.transaction() as db:
                        human(ledger, ledger.get('runs', run['id'], db), '连续两轮没有可验证进展，请补充修复方向或新的证据。', db)
        material = snapshot(ledger, product)
        digest = fingerprint(material)
        if not state.get('force') and digest == state.get('fingerprint'):
            continue
        candidates = [r for r in material['tasks'] if not r['protected'] and not r.get('coordination_pending') and r['status'] in ('blocked','queued')]
        reason = ''
        if not candidates:
            reason = '没有需要协调的任务'
        elif not selected(product).get('model'):
            reason = '请配置协调或验收模型'
        elif auxiliary_busy(ledger, pid):
            reason = '等待项目辅助执行槽'
        if reason:
            scheduler.change('coordinators', state, {'reason': reason}, 'idle')
            continue
        from .off_peak import waiting
        wait = waiting(product, 'coordinate', selected=selected(product))
        if wait:
            scheduler.change('coordinators', state, {'reason': wait['reason']}, 'waiting')
            continue
        # Each source fingerprint has one durable evaluation. Explicit evaluate uses a fresh operation epoch.
        with ledger.store.transaction() as db:
            state = ledger.get('coordinators', state['id'], db)
            decision = ledger.create('coordination_decisions', {'product_id': pid, 'material': material,
                'fingerprint': digest, 'selection': selected(product), 'config': product.get('coordinator', {}),
                'budget_kind': 'coordination_tokens', 'receipts': []}, 'queued', db=db)
            ledger.update('coordinators', state['id'], state['version'], {'fingerprint': digest,
                'force': False, 'last_decision_id': decision['id'], 'reason': '正在评估项目任务'}, 'running', db)
        launch(scheduler, decision, product)
    # Recover a crash between durable enqueue and process launch.
    for decision in ledger.list('coordination_decisions'):
        if decision['status'] == 'queued' and not decision.get('call'):
            product = ledger.get('products', decision['product_id'])
            if enabled(product) and not auxiliary_busy(ledger, product['id']):
                launch(scheduler, decision, product)


def launch(scheduler, decision, product):
    command = [sys.executable, str(Path(__file__).resolve().parents[1]/'scripts/intelligence-worker.py')]
    item = scheduler.start_call('coordination_decisions', decision, product, 'coordinate', command, timeout=600)
    if item.get('call'):
        scheduler.change('coordination_decisions', item, {}, 'running')
