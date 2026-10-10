"""thsoctop 产品适配器：只读采集、隔离验收、持久化发布及回滚。"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
from pathlib import Path
import re
import shutil
import shlex
import uuid
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .call import atomic
from .sandbox import restrict


def manifest_hash(root):
    root=Path(root)
    value=hashlib.sha256()
    for path in sorted(root.rglob('*')):
        rel=path.relative_to(root)
        if path.is_symlink():
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError(f'发布目录包含外部链接：{rel}')
            content=os.readlink(path).encode()
        elif path.is_file():
            content=path.read_bytes()
        else:
            continue
        value.update(str(rel).encode()+b'\0'+hashlib.sha256(content).digest())
    return value.hexdigest()


def browser_failure(check, result_path):
    """保留浏览器的环境阻塞分类，缺失回执时禁止据此返修业务源码。"""
    if check['status'] == 'pass':
        return None
    try:
        result = json.loads(Path(result_path).read_text())
        if not isinstance(result, dict) or result.get('status') not in ('blocked', 'fail'):
            raise ValueError('无有效失败回执')
        return {k: v for k, v in result.items() if k in (
            'status', 'reason', 'failure_kind', 'error_code', 'retryable')}
    except (OSError, ValueError):
        return {'status': 'blocked', 'failure_kind': 'acceptance_infrastructure',
                'reason': '浏览器验收未产生有效回执：' + check.get('summary', '')}


def http(url,key=None):
    req=Request(url,headers={'Authorization':'Bearer '+key} if key else {})
    with urlopen(req,timeout=8) as response:
        return json.load(response)


def endpoint(product):
    from .runtime_recovery import current_endpoint
    return current_endpoint(product)


def monitor(product):
    origin=endpoint(product)
    key=(Path(product['app_support'])/'dsh/thsoctop/autopilot-monitor.key').read_text().strip()
    result=http(origin+'/ths-octop/api/autopilot/status',key)
    data=result.get('data',{})
    if not result.get('ok') or data.get('protocol')!='thsoctop-monitor-v1' or abs(time.time()-data.get('sampledAt',0))>10:
        raise ValueError('监测协议缺失或数据过期')
    return data


def stopped_health(product):
    """仅在配置路径对应的所有正式进程均已退出时提供首次监测升级依据。"""
    roots=[product.get(k) for k in ('application','runtime','app_support')]
    if not all(roots):
        return None
    try:
        processes=subprocess.check_output(['ps','-axo','command='],text=True)
        paths={str(Path(p).resolve()) for p in roots} | set(roots)
        if not any(path in line for path in paths for line in processes.splitlines()):
            return {'status':'pass','health':{'host':'stopped','releaseId':None,
                'evidence':'系统进程列表中配置的正式应用、运行时和数据目录均无运行进程'}}
    except (OSError,subprocess.SubprocessError):
        pass
    return None


def idle(product):
    try:
        data=monitor(product)
    except (FileNotFoundError,URLError,TimeoutError,ConnectionError) as exc:
        legacy=isinstance(exc,FileNotFoundError) or not isinstance(exc,HTTPError) or exc.code==404
        if legacy:
            stopped=stopped_health(product)
            if stopped:
                return stopped
        reason='当前正式版未提供自动发布监测接口，无法确认空闲状态' if isinstance(exc,HTTPError) and exc.code==404 else f'空闲状态不可确认：{exc}'
        return {'status':'busy','reason':reason}
    except Exception as exc:
        return {'status':'busy','reason':f'空闲状态不可确认：{exc}'}
    active=data.get('activity',{})
    desktop=data.get('desktop',{})
    if not active.get('known') or not desktop.get('known'):
        return {'status':'busy','reason':'会话或桌面活动来源尚未就绪'}
    if active.get('sessions') or active.get('jobs') or desktop.get('foreground') or desktop.get('voiceBusy'):
        return {'status':'busy','reason':'存在活跃会话、任务、录音或前台窗口'}
    return {'status':'pass','health':data}


def probe(product):
    from error_messages import failure_fields
    from .runtime_recovery import RuntimeFault
    try:
        data=monitor(product)
        return {'status':'pass','health':data,'monitor_mode':'full','release_monitor_available':True}
    except Exception as exc:
        if isinstance(exc,RuntimeFault):
            return {'status':'blocked',**failure_fields(exc,'应用服务',exc.code),'signals':[]}
        # 回滚到旧版后仍可读取基础健康状态，但不能据此推断发布版本或空闲。
        if isinstance(exc,FileNotFoundError) or isinstance(exc,HTTPError) and exc.code==404:
            try:
                response=http(endpoint(product)+'/ths-octop/api/status')
                data=response.get('data') or {}
                if response.get('ok') is not True or not isinstance(data,dict) or not str(data.get('product','')).startswith('thsoctop') or not isinstance(data.get('version'),str):
                    raise ValueError('基础状态响应未通过产品身份核对')
                reason='正式应用在线，基础健康检查通过；当前版本未接入自动发布监测，尚不能确认发布版本、活跃任务和桌面空闲状态。'
                return {'status':'pass','monitor_mode':'basic','release_monitor_available':False,'reason':reason,
                    'health':{'host':'ready','protocol':'thsoctop-basic-status','sampledAt':time.time(),
                        'application_version':data['version'],'activity':{'known':False},'desktop':{'known':False}},
                    'signals':[{'source':'runtime','code':'MONITOR_CAPABILITY_MISSING','component':'host',
                        'summary':'正式版缺少自动发布监测能力','evidence':{'reason':reason,'basic_health':'ready'},
                        'classification':'environment','impact':'基础状态可读，自动发布仍需认证监测和空闲证据'}]}
            except Exception as basic_error:
                fields=failure_fields(basic_error,'应用服务')
                reason=fields['reason']
                return {'status':'blocked',**fields,'signals':[{'source':'runtime',
                    'code':'MONITOR_UNAVAILABLE','component':'host','summary':'无法确认正式版运行状态',
                    'evidence':{'error':reason},'classification':'environment','impact':'无法确认运行状态与发布时机'}]}
        return {'status':'blocked',**failure_fields(exc,'应用服务'),'signals':[{
            'source':'runtime','code':'MONITOR_UNAVAILABLE','component':'host',
            'summary':'无法读取正式版运行监测','evidence':{'error':str(exc)},
            'classification':'environment','impact':'暂时无法确认版本与空闲状态'}]}


def command(argv,cwd,env,log,timeout=1200):
    root=Path(log).parent
    temporary=root/'tmp'
    temporary.mkdir(exist_ok=True)
    env={'TMPDIR':str(temporary),**env}
    if env.get('THSOCTOP_AUTOPILOT_ALLOWED_ROOT'):
        argv=restrict(argv,[cwd,env['THSOCTOP_AUTOPILOT_ALLOWED_ROOT'], '/private/tmp',
                           str(Path.home()/'.cargo'),str(Path.home()/'.rustup')],root/(Path(log).stem+'.sb'))
    with Path(log).open('w') as output:
        result=subprocess.run(argv,cwd=cwd,env=os.environ | env,stdout=output,stderr=subprocess.STDOUT,timeout=timeout)
    text=Path(log).read_text(errors='replace')
    clean=re.sub(r'\x1b\[[0-9;]*m','',text)
    failures=[line for line in clean.splitlines() if any(mark in line for mark in ('✗','AUTOPILOT_BLOCKED','error TS','Error:'))]
    return {'name':' '.join(argv),'required':True,'status':'pass' if result.returncode==0 else 'fail',
            'exit_code':result.returncode,'evidence':str(log),'summary':('\n'.join(dict.fromkeys(failures))+'\n'+clean[-1500:])[-6000:]}


def bind_runtime_sdk(workspace,product):
    """显式从固定运行时提供 SDK；不依赖原工作目录的父目录依赖。"""
    source=Path(product['worker_runtime'])/'node_modules/@deepseek-ai'
    if not source.is_dir():
        raise ValueError('固定运行时缺少 SDK 依赖')
    scope=Path(workspace)/'node_modules/@deepseek-ai'
    scope.mkdir(parents=True,exist_ok=True)
    for package in source.iterdir():
        target=scope/package.name
        if not target.exists() and not target.is_symlink():
            target.symlink_to(package.resolve(),target_is_directory=True)


def stage_runtime(workspace,root,product):
    runtime=root/'runtime'
    if not runtime.exists():
        shutil.copytree(product['worker_runtime'],runtime,symlinks=False)
    support=root/'support'
    env={'THSOCTOP_APP_SUPPORT':str(support),'THSOCTOP_DSH_HOME':str(support/'dsh'),
         'THSOCTOP_RUNTIME_DIR':str(runtime),'THSOCTOP_AUTOPILOT_ISOLATED':'1',
         'THSOCTOP_AUTOPILOT_ALLOWED_ROOT':str(root)}
    if product.get('browser_runner'):
        env['THSOCTOP_AUTOPILOT_BROWSER_RUNNER']=product['browser_runner']
    result=command(['bash','scripts/bootstrap.sh','--deploy-only'],workspace,env,root/'deploy.log')
    if result['status']!='pass':
        raise ValueError('候选插件部署失败：'+result['summary'])
    test_home=product.get('test_environment',{}).get('home')
    if test_home:
        for name in ('credentials.json','hxkline.json'):
            credential=Path(test_home)/'thsoctop'/name
            if credential.is_file():
                target=support/'dsh/thsoctop'/name
                target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                shutil.copy2(credential,target)
                target.chmod(0o600)
    script_root=Path(__file__).resolve().parents[1]/'scripts'
    model_home=root/'model-profile'
    subprocess.run([product.get('node','node'),str(script_root/'autopilot-profile.mjs'),
        product['model_source']['home'],product['model_source']['profile'],str(model_home),
        product['worker_runtime'],str(script_root/'autopilot-guard.mjs')],check=True,capture_output=True)
    shutil.copy2(model_home/'.credentials.yaml',support/'dsh/.credentials.yaml')
    shutil.copy2(model_home/'profiles/autopilot-review/cordis.patch.yml',support/'dsh/profiles/octop/cordis.patch.yml')
    env.update(THSOCTOP_NODE=str(script_root/'autopilot-node.py'),THSOCTOP_REAL_NODE=product.get('node','node'))
    env['THSOCTOP_CHROME']=str(script_root/'autopilot-chrome.mjs')
    return env


def verify(request):
    run=request['record']
    diff=subprocess.check_output(['git','diff','--name-only',run['base_commit'],run['commit']],cwd=run['workspace'],text=True)
    persistent=[p for p in diff.splitlines() if re.search(r'(store|state|migration|credentials|paths)\.(ts|rs)$',p)]
    if persistent and not request['product'].get('compatibility_command'):
        return {'status':'blocked','reason':'持久化数据实现变化，缺少兼容与恢复验证命令','checks':[
            {'name':'数据兼容与恢复验证配置','required':True,'status':'blocked','files':persistent}]}
    from .test_instance import slot
    with slot(request['state_root'],request['product']):
        return verify_in_test_instance(request)


def verify_in_test_instance(request):
    product,run=request['product'],request['record']
    workspace=Path(run['workspace']).resolve()
    root=Path(request['state_root'])/'candidates'/run['id']/('attempt-'+str(uuid.uuid4()))
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    checks=[]
    diff=subprocess.check_output(['git','diff','--name-only',run['base_commit'],run['commit']],cwd=workspace,text=True)
    persistent=[p for p in diff.splitlines() if re.search(r'(store|state|migration|credentials|paths)\.(ts|rs)$',p)]
    compat=product.get('compatibility_command')
    if persistent and not compat:
        return {'status':'blocked','reason':'持久化数据实现变化，缺少兼容与恢复验证命令','checks':[
            {'name':'数据兼容与恢复验证配置','required':True,'status':'blocked','files':persistent}]}
    build_env={'THSOCTOP_AUTOPILOT_ALLOWED_ROOT':str(root),'npm_config_cache':str(root/'npm-cache')}
    commands=[['pnpm','install','--frozen-lockfile','--store-dir',str(root/'pnpm-store')],['pnpm','run','build'],['pnpm','run','typecheck'],['pnpm','test'],
              ['pnpm','run','vendor:check'],['pnpm','run','dsh:compat'],
              ['pnpm','--filter','@thsoctop/desktop','run','build']]
    for i,argv in enumerate(commands):
        check=command(argv,workspace,build_env,root/f'check-{i}.log')
        checks.append(check)
        if check['status']!='pass':
            return {'status':'fail','reason':check['summary'],'checks':checks}
        if i==0:
            bind_runtime_sdk(workspace,product)
    env=stage_runtime(workspace,root,product)
    app=workspace/'apps/desktop/src-tauri/target/release/bundle/macos/thsoctop.app'
    testing_app=root/'test-application/thsoctop 验收版.app'
    if testing_app.exists():
        shutil.rmtree(testing_app)
    shutil.copytree(app,testing_app,symlinks=True)
    info=testing_app/'Contents/Info.plist'
    metadata=plistlib.loads(info.read_bytes())
    bundle_id='com.dsh.autopilot.candidate.'+run['id']
    metadata.update(CFBundleIdentifier=bundle_id,CFBundleName='thsoctop 验收版',CFBundleDisplayName='thsoctop 验收版')
    info.write_bytes(plistlib.dumps(metadata))
    subprocess.run(['codesign','--force','--deep','--sign','-',str(testing_app)],check=True,capture_output=True)
    env.update(THSOCTOP_TEST_APPLICATION=str(testing_app/'Contents/MacOS/thsoctop'),THSOCTOP_AUTOPILOT_PRESTARTED='1')
    platform_dirs=[Path.home()/'Library'/kind/bundle_id for kind in ('WebKit','Caches','HTTPStorages')]
    platform_dirs.append(Path.home()/'Library/Saved Application State'/(bundle_id+'.savedState'))
    argv=restrict([env['THSOCTOP_TEST_APPLICATION']],[root,Path('/private/tmp'),*platform_dirs],root/'desktop.sb')
    # Only this isolated profile is visible to legacy verification scripts.
    with (root/'desktop.log').open('w') as log:
        ready=Path(env['THSOCTOP_APP_SUPPORT'])/'host-ready.private.json'
        ready.unlink(missing_ok=True)
        env['TMPDIR']=str(root/'tmp')
        desktop=subprocess.Popen(argv,env=os.environ | env,stdout=log,stderr=log)
        from .test_instance import record as record_instance
        environment={'pid':desktop.pid,'application':str(testing_app),'support':env['THSOCTOP_APP_SUPPORT'],
            'home':env['THSOCTOP_DSH_HOME'],'runtime':env['THSOCTOP_RUNTIME_DIR'],'commit':run['commit'],
            'run_id':run['id'],'status':'starting','started_at':time.time()}
        try:
            record_instance(request['state_root'],product['id'],environment)
            ready=Path(env['THSOCTOP_APP_SUPPORT'])/'host-ready.private.json'
            deadline=time.time()+60
            while not ready.exists() and desktop.poll() is None and time.time()<deadline:
                time.sleep(.5)
            if not ready.exists() or desktop.poll() is not None:
                return {'status':'blocked','reason':'隔离桌面应用未就绪','checks':checks,'evidence':str(root/'desktop.log')}
            from urllib.parse import urlsplit
            address=urlsplit(json.loads(ready.read_text())['url'])
            environment.update(status='ready',origin=address.scheme+'://'+address.netloc)
            record_instance(request['state_root'],product['id'],environment)
            probes=request.get('requirement',{}).get('resolution_probes')
            if probes:
                from urllib.parse import urlsplit
                from .probes import check
                address=urlsplit(json.loads(ready.read_text())['url'])
                passed=check(probes,lambda path:http(address.scheme+'://'+address.netloc+path))
                checks.append({'name':'原需求上线只读断言预验证','required':True,'status':'pass' if passed else 'fail'})
                if not passed:
                    return {'status':'fail','reason':'原需求只读断言在候选环境未通过','checks':checks}
            for milestone in ('m1','m2','m3','m4','m4b','resilience'):
                source=workspace/'scripts'/f'verify-{milestone}.sh'
                script=source
                if milestone=='m4b':
                    # Candidate homes are isolated; check defaults instead of skipping them as user data.
                    text=source.read_text().replace('ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"','ROOT='+shlex.quote(str(workspace)))
                    text=text.replace('if [ -n "$HOST_PID" ]; then\n    if node -e', 'if [ -n "$HOST_PID" ] || [ "${THSOCTOP_AUTOPILOT_ISOLATED:-}" = 1 ]; then\n    if node -e')
                    script=root/'verify-m4b.sh';script.write_text(text)
                check=command(['bash',str(script)]+(['--with-app'] if milestone=='m1' else []),workspace,env,root/f'integration-{milestone}.log',600)
                content=re.sub(r'\x1b\[[0-9;]*m','',Path(check['evidence']).read_text())
                failures={line.strip()[1:].strip() for line in content.splitlines() if line.strip().startswith('✗')}
                if milestone=='m4b' and product.get('acceptance_scope')=='finance-mainline' and failures=={'播放列表代理异常（HTTP 504）'} and '跳过' not in content:
                    # Preserve upstream unavailability as a failed optional health check, never a success.
                    checks.append(check | {'name':'财经广播外部源可用性（不属于本次三个需求）','required':False,'status':'blocked'})
                    check=check | {'name':'广播与小游戏本地回归、市场播报','status':'pass','exit_code':check['exit_code'],
                                   'summary':'逐项断言仅央广上游返回 HTTP 504；所有本地断言通过。原脚本 exit=1 与原始日志保留。'}
                checks.append(check)
                if check['status']!='pass' or re.search(r'跳过|SKIP|缺少.*(凭据|授权)|未登录|没找到 Chrome',content):
                    return {'status':'blocked','reason':check['summary'],'checks':checks}
            browser_env=env | {'THSOCTOP_AUTOPILOT_RUN_ID':run['id'],'THSOCTOP_AUTOPILOT_PRODUCT_ID':product['id'],
                              'THSOCTOP_AUTOPILOT_STATE':str(Path(request['state_root']).parent),
                              'THSOCTOP_AUTOPILOT_RESULT':str(root/'browser-result.json')}
            check=command([product.get('node','node'),product['browser_runner']],workspace,browser_env,root/'mainline-browser.log',900)
            failure=browser_failure(check,root/'browser-result.json')
            if failure:
                check=check | {'status':failure['status'],'summary':failure.get('reason',check['summary'])}
            from review_core import Store
            from .evidence import Recorder
            recorder=Recorder(Store(Path(request['state_root']).parent),product['id'],run['title']+'：财经主链路验收',
                              'Playwright 实际点击、真实账号取数、隔离会话和报告生成；每步保留截图与实际结果')
            recorder.inspection=recorder.ledger.update('inspections',recorder.inspection['id'],recorder.inspection['version'],{'run_id':run['id']})
            steps_files=list((Path(env['THSOCTOP_APP_SUPPORT'])/'browser-evidence').rglob('steps.json'))
            steps_file=max(steps_files,key=lambda p:p.stat().st_mtime) if steps_files else None
            if steps_file:
                for step in json.loads(steps_file.read_text()):
                    recorder.step(**step)
            recorder.finish(check['status'],check['summary'])
            from .test_chain_worker import verification_checks
            ui_checks = verification_checks(request, environment | {'branch':run.get('branch'), 'stopped':False})
            checks.extend(ui_checks)
            if any(c['status'] != 'pass' for c in ui_checks):
                failed = next(c for c in ui_checks if c['status'] != 'pass')
                return {'status':failed['status'], 'reason':failed['reason'], 'checks':checks, 'retryable':False}
        finally:
            desktop.terminate()
            try:
                desktop.wait(timeout=15)
            except subprocess.TimeoutExpired:
                desktop.kill();desktop.wait()
            record_instance(request['state_root'],product['id'],environment | {'status':'stopped','stopped_at':time.time()})
    checks.append(check)
    if check['status']!='pass':
        return failure | {'checks':checks}
    candidate_app=root/'thsoctop.app'
    if candidate_app.exists():
        shutil.rmtree(candidate_app)
    shutil.copytree(app,candidate_app,symlinks=True)
    atomic(root/'runtime/autopilot-release.json',{'id':run['id'],'commit':run['commit']})
    manifest={'id':run['id'],'commit':run['commit'],'root':str(root),'checks':checks,
              'runtime_hash':manifest_hash(root/'runtime'),'app_hash':manifest_hash(candidate_app),
              'data_compatibility':'unchanged'}
    # Source formats that may alter persistent state require explicit compatibility tests.
    if persistent:
        check=command(compat,workspace,env | {'THSOCTOP_COMPAT_BASE':run['base_commit'],
            'THSOCTOP_COMPAT_HEAD':run['commit']},root/'compatibility.log')
        checks.append(check)
        if check['status']!='pass':
            return {'status':'fail','reason':'数据兼容及恢复验证失败','checks':checks}
        manifest['data_compatibility']='verified'
    atomic(root/'manifest.json',manifest)
    return {'status':'pass','checks':checks,'manifest':str(root/'manifest.json')}


def journal(request):
    run=request.get('run',request['record'])
    root=Path(request['state_root'])/'deployments'/run['release_id']
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    return root,run


def quit_app(product):
    path=Path(product['application']).resolve()
    # Select only the configured application's executable, never all Harness processes.
    processes=subprocess.check_output(['ps','-axo','pid=,command='],text=True)
    pids=[int(line.strip().split(maxsplit=1)[0]) for line in processes.splitlines()
          if str(path/'Contents/MacOS')+'/' in line]
    for pid in pids:
        os.kill(pid,15)
    end=time.time()+30
    while pids and time.time()<end:
        remaining=[]
        for pid in pids:
            try:
                os.kill(pid,0)
                remaining.append(pid)
            except ProcessLookupError:
                pass
        pids=remaining
        if pids:
            time.sleep(.2)
    if pids:
        raise ValueError('桌面应用未能正常退出，未切换版本')


def open_app(product):
    subprocess.run(['open','-g',product['application']],check=True)


def wait_health(product,release_id=None,timeout=60):
    end=time.time()+timeout
    while time.time()<end:
        try:
            data=monitor(product)
            if data['host']=='ready' and (release_id is None or data.get('releaseId')==release_id):
                return data
        except Exception:
            pass
        time.sleep(1)
    raise ValueError('新版本启动或版本核验失败')


def rollback(request):
    root,run=journal(request)
    path=root/'journal.json'
    if not path.exists():
        return {'status':'blocked','reason':'缺少发布日志，禁止猜测回滚目标'}
    data=json.loads(path.read_text())
    product=request['product']
    if data['step']=='rolled_back':
        return {'status':'pass'}
    quit_app(product)
    runtime=Path(product['runtime'])
    old_runtime=root/'previous-runtime'
    application=Path(product['application'])
    old_app=root/'previous.app'
    if old_runtime.exists():
        if runtime.is_symlink():
            runtime.unlink()
        elif runtime.exists():
            raise ValueError('当前运行时不是受管链接，不能覆盖')
        os.rename(old_runtime,runtime)
    if old_app.exists():
        if application.exists():
            os.rename(application,root/'rejected.app')
        os.rename(old_app,application)
    profile=Path(product['app_support'])/'dsh/profiles/octop/package.json'
    if (root/'previous-profile.json').exists():
        shutil.copy2(root/'previous-profile.json',profile)
    open_app(product)
    # Legacy baseline might not yet expose the new monitor; a Host-ready check is still required.
    if data.get('previous_release'):
        wait_health(product,data['previous_release'])
    else:
        end=time.time()+60
        while time.time()<end:
            try:
                if http(endpoint(product)+'/ths-octop/api/status').get('ok'):
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            raise ValueError('旧版本恢复后仍未就绪')
    atomic(path,data | {'step':'rolled_back','finished':time.time()})
    return {'status':'pass','restored_release':data.get('previous_release')}


def publish(request):
    product=request['product']
    root,run=journal(request)
    path=root/'journal.json'
    if run.get('master_commit') and not path.exists():
        from .master import target
        if target(product) != run['master_commit']:
            return {'status':'busy','reason':'master 在更新前发生变化，等待重新核对'}
    elif product.get('repository') and not path.exists():
        from .workspace import git
        stable=git(product['repository'],'rev-parse','refs/heads/codex/autonomous')
        if stable!=run.get('base_commit'):
            return {'status':'blocked','reason':'已上线基线发生变化，候选版本需要重新合并并验收'}
    manifest=json.loads(Path(run['manifest']).read_text())
    candidate=Path(manifest['root'])
    if not manifest.get('checks') or any(c.get('status')!='pass' for c in manifest['checks'] if c.get('required',True)):
        return {'status':'blocked','reason':'候选产物缺少完整的必需验收证据'}
    if manifest['commit']!=run['commit'] or manifest['runtime_hash']!=manifest_hash(candidate/'runtime') or manifest['app_hash']!=manifest_hash(candidate/'thsoctop.app'):
        return {'status':'blocked','reason':'候选产物与已验收清单不一致'}
    if path.exists():
        data=json.loads(path.read_text())
        if data['step']=='published':
            wait_health(product,manifest['id'])
            return {'status':'pass','release':manifest['id']}
        if data['step']=='rolled_back':
            return {'status':'fail','reason':'该发布已回滚'}
        # Interrupted partial switch is reconciled through rollback, never replayed blindly.
        rollback(request)
        return {'status':'fail','reason':'已将中断的发布恢复到旧版本'}
    readiness=idle(product)
    if readiness['status']!='pass':
        return {'status':'busy','reason':readiness['reason']}
    atomic(path,{'step':'prepared','manifest':run['manifest'],'previous_release':readiness['health'].get('releaseId')})
    data=json.loads(path.read_text())
    try:
        quit_app(product)
        atomic(path,data | {'step':'switching'})
        runtime=Path(product['runtime'])
        os.rename(runtime,root/'previous-runtime')
        runtime.symlink_to(candidate/'runtime',target_is_directory=True)
        app=Path(product['application'])
        os.rename(app,root/'previous.app')
        # Same-volume prepared copy; the final path changes only after copying completes.
        prepared=app.with_name(f'.thsoctop-{run["id"]}.app')
        if not prepared.exists():
            shutil.copytree(candidate/'thsoctop.app',prepared,symlinks=True)
        os.rename(prepared,app)
        profile=Path(product['app_support'])/'dsh/profiles/octop/package.json'
        shutil.copy2(profile,root/'previous-profile.json')
        shutil.copy2(candidate/'support/dsh/profiles/octop/package.json',profile)
        # cordis.patch.yml and user data are deliberately never copied from the test profile.
        atomic(path,data | {'step':'starting'})
        open_app(product)
        health=wait_health(product,manifest['id'])
        atomic(path,data | {'step':'published','finished':time.time()})
        return {'status':'pass','release':manifest['id'],'health':health}
    except Exception as exc:
        try:
            rollback(request)
        except Exception as recovery:
            return {'status':'blocked','reason':f'发布失败：{exc}；回滚待核对：{recovery}','uncertain':True}
        return {'status':'fail','reason':f'发布失败并已回滚：{exc}'}


def observe(request):
    product,run=request['product'],request['record']
    try:
        health=monitor(product)
        if health.get('releaseId')!=run['id']:
            return {'status':'fail','reason':'正式版与发布版本不一致'}
        # Original acceptance remains explicit; running process alone does not prove user impact.
        checks=request.get('requirement',{}).get('resolution_probes',[])
        if not checks:
            return {'status':'pass','health':health,'problem_resolved':False,'reason':'运行健康；尚缺原需求的正式环境只读验证证据'}
        from .probes import check
        if not check(checks,lambda path:http(endpoint(product)+path)):
            return {'status':'fail','reason':'原需求只读验证失败'}
        return {'status':'pass','health':health,'problem_resolved':True}
    except (TimeoutError,ConnectionError,URLError) as exc:
        if isinstance(exc,HTTPError) and exc.code not in (408,429,502,503,504):
            return {'status':'fail','reason':str(exc)}
        return {'status':'busy','reason':'上线只读检查暂时不可用：'+str(exc)}
    except Exception as exc:
        return {'status':'fail','reason':str(exc)}


def main(action,request):
    if action in ('master_sync','final_acceptance'):
        from .master import sync, acceptance
        return sync(request) if action=='master_sync' else acceptance(request)
    if action in ('recover-runtime','publish','rollback'):
        import fcntl
        lock_path=Path(request['state_root'])/'runtime-locks'/(request['product']['id']+'.lock')
        lock_path.parent.mkdir(parents=True,exist_ok=True)
        with lock_path.open('a') as lock:
            try: fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'status':'busy','reason':'应用运行时操作进行中，请稍后重试'}
            if action=='recover-runtime':
                from .runtime_recovery import execute
                return execute(request)
            return publish(request) if action=='publish' else rollback(request)
    if action=='probe':
        return probe(request['product'])
    if action=='idle':
        return idle(request['product'])
    if action=='verify':
        result=verify(request)
        if result['status']=='pass' and request['product'].get('agents',{}).get('verification',{}).get('provider')=='codex':
            from .codex_executor import execute as codex
            independent=codex('validate',request | {'checks':result['checks']})
            result['checks'].append({'name':'Codex 独立验证','required':True,**independent})
            manifest_path=Path(result['manifest'])
            manifest=json.loads(manifest_path.read_text())
            atomic(manifest_path,manifest | {'checks':result['checks'],'independent_verification':independent})
            if independent['status']!='pass':
                result.update(status=independent['status'],reason=independent.get('reason','Codex 独立验证未通过'))
        return result
    if action=='publish':
        return publish(request)
    if action=='rollback':
        return rollback(request)
    if action=='observe':
        return observe(request)
    if action=='inspect':
        product=request['product']
        if product.get('git',{}).get('enabled'):
            from .master import enabled, inspect
            if not enabled(product):
                return {'status':'blocked','reason':'master 实例尚未完成接入，等待主分支与应用配置就绪'}
            return inspect(request)
        from review_core import Store
        from .store import Ledger
        from .workspace import git
        ledger=Ledger(Store(Path(request['state_root']).parent))
        stable=git(product['repository'],'rev-parse','refs/heads/codex/autonomous')
        if product.get('test_environment',{}).get('commit')!=stable:
            released=next((r for r in ledger.list('runs') if r['product_id']==product['id'] and r['status']=='completed' and r.get('commit')==stable),None)
            if released:
                import runpy
                start=runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts/autopilot-preview.py'))['start']
                product=product | {'test_environment':start(ledger.store.state,released['id'])}
        # Reuse a verified, isolated desktop for recurring read-only UX inspection.
        # No model calls or daily-development quota are consumed by this action.
        environment=product.get('test_environment',{})
        if environment.get('status')=='ready' and environment.get('commit'):
            run=ledger.get('runs',environment['run_id'])
            if run['commit']!=environment['commit']:
                return {'status':'blocked','reason':'巡检应用与已验收源码版本不一致'}
            from .workspace import digest
            if digest(run['workspace'])!=run['source_digest']:
                return {'status':'blocked','reason':'巡检源码在验收后发生变化'}
            root=Path(environment['support']).parent
            argv=[product.get('node','node'),str(Path(__file__).resolve().parents[1]/'scripts/autopilot-survey.mjs'),str(root),product['id']]
            proc=subprocess.run(argv,env=os.environ | {'DSH_AUTOPILOT_STATE':str(ledger.store.state)},
                                capture_output=True,text=True,timeout=480)
            try:
                result=json.loads(proc.stdout)
            except ValueError:
                return {'status':'blocked','reason':'隔离界面巡检未返回有效回执：'+proc.stderr[-1200:]}
            survey=Path(result['output'])/'survey.json'
            evidence=json.loads(survey.read_text())
            return {'status':'pass' if proc.returncode==0 else 'blocked','reason':result.get('error','隔离界面巡检完成，等待需求归因'),
                'inspection_workspace':run['workspace'],'source_commit':run['commit'],
                'signals':[{'source':'inspection','code':'EXPERIENCE_REVIEW','component':'finance-assistant',
                    'version':run['commit']+':'+str(int(time.time()//(product.get('policy',{}).get('inspection_seconds',900)))),
                    'summary':'定期财经主链路体验巡检','evidence':{'survey':str(survey),
                    'steps':[{k:s.get(k) for k in ('title','actual','status','errors')} for s in evidence.get('steps',[])]}}]}
        from .workspace import git
        commit_id=git(product['repository'],'rev-parse','refs/heads/codex/autonomous')
        workspace=Path(request['state_root'])/'inspection-sources'/commit_id
        if not workspace.exists():
            workspace.parent.mkdir(parents=True,exist_ok=True)
            git(product['repository'],'worktree','add','--detach',str(workspace),commit_id)
        root=Path(request['state_root'])/'inspections'/str(int(time.time()))
        root.mkdir(parents=True,mode=0o700)
        env={'THSOCTOP_AUTOPILOT_ALLOWED_ROOT':str(root),'npm_config_cache':str(root/'npm-cache')}
        prerequisites=[['pnpm','install','--frozen-lockfile','--store-dir',str(root/'pnpm-store')],['pnpm','run','build']]
        for index,argv in enumerate(prerequisites):
            prepared=command(argv,workspace,env,root/f'prepare-{index}.log',600)
            if prepared['status']!='pass':
                return {'status':'blocked','reason':'隔离巡检环境尚未就绪','checks':[prepared],'signals':[{
                    'source':'inspection','code':'INSPECTION_ENVIRONMENT','summary':'隔离巡检前置条件未满足',
                    'evidence':prepared,'classification':'environment','component':'finance-assistant'}]}
            if index==0:
                bind_runtime_sdk(workspace,product)
        check=command(['pnpm','test'],workspace,env,root/'tests.log',600)
        return {'status':check['status'],'checks':[check],'signals':[] if check['status']=='pass' else [{
            'source':'inspection','code':'QUALITY_FAILURE','summary':'隔离基线质量检查未通过',
            'evidence':check,'component':'finance-assistant','version':product.get('baseline','')}]}
    raise ValueError('未知产品动作')


if __name__=='__main__':
    try:
        result=main(sys.argv[1],json.load(sys.stdin))
    except Exception as exc:
        result={'status':'blocked','reason':str(exc)}
    print(json.dumps(result,ensure_ascii=False))
