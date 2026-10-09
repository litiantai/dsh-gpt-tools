#!/usr/bin/env python3
"""稳定的独立平台更新器；仅使用标准库，不加载候选版本代码。"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import time
from urllib.request import urlopen


def atomic(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False)); temporary.chmod(0o600)
    os.replace(temporary, path)


def checksum(root):
    root = Path(root).resolve(); rows = []
    for p in sorted(root.rglob('*')):
        if p.is_symlink():
            if not p.resolve().is_relative_to(root):
                raise ValueError('候选产物包含越界链接')
            rows.append((str(p.relative_to(root)), 'link:'+str(p.readlink())))
        elif p.is_file():
            rows.append((str(p.relative_to(root)), hashlib.sha256(p.read_bytes()).hexdigest()))
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def connect(state):
    return sqlite3.connect((Path(state)/'dashboard.sqlite3').as_uri()+'?mode=ro', uri=True)


def busy(state):
    """核验独立调用进程与活动审查，不以数据库任务阶段代替进程存活。"""
    calls = Path(state)/'autopilot/calls'
    for p in [*calls.glob('*/process.json'), *calls.glob('*/launch.json')]:
        try:
            value = json.loads(p.read_text())
            command = subprocess.run(['ps', '-p', str(value['pid']), '-o', 'command='], capture_output=True, text=True).stdout
            if 'call.py' in command and str(p.parent/'request.json') in command:
                return True
        except (OSError, ValueError, KeyError):
            return True
    with connect(state) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in tables:
            if table.startswith('auto_') and 'data' in {r[1] for r in db.execute('PRAGMA table_info('+table+')')}:
                for row in db.execute('SELECT data FROM '+table):
                    call = json.loads(row[0]).get('call')
                    if call and not (Path(call['path']).parent/'result.json').exists():
                        return True
        if 'reviews' in tables:
            if db.execute("SELECT 1 FROM reviews WHERE status IN ('running','queued','awaiting_human') OR execution_done=0 LIMIT 1").fetchone():
                return True
    return False


def switch(link, target):
    link = Path(link); temp = link.with_name(link.name+'.next')
    temp.unlink(missing_ok=True); temp.symlink_to(Path(target).resolve(), target_is_directory=True)
    os.replace(temp, link)


def restart(config):
    if config.get('service_plists'):
        for label, path in config['service_plists'].items():
            service=f'gui/{os.getuid()}/'+label
            present=subprocess.run(['launchctl','print',service],capture_output=True).returncode==0
            argv=['launchctl','kickstart','-k',service] if present else ['launchctl','bootstrap',f'gui/{os.getuid()}',path]
            subprocess.run(argv,check=True,capture_output=True,timeout=30)
    else:
        for argv in config['restart_commands']:
            subprocess.run(argv, check=True, capture_output=True, timeout=30)


def stop_services(config):
    if config.get('service_plists'):
        for label in reversed(config['service_plists']):
            service=f'gui/{os.getuid()}/'+label
            if subprocess.run(['launchctl','print',service],capture_output=True).returncode==0:
                subprocess.run(['launchctl','bootout',service],check=True,capture_output=True,timeout=30)
    else:
        for argv in config['stop_commands']:
            subprocess.run(argv, check=True, capture_output=True, timeout=30)


def healthy(config, manifest):
    try:
        with urlopen(config['origin']+'/api/runtime-identity', timeout=3) as response:
            data = json.load(response)
        if data.get('product_id') != manifest['product_id'] or data.get('commit') != manifest['commit']:
            return False
        with connect(config['state']) as db:
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                return False
        # A fresh scheduler heartbeat is required, even while dispatch is drained.
        beat = json.loads((Path(config['state'])/'autopilot/scheduler-heartbeat.json').read_text())
        return time.time()-beat['at'] < 30 and beat.get('commit') == manifest['commit']
    except (OSError, ValueError, KeyError, sqlite3.Error):
        return False


def step(config, path):
    state = Path(config['state']); auto = state/'autopilot'
    job = json.loads(path.read_text()); drain = auto/'update-drain.json'
    def save(**changes):
        job.update(changes); atomic(path, job)
    if job['status'] in ('completed', 'rolled_back', 'blocked'):
        if job['status'] != 'blocked' and drain.exists() and json.loads(drain.read_text()).get('job') == str(path):
            drain.unlink(missing_ok=True)
        return
    try:
        manifest = json.loads(Path(job['manifest']).read_text())
        artifact = Path(manifest['artifact']).resolve()
        if not artifact.is_relative_to(auto.resolve()) or manifest['product_id'] != job['product_id'] or manifest['commit'] != job['expected_commit']:
            raise ValueError('更新请求与候选身份不一致')
        if manifest.get('database_compatibility') != 'backward-compatible':
            raise ValueError('自动更新仅允许向后兼容的数据迁移')
        if job['status'] == 'queued':
            if checksum(artifact) != manifest['artifact_hash'] or not manifest.get('checks') or any(c.get('status') != 'pass' for c in manifest['checks'] if c.get('required', True)):
                raise ValueError('候选完整性或验收检查未通过')
            current = Path(config['current'])
            if not current.is_symlink():
                raise ValueError('首次受管发布基线尚未建立')
            save(status='draining', previous=str(current.resolve()), started=time.time())
        if job['status'] == 'draining':
            with (auto/'dispatch.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                atomic(drain, {'job': str(path), 'at': job['started']})
                if busy(state):
                    save(reason='等待活动调用及审查退出'); return
            # Preserve the intent before stopping services; recovery can repeat these commands.
            save(status='stopping', reason='')
        if job['status'] == 'stopping':
            stop_services(config)
            backup = path.with_suffix('.sqlite3')
            with connect(state) as source, sqlite3.connect(backup) as dest:
                source.backup(dest)
            save(status='switching', backup=str(backup))
        if job['status'] == 'switching':
            if checksum(artifact) != manifest['artifact_hash']:
                raise ValueError('切换前候选产物校验失败')
            switch(config['current'], artifact)
            restart(config)
            save(status='checking', check_started=time.time())
        if job['status'] == 'checking':
            if healthy(config, manifest):
                save(status='observing', healthy_since=time.time())
            elif time.time()-job['check_started'] > config.get('startup_seconds', 90):
                raise ValueError('新版本启动、数据库或调度心跳检查失败')
            return
        if job['status'] == 'observing':
            if not healthy(config, manifest):
                raise ValueError('新版本健康观察失败')
            if time.time()-job['healthy_since'] >= config.get('observation_seconds', 1800):
                save(status='completed', completed_at=time.time(), reason='版本切换与健康观察通过', resumed=True)
                drain.unlink(missing_ok=True)
    except Exception as exc:
        reason = str(exc)
        if job.get('previous'):
            save(status='rolling_back', reason=reason)
            try:
                stop_services(config)
                switch(config['current'], job['previous'])
                # All accepted migrations are backwards-compatible. Never replace live history.
                restart(config)
                old = json.loads((Path(job['previous'])/'autopilot-release.json').read_text())
                save(status='rollback_check', rollback_started=time.time(), rollback_identity=old)
            except Exception as rollback_error:
                save(status='rollback_retry', retry_at=time.time()+30, reason=reason+'；回滚未完成：'+str(rollback_error))
        else:
            save(status='blocked', reason=reason)
            drain.unlink(missing_ok=True)


def reconcile_rollback(config, path):
    job = json.loads(path.read_text()); drain = Path(config['state'])/'autopilot/update-drain.json'
    if job['status'] == 'rollback_retry' and time.time() < job.get('retry_at', 0):
        return
    if job['status'] in ('rolling_back', 'rollback_retry'):
        try:
            stop_services(config); switch(config['current'], job['previous']); restart(config)
            atomic(path, job | {'status':'rollback_check', 'rollback_started':time.time(),
                               'rollback_identity':json.loads((Path(job['previous'])/'autopilot-release.json').read_text())})
        except Exception as exc:
            atomic(path, job | {'status':'rollback_retry', 'retry_at':time.time()+30, 'reason':'回滚恢复失败：'+str(exc)})
        return
    if job['status'] == 'rollback_check':
        if healthy(config, job['rollback_identity']):
            atomic(path, job | {'status':'rolled_back', 'completed_at':time.time()}); drain.unlink(missing_ok=True)
        elif time.time()-job['rollback_started'] > config.get('startup_seconds',90):
            atomic(path, job | {'status':'rollback_retry', 'retry_at':time.time()+30, 'reason':'旧版本回滚后健康检查失败；保持停止派发并重试恢复'})


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--config', required=True); parser.add_argument('--once', action='store_true')
    args = parser.parse_args(); config = json.loads(Path(args.config).read_text())
    root = Path(config['state'])/'autopilot/updates'; root.mkdir(parents=True, exist_ok=True)
    with (root/'updater.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            jobs = sorted(root.glob('*.json'), key=lambda p: p.stat().st_mtime)
            active = [p for p in jobs if json.loads(p.read_text()).get('status') not in ('completed','rolled_back','blocked')]
            # Resume a switched/draining job before accepting another queued version.
            active.sort(key=lambda p: json.loads(p.read_text())['status']=='queued')
            drain = Path(config['state'])/'autopilot/update-drain.json'
            if drain.exists():
                owner = Path(json.loads(drain.read_text())['job'])
                if owner not in jobs:
                    raise ValueError('更新锁引用的切换记录不存在，需核对记录后恢复')
                owner_job = json.loads(owner.read_text())
                if owner_job['status'] == 'blocked' and owner_job.get('previous'):
                    atomic(owner, owner_job | {'status':'rollback_retry', 'retry_at':0})
                active = [owner]
            if active:
                path=active[0]
                if json.loads(path.read_text())['status'] in ('rolling_back','rollback_check','rollback_retry'):
                    reconcile_rollback(config,path)
                else:
                    step(config,path)
            if args.once:
                return
            time.sleep(5)


if __name__ == '__main__':
    main()
