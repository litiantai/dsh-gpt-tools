"""测试链路生成与执行工作进程，以及研发验收中的强制检查。"""
from .role_skills import rule as skill_rule
from pathlib import Path
import json
import sys
import time

from .store import Ledger
from .test_chains import (settings, scoped, linked, make_run, revise, validate_definition,
                          enqueue_generation, TERMINAL)
from .computer_use import model_role, execute_run


def ledger_for(request):
    from review_core import Store
    return Ledger(Store(Path(request['state_root']).parent))


def generate(request):
    from .intelligence_worker import run_model
    from .role_skills import output_schema
    chain = request['record']
    properties = output_schema('computer_generate')['properties']
    return run_model('computer_generate', request, model_role(request['product']),
        {'requirement':chain['requirement_snapshot'], 'project_config':request['product'].get('project_config', {})},
        skill_rule('test_chain_worker-verification-1'),
        properties)


def finish_generation(ledger, chain, result):
    from review_core import Conflict
    with ledger.store.transaction() as db:
        chain = ledger.get('test_chains', chain['id'], db)
        if chain.get('current_version_id'):
            return chain
        if result.get('status') != 'pass':
            return ledger.update('test_chains', chain['id'], chain['version'],
                                 {'reason':result.get('reason','无法生成链路')}, 'blocked', db)
        try:
            value = validate_definition(result['definition'], chain['requirement_snapshot'].get('acceptance'))
            req = scoped(ledger, 'requirements', chain['source_requirement_id'], chain['product_id'], db)
            from .test_chains import requirement_hash
            if requirement_hash(req) != chain['requirement_hash']:
                raise ValueError('需求已变化，请重新生成链路')
            ready = revise(ledger, chain, value, req['id'], db=db)
            if req.get('confirmation_status') == 'confirmed' and req['status'] in ('pending', 'awaiting_ui_chain') and not req.get('run_id'):
                req = ledger.update('requirements', req['id'], req['version'],
                    {'classification':'development', 'ui_acceptance':True}, 'pending', db=db)
                from .api import Control
                Control(ledger.store).queue(req, db=db)
            return ready
        except (ValueError, KeyError, Conflict) as exc:
            current = ledger.get('test_chains', chain['id'], db)
            return ledger.update('test_chains', current['id'], current['version'], {'reason':str(exc)}, 'blocked', db)


def verification_checks(request, instance):
    """必需 UI 检查直接阻断，不能被功能问题延期策略转为非必需。"""
    product = request['product']
    if not settings(product)['enabled']:
        return []
    ledger = ledger_for(request)
    chains = linked(ledger, request)
    if not chains:
        return []
    try:
        with ledger.store.transaction() as db:
            run = make_run(ledger, product, chains, request['record'], db)
            import os
            run = ledger.update('test_chain_runs', run['id'], run['version'], {'pid':os.getpid()}, 'running', db)
        result = execute_run(request | {'verify_workspace_identity':True}, run, instance or {})
    except Exception as exc:
        result = {'status':'blocked', 'reason':str(exc)}
    status = result['status']
    return [{'name':'Computer Use 必要链路验收', 'required':True,
             **result, 'status':'fail' if status == 'timeout' else status if status in ('pass','fail') else 'blocked',
             'failure_kind':'ui_acceptance' if status in ('fail','timeout') else 'ui_environment' if status != 'pass' else '',
             'retryable':False, 'commit':request['record'].get('commit')}]


def execute(action, request):
    if action == 'generate':
        return generate(request)
    ledger = ledger_for(request)
    run = ledger.get('test_chain_runs', request['record']['id'])
    if run.get('cancel_requested'):
        ledger.update('test_chain_runs', run['id'], run['version'], {'reason':'用户已停止运行'}, 'cancelled')
        return {'status':'blocked', 'reason':'用户已停止运行'}
    from .local_testing import verification
    test_request = request | {'computer_run_id':run['id'], 'verify_workspace_identity':True, 'record':run['target'], 'product':request['product'] | {'computer_use':run['config']}}
    with verification(test_request, Path(request['state_root'])/'test-chains'/run['id']/'checks') as result:
        if result['status'] != 'pass':
            with ledger.store.transaction() as db:
                current = ledger.get('test_chain_runs', run['id'], db)
                ledger.update('test_chain_runs', run['id'], current['version'],
                              {'reason':result['reason'], 'finished_at':time.time()}, 'cancelled' if current.get('cancel_requested') else 'blocked', db)
            return result
        result = execute_run(test_request, run, result['instance'])
    return result | {'status':'fail' if result['status'] == 'timeout' else 'blocked' if result['status'] == 'cancelled' else result['status']}


def tick(scheduler):
    ledger = scheduler.ledger
    for product in ledger.list('products'):
        for kind in ('test_chains', 'test_chain_runs'):
            for item in ledger.scoped(kind, product['id']):
                if item.get('call'):
                    result = scheduler.call_result(item)
                    if result is None:
                        continue
                    item = ledger.get(kind, item['id'])
                    item = scheduler.consume(kind, item, result)
                    if kind == 'test_chains':
                        finished = finish_generation(ledger, item, result)
                        scheduler.change(kind, finished, {'processed_call':item['receipts'][-1]['call_id']})
                    elif item['status'] not in TERMINAL:
                        scheduler.change(kind, item, {'reason':result.get('reason','执行器中断，需核对副作用'),
                            'finished_at':time.time()}, 'blocked')
                    continue
                if kind == 'test_chains' and item.get('receipts') and item['receipts'][-1]['call_id'] != item.get('processed_call'):
                    finished = finish_generation(ledger, item, item['receipts'][-1]['result'])
                    scheduler.change(kind, finished, {'processed_call':item['receipts'][-1]['call_id']})
                    continue
                if kind == 'test_chains' and item['status'] == 'running':
                    scheduler.change(kind, item, {'reason':'生成进程未建立，等待重新调度'}, 'queued')
                    continue
                if kind == 'test_chain_runs' and item['status'] in ('running','observing','cancelling'):
                    # Embedded verification belongs to the parent worker, not a new scheduler job.
                    pid = item.get('pid')
                    try:
                        if not pid:
                            raise ProcessLookupError()
                        import os
                        os.kill(pid, 0)
                    except (ProcessLookupError, PermissionError):
                        scheduler.change(kind, item, {'reason':'浏览器会话丢失，需核对副作用后重新运行'}, 'blocked')
                    continue
                if item['status'] != 'queued' or product.get('automation_disabled') or not settings(product)['enabled']:
                    continue
                if not scheduler.tokens_available(product):
                    continue
                from .coordinator import auxiliary_busy
                if auxiliary_busy(ledger, product['id']):
                    continue
                action = 'generate' if kind == 'test_chains' else 'run'
                command = [sys.executable, str(Path(__file__).resolve().parents[1]/'scripts/test-chain-worker.py')]
                timeout = 600 if action == 'generate' else item['config']['timeout_seconds'] + 1800
                item = scheduler.change(kind, item, {}, 'running')
                try:
                    started = scheduler.start_call(kind, item, product, action, command, timeout=timeout)
                    if not started.get('call'):
                        scheduler.change(kind, started, {}, 'queued')
                except Exception:
                    current = ledger.get(kind, item['id'])
                    if not current.get('call'):
                        scheduler.change(kind, current, {}, 'queued')
                    raise
