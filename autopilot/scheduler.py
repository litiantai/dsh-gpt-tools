"""独立持久化调度器：一次推进一个阶段，所有外部调用都有持久化回执。"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

from review_core import Store, Engine, Conflict, ACTIVE
from .api import Control
from .call import atomic
from .store import DEFAULTS, TERMINAL, redact
from .workspace import checkout, commit, digest, promote


class WorkerStore(Store):
    """审查绑定登记过的独立会话目录，不修改旧监工的 home 配置。"""
    def __init__(self,state,home,reviewer=None):
        super().__init__(state)
        self.worker_home=home
        self.reviewer=reviewer

    def settings(self):
        result=super().settings() | {'home':str(self.worker_home),'harness_profile':'autopilot-review'}
        # 隔离研发审查没有前台桥接客户端的 540 秒等待限制。
        result['review_timeout']=max(result['review_timeout'],600)
        result['handoff_timeout']=max(result['handoff_timeout'],result['review_timeout']+180)
        if self.reviewer:
            result.update(reviewer_mode='unified',reviewer_unified={k:v for k,v in self.reviewer.items() if k in ('provider','model','model_provider')})
            if self.reviewer.get('bin'):
                result[self.reviewer['provider']+'_bin']=self.reviewer['bin']
        return result


class Scheduler:
    def __init__(self,store):
        self.store=store
        self.control=Control(store)
        self.ledger=self.control.ledger
        self.root=store.state/'autopilot'
        self.root.mkdir(exist_ok=True,mode=0o700)
        self.owner=str(uuid.uuid4())
        self.engines={}
        self.children=[]

    def change(self,kind,item,changes=None,status=None):
        if status=='blocked' and kind in ('runs','deliveries'):
            from .retry import abnormal, INTERVAL
            changes=dict(changes or {})
            changes.update(next_auto_retry_at=time.time()+INTERVAL if abnormal(item | changes) else None,
                           auto_retry_wait_reason=None)
        return self.ledger.update(kind,item['id'],item['version'],changes or {},status)

    def start_call(self,kind,item,product,action,command,extra=None,timeout=600):
        if action in ('plan','develop','verify') and not self.tokens_available(product):
            if kind=='runs' and item.get('reason')!='每日 Token 额度已用尽，等待次日或调整额度':
                return self.change(kind,item,{'reason':'每日 Token 额度已用尽，等待次日或调整额度'})
            return item
        call_id=str(uuid.uuid4())
        directory=self.root/'calls'/call_id
        directory.mkdir(parents=True,mode=0o700)
        cwd=item.get('workspace',product['source'])
        if action=='prepare' and not Path(cwd).is_dir():
            cwd=product['source']  # The delivery adapter creates its isolated worktree.
        request={'command':command+[action],'timeout':timeout,'cwd':cwd,
                 'input':{'product':product,'record':item,'state_root':str(self.root)} | (extra or {})}
        path=directory/'request.json'
        atomic(path,request)
        item=self.change(kind,item,{'call':{'id':call_id,'action':action,'started':time.time(),'path':str(path),
            'signal_ids':[s['id'] for s in (extra or {}).get('signals',[])]},
            **({'reason':''} if item.get('reason')=='每日 Token 额度已用尽，等待次日或调整额度' else {})})
        self.children.append(subprocess.Popen([sys.executable,str(Path(__file__).with_name('call.py')),str(path)],
                         stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True))
        return item

    def call_result(self,item):
        call=item.get('call')
        if not call:
            return None
        directory=Path(call['path']).parent
        receipt=directory/'result.json'
        proc=directory/'process.json'
        if receipt.exists():
            result=json.loads(receipt.read_text())
            # A receipt written by timeout handling is not proof that tools have stopped.
            if proc.exists() and self.process_alive(json.loads(proc.read_text()),call['path']):
                return None
            return result
        if proc.exists():
            process=json.loads(proc.read_text())
            if self.process_alive(process,call['path']):
                return None
        if time.time()-call['started']>30:
            return {'status':'blocked','reason':'执行进程已失联，缺少回执；需核对副作用','uncertain':True}
        return None

    @staticmethod
    def process_alive(process,path):
        try:
            cmd=subprocess.check_output(['ps','-p',str(process['pid']),'-o','command='],text=True).strip()
            return str(path) in cmd and 'call.py' in cmd
        except subprocess.CalledProcessError:
            return False

    def consume(self,kind,item,result):
        receipt={'call_id':item['call']['id'],'action':item['call']['action'],
                 'at':time.time(),'result':redact(result)}
        from .evidence import receipt as save_receipt
        saved=save_receipt(self.ledger,kind,item,receipt)
        receipt['evidence_id']=saved['id']
        return self.change(kind,item,{'call':None,'receipts':(item.get('receipts',[])+[receipt])[-100:]})

    def block(self,item,reason,uncertain=False):
        return self.change('runs',item,{'reason':reason,'resume_status':item['status'],'uncertain':uncertain},'blocked')

    def tick(self):
        if not self.ledger.lease('scheduler',self.owner):
            return
        self.children=[child for child in self.children if child.poll() is None]
        from .usage import collect
        collect(self.ledger)
        self.reconcile_evaluations()
        from .delivery import tick as delivery_tick
        try:
            delivery_tick(self)
        except (OSError, ValueError, KeyError, Conflict) as exc:
            self.store.event('autopilot_delivery_error', detail={'reason': str(exc)})
        from .daily import tick as daily_tick
        daily_active=False
        try:
            daily_active=daily_tick(self)
        except (OSError,ValueError,KeyError,Conflict) as exc:
            self.store.event('autopilot_daily_error',detail={'reason':str(exc)})
        for product in self.ledger.list('products'):
            if product['status'] in ('active','observing'):
                try:
                    self.monitor(product,allow_discovery=not daily_active)
                except (OSError,ValueError,KeyError,Conflict) as exc:
                    self.store.event('autopilot_monitor_error',detail={'product_id':product['id'],'reason':str(exc)})
            if product['status']=='active' and not product.get('evaluation_id'):
                for requirement in reversed(self.ledger.list('requirements')):
                    if requirement['product_id']==product['id'] and requirement['status']=='pending' and requirement.get('classification')=='development' and requirement.get('in_scope') is True:
                        try:
                            self.control.queue(requirement)
                        except Conflict:
                            pass
        if daily_active:
            return
        for release in reversed(self.ledger.list('releases')):
            if release['status']=='rollback_pending':
                self.rollback(release)
                return
        from .progress import investigate
        if investigate(self,only_active=True):
            return
        runs=list(reversed(self.ledger.list('runs')))
        current=next((r for r in runs if r['status'] not in TERMINAL | {'queued','blocked'}),None)
        if current:
            product=self.ledger.get('products',current['product_id'])
            if (product.get('call') or {}).get('action')=='discover':
                return
            self.advance(current)
            if current['status']=='awaiting_release' and not self.ledger.get('runs',current['id']).get('call'):
                investigate(self)
            return
        from .progress import recover_review, investigate
        if recover_review(self):
            return
        for run in runs:
            if run['status']=='queued':
                product=self.ledger.get('products',run['product_id'])
                if product['status']=='active' and (product.get('call') or {}).get('action')!='discover':
                    self.advance(run)
                    return
        investigate(self)

    def monitor(self,product,allow_discovery=True):
        policy=DEFAULTS | product.get('policy',{})
        if product.get('call'):
            result=self.call_result(product)
            if result is None:
                return
            action=product['call']['action']
            signal_ids=product['call'].get('signal_ids',[])
            product=self.consume('products',product,result)
            changes={'last_'+action:time.time(),'last_'+action+'_result':redact(result)}
            if action=='inspect' and result.get('inspection_workspace'):
                changes['inspection_workspace']=result['inspection_workspace']
            if action=='master_sync':
                changes['master_idle_since']=result.get('master_idle_since')
                if result.get('instance'):
                    changes['master_environment']=result['instance']
                    if (product.get('master_environment') or {}).get('commit')!=result['instance'].get('commit'):
                        changes.update(last_inspect=0,last_final_acceptance=0)
            if action=='final_acceptance':
                from .master import consume
                consume(self.ledger,product,result)
            if action in ('probe','recover-runtime'):
                changes['monitor_reason']=result.get('reason','')
            if action=='recover-runtime':
                changes.update(last_probe=time.time(),last_probe_result=redact(result))
                changes['runtime_recovery_state']=result.get('runtime_recovery_state') or {'phase':'deferred','next_check_at':time.time()+5}
            if action in ('probe','recover-runtime') and result['status']=='pass':
                changes['health']=result.get('health',{})
            product=self.change('products',product,changes)
            for signal_data in result.get('signals',[]):
                self.ledger.signal(product['id'],signal_data)
            if action=='discover' and result['status']=='pass':
                self.accept_discovery(product,result)
                for ident in signal_ids:
                    source=self.ledger.get('signals',ident)
                    if source['status']=='pending':
                        self.change('signals',source,{},'reviewed')
            return
        if not product.get('adapter'):
            return
        from .runtime_recovery import enabled, busy
        recovering=enabled(product)
        if recovering and time.time()>=(product.get('runtime_recovery_state',{}).get('next_check_at') or 0):
            if not busy(self.ledger,product['id']):
                self.start_call('products',product,product,'recover-runtime',product['adapter'],timeout=30)
        elif not recovering and time.time()-product.get('last_probe',0)>=policy['probe_seconds']:
            self.start_call('products',product,product,'probe',product['adapter'],timeout=30)
        elif self.master_step(product):
            return
        elif time.time()-product.get('last_inspect',0)>=policy['inspection_seconds']:
            self.start_call('products',product,product,'inspect',product['adapter'],timeout=600)
        elif allow_discovery and product['status']=='active' and product.get('executor') and not product.get('nightly_attribution'):
            signals=[s for s in self.ledger.list('signals') if s['product_id']==product['id'] and s['status']=='pending']
            executing=any(r['product_id']==product['id'] and r['status'] not in TERMINAL | {'queued','blocked'} for r in self.ledger.list('runs'))
            retry_due=time.time()-product.get('last_discover',0)>=policy['inspection_seconds']
            if signals and retry_due and not executing and self.ledger.budget(product['id'],'discovery',policy['discovery_per_day']):
                self.start_call('products',product,product,'discover',product['executor'],{'signals':signals[:20]},timeout=300)

    def master_step(self,product):
        from .master import enabled, pending
        from .runtime_recovery import busy
        if not enabled(product) or busy(self.ledger,product['id']):
            return False
        now=time.time()
        if product['status']=='active' and now-product.get('last_master_sync',0)>=60:
            self.start_call('products',product,product,'master_sync',product['adapter'],timeout=600)
            return True
        requirements=pending(self.ledger,product)
        if (requirements and (product.get('last_master_sync_result') or {}).get('status')=='pass'
                and now-product.get('last_final_acceptance',0)>=60):
            self.start_call('products',product,product,'final_acceptance',product['adapter'],
                {'requirements':requirements},timeout=600)
            return True
        return False

    def tokens_available(self,product):
        from .quota import snapshot
        return not snapshot(self.ledger, product)['tokens_exhausted']

    def reconcile_evaluations(self):
        for evaluation in self.ledger.list('evaluations'):
            if evaluation['status']=='pass':
                continue
            runs=[self.ledger.get('runs',ident) for ident in evaluation.get('run_ids',[])]
            passed=sum(r['status'] in ('accepted','delivered','online','completed') for r in runs)
            blocked=sum(r['status']=='blocked' for r in runs)
            status='pass' if passed==evaluation['target_count'] else 'blocked' if runs and all(r['status'] in TERMINAL | {'blocked'} for r in runs) else 'running'
            summary={'passed':passed,'blocked':blocked,'target':evaluation['target_count'],
                     'judgement':f'{passed}/{evaluation["target_count"]} 项已通过；{blocked} 项阻塞。',
                     'results':[{'run_id':r['id'],'title':r['title'],'status':r['status'],'reason':r.get('reason','')} for r in runs]}
            if status!=evaluation['status'] or any(evaluation.get(k)!=v for k,v in summary.items()):
                self.change('evaluations',evaluation,summary,status)

    def accept_discovery(self,product,result,signal_ids=None,day=None,report_id=None,db=None):
        """同批信号可产生多个需求；整批原子入池，重放不重复创建。"""
        if db is None:
            with self.store.transaction() as connection:
                items=self.accept_discovery(product,result,signal_ids,day,report_id,connection)
            for item in items:
                if item.get('classification')=='development' and item.get('in_scope') is True and item['status']=='pending':
                    try:
                        self.control.queue(item)
                    except Conflict:
                        pass
            return items
        known={r['id']:self.ledger.decode(r) for r in db.execute('SELECT * FROM auto_signals WHERE product_id=?',(product['id'],))}
        eligible=set(signal_ids) if signal_ids is not None else {ident for ident,r in known.items() if r['status']=='pending'}
        titles={r['title'].strip().casefold():r for r in (self.ledger.decode(row) for row in db.execute('SELECT * FROM auto_requirements WHERE product_id=?',(product['id'],)))}
        items=[]
        for raw in result.get('requirements',[]):
            candidate=dict(raw)
            ids=candidate.get('signal_ids',[])
            if not ids or any(i not in known or i not in eligible for i in ids) or not all(candidate.get(k) for k in ('title','evidence','acceptance','impact','reproduction')):
                continue
            title=candidate['title'].strip().casefold()
            if title in titles:
                if report_id and titles[title].get('daily_report_id')==report_id:
                    items.append(titles[title])
                continue
            if product.get('agents',{}).get('discovery',{}).get('provider')=='codex' and candidate.get('classification')=='development':
                from .probes import validate
                try:
                    validate(candidate.get('resolution_probes'))
                    if not candidate.get('resolution_probes'):
                        raise ValueError('缺少原问题效果验证条件')
                except ValueError as exc:
                    candidate.update(classification='investigation',reason='需补充验收证据后转开发：'+str(exc))
            extra={'requirement_day':day,'daily_report_id':report_id} if report_id else {}
            item=self.ledger.create('requirements',redact(candidate) | {'product_id':product['id']} | extra,'pending',db=db)
            for ident in set(ids):
                source=self.ledger.get('signals',ident,db)
                links=list(dict.fromkeys(source.get('requirement_ids',[]) + ([source['requirement_id']] if source.get('requirement_id') else []) + [item['id']]))
                self.ledger.update('signals',ident,source['version'],{'requirement_id':links[0],'requirement_ids':links},'classified',db)
            titles[title]=item
            items.append(item)
        return items

    def advance(self,run):
        product=self.ledger.get('products',run['product_id'])
        policy=DEFAULTS | product.get('policy',{})
        if run['status'] in ('cancelling','pausing'):
            return self.stop_run(run)
        if product['status']!='active' and run['status'] not in ('deploying','observing'):
            return
        if run.get('call'):
            result=self.call_result(run)
            if result is None:
                return
            action=run['call']['action']
            run=self.consume('runs',run,result)
            if action in ('plan','develop'):
                run=self.change('runs',run,{'execution_seconds':run.get('execution_seconds',0)+result.get('elapsed',0)})
            try:
                return self.complete_action(run,product,action,result)
            except Exception as exc:
                # Receipt already exists: surface the controller error instead of silently invoking the model again.
                return self.block(run,'控制器处理 '+action+' 回执失败：'+str(exc))
        requirement=self.ledger.get('requirements',run['requirement_id'])
        if run['status']=='queued':
            from .delivery import configured, ready
            if not ready(product):
                if run.get('reason') != '等待首次源码基线 PR 合入':
                    self.change('runs', run, {'reason': '等待首次源码基线 PR 合入'})
                return
            if not self.tokens_available(product):
                if run.get('reason')!='每日 Token 额度已用尽，等待次日或调整额度':
                    self.change('runs',run,{'reason':'每日 Token 额度已用尽，等待次日或调整额度'})
                return
            evaluation=self.ledger.get('evaluations',run['evaluation_id']) if run.get('evaluation_id') else None
            budget_kind='evaluation:'+evaluation['id'] if evaluation else 'development'
            budget_limit=evaluation['target_count'] if evaluation else policy['runs_per_day']
            if not run.get('budget_reserved'):
                with self.store.transaction() as db:
                    reserved=self.ledger.budget(product['id'],budget_kind,budget_limit,db)
                    if reserved:
                        run=self.ledger.update('runs',run['id'],run['version'],{'budget_reserved':True},db=db)
                if not reserved:
                    if run.get('reason')!='每日需求上限已达到，排队等待次日或调整上限':
                        self.change('runs',run,{'reason':'每日需求上限已达到，排队等待次日或调整上限'})
                    return
            try:
                if configured(product):
                    from .delivery_worker import network_git
                    from .workspace import git
                    repository = product['delivery_repository']
                    branch = product['git'].get('base_branch', 'master')
                    network_git(repository, 'fetch', 'origin', 'refs/heads/'+branch+':refs/remotes/origin/'+branch)
                    destination = self.root/'workspaces'/run['id']
                    base = git(repository, 'rev-parse', 'refs/remotes/origin/'+branch)
                    if not destination.exists():
                        git(repository, 'worktree', 'add', '-b', 'codex/auto-'+run['id'], str(destination), base)
                    workspace = {'workspace': str(destination), 'base_commit': base}
                else:
                    workspace=checkout(product['repository'],self.root/'workspaces'/run['id'],run['id'])
                worker_home=self.root/'workers'/run['id']
                worker_home.mkdir(parents=True,mode=0o700)
                worker=self.ledger.create('workers',{'product_id':product['id'],'home':str(worker_home),
                    'run_id':run['id'],'workspace':workspace['workspace']},'registered')
                run=self.change('runs',run,workspace | {'worker_id':worker['id'],'worker_home':str(worker_home),'reason':''},'planning')
            except Exception as exc:
                return self.block(run,str(exc))
        if run['status'] in ('planning','developing'):
            remaining=policy['execution_seconds']-run.get('execution_seconds',0)
            if remaining<=0:
                return self.block(run,'累计开发执行时间已用尽')
            action='plan' if run['status']=='planning' else 'develop'
            return self.start_call('runs',run,product,action,product['executor'],{'requirement':requirement},timeout=remaining)
        if run['status'] in ('plan_review','acceptance_review'):
            return self.review(run,product,requirement)
        if run['status']=='verifying':
            return self.start_call('runs',run,product,'verify',product['adapter'],{'requirement':requirement},timeout=3600)
        if run['status']=='awaiting_release':
            if time.time()-run.get('last_idle_check',0)<policy['probe_seconds']:
                return
            return self.start_call('runs',run,product,'idle',product['adapter'],timeout=30)
        if run['status']=='deploying':
            return self.start_call('runs',run,product,'publish',product['adapter'],timeout=600)
        if run['status']=='observing':
            if time.time()-run.get('last_observe',0)<policy['probe_seconds']:
                return
            return self.start_call('runs',run,product,'observe',product['adapter'],{'requirement':requirement},timeout=60)

    def complete_action(self,run,product,action,result):
        policy=DEFAULTS | product.get('policy',{})
        if action=='observe' and result['status']=='busy':
            failures=run.get('observation_retries',0)+1
            if failures<3:
                return self.change('runs',run,{'observation_retries':failures,'healthy_since':None,'last_observe':time.time(),'reason':result.get('reason','上线观察暂时不可用')+f'；第 {failures}/3 次，等待复测'})
            result=result | {'status':'fail','reason':'连续三次无法完成上线观察：'+result.get('reason','未知故障')}
        if result['status']!='pass':
            if action in ('idle','publish') and result['status']=='busy':
                if action=='publish':
                    release=self.ledger.get('releases',run['release_id'])
                    self.change('releases',release,{'reason':'切换前活动状态变化'},'cancelled')
                    run=self.change('runs',run,{'release_id':None},'awaiting_release')
                return self.change('runs',run,{'idle_since':None,'last_idle_check':time.time(),'reason':result.get('reason','应用繁忙')})
            if action in ('publish','observe') and result['status']=='fail':
                release=self.ledger.get('releases',run['release_id'])
                self.change('releases',release,{'reason':result.get('reason','发布健康检查失败')},'rollback_pending')
                return self.block(run,result.get('reason','发布失败，等待回滚'))
            if action in ('develop','verify') and result['status']=='fail':
                return self.revise(run,product,result.get('reason','验证失败'),result.get('diagnostic'))
            return self.block(run,result.get('reason','阶段被阻塞'),result.get('uncertain',False))
        if action=='plan':
            if not result.get('plan'):
                return self.block(run,'执行器未提交开发方案')
            return self.change('runs',run,{'plan':redact(result['plan'])},'plan_review')
        if action=='develop':
            commit_id=commit(run['workspace'])
            return self.change('runs',run,{'commit':commit_id,'source_digest':digest(run['workspace']),'summary':redact(result.get('summary',''))},'verifying')
        if action=='verify':
            checks=result.get('checks',[])
            if not checks or any(c.get('status')!='pass' for c in checks if c.get('required',True)) or not result.get('manifest'):
                return self.block(run,'必需验收未全部通过或缺少产物清单')
            if digest(run['workspace'])!=run['source_digest']:
                return self.block(run,'验证期间源码发生变化，需要重新开发及验收')
            return self.change('runs',run,{'checks':checks,'manifest':result['manifest']},'acceptance_review')
        if action=='idle':
            now=time.time()
            continuous=now-run.get('last_idle_check',0)<=2*policy['probe_seconds']
            idle_since=(run.get('idle_since') if continuous else None) or now
            if now-idle_since < policy['idle_seconds']:
                return self.change('runs',run,{'idle_since':idle_since,'last_idle_check':now,'reason':'等待连续空闲'})
            if digest(run['workspace'])!=run['source_digest']:
                return self.block(run,'验收后源码已变化，禁止发布')
            release=self.ledger.create('releases',{'product_id':product['id'],'run_id':run['id'],'commit':run['commit'],
                'manifest':run['manifest'],'receipts':[]},'deploying')
            return self.change('runs',run,{'release_id':release['id'],'reason':''},'deploying')
        if action=='publish':
            release=self.ledger.get('releases',run['release_id'])
            self.change('releases',release,{'published_at':time.time(),'deployment':result},'observing')
            return self.change('runs',run,{'published_at':time.time(),'last_observe':0},'observing')
        if action=='observe':
            now=time.time()
            continuous=now-run.get('last_observe',0)<=2*policy['probe_seconds']
            healthy_since=(run.get('healthy_since') if continuous else None) or now
            run=self.change('runs',run,{'observation_retries':0,'last_observe':now,'healthy_since':healthy_since,'reason':result.get('reason','')})
            if result.get('problem_resolved') is True and now-healthy_since>=policy['observation_seconds']:
                promote(product['repository'],run['commit'],run['base_commit'])
                release=self.ledger.get('releases',run['release_id'])
                self.change('releases',release,{'verified_at':time.time()},'completed')
                self.change('runs',run,{'reason':''},'completed')
                req=self.ledger.get('requirements',run['requirement_id'])
                self.change('requirements',req,{},'completed')

    def revise(self,run,product,reason,diagnostic=None):
        if run.get('revisions',0)>=(DEFAULTS | product.get('policy',{}))['max_revisions']:
            return self.block(run,'返修次数已用尽：'+reason)
        # Human copy must not remove compiler/tool evidence from the next repair prompt.
        feedback=reason+('\n技术诊断：'+json.dumps(diagnostic,ensure_ascii=False) if diagnostic else '')
        return self.change('runs',run,{'revisions':run.get('revisions',0)+1,'feedback':redact(feedback),'review_id':None},
                           'planning' if run['status']=='plan_review' else 'developing')

    def review(self,run,product,requirement):
        home=Path(run['worker_home'])
        scoped=WorkerStore(self.store.state,home,product.get('agents',{}).get('acceptance'))
        engine=self.engines.setdefault(run['id'],Engine(scoped))
        if run.get('review_id'):
            try:
                review=scoped.get(run['review_id'])
            except KeyError:
                engine.submit(run['review_packet'],'handoff')
                return
            if review['status'] in ACTIVE:
                return
            result=review.get('result') or {}
            evidence_id='review-'+review['id']
            try:
                self.ledger.get('evidence',evidence_id)
            except KeyError:
                self.ledger.create('evidence',{'product_id':product['id'],'run_id':run['id'],
                    'review_id':review['id'],'title':'方案审查' if run['status']=='plan_review' else '验收审查',
                    'actual':result.get('summary',''),'judgement':result.get('instruction',''),
                    'details':redact(result),'provider':product.get('agents',{}).get('acceptance',{}).get('provider','inherited')},
                    'pass' if result.get('decision') in ('approve','done') else 'blocked',ident=evidence_id)
            expected='approve' if run['status']=='plan_review' else 'done'
            if result.get('decision')==expected and result.get('pause_proof',{}).get('pause_verified'):
                from .delivery import configured
                if expected == 'done' and configured(product):
                    self.change('runs', run, {'acceptance_review_id': review['id'], 'accepted_at': time.time(), 'review_id': None, 'reason': ''}, 'accepted')
                    self.change('requirements', requirement, {}, 'accepted')
                    return
                if expected=='done' and run.get('evaluation_id'):
                    evaluation=self.ledger.get('evaluations',run['evaluation_id'])
                    if evaluation.get('target_phase')=='acceptance':
                        self.change('runs',run,{'acceptance_review_id':review['id'],'accepted_at':time.time(),'review_id':None,'reason':''},'accepted')
                        self.change('requirements',requirement,{},'accepted')
                        return
                return self.change('runs',run,{'review_id':None} | ({'accepted_at':time.time(),'acceptance_review_id':review['id']} if expected=='done' else {}),'developing' if expected=='approve' else 'awaiting_release')
            if result.get('decision')=='revise':
                return self.revise(run,product,result.get('instruction','审查要求修改'))
            return self.block(self.change('runs',run,{'review_id':None}),result.get('summary','审查未通过'))
        if not self.tokens_available(product):
            if run.get('reason')!='每日 Token 额度已用尽，等待次日或调整额度':
                self.change('runs',run,{'reason':'每日 Token 额度已用尽，等待次日或调整额度'})
            return
        quota=self.store.state/'quota.json'
        if quota.exists():
            import datetime
            from zoneinfo import ZoneInfo
            value=json.loads(quota.read_text())
            day=datetime.datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
            if value.get('day')==day and value.get('count',0)>=scoped.settings()['max_per_day']:
                if run.get('reason')!='平台每日审查额度已用尽，保留进度等待次日':
                    self.change('runs',run,{'reason':'平台每日审查额度已用尽，保留进度等待次日'})
                return
        with self.store.connect() as db:
            if db.execute("SELECT 1 FROM reviews WHERE status IN ('queued','running','awaiting_human') LIMIT 1").fetchone():
                if run.get('reason')!='等待当前审查完成后自动提交':
                    self.change('runs',run,{'reason':'等待当前审查完成后自动提交'})
                return
        # This is the controller's real process-completion ledger, not a fabricated model transcript.
        log=home/'sessions'/'autopilot'/run['id']/'session.jsonl'
        log.parent.mkdir(parents=True,exist_ok=True)
        if not log.exists():
            atomic_rows=[{'type':'session/header','id':run['id'],'cwd':run['workspace'],'seq':0,
                          'source':'autopilot-process-controller'}]
            log.write_text('\n'.join(json.dumps(x) for x in atomic_rows)+'\n')
        with log.open('a') as stream:
            stream.write(json.dumps({'type':'autopilot/process-completed','seq':int(time.time()*1000),'data':{'receipts':run.get('receipts',[])}})+'\n')
        rid=str(uuid.uuid4())
        packet={'request_id':rid,'session_id':run['id'],'cwd':run['workspace'],'scope':['.'],
                'phase':'plan' if run['status']=='plan_review' else 'acceptance',
                'summary':json.dumps(redact({'requirement':requirement,'plan':run.get('plan'),'checks':run.get('checks'),
                       'summary':run.get('summary'),'execution':'独立执行进程已退出；控制器冻结工作区等待审查'}),ensure_ascii=False)[:20000]}
        self.change('runs',run,{'review_id':rid,'review_packet':packet,'reason':''})
        from .usage import register
        register(self.ledger,product['id'],self.store.state/'reviews'/rid/'trace.jsonl',record_id=run['id'],action=run['status'])
        engine.submit(packet,'handoff')

    def stop_run(self,run):
        if run.get('review_id'):
            try:
                self.store.cancel(run['review_id'],'自主任务停止')
            except Conflict:
                pass
        if run.get('call'):
            path=Path(run['call']['path'])
            process_path=path.parent/'process.json'
            if process_path.exists():
                process=json.loads(process_path.read_text())
                if self.process_alive(process,str(path)):
                    os.killpg(process['pid'],signal.SIGTERM)
                    return
            if run['call']['action'] in ('publish','rollback'):
                return self.block(run,'发布操作被中断，请先核对发布日志',True)
        status='cancelled' if run.get('control')=='cancel' else 'blocked'
        self.change('runs',run,{'call':None,'reason':'用户已停止执行','uncertain':False,
                    'resume_status':run.get('resume_status','planning')},status)

    def rollback(self,release):
        product=self.ledger.get('products',release['product_id'])
        run=self.ledger.get('runs',release['run_id'])
        if not release.get('call'):
            return self.start_call('releases',release,product,'rollback',product['adapter'],{'run':run},timeout=600)
        result=self.call_result(release)
        if result is None:
            return
        release=self.consume('releases',release,result)
        if result['status']=='pass':
            from .workspace import git
            current=git(product['repository'],'rev-parse','refs/heads/codex/autonomous')
            if current==run.get('commit'):
                promote(product['repository'],run['base_commit'],run['commit'])
            self.change('releases',release,{},'rolled_back')
            self.change('runs',run,{'reason':'已恢复上一稳定版本'},'rolled_back')
        else:
            self.change('releases',release,{'reason':result.get('reason','回滚失败')},'blocked')


def serve(state):
    store=Store(state)
    lock=(store.state/'autopilot.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
    store.recover()
    scheduler=Scheduler(store)
    while True:
        try:
            scheduler.tick()
        except Exception as exc:
            store.event('autopilot_error',detail={'reason':str(exc)})
        time.sleep(1)
