"""正式应用进程归属、当前监听地址与持久化自动恢复步骤。"""
from pathlib import Path
import re
import subprocess
import time

from error_messages import failure_fields


class RuntimeFault(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def processes(product):
    """按可执行文件及完整宿主参数识别实例，不能以路径子串匹配验收副本。"""
    apps_paths = {str(Path(product['application'])/'Contents/MacOS/thsoctop'),str(Path(product['application']).resolve()/'Contents/MacOS/thsoctop')}
    runtime = str(Path(product['runtime']).resolve())
    support = str(Path(product['app_support']).resolve())
    entries = {runtime+'/node_modules/@thsoctop/host/lib/index.js',str(Path(product['runtime'])/'node_modules/@thsoctop/host/lib/index.js')}
    text = subprocess.check_output(['ps','-ww','-axo','pid=,command='],text=True,timeout=3)
    apps, hosts = [], []
    for line in text.splitlines():
        parts = line.strip().split(None,1)
        if len(parts)!=2: continue
        pid, command = parts
        if any(command == app or command.startswith(app+' ') for app in apps_paths): apps.append(int(pid))
        # ps emits unquoted paths with spaces. Match complete option values, not token fragments.
        option = lambda key: re.search(r'(?:^| )--'+key+r' (.*?)(?= --[\w-]+(?: |$)|$)',command)
        home_arg, runtime_arg = option('app-support'), option('runtime-dir')
        prefix = command.split(' --',1)[0]
        if (any(prefix.endswith(' '+entry) for entry in entries) and home_arg and runtime_arg
                and str(Path(home_arg[1].strip('"\'')).resolve())==support
                and str(Path(runtime_arg[1].strip('"\'')).resolve())==runtime):
            hosts.append(int(pid))
    return {'apps':apps,'hosts':hosts}


def listening_ports(pid):
    proc = subprocess.run(['/usr/sbin/lsof','-nP','-a','-p',str(pid),'-iTCP','-sTCP:LISTEN','-Fn'],
                          text=True,capture_output=True,timeout=3)
    if proc.returncode not in (0,1): raise RuntimeError('无法检查应用服务的监听端口')
    return sorted({int(m[1]) for m in re.finditer(r'^n(?:127\.0\.0\.1|\[::1\]|\*):(\d+)$',proc.stdout,re.M)})


def current_endpoint(product):
    state = processes(product)
    if not state['hosts']:
        raise RuntimeFault('HOST_NOT_READY' if state['apps'] else 'APP_STOPPED')
    if len(state['hosts'])!=1: raise RuntimeFault('IDENTITY_MISMATCH')
    ports = listening_ports(state['hosts'][0])
    if not ports: raise RuntimeFault('HOST_NOT_READY')
    # Identity is checked without credentials before a monitoring key can be sent.
    from .thsoctop import http
    matches = []
    for port in ports:
        origin = f'http://127.0.0.1:{port}'
        try:
            data = http(origin+'/ths-octop/api/status')
            body = data.get('data') or {}
            if data.get('ok') is True and str(body.get('product','')).startswith('thsoctop') and isinstance(body.get('version'),str):
                matches.append(origin)
        except (OSError,ValueError):
            continue
    if len(matches)!=1: raise RuntimeFault('IDENTITY_MISMATCH')
    # Recheck owner immediately before returning the address, including port reuse.
    fresh = processes(product)
    if fresh['hosts']!=state['hosts'] or int(matches[0].rsplit(':',1)[1]) not in listening_ports(state['hosts'][0]):
        raise RuntimeFault('HOST_NOT_READY')
    return matches[0]


def busy(ledger, product_id):
    return any(r['product_id']==product_id and r['status'] in ('deploying','rollback_pending','rolling_back','pausing','cancelling')
               for kind in ('runs','releases') for r in ledger.list(kind))


def enabled(product):
    return product.get('runtime_recovery',{}).get('enabled') is True and product['status'] in ('active','observing')


def step(product, now=None):
    """一次只检查或发起一次系统启动；等待与退避由下一次调度推进。"""
    from .thsoctop import probe
    now = time.time() if now is None else now
    state = dict(product.get('runtime_recovery_state') or {})
    def result(phase, message, checked=None, **changes):
        state.update(phase=phase,message=message,updated_at=now,**changes)
        return (checked or {'status':'busy','reason':message}) | {'runtime_recovery_state':state}
    if not enabled(product):
        return result('disabled','应用自动恢复已关闭',next_check_at=None)
    checked = probe(product)
    if checked['status']=='pass':
        since = state.get('healthy_since') if state.get('phase')=='healthy' else None
        since = since or now
        return result('healthy','应用服务已恢复',checked,healthy_since=since,
                      attempts=0 if now-since>=300 else state.get('attempts',0),
                      next_check_at=now+product.get('policy',{}).get('probe_seconds',60),next_attempt_at=None)
    state['healthy_since'] = None
    current = processes(product)
    if current['apps'] or current['hosts']:
        if state.get('phase')=='starting' and now-state.get('started_at',now)<90:
            return result('starting','应用已启动，正在等待服务就绪',checked,next_check_at=now+5)
        if state.get('phase')!='starting':
            return result('unavailable','应用服务尚未就绪，请检查技术详情',checked,next_check_at=now+30)
        # Never kill an existing process or its conversations just to restart it.
        return failed(state,now,checked,'应用启动后未在规定时间内就绪')
    if state.get('phase')=='starting':
        if now-state.get('started_at',now)<90:
            return result('starting','正在启动应用',checked,next_check_at=now+5)
        return failed(state,now,checked,'应用未能在规定时间内启动')
    if now<(state.get('next_attempt_at') or 0):
        return result(state.get('phase','backoff'),'恢复失败，稍后重试',checked,next_check_at=state['next_attempt_at'])
    if state.get('phase')=='cooldown': state['attempts']=0
    state['attempts'] = state.get('attempts',0)+1
    if not Path(product['application']).is_dir():
        return failed(state,now,{'status':'blocked',**failure_fields(FileNotFoundError(product['application']))},'应用安装文件不存在')
    try:
        subprocess.run(['/usr/bin/open','-g',product['application']],check=True,capture_output=True,text=True,timeout=10)
    except (OSError,subprocess.SubprocessError) as exc:
        return failed(state,now,{'status':'blocked',**failure_fields(exc,'应用服务')},'应用启动失败')
    return result('starting','正在启动应用',started_at=now,next_check_at=now+5,next_attempt_at=None)


def failed(state, now, checked, message):
    attempts = state.get('attempts',1)
    delay = 900 if attempts>=3 else (60 if attempts==1 else 120)
    return checked | {'runtime_recovery_state':state | {'phase':'cooldown' if attempts>=3 else 'backoff',
        'message':message+'，稍后重试','next_attempt_at':now+delay,'next_check_at':now+delay,'updated_at':now,'healthy_since':None}}


def execute(request):
    """在锁内重新读取策略；与发布、回滚共用运行时操作锁。"""
    from review_core import Store
    from .store import Ledger
    ledger = Ledger(Store(Path(request['state_root']).parent))
    product = ledger.get('products',request['product']['id'])
    if busy(ledger,product['id']):
        return {'status':'busy','reason':'应用正在更新或停止，自动恢复稍后进行',
            'runtime_recovery_state':(product.get('runtime_recovery_state') or {}) | {'phase':'deferred','next_check_at':time.time()+5}}
    return step(product)
