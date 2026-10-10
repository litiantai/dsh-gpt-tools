#!/usr/bin/env python3
"""首次安装受管平台及稳定更新器；后续更新由独立更新器处理。"""
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
from autopilot.generic_adapter import validate_manifest
from autopilot.call import atomic


def install(state, manifest_path):
    manifest=validate_manifest(manifest_path)
    state=Path(state).resolve();managed=state/'platform';managed.mkdir(parents=True,exist_ok=True)
    current=managed/'current'
    if current.exists() or current.is_symlink():
        raise ValueError('受管平台基线已存在；后续版本必须通过独立更新器切换')
    from review_core import Store, ACTIVE
    store=Store(state)
    with store.connect() as db:
        if db.execute("SELECT 1 FROM reviews WHERE status IN ('running','queued','awaiting_human') LIMIT 1").fetchone():
            raise ValueError('仍有活动审查，等待结束后再安装')
    stable=managed/'updater';stable.mkdir(exist_ok=True)
    shutil.copy2(ROOT/'scripts/platform-updater.py',stable/'updater.py')
    current.symlink_to(Path(manifest['artifact']).resolve(),target_is_directory=True)
    agents=Path.home()/'Library/LaunchAgents';agents.mkdir(parents=True,exist_ok=True)
    labels=['com.dsh.dashboard','com.dsh.autopilot'];uid=f'gui/{os.getuid()}'
    commands=[[sys.executable,'-B',str(current/'dashboard_server.py'),'--state-dir',str(state)],
              [sys.executable,'-B',str(current/'scripts/autopilot.py'),'serve','--state-dir',str(state)]]
    status_path=state/'status.json'
    bridge_status=json.loads(status_path.read_text()) if status_path.exists() else {}
    if bridge_status.get('protocol')=='dashboard-v1':
        labels.append('com.dsh.bridge')
        commands.append([sys.executable,'-B',str(current/'dsh-gpt-supervisor/scripts/bridge.py'),'run','--state-dir',str(state),'--port',str(store.settings()['bridge_port'])])
    for label,argv in zip(labels,commands):
        path=agents/(label+'.plist')
        if path.exists():shutil.copy2(path,managed/(label+'.previous.plist'))
        log=managed/(label+'.log')
        path.write_bytes(plistlib.dumps({'Label':label,'ProgramArguments':argv,'WorkingDirectory':str(current),
            'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':10,
            'StandardOutPath':str(log),'StandardErrorPath':str(log),
            'EnvironmentVariables':{'PATH':os.environ.get('PATH','/usr/bin:/bin'),'PYTHONDONTWRITEBYTECODE':'1'}}))
    config={'state':str(state),'current':str(current),'origin':'http://127.0.0.1:13084',
            'startup_seconds':90,'observation_seconds':1800,
            'service_plists':{label:str(agents/(label+'.plist')) for label in labels}}
    atomic(stable/'config.json',config)
    label='com.dsh.platform-updater';path=agents/(label+'.plist');log=managed/'updater.log'
    path.write_bytes(plistlib.dumps({'Label':label,'ProgramArguments':[sys.executable,'-B',str(stable/'updater.py'),'--config',str(stable/'config.json')],
        'WorkingDirectory':str(stable),'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':10,
        'StandardOutPath':str(log),'StandardErrorPath':str(log)}))
    # Installer leaves existing unmanaged service shutdown to its caller, before bootstrap.
    atomic(managed/'installation.json',{'manifest':str(manifest_path),'product_id':manifest['product_id'],
        'created':time.time(),'services':labels+[label],'state':'prepared'})
    return {'current':str(current),'launch_agents':[str(agents/(x+'.plist')) for x in labels+[label]]}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--state-dir',required=True);parser.add_argument('--manifest',required=True)
    args=parser.parse_args();print(json.dumps(install(args.state_dir,args.manifest),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
