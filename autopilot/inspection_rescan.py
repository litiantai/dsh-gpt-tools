"""异常巡查每日北京时间九点复扫，保留历史并关联真实复扫结论。"""
import datetime as dt
import time
from zoneinfo import ZoneInfo

from review_core import Conflict
from .store import inspection_exception


def exceptions(ledger, product_id, db=None):
    return [s for s in ledger.scoped('signals', product_id, db)
            if inspection_exception(s) and s['status'] not in ('cancelled', 'dismissed')
            and s.get('rescan_status') not in ('recovered', 'collected')]


def enqueue(ledger, product, day=None, db=None):
    """冻结当前启用路线与异常来源；并发请求复用尚未结束的复扫。"""
    if db is None:
        with ledger.store.transaction() as connection:
            return enqueue(ledger, product, day, connection)
    product = ledger.get('products', product['id'], db)
    if product.get('automation_disabled') or product['status'] not in ('active', 'observing'):
        raise Conflict('项目未启用自动运行，恢复项目后可重新扫描')
    if not product.get('adapter'):
        raise ValueError('项目尚未配置巡查执行器')
    ident = f"rescan-{product['id']}-{day}" if day else None
    if ident:
        try:
            return ledger.get('inspection_rounds', ident, db)
        except KeyError:
            pass
    rounds = ledger.scoped('inspection_rounds', product['id'], db)
    active = next((r for r in rounds if r.get('rescan') and r['status'] in ('queued', 'running')), None)
    # 每日任务保留独立日期；前一天仍执行时排队等待，不吞掉当天复扫。
    if active and not day:
        return active
    if (ledger.store.state/'autopilot/update-drain.json').exists():
        raise Conflict('平台更新正在排空，更新结束后重新扫描')
    routes = ledger.scoped('inspection_routes', product['id'], db)
    enabled = [r for r in routes if r.get('enabled') and r['status'] == 'ready' and r.get('current_version_id')]
    if routes and not enabled:
        raise ValueError('没有可执行的启用路线，请先处理路线覆盖缺口')
    commit = product.get('master_environment', {}).get('commit')
    if enabled and not commit:
        raise ValueError('主实例尚未登记可信版本，登记后可重新扫描')
    sources = exceptions(ledger, product['id'], db)
    record = ledger.create('inspection_rounds', {'product_id':product['id'], 'title':'异常巡查重新扫描',
        'rescan':True, 'rescan_day':day, 'timezone':'Asia/Shanghai',
        'legacy_inspection':not routes, 'target_commit':commit,
        'snapshots':[ledger.get('inspection_route_versions', r['current_version_id'], db) for r in enabled],
        'exception_signal_ids':[s['id'] for s in sources], 'receipts':[], 'route_results':[]}, 'queued', ident, db)
    for source in sources:
        ledger.update('signals', source['id'], source['version'],
                      {'rescan_status':'queued', 'rescan_round_id':record['id']}, db=db)
    return record


def schedule(ledger, product, now):
    """错过九点时补跑最近一次到期复扫，重启及重复 tick 不重复入队。"""
    local = dt.datetime.fromtimestamp(now, ZoneInfo('Asia/Shanghai'))
    due = local.replace(hour=9, minute=0, second=0, microsecond=0)
    if local < due:
        due -= dt.timedelta(days=1)
    if due.timestamp() < product['created'] or not exceptions(ledger, product['id']):
        return None
    try:
        return enqueue(ledger, product, due.date().isoformat())
    except (ValueError, Conflict) as exc:
        # 前置条件恢复后重试入队；原异常留在页面可见，不伪造执行结果。
        return {'status':'deferred', 'reason':str(exc), 'rescan_day':due.date().isoformat()}


def finish(ledger, record, result):
    """按路线匹配复扫证据，只有真实断言通过才能标记已恢复。"""
    if not record.get('rescan'):
        return
    routes = {r['route_id']:r for r in result.get('routes', [])}
    with ledger.store.transaction() as db:
        for ident in record.get('exception_signal_ids', []):
            source = ledger.get('signals', ident, db)
            if source.get('rescan_round_id') != record['id']:
                continue
            route_id = source.get('component') or source.get('evidence', {}).get('route_id')
            if not route_id and source.get('inspection_id'):
                try:
                    route_id = ledger.get('inspections', source['inspection_id'], db).get('route_id')
                except KeyError:
                    pass
            checked = result if record.get('legacy_inspection') else routes.get(route_id)
            checked = checked or {'status':'blocked', 'reason':'本次未取得该异常场景的复扫证据，请核对路线覆盖'}
            status = {'pass':'recovered', 'observed':'collected'}.get(checked['status'], 'blocked')
            ledger.update('signals', ident, source['version'], {'rescan_status':status,
                'rescan_result':checked, 'rescan_checked_at':time.time(), 'attribution_pending':True}, db=db)
