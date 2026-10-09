"""受管 Git 工作区、首次迁移、独立验证与 GitHub 交付操作。"""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import time

from .call import atomic
from .github import GitHub, credential, repository, load_pull_request
from .workspace import git, source_files, digest, SENSITIVE, EXCLUDED

ROOT = Path(__file__).resolve().parents[1]


def network_git(root, *args):
    env = os.environ | {'GIT_TERMINAL_PROMPT': '0'}
    token = credential()
    if 'push' in args and not token:
        raise ValueError('本地运行时未找到 GitHub 凭据，推送已阻塞')
    if token:
        auth = base64.b64encode(('x-access-token:' + token).encode()).decode()
        env.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.https://github.com/.extraheader', GIT_CONFIG_VALUE_0='Authorization: Basic ' + auth)
    proc = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, env=env, timeout=120)
    if proc.returncode:
        if '403' in proc.stderr or 'Permission to ' in proc.stderr:
            raise ValueError('GitHub 拒绝 Git 推送或读取（HTTP 403）；请检查 Token 是否授权当前仓库及 Contents 写入权限')
        raise ValueError('Git 远程操作失败，请核对认证、权限和分支状态')
    return proc.stdout.strip()


def paths(request):
    root = Path(request['state_root']) / 'git-delivery' / request['product']['id']
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    batch = root / request['record']['day']
    batch.mkdir(exist_ok=True, mode=0o700)
    name = 'feat-workspace' if request['record'].get('review_target') == 'feature' else 'release-workspace' if request['record'].get('flow') == 'review_before_release' else 'workspace'
    if request['record'].get('review_target') == 'feature' and request['record'].get('feature_requirement_id'):
        key = hashlib.sha256(request['record']['feature_requirement_id'].encode()).hexdigest()[:24]
        return root, batch, root / 'repository.git', root / 'requirements' / key / 'workspace'
    return root, batch, root / 'repository.git', batch / name


def journal(path):
    return json.loads(path.read_text()) if path.exists() else {}


def backup_source(source, batch):
    target = batch / 'source-backup.tar.gz'
    if target.exists():
        return str(target)
    # Include the index, Git history, ignored user configuration and untracked files.
    # Regenerable dependency/build caches are excluded, never user source.
    skip = {'node_modules', '.pnpm-store', 'target', '__pycache__', '.cache'}
    temporary = target.with_suffix('.tmp')
    with tarfile.open(temporary, 'w:gz') as archive:
        def select(info):
            return None if any(p in skip for p in Path(info.name).parts) else info
        archive.add(source, arcname='source', filter=select)
    temporary.chmod(0o600)
    os.replace(temporary, target)
    with target.open('rb') as saved:
        checksum = hashlib.file_digest(saved, 'sha256').hexdigest()
    atomic(batch / 'backup.json', {'path': str(target), 'sha256': checksum,
        'source': str(source), 'head': git(source, 'rev-parse', 'HEAD'), 'excluded_caches': sorted(skip)})
    return str(target)


def scan_publishable(root, history=False):
    """发布前拒绝已知凭据文件及可识别的真实密钥；错误只包含路径。"""
    secret = re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,}|AKIA[A-Z0-9]{16}')
    rows = git(root, 'rev-list', '--objects', '--all').splitlines() if history else [git(root, 'hash-object', str(p)) + ' ' + str(p) for p in source_files(root)]
    for row in rows:
        sha, _, name = row.partition(' ')
        if not name or git(root, 'cat-file', '-t', sha) != 'blob':
            continue
        if SENSITIVE.search(name) and not name.endswith(('.example', '.sample', '.template')):
            raise ValueError('发布内容包含敏感文件，请先处理：' + name)
        data = subprocess.check_output(['git', '-C', str(root), 'cat-file', 'blob', sha])
        if secret.search(data):
            raise ValueError('发布内容检测到凭据，请先处理：' + name)


def exclude_dependency_cache(repo):
    # Bare repositories return a relative --git-path unless absolute is requested.
    # Keep generated caches out of all linked worktrees without hiding source edits.
    exclude = Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-path', 'info/exclude'))
    exclude.parent.mkdir(parents=True, exist_ok=True)
    ignored = exclude.read_text() if exclude.exists() else ''
    if '.pnpm-store/' not in ignored.splitlines():
        exclude.write_text(ignored + '\n.pnpm-store/\n')


def initialize(request):
    product, record = request['product'], request['record']
    root, batch, repo, workspace = paths(request)
    gh = GitHub(record['git_url'])
    gh.access(write=True)
    state = journal(batch / 'journal.json')
    if not state.get('backup') and (batch / 'backup.json').exists():
        state['backup'] = journal(batch / 'backup.json')['path']
    if not repo.exists():
        source = Path(product['source'])
        backup = backup_source(source, batch)
        git(source, 'clone', '--bare', '--no-hardlinks', str(source), str(repo))
        git(repo, 'config', 'user.name', 'DSH Delivery')
        git(repo, 'config', 'user.email', 'delivery@localhost')
        git(repo, 'remote', 'set-url', 'origin', 'https://github.com/' + repository(record['git_url']) + '.git')
        state['backup'] = backup
        atomic(batch / 'journal.json', state)
    remote = network_git(repo, 'ls-remote', 'origin', 'refs/heads/' + record['base_branch'])
    if not remote:
        if not record.get('bootstrap') or network_git(repo, 'ls-remote', 'origin'):
            raise ValueError('目标分支不存在，不能覆盖已有远程历史')
        scan_publishable(repo, history=True)
        if subprocess.run(['git','-C',str(repo),'show-ref','--verify','--quiet','refs/heads/'+record['base_branch']]).returncode != 0:
            git(repo, 'update-ref', 'refs/heads/' + record['base_branch'], git(product['source'], 'rev-parse', 'HEAD'))
        network_git(repo, 'push', 'origin', 'refs/heads/' + record['base_branch'] + ':refs/heads/' + record['base_branch'])
    network_git(repo, 'fetch', 'origin', 'refs/heads/' + record['base_branch'] + ':refs/remotes/origin/' + record['base_branch'])
    base = git(repo, 'rev-parse', 'refs/remotes/origin/' + record['base_branch'])
    if not workspace.exists():
        git(repo, 'worktree', 'add', '-b', record['branch'], str(workspace), base)
        if subprocess.run(['git', '-C', str(repo), 'show-ref', '--verify', '--quiet', 'refs/heads/' + record['feat_branch']]).returncode:
            git(repo, 'branch', record['feat_branch'], base)
    exclude_dependency_cache(repo)
    if record.get('bootstrap') and not state.get('snapshot_done'):
        source = Path(product['source'])
        before = digest(source)
        selected = list(source_files(source))
        existing = git(workspace, 'ls-files', '-z').split('\0')
        selected_names = {str(p) for p in selected}
        for name in existing:
            if name and name not in selected_names:
                path = workspace / name
                if path.is_file() or path.is_symlink():
                    path.unlink()
        for rel in selected:
            target = workspace / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / rel, target)
        if digest(source) != before:
            raise ValueError('建立基线期间原源码变化，请重试以获取一致快照')
        commit_source(workspace, 'Import existing source baseline')
        state.update(snapshot_done=True, source_digest=before)
        atomic(batch / 'journal.json', state)
    return gh, base


def commit_source(workspace, message):
    # Respect ignore rules, explicitly include legitimate ignored vendor bundles.
    git(workspace, 'add', '--update')
    files = [str(p) for p in source_files(workspace)]
    if files:
        git(workspace, 'add', '--force', '--', *files)
    if git(workspace, 'diff', '--cached', '--name-only'):
        git(workspace, 'commit', '-m', message)
    return git(workspace, 'rev-parse', 'HEAD')


def prepare(request):
    record, product = request['record'], request['product']
    gh, base = initialize(request)
    root, batch, repo, workspace = paths(request)
    state = journal(batch / 'journal.json')
    integrated = state.get('integrated_ids', [])
    for run in request['runs']:
        if run['id'] in integrated:
            continue
        if run['status'] not in ('accepted', 'delivered') or not run.get('commit'):
            raise ValueError('只能交付已通过业务验收的源码')
        if run.get('source_digest') != digest(run['workspace']):
            raise ValueError('任务源码与验收版本不一致，需要重新验收')
        if git(run['workspace'], 'rev-parse', 'HEAD') != run['commit']:
            raise ValueError('任务 HEAD 与验收提交不一致')
        marker = 'Delivery-Run: ' + run['id']
        # A crash after commit but before the journal must not reapply the patch.
        if not git(workspace, 'log', '--format=%H', '--fixed-strings', '--grep=' + marker):
            git(repo, 'fetch', run['workspace'], run['commit'])
            patch = subprocess.check_output(['git', '-C', run['workspace'], 'diff', '--binary', run['base_commit'], run['commit']])
            atomic(batch / 'journal.json', state | {'pending_run': run['id'], 'integrated_ids': integrated})
            applied = subprocess.run(['git', '-C', str(workspace), 'apply', '--3way', '--index'], input=patch, capture_output=True) if patch else None
            if applied is not None and applied.returncode:
                return {'status': 'fail', 'reason': '验收增量与当前基线冲突，需要 Agent 提出方案并解决',
                    'conflicts': git(workspace, 'diff', '--name-only', '--diff-filter=U').splitlines(), 'workspace': str(workspace), 'pending_run': run['id']}
            commit_source(workspace, marker)
            if not git(workspace, 'log', '--format=%H', '--fixed-strings', '--grep=' + marker):
                git(workspace, 'commit', '--allow-empty', '-m', marker)
        integrated.append(run['id'])
        state = journal(batch / 'journal.json') | {'integrated_ids': integrated, 'pending_run': None}
        atomic(batch / 'journal.json', state)
    head = git(workspace, 'rev-parse', 'HEAD')
    git(repo, 'update-ref', 'refs/heads/' + record['feat_branch'], head)
    scan_publishable(workspace, history=True)
    network_git(repo, 'push', 'origin', 'refs/heads/' + record['feat_branch'], 'refs/heads/' + record['branch'])
    checkpoint = {'repository': str(repo), 'workspace': str(workspace), 'head_sha': head, 'base_sha': base,
        'integrated_ids': integrated, 'backup': state.get('backup')}
    atomic(batch / 'journal.json', state | {'delivery_checkpoint': checkpoint})
    body = '\n'.join(['业务验收成果进入代码交付。北京时间零点收口，代码评审与必需检查通过后自动合入。', '',
        *[f"- {run['title']} · {run['commit']}" for run in request['runs']], '', '基线源码整理' if record.get('bootstrap') else '',
        '代码评审、问题修复与逐轮证据在监工看板关联记录。'])
    pr = gh.pull_request(record['branch'], record['base_branch'], record['title'], body)
    checkpoint.update(pr_number=pr['number'], pr_url=pr['html_url'])
    atomic(batch / 'journal.json', state | {'delivery_checkpoint': checkpoint})
    gh.status(head, 'pending', '等待独立代码评审与验证')
    return {'status': 'pass', **checkpoint}


def sync(request):
    record = request['record']
    _, batch, repo, workspace = paths(request)
    gh, pr = load_pull_request(record)
    if pr.get('merged'):
        raise ValueError('PR 已在外部合并，请核对后恢复合并确认步骤')
    if pr.get('state') != 'open':
        raise ValueError('已保存的 PR 已关闭，不能继续同步')
    exclude_dependency_cache(repo)
    if git(workspace, 'status', '--porcelain'):
        raise ValueError('交付工作区存在未提交修改，需先完成修复')
    network_git(repo, 'fetch', 'origin', 'refs/heads/' + record['base_branch'] + ':refs/remotes/origin/' + record['base_branch'])
    base = git(repo, 'rev-parse', 'refs/remotes/origin/' + record['base_branch'])
    if pr['head']['sha'] != git(workspace, 'rev-parse', 'HEAD'):
        # External edits are incorporated, never overwritten.
        remote_ref = 'refs/remotes/origin/' + record['branch']
        network_git(repo, 'fetch', 'origin', 'refs/heads/' + record['branch'] + ':' + remote_ref)
        upstream = subprocess.run(['git', '-C', str(workspace), 'merge', '--no-edit', remote_ref], capture_output=True, text=True)
        if upstream.returncode:
            return {'status': 'fail', 'reason': 'PR 外部修改与本地修复冲突，需要解决后重新评审',
                'conflicts': git(workspace, 'diff', '--name-only', '--diff-filter=U').splitlines()}
    merged = subprocess.run(['git', '-C', str(workspace), 'merge', '--no-edit', base], capture_output=True, text=True)
    if merged.returncode:
        return {'status': 'fail', 'reason': 'master 合并冲突，需记录解决方案并修复',
            'conflicts': git(workspace, 'diff', '--name-only', '--diff-filter=U').splitlines()}
    head = git(workspace, 'rev-parse', 'HEAD')
    network_git(repo, 'push', 'origin', record['branch'])
    if head != record.get('head_sha') or base != record.get('base_sha'):
        gh.status(head, 'pending', '目标分支变化，等待重新验证与评审')
    return {'status': 'pass', 'base_sha': base, 'head_sha': head}


def merge(request):
    record = request['record']
    gh, pr = load_pull_request(record)
    if pr.get('merged'):
        if pr['head']['sha'] != record.get('review_pass', {}).get('head_sha'):
            raise ValueError('外部合并版本与评审证据不一致，需人工核对')
        return {'status': 'pass', 'merged': True, 'merge_sha': pr['merge_commit_sha']}
    proof = record.get('review_pass') or {}
    validation = record.get('validation_pass') or {}
    if any(evidence.get('head_sha') != pr['head']['sha'] or evidence.get('base_sha') != pr['base']['sha'] for evidence in (proof, validation)):
        return {'status': 'pass', 'stale': True}
    if not record.get('frozen') or time.time() < record['cutoff']:
        return {'status': 'busy', 'reason': '等待北京时间零点收口'}
    if not gh.checks_pass(pr['head']['sha']):
        return {'status': 'busy', 'reason': '等待 GitHub 检查通过'}
    if pr.get('mergeable') is not True or pr.get('mergeable_state') != 'clean':
        return {'status': 'busy', 'reason': '等待 GitHub 确认分支保护、必需评审与合并条件满足'}
    merged = gh.merge(record['pr_number'], proof['head_sha'])
    return {'status': 'pass', 'merged': True, 'merge_sha': merged['merge_commit_sha']}


def execute(action, request):
    root, batch, repo, workspace = paths(request)
    with (root / 'delivery.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if request['record'].get('flow') == 'review_before_release':
            from .staged_delivery import execute as staged_execute
            return staged_execute(action, request)
        if action in ('prepare', 'sync', 'merge'):
            try:
                return {'prepare': prepare, 'sync': sync, 'merge': merge}[action](request)
            except Exception as exc:
                checkpoint = journal(batch / 'journal.json').get('delivery_checkpoint', {}) if action == 'prepare' else {}
                return {'status': 'blocked', 'reason': str(exc), **checkpoint}
        from .delivery_review import execute as review
        return review(action, request)
