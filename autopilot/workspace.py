"""源码快照、隔离工作区及可重建的版本证据。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

EXCLUDED = {'.git','node_modules','.pnpm-store','target','dist','lib','.dashboard','.playwright',
            '__pycache__','.cache','secrets','.DS_Store','.env','.env.local'}
SENSITIVE = re.compile(r'(?i)(credentials|connector\.key|login-bridge\.json|hxkline\.json|\.pem$|\.key$|\.env(?:\.|$))')


def git(root, *args):
    return subprocess.check_output(['git','-C',str(root),*args], text=True, stderr=subprocess.PIPE).strip()


def metadata(workspace):
    """返回 (git_dir, common_dir) 的绝对只读元数据路径。

    链接式 worktree 把 HEAD/index/commondir 放在 git_dir，把 objects/refs
    放在 common_dir；两者都可能位于 checkout 之外。`--git-common-dir` 在部分
    git 版本里可能是相对路径，因此同时按 workspace 与 git_dir 解析并取存在者。
    """
    git_dir=Path(git(workspace,'rev-parse','--absolute-git-dir'))
    common=Path(git(workspace,'rev-parse','--git-common-dir'))
    if not common.is_absolute():
        candidates=[(Path(workspace)/common).resolve(),(git_dir/common).resolve()]
        common=next((path for path in candidates if path.exists()), candidates[0])
    else:
        common=common.resolve()
    return git_dir.resolve(), common


def metadata_paths(workspace):
    """只读暴露 HEAD/index/commondir/objects/refs 及裸仓库 config/info/logs；绝不加入可写集合。

    `git --git-dir=<bare> --work-tree=<workspace>` 必须能读取裸仓库的 config，
    否则 checkout 元数据不可读时的差异回退会被沙箱拒绝。config 只进入只读集合，
    绝不进入可写集合。
    """
    git_dir, common_dir = metadata(workspace)
    paths=[git_dir, common_dir]
    paths += [git_dir/name for name in ('HEAD','commondir','index','config','config.worktree','info','logs')]
    paths += [common_dir/name for name in ('objects','refs','packed-refs','config','info','logs')]
    return paths


def source_files(root):
    """尊重 Git ignore，并排除运行数据、秘密文件和越界符号链接。"""
    root = Path(root).resolve()
    config = root/'.autopilot.json'
    if config.is_symlink():
        raise ValueError('项目配置不能是符号链接')
    excluded_paths = json.loads(config.read_text()).get('exclude', []) if config.is_file() else []
    result = subprocess.check_output(['git','-C',str(root),'ls-files','-z','--cached','--others','--exclude-standard'])
    names=result.decode().split('\0')
    # Offline vendor bundles are source dependencies even when a global dist/ ignore matches them.
    for manifest in root.glob('vendor/*/manifest.json'):
        data=json.loads(manifest.read_text())
        for package,entry in data.get('packages',{}).items():
            names += [str((manifest.parent/package/name).relative_to(root)) for name in entry.get('files',{})]
    for name in dict.fromkeys(names):
        rel = Path(name)
        excluded=EXCLUDED-{'dist','lib'} if rel.parts and rel.parts[0]=='vendor' else EXCLUDED
        if not name or rel.is_absolute() or '..' in rel.parts or any(p in excluded for p in rel.parts) or SENSITIVE.search(name):
            continue
        if any(str(rel) == p or str(rel).startswith(p.rstrip('/')+'/') for p in excluded_paths):
            continue
        path = root / rel
        if not path.exists():
            continue
        if path.is_symlink():
            if not path.resolve().is_relative_to(root):
                raise ValueError(f'源码包含越界链接：{name}')
            raise ValueError(f'请先显式处理源码链接：{name}')
        if path.is_file():
            yield rel


def digest(root):
    root = Path(root)
    return hashlib.sha256(json.dumps([(str(p),hashlib.sha256((root/p).read_bytes()).hexdigest())
                                      for p in sorted(source_files(root))]).encode()).hexdigest()


def snapshot(source, destination, tracked_only=False, exclude=()):
    """在新仓库记录当前工作树；不修改来源索引、分支及未提交文件。"""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ValueError('基线目录已存在，禁止覆盖')
    tracked = set(git(source, "ls-files").splitlines()) if tracked_only else None
    files = [p for p in source_files(source) if (tracked is None or str(p) in tracked)
             and not any(str(p) == x or str(p).startswith(x.rstrip("/")+"/") for x in exclude)]
    destination.mkdir(parents=True, mode=0o700)
    try:
        for rel in files:
            target = destination / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source/rel,target)
        git(destination,'init','-b','codex/autonomous')
        git(destination,'config','user.name','Autonomous Development')
        git(destination,'config','user.email','autopilot@localhost')
        git(destination,'add','--all')
        vendor=[str(p) for p in files if p.parts[0]=='vendor']
        if vendor:
            git(destination,'add','--force','--',*vendor)
        git(destination,'commit','-m','Capture local source baseline')
        return {'repository':str(destination),'commit':git(destination,'rev-parse','HEAD'),
                'source':str(source),'files':len(files),'digest':digest(destination)}
    except Exception:
        shutil.rmtree(destination)
        raise


def checkout(repository, destination, run_id):
    destination = Path(destination)
    base = git(repository,'rev-parse','refs/heads/codex/autonomous')
    if not destination.exists():
        git(repository,'worktree','add','-b',f'codex/auto-{run_id}',str(destination),base)
    return {'workspace':str(destination),'base_commit':base}


def commit(workspace):
    # Model dependency installation must never become part of the source commit.
    git(workspace,'rm','-r','--cached','--ignore-unmatch','--','.pnpm-store')
    exclude=Path(git(workspace,'rev-parse','--git-path','info/exclude'))
    exclude.parent.mkdir(parents=True,exist_ok=True)
    text=exclude.read_text() if exclude.exists() else ''
    if '.pnpm-store/' not in text:
        exclude.write_text(text+'\n.pnpm-store/\n')
    git(workspace,'add','--all')
    vendor=[str(p) for p in source_files(workspace) if p.parts[0]=='vendor']
    if vendor:
        git(workspace,'add','--force','--',*vendor)
    if git(workspace,'status','--porcelain'):
        git(workspace,'commit','-m','Autonomous task implementation')
    return git(workspace,'rev-parse','HEAD')


def promote(repository, commit_id, base_commit):
    git(repository,'update-ref','refs/heads/codex/autonomous',commit_id,base_commit)
