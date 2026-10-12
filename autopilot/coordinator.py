"""项目协调实例：只读模型建议经过版本、资源和人工控制校验后原子执行。"""
import hashlib
import json
from pathlib import Path
import runpy
import sys
import time

from review_core import Conflict, ACTIVE
from .store import DEFAULTS, TERMINAL, redact

AUXILIARY = ('chat_messages', 'research_jobs', 'agent_messages', 'coordination_decisions', 'test_chains', 'test_chain_runs', 'inspection_plans', 'requirement_groups')
PHASES = ('planning', 'developing', 'verifying', 'plan_review', 'acceptance_review')
ACTIONS = ('wait', 'repair', 'verify', 'assist', 'reprioritize', 'human')
WATCH_SECONDS = 300


def config(product):
    return {'enabled': True, 'model': None, 'human_authority': False} | product.get('coordinator', {})


def selected(product):
    return config(product).get('model') or product.get('agents', {}).get('acceptance', {})


def enabled(product):
    return config(product)['enabled'] and product['status'] == 'active' and not product.get('automation_disabled')


def validate(value):
    if not isinstance(value, dict) or set(value) - {'enabled', 'model', 'human_authority'} or type(value.get('enabled', True)) is not bool:
        raise ValueError('协调配置无效')
    if type(value.get('human_authority', False)) is not bool:
        raise ValueError('Watch 决策授权必须为布尔值')
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


def protected(run, delegated=False):
    return bool(run.get('control') or run.get('uncertain') or run.get('pending_result') or run.get('call') or
                run['status'] in TERMINAL | {'pausing', 'paused', 'cancelling', 'deploying', 'observing', 'awaiting_release'} or
                run.get('resume_status') in ('deploying', 'observing', 'awaiting_release') or (run.get('coordinator_human') and not delegated))


def delegated_question(ledger, product, run, db=None):
    """项目显式授权后，Watch 可回答自己提交的技术协调问题；保留真实人工控制。"""
    if not config(product)['human_authority'] or run['status'] != 'waiting_for_reply':
        return None
    ident = run.get('coordinator_human')
    if not ident or ident != run.get('collaboration_wait') or run.get('collaboration_resume_status') != 'blocked':
        return None
    try:
        question = ledger.get('agent_messages', ident, db)
    except KeyError:
        return None
    if (question['status'] != 'needs_human' or question.get('call') or question.get('parent_message_id') or
            question['product_id'] != product['id'] or question.get('run_id') != run['id'] or
            question.get('from_role') != 'coordinator' or question.get('to_role') != 'human'):
        return None
    return {k:question.get(k) for k in ('id', 'version', 'content', 'reason')}


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
    from .run_progress import RunProgress
    progress_view = RunProgress(ledger)
    tasks = []
    for run in ledger.scoped('runs', product['id']):
        if run['status'] in TERMINAL:
            continue
        question = delegated_question(ledger, product, run)
        tasks.append({k: run.get(k) for k in ('id', 'version', 'title', 'status', 'resume_status', 'reason',
            'commit', 'revisions', 'extra_revisions', 'execution_seconds', 'feedback', 'coordination_no_progress',
            'coordination_progress', 'coordination_pending', 'coordinator_human', 'requirement_id')} |
            {'current_progress':progress_view.current(run), 'protected': protected(run, delegated=bool(question)), 'delegated_question': question, 'checks': checks_snapshot(run),
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
    requirements = requirement_states(ledger, product['id'])
    return {'product_id': product['id'], 'human_authority': config(product)['human_authority'], 'tasks': tasks, 'requirements': requirements, 'quota': resources,
            'policy': DEFAULTS | product.get('policy', {}), 'model': selected(product),
            'messages': [{k:m.get(k) for k in ('id','status','run_id','topic','reply')}
                         for m in ledger.scoped('agent_messages', product['id'])[-20:]]}


def requirement_states(ledger, pid):
    """核对完整需求台账与关联任务；需求仍为 queued 时采用任务实际状态。"""
    runs = ledger.scoped('runs', pid)
    by_id = {r['id']: r for r in runs}
    by_requirement = {}
    for run in runs:
        by_requirement[run.get('requirement_id')] = run
    result = []
    for req in ledger.scoped('requirements', pid):
        run = by_id.get(req.get('run_id')) if req.get('run_id') else by_requirement.get(req['id'])
        issue = ''
        if run and run.get('requirement_id') != req['id']:
            run = None
            issue = '关联任务不属于该需求'
        elif req.get('run_id') and not run:
            issue = '关联任务不存在或不属于当前项目'
        elif req['status'] == 'queued' and not run:
            issue = '需求已入队但缺少关联任务'
        result.append({k:req.get(k) for k in ('id','title','status','version')} | {
            'run_id': run['id'] if run else req.get('run_id'),
            'effective_status': run['status'] if run else req['status'],
            'reason': run.get('reason', '') if run else req.get('reason', ''), 'issue': issue})
    return result


def watch_summary(material):
    """记录异常及可协调性；人工控制或未确认的执行结果只报告，不自动重放。"""
    findings = []
    for req in material['requirements']:
        if req['issue'] or (not req.get('run_id') and req['effective_status'] in ('blocked','failed','waiting_for_reply')):
            findings.append({'requirement_id':req['id'], 'title':req.get('title'),
                'status':req['effective_status'], 'reason':req['issue'] or req['reason'], 'action':'核对需求及关联任务'})
    known = set(PHASES) | TERMINAL | {'queued','blocked','waiting_for_reply','pausing','paused','cancelling',
        'deploying','observing','awaiting_release','awaiting_ui_chain','rollback_pending'}
    for task in material['tasks']:
        if task['status'] not in ('blocked','waiting_for_reply') and task['status'] in known:
            continue
        action = ('等待人工回复' if task.get('coordinator_human') else '保持人工控制或核对执行结果') if task['protected'] else (
            '评估帮助与资源协调' if task.get('delegated_question') else
            '等待已派发协作回复' if task['status']=='waiting_for_reply' else
            '等待已批准协调的资源' if task.get('coordination_pending') else '评估帮助与资源协调')
        findings.append({'run_id':task['id'], 'requirement_id':task.get('requirement_id'),
            'title':task.get('title'), 'status':task['status'], 'reason':(task.get('current_progress') or {}).get('reason') or task.get('reason'), 'action':action})
    return {'requirements_checked':len(material['requirements']), 'tasks_checked':len(material['tasks']),
            'abnormal_count':len(findings), 'findings':findings}


def help_plan(result):
    """需要帮助时必须先给出可执行、可验证的方案，禁止用一句建议代替方案。"""
    if result.get('action') == 'wait':
        return None
    plan = result.get('help_plan')
    if not isinstance(plan, dict) or not isinstance(plan.get('basis'), str) or not plan['basis'].strip():
        raise ValueError('帮助方案缺少问题依据')
    for field in ('steps', 'resources', 'verification', 'stop_conditions'):
        values = plan.get(field)
        if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError('帮助方案缺少有效的 ' + field)
    return redact(plan)


def fingerprint(material):
    value = json.loads(json.dumps(material))
    # Local coordinator bookkeeping and quota reset timestamp must not trigger a loop.
    for task in value['tasks']:
        task.pop('version', None)
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def status(ledger, product):
    items = ledger.scoped('coordinators', product['id'])
    return {'config': config(product), 'model': selected(product), 'version': product['version'], 'watch_seconds':WATCH_SECONDS,
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


def human(ledger, run, reason, db, plan_id=None):
    from .collaboration import request_reply
    if plan_id is None:
        plan = {'basis':reason, 'steps':['核对最近控制器检查与正式审查回执','补充修复方向或新证据后再评估'],
            'resources':['人工确认；确认前不占用开发执行槽'], 'verification':['取得人工回复及可验证的新证据'],
            'stop_conditions':['人工未回复时保持等待','副作用未确认时禁止自动重放']}
        decision = ledger.create('coordination_decisions', {'product_id':run['product_id'], 'reason':reason,
            'help_plan':plan, 'plan_saved_at':time.time(), 'result':{'action':'human','run_id':run['id'],
            'run_version':run['version'],'strategy':reason}, 'source':'watch'}, 'needs_human', db=db)
        plan_id = decision['id']
        reason += '\n帮助方案：\n' + json.dumps(plan, ensure_ascii=False, indent=2)
    message = request_reply(ledger, run, [{'to_role': 'human', 'topic': '项目协调需要帮助',
        'content': reason, 'evidence_ids': []}], 'coordinator', 'coordinator-human-' + run['id'] + '-' + str(run.get('revisions', 0))
        + ('-manual-' + str(run['manual_retry_count']) if run.get('manual_retry_count') else ''), db)
    return ledger.update('runs', run['id'], run['version'], {'coordinator_human': message['id'],
        'collaboration_wait': message['id'], 'collaboration_resume_status': 'blocked',
        'reason': reason, 'coordination_pending': None, 'coordination_plan_id':plan_id}, 'waiting_for_reply', db)


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
        try:
            changes.update(help_plan=help_plan(result), plan_saved_at=time.time())
        except ValueError as exc:
            return finish('blocked', str(exc))
        target = next((t for t in decision['material']['tasks'] if t['id'] == result.get('run_id')), None)
        if not target or result.get('run_version') != target['version']:
            return finish('stale', '建议没有引用本次任务版本')
        run = ledger.get('runs', target['id'], db)
        if run['product_id'] != product['id'] or run['version'] != target['version']:
            return finish('stale', '任务已变化，等待重新评估')
        question = delegated_question(ledger, product, run, db)
        if question != target.get('delegated_question'):
            return finish('stale', '待答问题或授权已变化，等待重新评估')
        if protected(run, delegated=bool(question)) or run.get('coordination_pending'):
            return finish('blocked', '任务受人工控制、仍在执行或结果待核对')
        if run.get('review_id'):
            review = scheduler.store.get(run['review_id'])
            if review['status'] in ACTIVE or not review.get('execution_done') or review.get('human'):
                return finish('blocked', '原审查尚未结束或由人工接管')
        strategy = result.get('strategy', '')
        if not isinstance(strategy, str) or not strategy.strip():
            return finish('blocked', '缺少具体处理策略')
        instruction = strategy + '\n帮助方案：\n' + json.dumps(changes['help_plan'], ensure_ascii=False, indent=2)
        if question and action in ('repair', 'verify', 'assist'):
            phase = 'verifying' if action == 'verify' else result.get('resume_phase')
            if action == 'repair' and phase not in ('planning', 'developing'):
                return finish('blocked', '返修阶段无效，保留待答问题')
            if action == 'assist' and result.get('to_role') not in ('discovery','implementation','verification','acceptance'):
                return finish('blocked', '协助角色无效，保留待答问题')
            from .collaboration import reply
            message = ledger.get('agent_messages', question['id'], db)
            reply(ledger, message, instruction, db, author='coordinator')
            run = ledger.get('runs', run['id'], db)
            changes['answered_question_id'] = question['id']
            changes['decision_authority'] = 'delegated_watch'
        if action == 'reprioritize':
            priority = result.get('priority')
            if run['status'] != 'queued' or type(priority) is not int or not 0 <= priority <= 10:
                return finish('blocked', '只能调整未开始任务的队列优先级')
            ledger.update('runs', run['id'], run['version'], {'coordination_priority': priority,
                'coordination_reason': strategy}, db=db)
        elif action == 'human':
            human(ledger, run, instruction, db, plan_id=decision['id'])
        elif action == 'assist':
            role = result.get('to_role')
            if role not in ('discovery','implementation','verification','acceptance') or run['status'] != 'blocked':
                return finish('blocked', '只能为阻塞任务派发已配置角色协助')
            from .collaboration import request_reply
            msg = request_reply(ledger, run, [{'to_role': role, 'topic': '协调协助：' + run.get('title', run['id']),
                'content': instruction, 'evidence_ids': target['evidence_ids']}], 'coordinator', decision['id'], db)
            ledger.update('runs', run['id'], run['version'], {'collaboration_wait': msg['id'],
                'collaboration_resume_status': 'blocked', 'coordinator_assist': True,
                'reason': msg['reason']}, 'waiting_for_reply', db)
        else:
            phase = 'verifying' if action == 'verify' else result.get('resume_phase')
            if run['status'] != 'blocked' or phase not in ('planning','developing','verifying') or (action == 'repair' and phase == 'verifying'):
                return finish('blocked', '当前任务不能恢复到建议阶段')
            if run.get('coordination_no_progress', 0) >= 2 and not config(product)['human_authority']:
                human(ledger, run, '连续两轮没有可验证进展：' + strategy, db)
                return finish('needs_human', '停止自动追加，等待人工回复')
            # 调度方案只授权一轮；技术修复方案必须重新生成并通过方案评审。
            phase = 'planning' if action == 'repair' else phase
            ledger.update('runs', run['id'], run['version'], {'coordination_pending': {
                'decision_id': decision['id'], 'phase': phase, 'strategy': instruction, 'action': action,
                'requires_human_authority': config(product)['human_authority']},
                'coordination_reason': strategy, 'revision_exhausted': False}, db=db)
        latest = ledger.get('runs', run['id'], db)
        ledger.update('runs', latest['id'], latest['version'], {'coordination_plan_id':decision['id']}, db=db)
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
            if pending.get('requires_human_authority') and not config(product)['human_authority']:
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
                from .retry import repair_plan_changes
                planning = repair_plan_changes(run) if pending['action'] == 'repair' else {}
                ledger.update('runs', run['id'], run['version'], planning | {'feedback': pending['strategy'] + '\n原失败原因：' + run.get('reason', ''),
                    'review_id': None, 'review_packet': None, 'reason': '', 'coordination_wait': '',
                    'next_auto_retry_at': None, 'coordination_baseline': checks_snapshot(run)}, pending['phase'], db)
            return True
    return False


def consume_grant(scheduler, run):
    pending = run.get('coordination_pending')
    if not pending:
        return run
    product = scheduler.ledger.get('products', run['product_id'])
    if not enabled(product) or (pending.get('requires_human_authority') and not config(product)['human_authority']):
        raise Conflict('协调或 Watch 决策授权已停用，未消费的授权保持等待')
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
    if run.get('coordination_pending') and (not enabled(product) or
            (run['coordination_pending'].get('requires_human_authority') and not config(product)['human_authority'])):
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
            help_plan(result)
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
        with ledger.store.transaction() as db:
            state = instance(ledger, product, db)
            forced = state.get('force', False)
            if not forced and state.get('last_check') and now - state['last_check'] < WATCH_SECONDS:
                continue
        for run in ledger.scoped('runs', pid):
            if run['status'] == 'blocked' and not protected(run):
                changes = progress(run)
                if changes:
                    run = scheduler.change('runs', run, changes)
                if run.get('coordination_no_progress', 0) >= 2 and not config(product)['human_authority']:
                    with ledger.store.transaction() as db:
                        human(ledger, ledger.get('runs', run['id'], db), '连续两轮没有可验证进展，请补充修复方向或新的证据。', db)
        material = snapshot(ledger, product)
        watch = watch_summary(material)
        with ledger.store.transaction() as db:
            state = ledger.update('coordinators', state['id'], state['version'], {'last_check':now, 'watch':watch, 'force':False}, db=db)
            ledger.store.event('autopilot_requirement_watch', detail={'product_id':pid, 'checked_at':now, **watch}, db=db)
        # The lightweight watch continues while a slow model or auxiliary slot is busy.
        if any(d['status'] in ('queued', 'running') for d in ledger.scoped('coordination_decisions', pid)):
            continue
        digest = fingerprint(material)
        if not forced and not watch['abnormal_count'] and digest == state.get('fingerprint'):
            continue
        help_ids = {f['run_id'] for f in watch['findings'] if f.get('run_id') and f['action']=='评估帮助与资源协调'}
        candidates = [r for r in material['tasks'] if not r['protected'] and not r.get('coordination_pending') and (r['status'] in ('blocked','queued') or r['id'] in help_ids)]
        reason = ''
        if not candidates and not any(r['issue'] for r in material['requirements']):
            reason = '异常已核对，等待人工、协作回复或已批准资源；详见需求检查' if watch['abnormal_count'] else '没有需要协调的任务'
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
