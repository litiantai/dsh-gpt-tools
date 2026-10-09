#!/usr/bin/env python3
"""将已验收候选交给用户在独立测试应用中查看，保留原测试数据和原始验收产物。"""
import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'dsh-gpt-supervisor/scripts')]
from review_core import Store
from autopilot.store import Ledger
from autopilot.call import atomic
from autopilot.sandbox import restrict
from autopilot.thsoctop import manifest_hash, http


def start(state,run_id):
    ledger=Ledger(Store(state));run=ledger.get('runs',run_id)
    if run['status'] not in ('accepted','delivered','online','completed'):
        raise ValueError('只能预览已经通过业务验收的任务')
    from autopilot.test_instance import slot
    with slot(state/'autopilot',ledger.get('products',run['product_id'])):
        return start_in_slot(state,run_id)


def start_in_slot(state,run_id):
    ledger=Ledger(Store(state));run=ledger.get('runs',run_id)
    if run['status'] not in ('accepted','delivered','online','completed'):
        raise ValueError('只能预览已经通过业务验收的任务')
    manifest=json.loads(Path(run['manifest']).read_text());candidate=Path(manifest['root'])
    if manifest['commit']!=run['commit'] or any(c['status']!='pass' for c in manifest['checks'] if c.get('required',True)):
        raise ValueError('候选版本或必需验收不一致')
    if manifest_hash(candidate/'runtime')!=manifest['runtime_hash'] or manifest_hash(candidate/'thsoctop.app')!=manifest['app_hash']:
        raise ValueError('验收后产物发生改变，不能交付测试入口')
    product=ledger.get('products',run['product_id']);root=state/'autopilot/previews'/run_id
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    instance=root/'instance.json'
    if instance.exists():
        old=json.loads(instance.read_text())
        proc=subprocess.run(['ps','-p',str(old['pid']),'-o','command='],capture_output=True,text=True)
        if proc.returncode==0 and str(root) in proc.stdout:
            return old
    runtime=root/'runtime';support=root/'support';application=root/'thsoctop 已验收测试版.app'
    if not runtime.exists():shutil.copytree(candidate/'runtime',runtime,symlinks=True)
    if not support.exists():
        # Copy the user's isolated test home, never the production data directory.
        source=Path(product['test_environment']['home']).parent.resolve()
        if not source.is_relative_to((state/'autopilot').resolve()):
            raise ValueError('测试数据目录不属于自主研发隔离区')
        shutil.copytree(source,support,symlinks=True)
    if not application.exists():shutil.copytree(candidate/'test-application/thsoctop 验收版.app',application,symlinks=True)
    link=support/'dsh/profiles/node_modules'
    if link.is_symlink():link.unlink()
    if not link.exists():link.symlink_to(runtime/'node_modules',target_is_directory=True)
    ready=support/'host-ready.private.json';ready.unlink(missing_ok=True)
    temporary=root/'tmp';temporary.mkdir(exist_ok=True)
    bundle=plistlib.loads((application/'Contents/Info.plist').read_bytes())['CFBundleIdentifier']
    platform=[Path.home()/'Library'/kind/bundle for kind in ('WebKit','Caches','HTTPStorages')]
    platform.append(Path.home()/'Library/Saved Application State'/(bundle+'.savedState'))
    env=os.environ | {'THSOCTOP_APP_SUPPORT':str(support),'THSOCTOP_DSH_HOME':str(support/'dsh'),
        'THSOCTOP_RUNTIME_DIR':str(runtime),'THSOCTOP_AUTOPILOT_ISOLATED':'1','TMPDIR':str(temporary),
        'THSOCTOP_NODE':str(ROOT/'scripts/autopilot-node.py'),'THSOCTOP_REAL_NODE':product['node']}
    command=restrict([str(application/'Contents/MacOS/thsoctop')],[root,Path('/private/tmp'),*platform],root/'desktop.sb')
    with (root/'desktop.log').open('a') as log:
        child=subprocess.Popen(command,env=env,stdout=log,stderr=log,start_new_session=True)
    record={'pid':child.pid,'application':str(application),'support':str(support),'home':str(support/'dsh'),
        'runtime':str(runtime),'commit':run['commit'],'run_id':run_id,'started_at':time.time(),'status':'starting'}
    atomic(instance,record)
    end=time.time()+60
    while not ready.exists() and child.poll() is None and time.time()<end:time.sleep(.5)
    if not ready.exists() or child.poll() is not None:raise RuntimeError('已验收测试版未能启动，请检查 preview desktop.log')
    from urllib.parse import urlsplit
    url=urlsplit(json.loads(ready.read_text())['url']);origin=url.scheme+'://'+url.netloc
    status=http(origin+'/ths-octop/api/status')
    if not status.get('ok'):raise RuntimeError('已验收测试 Host 健康检查未通过')
    record.update(status='ready',origin=origin);atomic(instance,record)
    product=ledger.get('products',product['id'])
    ledger.update('products',product['id'],product['version'],{'test_environment':record})
    ledger.create('evidence',{'product_id':product['id'],'run_id':run_id,'title':'已验收版本接入用户测试入口',
        'actual':'测试应用运行 '+run['commit']+'；正式应用与正式数据未修改。','details':record,
        'judgement':'源码版本与验收清单一致，独立测试 Host 已就绪；旧测试目录与原始验收产物保留。'},'pass')
    return record


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_id');parser.add_argument('--state-dir',type=Path,default=Path.home()/'.dsh/supervisor')
    args=parser.parse_args();print(json.dumps(start(args.state_dir,args.run_id),ensure_ascii=False))
