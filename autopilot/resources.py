"""研发阶段资源快照：独立工作区可跨阶段并行，实际占用和派发共用判断。"""
from pathlib import Path

LABELS = {'planning':'方案执行槽', 'developing':'开发执行槽', 'verifying':'验证执行槽',
          'review':'评审执行槽', 'exclusive':'发布执行槽'}
PHASE_SLOTS = {'queued':'planning', 'planning':'planning', 'developing':'developing',
               'verifying':'verifying', 'plan_review':'review', 'acceptance_review':'review'}
BACKGROUND_ACTIONS = {'requirements':{'investigate'}, 'products':{'discover'},
                      'daily_reports':{'daily_attribution','daily_retrospective'}}
READ_PHASES = {'queued','planning','plan_review'}

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
        self.owners = [r for r in ledger.list('runs') if r.get('call') or self.review_occupies(r) or
                       r['status'] in ('awaiting_release','deploying','observing','pausing','cancelling')]
        from .daily import blocks_development
        self.owners.extend(self.daily_candidate(r) for r in ledger.list('daily_reports') if blocks_development(r))

    def review_occupies(self, run):
        """历史审查引用不占资源；审查阶段及仍活动的审查继续保护。"""
        if not run.get('review_id'):
            return False
        if run['status'] in ('plan_review','acceptance_review'):
            return True
        from review_core import ACTIVE
        try:
            return self.ledger.store.get(run['review_id'])['status'] in ACTIVE
        except KeyError:
            return True

    def workspace(self, run):
        return run.get('workspace') or (str(self.ledger.store.state/'autopilot/workspaces'/run['id'])
            if run['status']=='queued' else self.products.get(run['product_id'], {}).get('source'))

    def blocker(self, run):
        requested = slot(run)
        phase = (run.get('coordination_pending') or {}).get('phase', run['status'])
        if phase not in READ_PHASES:
            for kind, item, product, action in self.background_owners():
                if requested == 'exclusive' or overlaps(self.workspace(run), self.background_workspace(item,product)):
                    return {'kind':'main_slot','resource':requested,'reason':'等待只读调用释放共享源码或发布资源',
                            'blocking_id':item['id'],'blocking_kind':kind}
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
                        'blocking_id':owner['id'], 'blocking_kind':owner.get('resource_kind','runs')}
        return None

    def daily_candidate(self, report):
        """实际复验按目标候选占验证资源；旧记录目标不明时保守独占。"""
        product = self.products.get(report['product_id'], {})
        ids = report.get('run_ids', [])
        index = len(report.get('audits', []))
        try:
            target = self.ledger.get('runs',ids[index])
        except (IndexError, KeyError):
            return report | {'status':'exclusive','resource_kind':'daily_reports'}
        workspace = (str(self.ledger.store.state/'autopilot/master-source'/product['id'])
                     if product.get('git', {}).get('enabled') else self.workspace(target))
        return report | {'status':'verifying','workspace':workspace,'resource_kind':'daily_reports'}

    def background_workspace(self, item, product):
        if product.get('test_execution') == 'local':
            return str(self.ledger.store.state/'autopilot/master-source'/product['id'])
        return item.get('workspace') or product.get('inspection_workspace') or product.get('repository') or product.get('source')

    def background_owners(self):
        for kind, actions in BACKGROUND_ACTIONS.items():
            for item in self.ledger.list(kind):
                action = (item.get('call') or {}).get('action')
                pid = item['id'] if kind == 'products' else item['product_id']
                if action in actions:
                    yield kind, item, self.products.get(pid, {}), action

    def background_blocker(self, kind, item, product, action):
        """只读后台任务仅等待同类容量、共享写入及发布，不等待无关研发阶段。"""
        if action not in BACKGROUND_ACTIONS.get(kind, set()):
            return None
        for other_kind, owner, _, _ in self.background_owners():
            if other_kind == kind:
                return {'blocking_id':owner['id'], 'blocking_kind':kind, 'reason':'等待同类只读执行槽'}
        workspace = self.background_workspace(item,product)
        for owner in self.owners:
            if owner['status'] in READ_PHASES:
                continue
            if slot(owner) == 'exclusive' or not workspace or overlaps(workspace,self.workspace(owner)):
                return {'blocking_id':owner['id'],'blocking_kind':owner.get('resource_kind','runs'),'reason':'等待共享源码写入或发布结束'}
        return None

    def snapshot(self):
        return {'capacities':{name:1 for name in LABELS},
                'occupants':[{'run_id':r['id'], 'product_id':r['product_id'], 'phase':r['status'],
                    'resource':slot(r), 'call_id':(r.get('call') or {}).get('id'), 'review_id':r.get('review_id')}
                    for r in self.owners],
                'background_capacities':{kind:1 for kind in BACKGROUND_ACTIONS},
                'background_occupants':[{'kind':k,'id':r['id'],'action':a,'call_id':r['call'].get('id')}
                    for k,r,p,a in self.background_owners()],
                'constraints':['同阶段容量为一','工作目录和执行器 home 重叠互斥','旧桌面开发与验证共用测试实例','发布阶段独占']}
