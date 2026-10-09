"""调查推进和按小时恢复异常阻塞；不替代模型审批或发布验收。"""
import datetime
import time
from zoneinfo import ZoneInfo

from .store import DEFAULTS, redact
from .probes import validate


def recover_review(scheduler):
    from .retry import recover
    for run in reversed(scheduler.ledger.list('runs')):
        if run['status']!='blocked':
            continue
        product=scheduler.ledger.get('products',run['product_id'])
        if product.get('automation_disabled'):
            continue
        if recover(scheduler,'runs',run,product)['status']!='blocked':
            return True
    return False


def investigate(scheduler,only_active=False):
    ledger=scheduler.ledger
    rows=[r for r in ledger.list('requirements') if not ledger.get('products',r['product_id']).get('automation_disabled')]
    for index,row in enumerate(rows):
        if row['status']=='awaiting_external' and row.get('reason','').startswith('调查已执行，但转开发证据不足：') and not row.get('investigation_evidence_retries'):
            rows[index]=scheduler.change('requirements',row,{'investigation_evidence_retries':1,'next_investigation':row['updated']+300,
                'reason':row['reason']+'；已安排自动补齐验收条件'},'pending')
    item=next((r for r in rows if r['status']=='investigating'),None)
    if item:
        product=ledger.get('products',item['product_id'])
        if item.get('call'):
            result=scheduler.call_result(item)
            if result is None:
                return True
            item=scheduler.consume('requirements',item,result)
        receipt=(item.get('receipts') or [None])[-1]
        if not receipt:
            scheduler.change('requirements',item,{'reason':'调查调用未建立，稍后重试','next_investigation':time.time()+300},'awaiting_external')
            return True
        result=receipt['result']
        if receipt['call_id']==item.get('investigation_processed_call'):
            return True
        changes={'investigation_result':result,'investigation_processed_call':receipt['call_id'],
            'reason':result.get('reason') or result.get('summary') or '调查未返回结论',
            'next_investigation':time.time()+86400}
        from .retry import abnormal, INTERVAL
        if result.get('status')=='blocked' and abnormal({'reason':changes['reason'],'receipts':[receipt]}):
            changes['next_investigation']=time.time()+INTERVAL
        status='awaiting_external'
        if result.get('status')=='pass' and result.get('checks'):
            if result.get('outcome')=='development':
                try:
                    from .project import generic
                    validate(result.get('resolution_probes'), product if generic(product) else None, assertions=generic(product))
                    if not result.get('resolution_probes') or not result.get('acceptance'):
                        raise ValueError('缺少可执行验收条件')
                    changes.update(classification='development',acceptance=result['acceptance'],
                        resolution_probes=result['resolution_probes'],evidence=redact(result))
                    status='pending'
                except ValueError as exc:
                    changes['reason']='调查已执行，但转开发证据不足：'+str(exc)
                    attempts=item.get('investigation_evidence_retries',0)+1
                    changes['investigation_evidence_retries']=attempts
                    if attempts<=2:
                        changes['next_investigation']=time.time()+300
                        status='pending'
            elif result.get('outcome')=='resolved':
                status='resolved'
        elif not result.get('checks') and result.get('status')=='pass':
            changes['reason']='调查未提供实际检查记录，不能认定已解决或转开发'
        item=scheduler.change('requirements',item,changes,status)
        if status=='pending' and item.get('classification')=='development':
            scheduler.control.queue(item)
        return True
    if only_active:
        return False
    day=datetime.datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    for item in sorted(rows,key=lambda r:(r.get('next_investigation',0),r['created'])):
        if item['status'] not in ('pending','awaiting_external') or item.get('classification') not in ('investigation','environment') or item.get('in_scope') is not True:
            continue
        if item.get('next_investigation',0)>time.time():
            continue
        product=ledger.get('products',item['product_id'])
        if product['status']!='active' or product.get('call') and product['call'].get('action')=='discover':
            continue
        # 每个调查轮次消耗一个当日需求名额；同一需求随后转开发不重复扣名额。
        if item.get('investigation_day')!=day:
            with scheduler.store.transaction() as db:
                if not ledger.budget(product['id'],'development',(DEFAULTS | product.get('policy',{}))['runs_per_day'],db):
                    continue
                item=ledger.update('requirements',item['id'],item['version'],{'investigation_day':day,'investigation_budget_reserved':True},db=db)
        item=scheduler.change('requirements',item,{'reason':'正在只读调查根因并补充验收证据'},'investigating')
        scheduler.start_call('requirements',item,product,'investigate',product['executor'],{'requirement':item},timeout=600)
        return True
    return False
