"""研发阶段资源快照：独立工作区可跨阶段并行，实际占用和派发共用判断。"""
from pathlib import Path

LABELS = {'planning':'方案执行槽', 'developing':'开发执行槽', 'verifying':'验证执行槽',
          'review':'评审执行槽', 'exclusive':'发布执行槽'}
PHASE_SLOTS = {'queued':'planning', 'planning':'planning', 'developing':'developing',
               'verifying':'verifying', 'plan_review':'review', 'acceptance_review':'review'}
ACTION_PHASES = {'plan':'planning', 'design_ui':'planning', 'design_api':'planning',
                 'develop':'developing', 'verify':'verifying'}


def slot(run):
    phase = ACTION_PHASES.get((run.get('call') or {}).get('action')) or (run.get('coordination_pending') or {}).get('phase', run['status'])
    return PHASE_SLOTS.get(phase, 'exclusive')


def overlaps(first, second):
    if not first or not second:
        return False
    a, b = Path(first).resolve(), Path(second).resolve()
    return a == b or a in b.parents or b in a.parents


class ResourceSlots:
    """容量按阶段全局限一；重启从持久化调用重建占用，不另建易失信号量。"""
    def __init__(self, ledger):
        self.ledger = ledger
        self.products = {p['id']:p for p in ledger.list('products')}
        self.owners = [r for r in ledger.list('runs') if r.get('call') or r.get('review_id') or
                       r['status'] in ('awaiting_release','deploying','observing','pausing','cancelling')]

    def workspace(self, run):
        return run.get('workspace') or (str(self.ledger.store.state/'autopilot/workspaces'/run['id'])
            if run['status']=='queued' else self.products.get(run['product_id'], {}).get('source'))

    def blocker(self, run):
        requested = slot(run)
        from .daily import blocks_development
        for report in self.ledger.list('daily_reports'):
            if not self.products.get(report['product_id'], {}).get('automation_disabled') and blocks_development(report):
                return {'kind':'main_slot', 'resource':requested, 'reason':'等待日报实际复验结束，保护候选目录和测试实例',
                        'blocking_id':report['id'], 'blocking_kind':'daily_reports'}
        for owner in self.owners:
            if owner['id'] == run['id']:
                continue
            occupied = slot(owner)
            # 多个发布候选按登记次序取一个，避免互相等待；真实在途调用不让位。
            if (requested == occupied == 'exclusive' and not owner.get('call') and not owner.get('review_id')
                    and (run.get('created',0),run['id']) < (owner.get('created',0),owner['id'])):
                continue
            resource = None
            if requested == occupied:
                resource = LABELS[requested]
            elif 'exclusive' in (requested, occupied):
                resource = '发布独占资源'
            elif not self.workspace(run) or not self.workspace(owner):
                resource = '工作目录隔离核对'
            elif overlaps(self.workspace(run), self.workspace(owner)) or overlaps(run.get('worker_home'), owner.get('worker_home')):
                resource = '共享工作目录'
            elif requested in ('developing','verifying') and occupied in ('developing','verifying'):
                # 旧桌面适配器开发与验收共用测试实例，不能只按阶段名放开。
                from .project import generic
                if not generic(self.products.get(run['product_id'], {})) or not generic(self.products.get(owner['product_id'], {})):
                    resource = '共享测试实例'
            if resource:
                return {'kind':'main_slot', 'resource':requested, 'reason':f'等待{resource}：任务“{owner.get("title") or owner["id"]}”正在占用',
                        'blocking_id':owner['id'], 'blocking_kind':'runs'}
        return None

    def snapshot(self):
        return {'capacities':{name:1 for name in LABELS},
                'occupants':[{'run_id':r['id'], 'product_id':r['product_id'], 'phase':r['status'],
                    'resource':slot(r), 'call_id':(r.get('call') or {}).get('id'), 'review_id':r.get('review_id')}
                    for r in self.owners],
                'constraints':['同阶段容量为一','工作目录和执行器 home 重叠互斥','旧桌面开发与验证共用测试实例','发布阶段独占']}
