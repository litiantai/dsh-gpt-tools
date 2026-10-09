"""北京时间 19 点的持久化日报、逐项复验与复盘，使用独立用量账本。"""
import datetime as dt
import json
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

from .store import redact
from .workspace import digest, git

ZONE = ZoneInfo('Asia/Shanghai')


def snapshot(ledger, product, now):
    local = dt.datetime.fromtimestamp(now, ZONE)
    due = local.replace(hour=19, minute=0, second=0, microsecond=0)
    if local < due:
        due -= dt.timedelta(days=1)
    if due.timestamp() < product.get('daily_report_enabled_at', now):
        return None
    ident = product['id'] + '-' + due.date().isoformat()
    with ledger.store.transaction() as db:
        try:
            return ledger.get('daily_reports', ident, db)
        except KeyError:
            pass
        previous = db.execute('SELECT data FROM auto_daily_reports WHERE product_id=? ORDER BY created DESC LIMIT 1', (product['id'],)).fetchone()
        start = json.loads(previous[0])['cutoff'] if previous else due.replace(hour=0).timestamp()
        cutoff = due.timestamp()
        def records(kind):
            return [ledger.decode(row) for row in db.execute(f'SELECT * FROM auto_{kind} WHERE product_id=?', (product['id'],))]
        runs = records('runs')
        releases = records('releases')
        deliveries = records('deliveries')
        accepted = [r for r in runs if start < r.get('accepted_at', 0) <= cutoff]
        audit_runs = ([r for r in runs if r['status']=='online' and start < r.get('online_at',0) <= cutoff]
                      if product.get('git',{}).get('enabled') else accepted)
        activity = [r for r in runs if start <= r['updated'] <= now]
        attribution={}
        if product.get('nightly_attribution'):
            signals=records('signals')
            ids=[r['id'] for r in signals if r['created']<=cutoff and (r['status']=='pending' or r.get('attribution_pending') or start<r['created']<=cutoff)]
            for inspection in records('inspections'):
                if not start < inspection['created'] <= cutoff:
                    continue
                steps=[ledger.get('evidence',ident,db) for ident in inspection.get('steps',[])]
                signal=ledger.create('signals',{'product_id':product['id'],'source':'inspection','code':'NIGHTLY_INSPECTION',
                    'summary':inspection['title'],'inspection_id':inspection['id'],
                    'evidence':{'status':inspection['status'],'judgement':inspection.get('judgement'),
                        'steps':[{k:step.get(k) for k in ('title','action','expected','actual','judgement','status')} for step in steps]}},
                    ident='nightly-'+inspection['id'],db=db)
                ids.append(signal['id'])
            attribution={'attribution_signal_ids':list(dict.fromkeys(ids)), 'attribution_results':[], 'requirement_ids':[], 'daily_requirements':[]}
        stats = {'requirements_found': sum(start <= r['created'] <= cutoff for r in records('requirements')),
                 'inspections_done': sum(start <= r['created'] <= cutoff for r in records('inspections')),
                 'accepted_count': len(accepted),
                 'published_count': sum(start <= r['created'] <= cutoff and r['status'] in ('observing', 'completed') for r in releases),
                 'online_count': sum(start < r.get('merged_at', 0) <= cutoff and r['status']=='online' for r in deliveries),
                 'delivered_count': sum(r['status']=='delivered' for r in runs),
                 'queued_count': sum(r['status'] == 'queued' for r in runs)}
        return ledger.create('daily_reports', {'product_id': product['id'], 'title': due.date().isoformat() + ' 研发日报与复盘',
            'day': due.date().isoformat(), 'cutoff': cutoff, 'window_start': start, 'timezone': 'Asia/Shanghai',
            'stats': stats, 'run_ids': [r['id'] for r in audit_runs], 'audits': [], 'quota_exempt': True,
            'release_results': [{'status': r['status'], 'reason': r.get('reason', '')} for r in releases if start <= r['updated'] <= now],
            'code_deliveries': [{'status': r['status'], 'pr_url': r.get('pr_url'), 'feature_pr_url': r.get('feature_pr_url'),
                'feature_prs': r.get('feature_prs', []), 'cutoff': r.get('cutoff'), 'merge_sha': r.get('merge_sha'),
                'reason': r.get('reason', '')} for r in deliveries if start <= r['updated'] <= now],
            'activity': [{'title': r['title'], 'status': r['status'], 'reason': r.get('reason', '')} for r in activity],
            'reason': '日报已建立，按顺序进行晚间归因、复验与复盘；统计截至北京时间 19:00，排队状态为生成时快照'} | attribution, 'running', ident, db)


def tick(scheduler):
    ledger = scheduler.ledger
    for product in ledger.list('products'):
        if not product.get('automation_disabled') and product.get('daily_report_enabled'):
            snapshot(ledger, product, time.time())
    # 每次仅推进一份报告；每项调用与回执持久化，重启不会重复验收已完成项。
    report = next((r for r in reversed(ledger.list('daily_reports')) if r['status'] == 'running' and not ledger.get('products',r['product_id']).get('automation_disabled')), None)
    if not report:
        return
    product = ledger.get('products', report['product_id'])
    pending_receipt = len(report.get('receipts', [])) > report.get('processed_receipts', 0)
    if report.get('call') or pending_receipt:
        if report.get('call'):
            result = scheduler.call_result(report)
            if result is None:
                return True
            report = scheduler.consume('daily_reports', report, result)
        receipt = report['receipts'][-1]
        result, action = receipt['result'], receipt['action']
        processed = {'processed_receipts': len(report['receipts'])}
        if action == 'daily_attribution':
            index=len(report.get('attribution_results',[]))
            ids=report['attribution_signal_ids'][index*8:(index+1)*8]
            with ledger.store.transaction() as db:
                items=[]
                if result['status']=='pass':
                    items=scheduler.accept_discovery(product,result,ids,report['day'],report['id'],db)
                    for conclusion in result.get('attributions',[]):
                        source=ledger.get('signals',conclusion['signal_id'],db)
                        ledger.update('signals',source['id'],source['version'],{'attribution':conclusion,'attribution_day':report['day'],'attribution_pending':False},
                            'reviewed' if source['status']=='pending' else source['status'],db)
                else:
                    for ident in ids:
                        source=ledger.get('signals',ident,db)
                        ledger.update('signals',ident,source['version'],{'attribution_pending':True,'reason':result.get('reason','晚间归因未完成')},db=db)
                requirement_ids=list(dict.fromkeys(report.get('requirement_ids',[]) + [r['id'] for r in items]))
                all_items=[ledger.get('requirements',ident,db) for ident in requirement_ids]
                ledger.update('daily_reports',report['id'],report['version'],processed | {
                    'attribution_results':report.get('attribution_results',[])+[result | {'signal_ids':ids}],
                    'requirement_ids':requirement_ids,
                    'daily_requirements':[{k:r.get(k) for k in ('id','title','classification','reason','status')} for r in all_items],
                    'reason':f'晚间归因已处理 {min((index+1)*8,len(report["attribution_signal_ids"]))}/{len(report["attribution_signal_ids"])} 条信号，已入池 {len(requirement_ids)} 个当日需求'},db=db)
        elif action == 'daily_acceptance':
            run = ledger.get('runs', report['run_ids'][len(report['audits'])])
            scheduler.change('daily_reports', report, processed | {'audits': report['audits'] + [redact(result) | {'run_id': run['id'], 'title': run['title']} ]})
        else:
            statuses = [a['status'] for a in report['audits'] + report.get('attribution_results',[])] + [result['status']]
            status = 'fail' if 'fail' in statuses else 'blocked' if any(s != 'pass' for s in statuses) else 'pass'
            from .usage import collect, trace_tokens
            collect(ledger)
            traces = [Path(r['result']['evidence']) / 'trace.jsonl' for r in report['receipts'] if r['result'].get('evidence')]
            usage = sum(trace_tokens(path) for path in traces if path.is_file())
            scheduler.change('daily_reports', report, processed | {'retrospective': redact(result), 'finished_at': time.time(),
                'exempt_usage': usage, 'reason': result.get('summary') or result.get('reason', '日报执行未返回说明')}, status)
        return True
    # 复验与研发模型串行，巡检可以继续；已启动的调用先收取回执。
    if any(p.get('call', {}).get('action') == 'discover' for p in ledger.list('products') if p.get('call')):
        return False
    if any(r.get('call') or r.get('review_id') for r in ledger.list('runs') if r['status'] not in ('accepted','completed','blocked','cancelled','rolled_back')):
        return False
    if any(r['status']=='investigating' for r in ledger.list('requirements')):
        return False
    command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/autopilot-adapter.py'), 'daily']
    remaining=report.get('attribution_signal_ids',[])[len(report.get('attribution_results',[]))*8:]
    if remaining:
        extra={'signals':[ledger.get('signals',ident) for ident in remaining[:8]]}
        action='daily_attribution'
    elif len(report['audits']) < len(report['run_ids']):
        run = ledger.get('runs', report['run_ids'][len(report['audits'])])
        extra = {'audit_run': run, 'requirement': ledger.get('requirements', run['requirement_id'])}
        action = 'daily_acceptance'
    else:
        extra = {'daily_report': report}
        action = 'daily_retrospective'
    scheduler.start_call('daily_reports', report, product, action, command, extra, timeout=900)
    return True


def execute(action, request):
    from .codex_executor import execute as codex
    if action=='daily_attribution':
        result=codex(action,request)
        if result.get('status')=='pass':
            ids={s['id'] for s in request['signals']}
            actual=[a.get('signal_id') for a in result.get('attributions',[])]
            if len(actual)!=len(ids) or set(actual)!=ids or any(not a.get('reason') for a in result.get('attributions',[])):
                return result | {'status':'blocked','reason':'归因未覆盖本批全部信号，保留待下次处理'}
            if any(not all(r.get(k) for k in ('title','evidence','acceptance','impact','reproduction')) for r in result.get('requirements',[])):
                return result | {'status':'blocked','reason':'候选需求缺少归因或验收材料，保留信号待补齐'}
            linked={ident for r in result.get('requirements',[]) for ident in r.get('signal_ids',[])}
            if any(a['outcome'] in ('requirement','investigation','environment') and a['signal_id'] not in linked for a in result.get('attributions',[])):
                return result | {'status':'blocked','reason':'归因提出问题但未提供对应需求，保留信号待补齐'}
            if any(not r.get('signal_ids') or not set(r['signal_ids'])<=ids for r in result.get('requirements',[])):
                return result | {'status':'blocked','reason':'需求关联了本批以外的信号，待重新核对'}
        return result
    if action == 'daily_retrospective':
        return codex(action, request)
    if action != 'daily_acceptance':
        raise ValueError('日报执行步骤无效')
    if request.get('product',{}).get('git',{}).get('enabled'):
        from .master import acceptance
        run=request['audit_run']
        if run.get('status')!='online' or not run.get('merge_sha'):
            return {'status':'blocked','reason':'需求尚未合入 master，不能进行最终复验'}
        requirement=request['requirement'] | {'merge_sha':run['merge_sha']}
        return acceptance(request | {'requirements':[requirement]})
    run = request['audit_run']
    workspace = Path(run['workspace'])
    before = digest(workspace)
    if git(workspace, 'rev-parse', 'HEAD') != run.get('commit'):
        return {'status': 'blocked', 'reason': '候选版本已变化，无法对原验收版本复验'}
    manifest = json.loads(Path(run['manifest']).read_text())
    if before != run.get('source_digest') or manifest.get('commit') != run.get('commit'):
        return {'status': 'blocked', 'reason': '候选源码与验收清单不一致，需核对后复验'}
    if manifest.get('kind') == 'command':
        from .generic_adapter import validate_manifest
        validate_manifest(run['manifest'])
    if manifest.get('runtime_hash') or manifest.get('app_hash'):
        from .thsoctop import manifest_hash
        candidate = Path(manifest['root'])
        if manifest_hash(candidate/'runtime') != manifest.get('runtime_hash') or manifest_hash(candidate/'thsoctop.app') != manifest.get('app_hash'):
            return {'status': 'fail', 'reason': '验收后构建产物发生变化，复验不能通过'}
    checks = run.get('checks', [])
    if not checks or any(c.get('status') != 'pass' for c in checks if c.get('required', True)):
        return {'status': 'fail', 'reason': '原验收缺少检查回执或存在未通过的必需项'}
    result = codex(action, request | {'record': run, 'checks': checks})
    if digest(workspace) != before:
        return {'status': 'fail', 'reason': '复验期间源码发生变化，本次复验无效', 'details': result}
    if result.get('status') == 'pass' and not result.get('checks'):
        return result | {'status': 'blocked', 'reason': '未提供此次复验的实际检查记录'}
    return result
