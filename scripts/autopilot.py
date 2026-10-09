#!/usr/bin/env python3
"""持续研发服务与本机 thsoctop 接入命令。"""
import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'dsh-gpt-supervisor/scripts')]
from review_core import Store
from autopilot.api import Control
from autopilot.scheduler import serve
from autopilot.workspace import snapshot


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['serve','onboard','status','install-service'])
    parser.add_argument('--state-dir',default=os.environ.get('DSH_SUPERVISOR_STATE',str(Path.home()/'.dsh/supervisor')))
    parser.add_argument('--source',default=str(Path.home()/'Desktop/thsoctop'))
    parser.add_argument('--application',default='/Applications/thsoctop.app')
    parser.add_argument('--model-profile',default='supervisor-review')
    args=parser.parse_args()
    state=Path(args.state_dir).resolve()
    if args.action=='serve':
        serve(state); return
    control=Control(Store(state))
    if args.action=='status':
        print(json.dumps({'products':control.get('/products'),'metrics':control.get('/autopilot/metrics')},ensure_ascii=False,indent=2)); return
    if args.action=='install-service':
        dest=Path.home()/'Library/LaunchAgents/com.dsh.autopilot.plist'
        dest.parent.mkdir(parents=True,exist_ok=True)
        log=state/'autopilot/service.log'
        log.parent.mkdir(parents=True,exist_ok=True)
        value={'Label':'com.dsh.autopilot','ProgramArguments':[sys.executable,str(Path(__file__).resolve()),'serve','--state-dir',str(state)],
               'WorkingDirectory':str(ROOT),'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':10,
               'StandardOutPath':str(log),'StandardErrorPath':str(log),'EnvironmentVariables':{'PATH':os.environ.get('PATH','/usr/bin:/bin')}}
        dest.write_bytes(plistlib.dumps(value))
        subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(dest)],check=True)
        print(json.dumps({'service':str(dest)})); return
    existing=[p for p in control.get('/products') if Path(p['source']).resolve()==Path(args.source).resolve()]
    if existing:
        print(json.dumps(existing[0],ensure_ascii=False,indent=2)); return
    base=state/'autopilot/baselines/thsoctop'
    captured=snapshot(args.source,base)
    support=Path.home()/'Library/Application Support/com.thsoctop.desktop'
    runtime=support/'dependencies/dsh'
    worker_runtime=state/'autopilot/worker-runtime'
    if not worker_runtime.exists():
        shutil.copytree(runtime,worker_runtime,symlinks=False)
    model_source={'home':control.ledger.store.settings()['home'],'profile':args.model_profile}
    config={'name':'thsoctop','source':str(Path(args.source).resolve()),'repository':str(base),'baseline':captured['commit'],
            'goal':'优先完善行情、自选、研究报告、AI 对话和桌宠交互的财经助手主链路；质量、正确性和流程完整优先。',
            'application':args.application,'app_support':str(support),'runtime':str(runtime),'worker_runtime':str(worker_runtime),
            'model_source':model_source,'node':shutil.which('node') or 'node',
            'browser_runner':str(ROOT/'scripts/autopilot-browser.mjs'),
            'adapter':[sys.executable,str(ROOT/'scripts/autopilot-adapter.py'),'thsoctop'],
            'executor':[sys.executable,str(ROOT/'scripts/autopilot-adapter.py'),'executor']}
    product=control.mutate('/products',{'config':config})
    product=control.mutate(f'/products/{product["id"]}/observe',{'version':product['version']})
    print(json.dumps(product,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
