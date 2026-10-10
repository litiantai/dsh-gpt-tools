"""控制中心 API；沿用外层 Cookie、CSRF、操作 UUID。"""
from __future__ import annotations

from pathlib import Path
import base64
import hashlib
import time
import uuid

from review_core import Conflict
from .store import DEFAULTS, KINDS, TERMINAL, Ledger, redact
from .scan_identity import repository_key


class Control:
    def __init__(self, store):
        self.ledger = Ledger(store)
        from .delivery_board import DeliveryBoard
        self.delivery_board = DeliveryBoard(self.ledger)

    @staticmethod
    def handles(path):
        return path.strip('/').split('/')[0] in (*KINDS,'autopilot')

    def get(self,path):
        if len(path.strip('/').split('/')) >= 3 and path.strip('/').split('/')[0] == 'products' and path.strip('/').split('/')[2] == 'test-chains':
            from .test_chains import api
            return api(self, path)
        if '/intelligence' in path:
            from .intelligence import get
            return get(self, path)
        parts = path.strip('/').split('/')
        if len(parts) >= 3 and parts[0] == 'products' and parts[2] == 'coordinator':
            from .coordinator import api
            return api(self, parts)
        if len(parts)==3 and parts[0]=='scans' and parts[2]=='screenshot':
            scan=self.ledger.get('scans',parts[1])
            check=next((c for c in scan.get('result',{}).get('checks',[]) if c.get('screenshot')),None)
            if not check:
                raise KeyError('扫描尚无截图')
            path=Path(check['screenshot']).resolve()
            root=(self.ledger.store.state/'autopilot/scans'/scan['id']).resolve()
            if not path.is_relative_to(root) or path.suffix!='.png' or path.stat().st_size>12*1024*1024:
                raise ValueError('截图路径或大小无效')
            content=path.read_bytes()
            if hashlib.sha256(content).hexdigest()!=check.get('screenshot_sha256'):
                raise ValueError('扫描截图发生变化')
            return {'data_url':'data:image/png;base64,'+base64.b64encode(content).decode()}
        if len(parts)==3 and parts[2]=='context':
            from .record_context import record_context
            return record_context(self.ledger, parts[0], parts[1])
        if len(parts)==3 and parts[0]=='deliveries' and parts[2]=='tasks':
            from .delivery_tasks import delivery_tasks
            return delivery_tasks(self.ledger, self.ledger.get('deliveries', parts[1]))
        if len(parts)==3 and parts[0]=='runs' and parts[2]=='usage':
            from .delivery_tasks import task_usage
            return task_usage(self.ledger, self.ledger.get('runs', parts[1]))
        if len(parts)==3 and parts[0]=='products' and parts[2]=='sync-master':
            from .branch_sync import snapshot
            return snapshot(self.ledger, self.ledger.get('products', parts[1]))
        if len(parts)==3 and parts[0]=='products' and parts[2]=='delivery-board':
            return self.delivery_board.get(self.ledger.get('products', parts[1]))
        if parts == ['autopilot','metrics']:
            return self.ledger.metrics()
        if parts == ['scans']:
            return [scan | {'repository_key': repository_key(scan['source'])}
                    for scan in self.ledger.list('scans')]
        if len(parts)==1:
            return self.ledger.list(parts[0])
        if len(parts)==2:
            return self.ledger.get(*parts)
        if len(parts)==3 and parts[0]=='products' and parts[2]=='git-token':
            from .github import credential_file
            self.ledger.get('products',parts[1])
            return {'configured': credential_file().is_file()}
        if len(parts)==3 and parts[0]=='products' and parts[2]=='notifications':
            from .notifications import get as notifications_get
            self.ledger.get('products',parts[1])
            return notifications_get(self.ledger,parts[1])
        if len(parts)==3 and parts[0]=='products' and parts[2]=='git-status':
            from .github import connection_status
            product=self.ledger.get('products',parts[1])
            return connection_status(product.get('git',{}).get('url'))
        if len(parts)==3 and parts[0]=='products' and parts[2]=='automation':
            product=self.ledger.get('products',parts[1])
            policy=DEFAULTS | product.get('policy',{})
            from .quota import snapshot
            from .off_peak import window
            return {'evaluation_id':product.get('evaluation_id'),
                    'deepseek_schedule': window() | {'enabled': policy['deepseek_off_peak_only']},
                    'next_inspection':product.get('last_inspect',0)+policy['inspection_seconds'],
                    'tasks_used':self.ledger.budget_used(product['id'],'development'),
                    'code_delivery_tokens_used':self.ledger.budget_used(product['id'],'code_delivery_tokens'),
                    'tasks_limit':policy['runs_per_day'], **snapshot(self.ledger, product),
                    'queued':sum(r['product_id']==product['id'] and r['status']=='queued' for r in self.ledger.list('runs'))}
        if len(parts)==3 and parts[0]=='evidence' and parts[2]=='screenshot':
            evidence=self.ledger.get('evidence',parts[1])
            path=Path(evidence['screenshot']).resolve()
            root=(self.ledger.store.state/'autopilot/evidence').resolve()
            if not path.is_relative_to(root) or path.suffix.lower() not in ('.png','.jpg','.jpeg','.webp'):
                raise ValueError('截图路径无效')
            if path.stat().st_size>12*1024*1024:
                raise ValueError('截图超过预览上限')
            content=path.read_bytes()
            if hashlib.sha256(content).hexdigest()!=evidence['sha256']:
                raise ValueError('截图校验失败')
            mime='image/jpeg' if path.suffix.lower() in ('.jpg','.jpeg') else 'image/'+path.suffix[1:]
            return {'data_url':f'data:{mime};base64,'+base64.b64encode(content).decode()}
        raise KeyError('接口不存在')

    def enqueue_scan(self, data, previous_id=None, version=None):
        """在同一事务中核对历史版本和活动扫描，防止并发重复入队。"""
        with self.ledger.store.transaction() as db:
            if previous_id:
                old = self.ledger.get('scans', previous_id, db)
                if old['version'] != version:
                    raise Conflict('记录已更新，请刷新后重试')
                data = {k: old[k] for k in ('source', 'product_id', 'title')}
                data['previous_scan_id'] = previous_id
            key = repository_key(data['source'])
            for row in db.execute("SELECT * FROM auto_scans WHERE status IN ('queued', 'running') OR json_extract(data, '$.call') IS NOT NULL"):
                active = self.ledger.decode(row)
                if repository_key(active['source']) == key:
                    raise Conflict('该仓库已有扫描正在执行或等待扫描，请查看已有记录，完成后再重试')
            return self.ledger.create('scans', data | {'receipts': []}, 'queued', db=db)

    def mutate(self,path,body):
        if len(path.strip('/').split('/')) >= 3 and path.strip('/').split('/')[0] == 'products' and path.strip('/').split('/')[2] == 'test-chains':
            from .test_chains import api
            return api(self, path, body)
        if '/intelligence' in path:
            from .intelligence import mutate
            return mutate(self, path, body)
        parts = path.strip('/').split('/')
        if len(parts) >= 3 and parts[0] == 'products' and parts[2] == 'coordinator':
            from .coordinator import api
            return api(self, parts, body)
        kind = parts[0]
        if len(parts) == 3 and kind == 'requirements' and parts[2] in ('edit','confirm','reject','amend'):
            from .intake import mutate
            return mutate(self, parts[1], parts[2], body)
        if parts == ['scans']:
            source = body.get('source', '')
            if isinstance(source, str):
                source = source.strip()
            if not isinstance(source, str) or not (Path(source).is_absolute() or source.startswith('https://')):
                raise ValueError('请提供本地仓库绝对路径或 HTTPS Git URL')
            if source.startswith('https://'):
                from urllib.parse import urlsplit
                address = urlsplit(source)
                if not address.hostname or address.username or address.password or address.query or address.fragment:
                    raise ValueError('仓库 URL 不允许内嵌凭据或查询参数')
            product_id = body.get('product_id', '')
            if product_id:
                self.ledger.get('products', product_id)
            return self.enqueue_scan({'source': source, 'product_id': product_id, 'title': '仓库接入扫描'})
        if parts == ['products']:
            config = body.get('config',{})
            self.validate_product(config)
            if config.get('git'):
                from .github import GitHub
                GitHub(config['git']['url']).access(write=config['git'].get('enabled', False))
            return self.ledger.create(kind,config | {'policy':DEFAULTS | config.get('policy',{})},'paused')
        if parts == ['signals']:
            return self.ledger.signal(body['product_id'],body['signal'])
        if parts == ['requirements']:
            self.ledger.get('products',body['product_id'])
            value = redact({k:body[k] for k in ('product_id','title','evidence','acceptance','impact')})
            if not all(value.values()):
                raise ValueError('需求证据与验收条件不能为空')
            return self.ledger.create(kind,value | {'priority':body.get('priority',2)},'pending')
        if len(parts)==4 and parts[0]=='products' and parts[2]=='notifications':
            self.ledger.get('products',parts[1])
            if parts[3]=='configure':
                from .notifications import configure
                return configure(self.ledger,parts[1],body)
            if parts[3]=='recipients':
                from .notifications import add_recipients
                return add_recipients(self.ledger,parts[1],body)
            if parts[3]=='send-test':
                from .notifications import send_test
                return send_test(self.ledger,parts[1])
            raise KeyError('接口不存在')
        if len(parts)!=3:
            raise KeyError('接口不存在')
        _,ident,action=parts
        if kind=='products' and action=='sync-master':
            from .branch_sync import start
            return start(self.ledger, self.ledger.get('products', ident))
        if kind=='products' and action=='today-token-limit':
            from .quota import adjust_today
            return adjust_today(self.ledger, ident, body)
        if kind=='products' and action=='git-token':
            from .github import save_credential
            product=self.ledger.get('products',ident)
            return save_credential(product.get('git',{}).get('url'), body.get('token'))
        if kind=='products' and action=='nightly-attribution':
            with self.ledger.store.transaction() as db:
                product=self.ledger.get(kind,ident,db)
                import datetime
                from zoneinfo import ZoneInfo
                midnight=datetime.datetime.now(ZoneInfo('Asia/Shanghai')).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
                return self.ledger.update(kind,ident,product['version'],{'nightly_attribution':True,
                    'daily_report_enabled':True,'daily_report_enabled_at':product.get('daily_report_enabled_at',midnight),
                    'policy':product.get('policy',{}) | {'inspection_seconds':3600}},db=db)
        if kind=='products' and action=='daily-report':
            if type(body.get('enabled')) is not bool:
                raise ValueError('必须指定是否启用日报')
            import datetime
            from zoneinfo import ZoneInfo
            with self.ledger.store.transaction() as db:
                product=self.ledger.get(kind,ident,db)
                midnight=datetime.datetime.now(ZoneInfo('Asia/Shanghai')).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
                return self.ledger.update(kind,ident,product['version'],{'daily_report_enabled':body['enabled'],
                    'daily_report_enabled_at':product.get('daily_report_enabled_at',midnight)},db=db)
        if kind=='products' and action=='continuous':
            with self.ledger.store.transaction() as db:
                product=self.ledger.get(kind,ident,db)
                if body.get('version')!=product['version']:
                    raise Conflict('记录已更新，请刷新后重试')
                if not all(product.get(key) for key in ('repository','adapter','executor')):
                    raise ValueError('尚未配置源码基线、巡检适配器和开发执行器')
                if product.get('evaluation_id'):
                    evaluation=self.ledger.get('evaluations',product['evaluation_id'],db)
                    if evaluation['status']!='pass':
                        raise Conflict('当前评测尚未完成，请先处理评测任务')
                return self.ledger.update(kind,ident,product['version'],{'evaluation_id':None},'active',db)
        if kind=='products' and action=='configure':
            return self.configure_product(ident,body)
        if kind=='signals' and action=='promote':
            return self.promote_signal(ident,body)
        old=self.ledger.get(kind,ident)
        if body.get('version') != old['version']:
            raise Conflict('记录已更新，请刷新后重试')
        if kind == 'scans' and action == 'retry':
            return self.enqueue_scan({}, previous_id=ident, version=body['version'])
        if kind == 'scans' and action == 'onboard':
            if old['status'] != 'pass' or old.get('product_id'):
                raise Conflict('仅可登记尚未关联项目的通过扫描')
            from .onboarding import onboard
            return onboard(self.ledger, old, body)
        if kind == 'scans' and action == 'apply':
            if old['status'] != 'pass' or not (old.get('product_id') or body.get('product_id')):
                raise Conflict('仅可将通过的扫描应用于已关联项目')
            if old.get('product_id') and body.get('product_id') and old['product_id'] != body['product_id']:
                raise Conflict('扫描记录已关联其他项目，不能应用到当前项目')
            product = self.ledger.get('products', old.get('product_id') or body['product_id'])
            from .project import generic
            if not generic(product):
                raise Conflict('旧适配器项目不可应用通用扫描；请登记独立项目')
            source = product.get('repository_source', product['source'])
            same = (Path(old['source']).resolve() == Path(source).resolve()) if Path(old['source']).is_absolute() and Path(source).is_absolute() else old['source'] == source
            if not same:
                raise Conflict('扫描仓库与当前项目源码不一致')
            result = self.configure_product(product['id'], {'version': body.get('product_version'), 'config': {
                'project_config': old['result']['configuration'], 'config_scan_id': ident}})
            return result
        if kind == 'products' and action == 'migrate-git':
            from .delivery import start_migration
            return start_migration(self.ledger, old)
        if kind == 'deliveries' and action == 'close' and old['status'] not in ('online','cancelled'):
            return self.ledger.update(kind, ident, old['version'], {'frozen': True, 'cutoff': time.time(), 'manual_close_at': time.time()})
        if kind == 'deliveries' and action == 'manual-merge':
            from .delivery import request_manual_merge
            return request_manual_merge(self.ledger, ident, body['version'])
        if kind == 'deliveries' and action == 'migrate-review-flow':
            from .delivery_migration import migrate
            return migrate(self.ledger, old)
        if kind == 'deliveries' and action == 'retry' and old['status'] in ('blocked', 'release_failed') and not old.get('call'):
            if old.get('pending_result') or old.get('uncertain'):
                raise Conflict('当前执行结果待核对，不能重复重试')
            resume = 'syncing_release' if old['status'] == 'release_failed' else old.get('resume_status', 'preparing')
            if old.get('flow') == 'review_before_release' and resume == 'reviewing_release':
                resume = 'syncing_release'
            return self.ledger.update(kind, ident, old['version'], {'reason': '', 'next_attempt': 0, 'next_auto_retry_at': None, 'auto_retry_wait_reason': None}, resume)
        if kind=='products':
            if action=='recover-runtime':
                from .runtime_recovery import enabled, busy
                with self.ledger.store.transaction() as db:
                    latest=self.ledger.get(kind,ident,db)
                    if latest['version']!=body.get('version'):
                        raise Conflict('记录已更新，请刷新后重试')
                    if not enabled(latest):
                        raise Conflict('请先启用应用自动恢复，并将项目设为自主运行或仅监测')
                    if latest.get('call') or (latest.get('runtime_recovery_state') or {}).get('phase') in ('requested','starting') or busy(self.ledger,ident):
                        raise Conflict('应用已有操作进行中，请等待当前操作结束')
                    return self.ledger.update(kind,ident,latest['version'],{'runtime_recovery_state':{'phase':'requested','attempts':0,'next_check_at':0,'message':'已安排恢复应用'}},db=db)
            if action in ('enable','pause','observe'):
                if action=='enable':
                    if old.get('automation_disabled'):
                        raise Conflict('项目已停用；需先显式解除停用状态')
                    from .project import generic
                    if generic(old):
                        if not old.get('agents') or not old.get('config_scan_id'):
                            raise Conflict('请先完成接入扫描并配置模型分工')
                        if any(r.get('provider')=='harness' for r in old['agents'].values()) and not (Path(old.get('worker_runtime','/nonexistent'))/'node_modules/@deepseek-ai/dsh/package.json').is_file():
                            raise Conflict('独立 Harness 运行时尚未安装')
                    for key in ('repository','adapter','executor'):
                        if not old.get(key):
                            raise ValueError(f'尚未配置 {key}')
                return self.ledger.update(kind,ident,old['version'],{}, {'enable':'active','pause':'paused','observe':'observing'}[action])
        if kind=='requirements' and action=='queue':
            return self.queue(old)
        if kind=='runs':
            if action=='repair-timing':
                import json
                product=self.ledger.get('products',old['product_id'])
                if old['status']!='blocked' or old.get('reason')!='累计开发执行时间已用尽' or old.get('legacy_execution_seconds') is not None:
                    raise Conflict('任务不符合旧计时修复条件')
                affected=False
                for receipt in old.get('receipts',[]):
                    result=receipt.get('result',{})
                    if receipt.get('action') not in ('plan','develop') or result.get('status')!='pass' or result.get('timing'):
                        continue
                    call_id=str(uuid.UUID(receipt['call_id']))
                    path=self.ledger.store.state/'autopilot/calls'/call_id/'request.json'
                    request=json.loads(path.read_text())
                    if request['input']['record']['id']==ident and result.get('elapsed',0)>request['timeout']+30:
                        affected=True
                if not affected:
                    raise Conflict('未找到成功回执超出调用上限的旧计时异常')
                return self.ledger.update(kind,ident,old['version'],{'legacy_execution_seconds':old.get('execution_seconds',0),
                    'execution_seconds':0,'timing_repair_reason':'旧墙钟计时超过调用超时上限且执行成功；历史实际运行时长无法还原，保留旧记录并恢复一次单调计时窗口',
                    'reason':'旧计时异常已隔离，使用单调计时继续开发'},old.get('resume_status','developing'))
            if action=='release' and old['status']=='accepted':
                product=self.ledger.get('products', old['product_id'])
                from .project import generic
                if generic(product) and not {'idle','publish','observe'} <= set(product.get('adapter_spec',{}).get('capabilities',[])):
                    raise Conflict('项目尚未配置部署能力，已验收成果继续保留')
                from .delivery import configured
                if configured(self.ledger.get('products', old['product_id'])):
                    raise Conflict('已启用 Git 交付；验收成果将自动进入每日 release，客户端安装独立管理')
                if not old.get('manifest') or not old.get('commit'):
                    raise Conflict('缺少已验收的候选产物')
                if any(r['product_id']==old['product_id'] and r['status'] not in TERMINAL | {'queued','blocked'} for r in self.ledger.list('runs')):
                    raise Conflict('请等待当前任务完成后再发布已验收版本')
                return self.ledger.update(kind,ident,old['version'],{'reason':'已验收版本进入连续空闲检查','last_idle_check':0,'idle_since':None},'awaiting_release')
            if action=='cancel' and old['status'] not in TERMINAL:
                return self.ledger.update(kind,ident,old['version'],{'control':'cancel','reason':'用户取消；等待执行器停止并核对'},'cancelling')
            if action=='pause' and old['status'] not in TERMINAL:
                return self.ledger.update(kind,ident,old['version'],{'control':'pause','resume_status':old['status'],'reason':'用户暂停；等待执行器停止并核对'},'pausing')
            if action=='retry' and old['status']=='blocked':
                if old.get('uncertain'):
                    raise Conflict('先核对未确认的进程或发布结果，不能重复执行')
                return self.ledger.update(kind,ident,old['version'],{'control':None,'reason':'','next_auto_retry_at':None,'auto_retry_wait_reason':None},old.get('resume_status','queued'))
        if kind=='releases' and action=='rollback' and old['status'] in ('observing','completed','blocked'):
            newer=[r for r in self.ledger.list('releases') if r['product_id']==old['product_id'] and r['created']>old['created'] and r['status'] not in ('cancelled','rolled_back')]
            if newer:
                raise Conflict('只能回滚当前最新发布，不能覆盖后续版本')
            return self.ledger.update(kind,ident,old['version'],{'reason':'用户请求回滚'},'rollback_pending')
        raise Conflict('当前状态不支持此操作')

    def configure_product(self,ident,body):
        """在同一事务中核对编辑基线，允许监测记录更新而不覆盖并发配置修改。"""
        changes=body.get('config',{})
        if not isinstance(changes,dict):
            raise ValueError('项目配置必须为对象')
        current = self.ledger.get('products', ident)
        if 'git' in changes and changes['git'] != current.get('git'):
            self.validate_product({k:v for k,v in current.items()} | changes)
            from .github import GitHub
            GitHub(changes['git']['url']).access(write=changes['git'].get('enabled', False))
        with self.ledger.store.transaction() as db:
            old=self.ledger.get('products',ident,db)
            if 'base_config' in body:
                base=body['base_config']
                # Only the settings form may use content-based concurrency checks.
                if not isinstance(base,dict) or not set(changes)<=set(base)<= {'goal','agents','policy','git','code_review','runtime_recovery'}:
                    raise ValueError('项目配置编辑基线无效')
                if any(old.get(key)!=base[key] for key in changes):
                    raise Conflict('项目配置已被其他操作修改；当前草稿已保留，请核对后放弃修改以载入最新配置')
            elif body.get('version')!=old['version']:
                raise Conflict('记录已更新，请刷新后重试')
            runs=db.execute('SELECT status FROM auto_runs WHERE product_id=?',(ident,))
            changed={key for key,value in changes.items() if value!=old.get(key)}
            live_limits={'runs_per_day','discovery_per_day','tokens_per_day','deepseek_off_peak_only'}
            policy_changes={key for key in set(changes.get('policy',{})) | set(old.get('policy',{}))
                            if changes.get('policy',{}).get(key)!=old.get('policy',{}).get(key)} if isinstance(changes.get('policy',{}),dict) else {'invalid'}
            if 'policy' not in changes:
                policy_changes = set()
            limits_only=changed<= {'policy','code_review','runtime_recovery'} and policy_changes<=live_limits
            staged_git = 'git' in changed and not old.get('git', {}).get('enabled') and not changes['git'].get('enabled')
            limits_only = limits_only or (staged_git and changed <= {'git','code_review','policy'} and policy_changes <= live_limits)
            if 'git' in changed and any(r['product_id']==ident and r['status'] not in ('online','cancelled') for r in self.ledger.list('deliveries')):
                raise Conflict('有未结束的交付批次，不能更改 Git 仓库配置')
            if not limits_only and any(r['status'] not in TERMINAL | {'queued','blocked'} for r in runs):
                raise Conflict('有执行中的任务，请暂停并核对后修改产品策略')
            config={k:v for k,v in old.items() if k not in ('id','status','version','created','updated')}
            config.update(changes)
            self.validate_product(config)
            if 'runtime_recovery' in changed:
                changes['runtime_recovery_state']={'phase':'requested' if changes['runtime_recovery']['enabled'] else 'disabled',
                    'attempts':(old.get('runtime_recovery_state') or {}).get('attempts',0),'next_check_at':0}
            if 'git' in changed:
                changes['git_migration'] = {'status': 'required'}
            return self.ledger.update('products',ident,old['version'],changes,db=db)

    def promote_signal(self, ident, body):
        """将待处理信号原子登记为高优先级需求，保留来源并防止重复入池。"""
        import datetime
        from zoneinfo import ZoneInfo
        value={}
        for key in ('title','evidence','impact'):
            raw=body.get(key)
            if not isinstance(raw,str) or not raw.strip():
                raise ValueError('需求名称、证据、用户影响与验收条件不能为空')
            value[key]=raw.strip()
        acceptance=body.get('acceptance')
        if not isinstance(acceptance,list) or not acceptance or any(not isinstance(item,str) or not item.strip() for item in acceptance):
            raise ValueError('验收条件必须是非空文本列表')
        value['acceptance']=[item.strip() for item in acceptance]
        with self.ledger.store.transaction() as db:
            signal=self.ledger.get('signals',ident,db)
            if signal['version']!=body.get('version'):
                raise Conflict('信号已更新，请刷新后重试')
            if signal['status']!='pending' or signal.get('requirement_id') or signal.get('requirement_ids'):
                raise Conflict('该信号已处理或已进入需求池，请查看关联需求')
            self.ledger.get('products',signal['product_id'],db)
            now=time.time()
            requirement=self.ledger.create('requirements',redact(value) | {
                'product_id':signal['product_id'],'signal_ids':[ident],'source':'manual',
                'priority':0,'queue_first':True,'queue_first_at':now,
                'requirement_day':datetime.datetime.fromtimestamp(now,ZoneInfo('Asia/Shanghai')).date().isoformat(),
            },'pending',db=db)
            self.ledger.update('signals',ident,signal['version'],{
                'requirement_id':requirement['id'],'requirement_ids':[requirement['id']],
                'manual_requirement_id':requirement['id'],'attribution_pending':False,
            },'classified',db)
            return requirement

    def queue(self, requirement, db=None):
        if db is None:
            with self.ledger.store.transaction() as connection:
                return self.queue(requirement, db=connection)
        if db is not None:
            latest=self.ledger.get('requirements',requirement['id'],db)
            if latest['version']!=requirement['version'] or latest['status']!='pending':
                raise Conflict('需求已发生变化或已进入任务队列')
            from .intake import approved
            if not approved(latest):
                raise Conflict('需求必须由用户确认后才能排队')
            product=self.ledger.get('products',latest['product_id'],db)
            evaluation=self.ledger.get('evaluations',product['evaluation_id'],db) if product.get('evaluation_id') else None
            if evaluation and len(evaluation.get('run_ids',[]))>=evaluation['target_count']:
                raise Conflict('本次用户指定的评测任务数已满，其余需求保留待评估')
            from .test_chains import settings, enqueue_generation
            if settings(product)['enabled']:
                enqueue_generation(self.ledger, product, latest, db)
            run=self.ledger.create('runs',{'product_id':latest['product_id'],'requirement_id':latest['id'],
                 'priority':latest.get('priority',2),'queue_first':latest.get('queue_first',False),
                 'queue_first_at':latest.get('queue_first_at',0),
                 'title':latest['title'],'budget_reserved':bool(latest.get('investigation_budget_reserved')),'revisions':0,'execution_seconds':0,'reason':'','receipts':[],
                 **({'evaluation_id':evaluation['id']} if evaluation else {})},'queued',db=db)
            if evaluation:
                self.ledger.update('evaluations',evaluation['id'],evaluation['version'],{'run_ids':evaluation.get('run_ids',[])+[run['id']]},db=db)
            self.ledger.update('requirements',latest['id'],latest['version'],{'run_id':run['id']},'queued',db)
            return run

    @staticmethod
    def validate_product(config):
        from .project import validate
        validate(config)
        if 'runtime_recovery' in config:
            value=config['runtime_recovery']
            if not isinstance(value,dict) or set(value)!={'enabled'} or type(value.get('enabled')) is not bool:
                raise ValueError('应用自动恢复开关必须为布尔值')
            if value['enabled'] and any(not isinstance(config.get(k),str) or not Path(config[k]).is_absolute()
                                        for k in ('application','app_support','runtime')):
                raise ValueError('开启自动恢复前，请配置正式应用、数据目录和运行时的绝对路径')
        if not isinstance(config,dict) or not all(isinstance(config.get(k),str) and config[k].strip() for k in ('name','source','goal')):
            raise ValueError('产品名称、源码路径和目标必填')
        if not Path(config['source']).is_absolute():
            raise ValueError('源码必须为绝对路径')
        if 'git' in config:
            from .github import repository
            import re
            value = config['git']
            if not isinstance(value, dict) or set(value) - {'url','base_branch','enabled'}:
                raise ValueError('Git 配置字段无效')
            repository(value.get('url'))
            branch = value.get('base_branch', 'master')
            if not isinstance(branch, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_/-]*', branch) or '//' in branch or branch.endswith('/'):
                raise ValueError('目标分支名称无效')
            if type(value.get('enabled', False)) is not bool:
                raise ValueError('自动交付开关必须为布尔值')
            if value.get('enabled'):
                from reviewers import selection
                choices = config.get('code_review', {})
                selection(choices.get('reviewer') or config.get('agents', {}).get('acceptance'))
                selection(choices.get('fixer') or config.get('agents', {}).get('implementation'))
        if 'code_review' in config:
            from reviewers import selection
            value = config['code_review']
            if not isinstance(value, dict) or set(value) - {'reviewer','fixer','max_revisions'}:
                raise ValueError('代码评审配置字段无效')
            for role in ('reviewer','fixer'):
                if role in value:
                    selection(value[role])
                    if value[role].get('reasoning_effort', 'medium') not in ('low','medium','high','xhigh','max','ultra'):
                        raise ValueError('推理强度无效')
            limit = value.get('max_revisions', 0)
            if type(limit) is not int or not 0 <= limit <= 20:
                raise ValueError('自动修复轮数必须为 0 到 20，0 表示不限')
        for key,value in config.get('policy',{}).items():
            if key == 'deepseek_off_peak_only':
                if type(value) is not bool:
                    raise ValueError('DeepSeek 仅空闲时段运行开关必须为布尔值')
                continue
            minimum=0 if key in ('runs_per_day','discovery_per_day','tokens_per_day') else 1
            if key not in DEFAULTS or isinstance(value,bool) or not isinstance(value,int) or value<minimum:
                raise ValueError('无效策略值')
        for key in ('adapter','executor'):
            if key in config and (not isinstance(config[key],list) or not config[key] or not all(isinstance(x,str) for x in config[key])):
                raise ValueError(f'{key} 必须为命令参数数组')
        if 'agents' in config:
            from reviewers import selection
            required={'discovery':'codex','implementation':'harness','verification':'codex','acceptance':'codex'}
            if not isinstance(config['agents'],dict) or set(config['agents'])!=set(required):
                raise ValueError('请配置需求发现、实现、验证、验收四个角色')
            for name,provider in required.items():
                selected=selection(config['agents'][name])
                if selected['provider'] not in ('codex', 'harness', 'claude'):
                    raise ValueError(f'{name} 执行器不受支持')
