#!/usr/bin/env python3
"""持续研发服务、通用仓库扫描、接入及独立 Harness 运行时管理。"""
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


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['serve','scan','onboard','status','install-service','install-runtime','migrate-records'])
    parser.add_argument('--state-dir',default=os.environ.get('DSH_SUPERVISOR_STATE',str(Path.home()/'.dsh/supervisor')))
    parser.add_argument('--source',default=str(ROOT))
    parser.add_argument('--model-profile',default='supervisor-review')
    parser.add_argument('--runtime-version',default='0.1.7-rc.1')
    parser.add_argument('--inherit-policy-from',help='仅继承指定项目的模型、额度及交付时间，不继承业务或运行数据')
    args=parser.parse_args();state=Path(args.state_dir).resolve()
    if args.action=='serve':
        serve(state);return
    control=Control(Store(state));ledger=control.ledger
    if args.action=='status':
        print(json.dumps({'products':control.get('/products'),'metrics':control.get('/autopilot/metrics')},ensure_ascii=False,indent=2));return
    if args.action=='migrate-records':
        from autopilot.project import migrate
        migrate(ledger);print(json.dumps({'status':'pass','schema_version':1}));return
    if args.action=='install-service':
        dest=Path.home()/'Library/LaunchAgents/com.dsh.autopilot.plist';dest.parent.mkdir(parents=True,exist_ok=True)
        log=state/'autopilot/service.log';log.parent.mkdir(parents=True,exist_ok=True)
        value={'Label':'com.dsh.autopilot','ProgramArguments':[sys.executable,str(Path(__file__).resolve()),'serve','--state-dir',str(state)],
               'WorkingDirectory':str(ROOT),'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':10,
               'StandardOutPath':str(log),'StandardErrorPath':str(log),'EnvironmentVariables':{'PATH':os.environ.get('PATH','/usr/bin:/bin')}}
        dest.write_bytes(plistlib.dumps(value));subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(dest)],check=True)
        print(json.dumps({'service':str(dest)}));return
    if args.action=='install-runtime':
        import re
        if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[a-zA-Z0-9.]+)?',args.runtime_version):
            raise ValueError('运行时需要固定的发布版本')
        runtime=state/'autopilot/runtime'
        runtime.mkdir(parents=True,exist_ok=True)
        manifest=runtime/'package.json'
        expected={'name':'autopilot-independent-runtime','private':True,'dependencies':{name:args.runtime_version for name in ('@deepseek-ai/dsh','@deepseek-ai/dsh-base','@deepseek-ai/dsh-headless')}}
        if manifest.exists() and json.loads(manifest.read_text())!=expected:
            raise ValueError('独立运行时已使用其他版本，禁止原位覆盖')
        manifest.write_text(json.dumps(expected,indent=2))
        subprocess.run(['npm','install','--ignore-scripts','--no-audit','--no-fund'],cwd=runtime,check=True,stdout=sys.stderr)
        print(json.dumps({'status':'pass','runtime':str(runtime),'version':args.runtime_version}));return
    from autopilot.onboarding import tick,onboard
    from autopilot.scheduler import Scheduler
    from review_core import Conflict
    import time
    scan=control.mutate('/scans',{'source':args.source})
    scheduler=Scheduler(ledger.store)
    while True:
        scan=ledger.get('scans',scan['id'])
        if scan['status'] not in ('queued','running'):
            break
        try:
            tick(scheduler)
        except Conflict:
            pass  # The resident scheduler won the version check; consume its receipt.
        time.sleep(.25)
    if args.action=='onboard' and scan['status']=='pass':
        scan=onboard(ledger,scan,{})
        product=ledger.get('products',scan['product_id'])
        changes={'worker_runtime':str(state/'autopilot/runtime'),'model_source':{'home':ledger.store.settings()['home'],'profile':args.model_profile},'node':shutil.which('node') or 'node'}
        if args.inherit_policy_from:
            old=ledger.get('products',args.inherit_policy_from)
            changes.update({k:old[k] for k in ('agents','policy','code_review','nightly_attribution','daily_report_enabled') if k in old})
        ledger.update('products',product['id'],product['version'],changes)
    print(json.dumps(scan,ensure_ascii=False,indent=2))
    if scan['status']!='pass':sys.exit(2)


if __name__=='__main__':main()
