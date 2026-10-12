"""以固定主分支提交启动巡检隔离实例，沿用项目已有测试凭据。"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import time
from urllib.parse import urlsplit

from .workspace import git


@contextmanager
def desktop_instance(request, workspace, root):
    """旧 thsoctop 适配器复用构建和隔离运行时，退出后回收本轮进程。"""
    from .thsoctop import command, bind_runtime_sdk, stage_runtime
    from .test_instance import slot, record
    from .isolated import stop
    from .sandbox import restrict
    product=request['product'];commit=request['record']['target_commit']
    root.mkdir(parents=True,exist_ok=True)
    with slot(request['state_root'],product):
        env={'THSOCTOP_AUTOPILOT_ALLOWED_ROOT':str(root),'npm_config_cache':str(root/'npm-cache')}
        commands=[['pnpm','install','--frozen-lockfile','--store-dir',str(root/'pnpm-store')],
                  ['pnpm','run','build'],['pnpm','--filter','@thsoctop/desktop','run','build']]
        for index,argv in enumerate(commands):
            check=command(argv,workspace,env,root/f'build-{index}.log')
            if check['status']!='pass':
                raise ValueError('隔离巡检构建未完成：'+check['summary'])
            if index==0:
                bind_runtime_sdk(workspace,product)
        env=stage_runtime(workspace,root,product)
        original=workspace/'apps/desktop/src-tauri/target/release/bundle/macos/thsoctop.app'
        application=root/'application/thsoctop.app'
        shutil.copytree(original,application,symlinks=True)
        info=application/'Contents/Info.plist';metadata=plistlib.loads(info.read_bytes())
        bundle_id='com.dsh.inspection.'+request['record']['id']
        metadata.update(CFBundleIdentifier=bundle_id,CFBundleName='thsoctop 巡检版')
        info.write_bytes(plistlib.dumps(metadata))
        subprocess.run(['codesign','--force','--deep','--sign','-',str(application)],check=True,capture_output=True)
        ready=Path(env['THSOCTOP_APP_SUPPORT'])/'host-ready.private.json';ready.unlink(missing_ok=True)
        proc=None;environment={'commit':commit,'application':str(application),'support':env['THSOCTOP_APP_SUPPORT'],
            'home':env['THSOCTOP_DSH_HOME'],'runtime':env['THSOCTOP_RUNTIME_DIR'],'instance_role':'isolated','status':'starting'}
        try:
            with (root/'desktop.log').open('w') as stream:
                platform_dirs=[Path.home()/'Library'/kind/bundle_id for kind in ('WebKit','Caches','HTTPStorages')]
                platform_dirs.append(Path.home()/'Library/Saved Application State'/(bundle_id+'.savedState'))
                argv=restrict([str(application/'Contents/MacOS/thsoctop')],[root,Path('/private/tmp'),*platform_dirs],root/'desktop.sb')
                proc=subprocess.Popen(argv,env=os.environ|env,
                    stdout=stream,stderr=stream,start_new_session=True)
            environment['pid']=proc.pid;record(request['state_root'],product['id'],{k:v for k,v in environment.items() if k!='entry_url'})
            deadline=time.monotonic()+60
            while not ready.exists() and proc.poll() is None and time.monotonic()<deadline:
                time.sleep(.2)
            if not ready.exists() or proc.poll() is not None:
                raise ValueError('主分支隔离巡检实例未就绪')
            entry=json.loads(ready.read_text())['url'];url=urlsplit(entry)
            if url.scheme!='http' or url.hostname not in ('127.0.0.1','localhost'):
                raise ValueError('隔离桌面入口不是本机地址')
            environment.update(origin=f'{url.scheme}://{url.netloc}',entry_url=entry,status='ready',
                auth_ready=(Path(environment['home'])/'thsoctop/credentials.json').is_file())
            record(request['state_root'],product['id'],{k:v for k,v in environment.items() if k!='entry_url'})
            yield environment
            if git(workspace,'rev-parse','HEAD')!=commit or proc.poll() is not None:
                raise ValueError('巡检期间隔离实例已变化或退出')
        finally:
            if proc:
                stop(proc)
            record(request['state_root'],product['id'],{k:v for k,v in environment.items() if k!='entry_url'} | {'status':'stopped'})
