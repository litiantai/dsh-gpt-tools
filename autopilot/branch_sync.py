"""通过独立工作区把 master 合入 feat/release，保存终端日志和先方案后修复的记录。"""
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

from review_core import Conflict, Store, dispatch_lock
from .call import atomic
from .store import Ledger, redact
from .workspace import git
from .delivery_worker import network_git

ROOT = Path(__file__).resolve().parents[1]
ACTIVE = {'queued', 'running', 'planning', 'resolving'}


def directory(state, product_id):
    return Path(state)/'autopilot/master-sync'/product_id


def snapshot(ledger, product):
    root = directory(ledger.store.state, product['id'])
    latest = root/'latest.json'
    if not latest.exists():
        return {'status': 'idle', 'events': [], 'branches': []}
    ident = json.loads(latest.read_text())['id']
    uuid.UUID(ident)
    folder = root/ident
    job = json.loads((folder/'job.json').read_text())
    receipt = ledger.store.state/'autopilot/calls'/ident/'result.json'
    if job['status'] in ACTIVE and receipt.exists():
        result = json.loads(receipt.read_text())
        job.update(status='blocked', reason=result.get('reason', '执行中断，请核对日志后重新同步'))
    if job['status'] in ACTIVE and time.time()-job['started'] > 30:
        process = ledger.store.state/'autopilot/calls'/ident/'process.json'
        pid = json.loads(process.read_text()).get('pid') if process.exists() else json.loads((receipt.parent/'launch.json').read_text()).get('pid') if (receipt.parent/'launch.json').exists() else None
        command = subprocess.run(['ps', '-p', str(pid or 0), '-o', 'command='], capture_output=True, text=True).stdout
        if str(ledger.store.state/'autopilot/calls'/ident/'request.json') not in command:
            job.update(status='blocked', reason='执行进程已退出；保留各分支结果，重新同步前会核对真实 Git 状态')
    events = folder/'terminal.jsonl'
    job['events'] = []
    for line in events.read_text().splitlines()[-250:] if events.exists() else []:
        try: job['events'].append(json.loads(line))
        except json.JSONDecodeError: pass  # A concurrent append can leave the last line incomplete.
    return job


def start(ledger, product):
    if not product.get('git', {}).get('enabled') or not product.get('delivery_repository'):
        raise Conflict('请先完成 Git 仓库接入')
    root = directory(ledger.store.state, product['id']); root.mkdir(parents=True, exist_ok=True)
    with dispatch_lock(ledger.store.state), (root/'start.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (ledger.store.state/'autopilot/update-drain.json').exists():
            raise Conflict('平台更新中，结束观察后可同步分支')
        previous = snapshot(ledger, product)
        if previous['status'] in ACTIVE:
            return previous
        ident = str(uuid.uuid4()); folder = root/ident; folder.mkdir(mode=0o700)
        calls = ledger.store.state/'autopilot/calls'/ident; calls.mkdir(parents=True, mode=0o700)
        job = {'id': ident, 'product_id': product['id'], 'status': 'queued', 'started': time.time(),
               'branches': [], 'reason': '等待同步 master 到 feat / release'}
        atomic(folder/'job.json', job); atomic(root/'latest.json', {'id': ident})
        request = {'command': [sys.executable, '-B', str(ROOT/'scripts/sync-master.py')],
                   'timeout': 14400, 'cwd': str(ROOT),
                   'input': {'product': product, 'id': ident, 'state_root': str(ledger.store.state/'autopilot')}}
        path = calls/'request.json'; atomic(path, request)
        child = subprocess.Popen([sys.executable, '-B', str(ROOT/'autopilot/call.py'), str(path)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        atomic(calls/'launch.json', {'pid': child.pid, 'started': time.time(), 'request': str(path)})
        return job | {'events': []}


def worktrees(repo):
    rows = {}; path = None
    for line in git(repo, 'worktree', 'list', '--porcelain').splitlines():
        if line.startswith('worktree '): path = line[9:]
        if line.startswith('branch refs/heads/'): rows[line[18:]] = Path(path)
    return rows


def busy(ledger, product_id, branch, workspace):
    for kind in ('runs', 'deliveries'):
        for record in ledger.list(kind):
            if record['product_id'] != product_id or not record.get('call'):
                continue
            if branch in (record.get('branch'), record.get('feat_branch')) or workspace and record.get('workspace') == str(workspace):
                # Wait for the controller to consume its call, including commit/finalization.
                return True
    return False


def resolve(request, workspace, folder, conflicts, emit):
    from .delivery import config
    from .delivery_review import model
    folder.mkdir(parents=True, exist_ok=True)
    selection = config(request['product']).get('fixer')
    if not selection:
        raise ValueError('未配置冲突修复模型；冲突工作区已保留')
    context = request | {'record': {'id': request['id'], 'selection': selection, 'conflict_resolution': True}}
    details = '\n'.join(conflicts)
    plan = model(context, folder/'plan', '只读分析当前 Git 合并冲突，先提出逐文件处理方案，说明如何保留分支与 master 双方有效改动。不要修改文件。方案可执行即返回 pass，把逐文件步骤写入 summary，现有冲突不代表方案失败。冲突文件：\n'+details, workspace=workspace)
    if plan.get('status') != 'pass': raise ValueError(plan.get('reason') or '未能提出冲突处理方案')
    atomic(folder/'plan.json', plan)
    emit('plan', '冲突处理方案', plan)
    emit('status', '方案已记录，开始按方案处理冲突')
    result = model(context, folder/'resolve', '按已经记录的方案处理冲突；仅修改冲突文件，不运行测试、提交或推送。移除冲突标记，保留双方有效逻辑。\n'+json.dumps(plan, ensure_ascii=False), write=True, workspace=workspace)
    if result.get('status') != 'pass': raise ValueError(result.get('reason') or '冲突处理失败')
    changed = set(git(workspace, 'diff', '--name-only').splitlines())
    changed.update(git(workspace, 'ls-files', '--others', '--exclude-standard').splitlines())
    if changed-set(conflicts): raise ValueError('冲突修复修改了范围外文件，保留工作区供核对')
    for name in conflicts:
        path = workspace/name
        if path.is_file() and any(line.startswith(('<<<<<<< ', '=======', '>>>>>>> ')) for line in path.read_text(errors='replace').splitlines()):
            raise ValueError('仍有未处理的冲突标记：'+name)
    emit('command', '$ git add -- ' + ' '.join(conflicts))
    git(workspace, 'add', '--', *conflicts)
    if git(workspace, 'diff', '--name-only', '--diff-filter=U'):
        raise ValueError('冲突尚未全部解决')
    git(workspace, 'diff', '--cached', '--check')
    emit('command', '$ git commit --no-edit')
    git(workspace, '-c', 'user.name=DSH Branch Sync', '-c', 'user.email=local@localhost', 'commit', '--no-edit')
    emit('result', '冲突处理已提交；这是分支同步，不代表业务测试已通过')


def execute(request):
    product = request['product']; state = Path(request['state_root']).parent
    ledger = Ledger(Store(state)); root = directory(state, product['id'])/request['id']
    job = json.loads((root/'job.json').read_text()); repo = product['delivery_repository']
    def emit(kind, text, detail=None):
        if kind == 'status':
            save(status='resolving', reason=text)
        with (root/'terminal.jsonl').open('a') as stream:
            stream.write(json.dumps(redact({'at': time.time(), 'kind': kind, 'text': text, 'detail': detail}), ensure_ascii=False)+'\n')
    def save(**changes):
        job.update(changes, updated=time.time()); atomic(root/'job.json', job)
    def command(workspace, *args, remote=False):
        emit('command', '$ git '+ ' '.join(args))
        try:
            result = network_git(workspace, *args) if remote else git(workspace, *args)
        except subprocess.CalledProcessError as exc:
            emit('output', (exc.stdout or '') + (exc.stderr or ''))
            raise
        if result: emit('output', result)
        return result
    try:
        delivery_root = Path(request['state_root'])/'git-delivery'/product['id']; delivery_root.mkdir(parents=True, exist_ok=True)
        with (delivery_root/'delivery.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            save(status='running', reason='获取最新 master 和分支')
            command(repo, 'fetch', '--prune', 'origin', '+refs/heads/*:refs/remotes/origin/*', remote=True)
            master_branch = product.get('git', {}).get('base_branch', 'master')
            master = git(repo, 'rev-parse', 'refs/remotes/origin/'+master_branch)
            save(master_commit=master)
            refs = dict(line.split(' ', 1) for line in git(repo, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads', 'refs/remotes/origin').splitlines())
            names = sorted({ref.removeprefix('refs/heads/').removeprefix('refs/remotes/origin/') for ref in refs
                            if ref.removeprefix('refs/heads/').removeprefix('refs/remotes/origin/').startswith(('feat-', 'release-'))})
            names = [name for name in names if name != master_branch]
            trees = worktrees(repo)
            for index, branch in enumerate(names):
                row = {'branch': branch, 'status': 'running'}; job['branches'].append(row); save(reason='同步 '+branch)
                original = trees.get(branch); local = refs.get('refs/heads/'+branch); remote = refs.get('refs/remotes/origin/'+branch)
                workspace = root/('branch-'+str(index)); keep = False
                try:
                    if busy(ledger, product['id'], branch, original): raise ValueError('分支仍有开发或验收任务执行，保留工作区，完成后可重新同步')
                    if original and git(original, 'status', '--porcelain'): raise ValueError('工作区有未提交改动，保留改动，提交后可重新同步')
                    start_sha = local or remote
                    command(repo, 'worktree', 'add', '--detach', str(workspace), start_sha)
                    for incoming in dict.fromkeys([remote, master]):
                        if not incoming: continue
                        try: command(workspace, '-c', 'user.name=DSH Branch Sync', '-c', 'user.email=local@localhost', 'merge', '--no-edit', incoming)
                        except subprocess.CalledProcessError:
                            conflicts = git(workspace, 'diff', '--name-only', '--diff-filter=U').splitlines()
                            if not conflicts: raise
                            keep = True; save(status='planning'); emit('conflict', branch+' 出现冲突', conflicts)
                            resolve(request, workspace, root/('resolution-'+str(index)), conflicts, emit)
                            save(status='running'); keep = False
                    head = git(workspace, 'rev-parse', 'HEAD')
                    with dispatch_lock(state):
                        if busy(ledger, product['id'], branch, original): raise ValueError('同步期间分支开始执行任务，未更新目标分支')
                        if worktrees(repo).get(branch) != original:
                            raise ValueError('同步期间分支工作区发生变化，未更新目标分支')
                        if original:
                            if git(original, 'symbolic-ref', '--short', 'HEAD') != branch or git(original, 'rev-parse', 'HEAD') != local or git(original, 'status', '--porcelain'):
                                raise ValueError('同步期间原工作区发生变化，未覆盖改动')
                            command(original, 'merge', '--ff-only', head)
                        else:
                            command(repo, 'update-ref', 'refs/heads/'+branch, head, local or '0'*40)
                    if remote and head != remote:
                        # Ordinary push rejects concurrent remote updates; never force a branch.
                        command(repo, 'push', 'origin', head+':refs/heads/'+branch, remote=True)
                    row.update(status='pass', commit=head, remote_updated=bool(remote), reason='已同步 master' if head != start_sha else '已包含最新 master')
                    emit('result', branch+'：'+row['reason'])
                except Exception as exc:
                    row.update(status='blocked', reason=str(exc), workspace=str(workspace) if keep else None)
                    emit('error', branch+'：'+str(exc))
                finally:
                    if workspace.exists() and not keep:
                        git(repo, 'worktree', 'remove', '--force', str(workspace))
                    save()
            failed = any(row['status'] != 'pass' for row in job['branches'])
            save(status='blocked' if failed else 'completed', reason='部分分支需要处理，见终端记录' if failed else '全部 feat / release 已同步最新 master' if names else '没有 feat / release 分支需要同步')
            emit('result', job['reason'])
    except Exception as exc:
        save(status='blocked', reason=str(exc)); emit('error', str(exc))
    return {'status': 'pass' if job['status']=='completed' else 'blocked', 'reason': job['reason'], 'job_id': job['id']}
