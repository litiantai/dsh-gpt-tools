"""只读计算任务当前进度；历史失败和旧调度原因不作为当前等待条件。"""
from .store import TERMINAL
from .quota import snapshot
from .off_peak import waiting
from .retry import quota_reason_for

PHASES = {'queued':'排队', 'planning':'制定方案', 'plan_review':'方案审查',
          'developing':'开发实现', 'verifying':'独立验证', 'acceptance_review':'业务验收'}
ACTIONS = {'queued':'plan', 'planning':'plan', 'developing':'develop', 'verifying':'verify'}


class RunProgress:
    """一次 API 读取内复用项目、额度和阶段资源快照，不改变台账版本。"""
    def __init__(self, ledger):
        self.ledger = ledger
        self.products = {p['id']:p for p in ledger.list('products')}
        self.runs = ledger.list('runs')
        self.quotas = {}
        from .resources import ResourceSlots
        self.resources = ResourceSlots(ledger)

    def authority_wait(self, run, product):
        from .coordinator import enabled, config
        pending = run.get('coordination_pending') or {}
        return bool(pending and (not enabled(product) or
            pending.get('requires_human_authority') and not config(product)['human_authority']))

    def admission(self, run, product, phase):
        if product['id'] not in self.quotas:
            self.quotas[product['id']] = snapshot(self.ledger, product)
        reason = quota_reason_for(self.ledger, run | {'resume_status':phase}, product, 'runs',
                                 not self.quotas[product['id']]['tokens_exhausted'])
        if reason:
            return {'kind':'quota','reason':reason}
        selected = product.get('agents', {}).get('acceptance') if 'review' in phase else None
        wait = waiting(product, ACTIONS.get(phase, phase), selected=selected)
        return {'kind':'off_peak', **wait} if wait else None

    def annotate(self, run):
        current = self.current(run)
        return run | {'current_progress':current} if current else run

    def current(self, run):
        status = run['status']
        product = self.products.get(run['product_id'])
        if product is None:
            return {'kind':'reconcile','label':'等待核对项目','reason':'任务关联项目不存在，等待核对','running':False}
        pending = run.get('coordination_pending') or {}
        phase = pending.get('phase', status)
        label = PHASES.get(phase, phase)
        def result(kind, reason, state='等待执行', running=False, **extra):
            return {'kind':kind, 'label':label+' · '+state, 'reason':reason, 'running':running, **extra}
        if status in TERMINAL:
            return None
        if run.get('call'):
            if status in ('pausing','cancelling'):
                label = '正在暂停' if status == 'pausing' else '正在取消'
                return result('control', run.get('reason') or '等待当前调用停止并核对结果', '等待执行结束', True)
            return result('call', '本轮执行尚未结束，等待回执', '执行中', True, call_id=run['call'].get('id'))
        if run.get('uncertain') or run.get('pending_result'):
            return result('reconcile',run.get('reason') or '等待核对上轮执行结果','待核对')
        if status in ('paused','pausing','cancelling') or run.get('control'):
            return result('control',run.get('reason') or '等待人工恢复任务','已暂停')
        if run.get('review_id') and status in ('plan_review','acceptance_review'):
            try:
                review = self.ledger.store.get(run['review_id'])
            except KeyError:
                return result('review','关联审查记录不可用，等待核对','待核对')
            state = {'running':'执行中','queued':'等待执行','awaiting_human':'等待人工审批',
                     'completed':'回执待处理','blocked':'已阻塞','cancelled':'已取消'}.get(review['status'],'待核对')
            reason = {'running':'本轮审查尚未结束，等待回执','queued':'等待审查执行槽',
                      'completed':'审查已结束，等待调度器处理回执'}.get(review['status'],review.get('reason') or state)
            return result('review',reason,state,review['status']=='running',review_id=review['id'])
        if status == 'waiting_for_reply':
            return result('reply',run.get('reason') or '等待协作回复','等待回复')
        if not pending and status not in PHASES:
            return None
        if (self.ledger.store.state/'autopilot/update-drain.json').exists():
            return result('platform_update','等待平台更新完成，当前暂缓派发')
        if product.get('automation_disabled') or product['status'] != 'active':
            return result('project_paused','项目已暂停或停用自动运行，等待恢复')
        if self.authority_wait(run, product):
            return result('authority','协调或 Watch 决策授权已停用，已批准方案等待恢复授权')
        admission = self.admission(run, product, phase)
        if admission:
            return result(admission['kind'],admission['reason'])
        from .project_groups import runnable
        if not runnable(self.ledger, run):
            return result('dependency','等待关联项目完成前置设计或方案评审')
        # 日报和调查在调度器中优先于主任务，正在执行时明确标出占用者。
        for kind, title in (('daily_reports','每日报告'),('requirements','需求调查')):
            for item in reversed(self.ledger.list(kind)):
                if self.products.get(item['product_id'], {}).get('automation_disabled'):
                    continue
                if item.get('call') and item['status'] in ('running','investigating'):
                    return result('main_slot',f'等待主执行槽：{title}“{item.get("title") or item["id"]}”正在执行',
                                  blocking_id=item['id'],blocking_kind=kind)
        blocker = self.resources.blocker(run)
        if blocker:
            return result(blocker['kind'],blocker['reason'], **{k:v for k,v in blocker.items() if k not in ('kind','reason')})
        if (product.get('call') or {}).get('action') == 'discover':
            return result('discovery','等待当前项目需求发现调用完成')
        return result('dispatch','条件已满足，等待调度器派发'+('新的技术修复方案' if pending.get('action')=='repair' else '下一阶段'))
