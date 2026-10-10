"""先评审 feat → release MR，23:30 封板后对每日 release 统一验证。"""
import copy
import hashlib
import datetime as dt
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

from .call import atomic
from .delivery_worker import paths, journal, initialize, commit_source, network_git, scan_publishable, sync, merge
from .github import GitHub, load_pull_request
from .workspace import git, digest, source_files


def feature_request(request):
    """代码评审和修复只读取已保存的 feat → release MR。"""
    b = request['record']
    return request | {'record': b | {'review_target': 'feature', 'branch': b['feat_branch'],
        'base_branch': b['branch'], 'pr_url': b.get('feature_pr_url'), 'pr_number': b.get('feature_pr_number'),
        'head_sha': b.get('feature_head_sha'), 'base_sha': b.get('feature_base_sha')}}


def snapshot_source(source, workspace):
    before = digest(source)
    selected = list(source_files(source))
    names = {str(p) for p in selected}
    for name in git(workspace, 'ls-files', '-z').split('\0'):
        if name and name not in names:
            path = workspace / name
            if path.is_file() or path.is_symlink():
                path.unlink()
    for rel in selected:
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, target)
    if before != digest(source):
        raise ValueError('首次源码基线建立期间源文件变化，请重试')
    commit_source(workspace, 'Import existing source baseline')
    return before


def prepare(request):
    b = request['record']
    gh, base = initialize(request | {'record': b | {'bootstrap': False}}) if not b.get('bootstrap') else initialize_seed(request)
    _, folder, repo, release = paths(request)
    feature = paths(feature_request(request))[3]
    if not feature.exists():
        feature.parent.mkdir(parents=True, exist_ok=True)
        if subprocess.run(['git', '-C', str(repo), 'show-ref', '--verify', '--quiet', 'refs/heads/' + b['feat_branch']]).returncode:
            git(repo, 'branch', b['feat_branch'], git(release, 'rev-parse', 'HEAD'))
        git(repo, 'worktree', 'add', str(feature), b['feat_branch'])
    state = journal(folder / 'staged.json')
    # release is created and pushed from master before receiving any feature code.
    network_git(repo, 'push', 'origin', b['branch'])
    if b.get('bootstrap') and not state.get('snapshot_done'):
        carried = any(r.get('delivery_carry') for r in request['runs'])
        source_digest = None if carried else snapshot_source(Path(request['product']['source']), feature)
        state |= {'snapshot_done': True, 'source_digest': source_digest}
        atomic(folder / 'staged.json', state)
    pending = [r for r in request['runs'] if r['id'] not in b.get('integrated_ids', [])]
    if b.get('feature_requirement_id'):
        pending = [r for r in pending if r['requirement_id'] == b['feature_requirement_id']][:1]
        if not pending:
            raise ValueError('当前需求没有可交付任务，不能复用其他需求的 PR')
    elif not b.get('feature_pr_url'):
        pending = pending[:1]
    if b.get('frozen') or time.time() >= b['cutoff']:
        return {'status': 'deferred', 'reason': '23:30 已封板，不再创建新的代码评审'}
    release_head = git(release, 'rev-parse', 'HEAD')
    joined = subprocess.run(['git', '-C', str(feature), 'merge', '--no-edit', release_head], capture_output=True)
    if joined.returncode:
        return {'status': 'fail', 'reason': 'feat 同步 release 冲突，需要修复后评审', 'repair_target': 'feature'}
    for run in pending:
        if run['status'] != 'accepted' or not run.get('commit'):
            raise ValueError('feat 只能纳入业务验收通过的任务')
        marker = 'Delivery-Run: ' + run['id']
        if git(feature, 'log', '--format=%H', '--fixed-strings', '--grep=' + marker):
            continue
        if run.get('source_digest') != digest(run['workspace']) or git(run['workspace'], 'rev-parse', 'HEAD') != run['commit']:
            raise ValueError('任务源码与业务验收提交不一致')
        carry = run.get('delivery_carry')
        source, start, end = (carry['repository'], carry['base_sha'], carry['head_sha']) if carry else (run['workspace'], run['base_commit'], run['commit'])
        group_marker = 'Deferred-Delivery: ' + carry['group'] if carry else marker
        if not git(feature, 'log', '--format=%H', '--fixed-strings', '--grep=' + group_marker):
            git(repo, 'fetch', source, end)
            patch = subprocess.check_output(['git', '-C', source, 'diff', '--binary', start, end])
            atomic(folder / 'journal.json', journal(folder / 'journal.json') | {'pending_run': run['id']})
            applied = subprocess.run(['git', '-C', str(feature), 'apply', '--3way', '--index'], input=patch, capture_output=True) if patch else None
            if applied is not None and applied.returncode:
                return {'status': 'fail', 'reason': '验收增量与 feat 冲突，需要修复后代码评审', 'repair_target': 'feature'}
            commit_source(feature, group_marker)
        if not git(feature, 'log', '--format=%H', '--fixed-strings', '--grep=' + marker):
            git(feature, 'commit', '--allow-empty', '-m', marker)
        atomic(folder / 'journal.json', journal(folder / 'journal.json') | {'pending_run': None})
    head = git(feature, 'rev-parse', 'HEAD')
    if not git(feature, 'diff', '--name-only', release_head, head):
        return {'status': 'blocked', 'reason': '没有可评审代码增量，未创建空 MR'}
    scan_publishable(feature, history=True)
    network_git(repo, 'push', 'origin', b['feat_branch'])
    title = pending[0]['title'] if len(pending) == 1 else b['title']
    body = '业务验收通过的需求独立评审，通过后合入 release。\n\n' + '\n'.join('- ' + r['title'] for r in pending)
    if b.get('feature_pr_number'):
        pr = gh.api('/pulls/' + str(b['feature_pr_number']))
        if pr.get('merged') or pr.get('state') != 'open':
            raise ValueError('该需求 PR 已合并或关闭；保留原记录，禁止重复创建')
        if pr['head']['ref'] != b['feat_branch']:
            raise ValueError('需求 PR 源分支与登记分支不一致')
        pr = gh.api('/pulls/' + str(b['feature_pr_number']), 'PATCH', {'base': b['branch'], 'title': title + ' · Code Review', 'body': body})
    else:
        pr = gh.pull_request(b['feat_branch'], b['branch'], title + ' · Code Review', body,
            successive=not bool(b.get('feature_requirement_id')))
    result = {'status': 'pass', 'repository': str(repo), 'workspace': str(release),
        'feature_requirement_id': b.get('feature_requirement_id'),
        'base_sha': base, 'head_sha': release_head, 'feature_head_sha': head, 'feature_base_sha': release_head,
        'feature_pr_url': pr['html_url'], 'feature_pr_number': pr['number'], 'pending_ids': [r['id'] for r in pending]}
    atomic(folder / 'staged.json', state | {'checkpoint': result})
    gh.status(head, 'pending', '等待 feat → release 代码评审；此阶段不启动客户端')
    return result


def initialize_seed(request):
    """允许首次初始化空 master，但将未提交源码只导入 feat。"""
    _, folder, _, _ = paths(request)
    path = folder / 'journal.json'
    old = journal(path)
    atomic(path, old | {'snapshot_done': True})
    return initialize(request)


def merge_feature(request):
    b = request['record']
    target = feature_request(request)['record']
    gh, pr = load_pull_request(target)
    proof = b.get('feature_review_pass') or {}
    if pr.get('merged'):
        if pr['head']['sha'] != proof.get('head_sha'):
            raise ValueError('feat MR 外部合并版本与评审证据不一致')
    else:
        if b.get('frozen') or time.time() >= b['cutoff']:
            return {'status': 'deferred', 'reason': '23:30 已封板，未合入成果移至次日'}
        if proof.get('head_sha') != pr['head']['sha'] or proof.get('base_sha') != pr['base']['sha']:
            return {'status': 'stale', 'stale': True, 'reason': 'feat 或 release 变化，必须重新评审'}
        if not gh.checks_pass(pr['head']['sha']) or pr.get('mergeable') is not True or pr.get('mergeable_state') != 'clean':
            return {'status': 'busy', 'reason': '等待 feat MR 检查和仓库保护规则满足'}
        pr = gh.merge(pr['number'], proof['head_sha'])
    _, folder, repo, release = paths(request)
    network_git(repo, 'fetch', 'origin', 'refs/heads/' + b['branch'] + ':refs/remotes/origin/' + b['branch'])
    git(release, 'merge', '--ff-only', 'refs/remotes/origin/' + b['branch'])
    head = git(release, 'rev-parse', 'HEAD')
    if head != pr['merge_commit_sha']:
        raise ValueError('release 存在未记录的新提交，需要核对后重新评审')
    # The daily outbound PR is created only after the first reviewed contribution.
    if b.get('pr_url'):
        _, final_pr = load_pull_request(b)
        if final_pr.get('state') != 'open':
            raise ValueError('当天上线 PR 已关闭或合并，不能重复创建')
    else:
        final_pr = gh.pull_request(b['branch'], b['base_branch'], b['title'],
            '每日验证分支，23:30（北京时间）封板后统一验证，通过后合入 master。\n\n代码评审 MR：' + b['feature_pr_url'], successive=True)
    result = {'status': 'pass', 'merged': True, 'feature_merge_sha': pr['merge_commit_sha'], 'head_sha': head,
        'pr_url': final_pr['html_url'], 'pr_number': final_pr['number'], 'integrated_ids': list(dict.fromkeys(b.get('integrated_ids', []) + b.get('pending_ids', []))),
        'merged_feature': {'pr_url': b['feature_pr_url'], 'pr_number': b['feature_pr_number'], 'merge_sha': pr['merge_commit_sha'],
            'review': proof, 'run_ids': b.get('pending_ids', [])}}
    atomic(folder / 'staged.json', journal(folder / 'staged.json') | {'merged_checkpoint': result})
    gh.status(head, 'pending', '已通过代码评审，等待 23:30 封板后统一验证')
    return result


def execute(action, request):
    from .delivery_review import execute as review, verify
    b = request['record']
    try:
        if action == 'prepare':
            return prepare(request)
        if action in ('review_feature', 'repair_feature'):
            target = feature_request(request)
            # A pre-MR conflict has no PR to load yet.
            if not b.get('feature_pr_url'):
                target['record']['pr_url'] = None
            result = review('review' if action == 'review_feature' else 'repair', target)
            if action == 'review_feature' and result.get('status') == 'pass':
                GitHub(b['git_url']).status(result['head_sha'], 'success', 'feat → release 代码评审通过', target_url=result.get('report_url'))
            return result
        if action == 'merge_feature':
            return merge_feature(request)
        if action == 'sync_feature':
            return sync(feature_request(request))
        if action in ('review_release', 'repair_release'):
            return review('review' if action == 'review_release' else 'repair', request)
        if action == 'sync_release':
            return sync(request)
        if action == 'validate_release':
            if not b.get('frozen') or time.time() < b['cutoff']:
                return {'status': 'busy', 'reason': '等待 23:30 封板后统一验证'}
            gh, pr = load_pull_request(b)
            proof = b.get('release_sync_pass') or {}
            if proof.get('head_sha') != pr['head']['sha'] or proof.get('base_sha') != pr['base']['sha']:
                return {'status': 'stale', 'stale': True, 'reason': '统一验证前 PR 版本变化'}
            _, folder, _, workspace = paths(request)
            before = digest(workspace)
            if git(workspace, 'rev-parse', 'HEAD') != pr['head']['sha']:
                raise ValueError('验证工作区与 release 不一致')
            evidence = folder / 'validations' / b['validation_id']
            evidence.mkdir(parents=True, exist_ok=True)
            gh.status(pr['head']['sha'], 'pending', 'release 已封板，统一运行验证中')
            checked = verify(request | {'budget_kind': 'code_delivery_tokens'}, workspace, pr['head']['sha'], pr['base']['sha'], evidence)
            _, after = load_pull_request(b)
            if before != digest(workspace) or after['head']['sha'] != pr['head']['sha'] or after['base']['sha'] != pr['base']['sha']:
                return {'status': 'stale', 'stale': True, 'reason': '统一验证期间源码变化，证据失效'}
            gh.status(pr['head']['sha'], 'success' if checked['status'] == 'pass' else 'failure',
                'release 统一验证通过' if checked['status'] == 'pass' else '统一验证：' + checked.get('reason', '必需检查未通过'))
            return checked | {'head_sha': pr['head']['sha'], 'base_sha': pr['base']['sha']}
        if action == 'merge_release':
            return merge(request)
        raise ValueError('未知每日交付步骤')
    except Exception as exc:
        checkpoint = journal(paths(request)[1] / 'staged.json').get('checkpoint', {}) if action == 'prepare' else {}
        if checkpoint.get('feature_requirement_id') != b.get('feature_requirement_id'):
            checkpoint = {}
        return checkpoint | {'status': 'blocked', 'reason': str(exc)}


def defer_pending(scheduler, b):
    """保留跨日补丁（含修复），释放未进入 release 的任务供次日重新评审。"""
    ledger = scheduler.ledger
    pending = [ident for ident in b['run_ids'] if ident not in b.get('integrated_ids', [])]
    with ledger.store.transaction() as db:
        for ident in pending:
            run = ledger.get('runs', ident, db)
            if run['status'] != 'accepted':
                continue
            carry = {'group': b['id'], 'repository': b.get('repository'), 'base_sha': b.get('feature_base_sha'), 'head_sha': b.get('feature_head_sha')}
            if b.get('feature_requirement_id'):
                carry['group'] += ':' + b['feature_requirement_id']
            values = {'delivery_id': None, 'deferred_from': b['id']}
            belongs_to_feature = not b.get('feature_requirement_id') or run['requirement_id'] == b['feature_requirement_id']
            if belongs_to_feature and all(carry.values()):
                values['delivery_carry'] = carry
            ledger.update('runs', ident, run['version'], values, db=db)
        current = ledger.get('deliveries', b['id'], db)
        return ledger.update('deliveries', b['id'], current['version'], {'frozen': True, 'pending_ids': [],
            'repair_deferred_until': None,
            'deferred_ids': list(dict.fromkeys(b.get('deferred_ids', []) + pending)), 'reason': '已封板，未合入成果保留到次日评审'},
            'collecting' if b.get('integrated_ids') or b.get('feature_merge_sha') else 'cancelled', db)


def complete(scheduler, b, result, action):
    from .delivery import mark_online
    ledger = scheduler.ledger
    change = lambda values, status=None: scheduler.change('deliveries', b, values, status)
    if b.get('round_id') and action.startswith(('review', 'repair')):
        row = ledger.get('code_reviews', b['round_id'])
        expected_status = {'review_feature': 'code_review', 'review_release': 'reviewing_release',
            'repair_feature': 'repairing_feature', 'repair_release': 'repairing_release'}[action]
        if row.get('phase') != action or row['status'] != 'running' and b['status'] != expected_status:
            return b
        if row['status'] == 'running' and row.get('phase') == action:
            scheduler.change('code_reviews', row, {'result': result, 'reason': result.get('reason') or result.get('summary', ''), 'finished_at': time.time()}, result['status'])
    if result.get('status') == 'deferred':
        return defer_pending(scheduler, b)
    if result.get('stale'):
        return change({'reason': result.get('reason', ''), 'feature_review_pass': None, 'review_pass': None, 'validation_pass': None},
            'syncing_feature' if action.endswith('feature') else 'syncing_release')
    if result['status'] == 'busy':
        return change({'reason': result.get('reason', ''), 'next_attempt': time.time() + 30})
    if result['status'] != 'pass':
        if result['status'] == 'fail' and not action.startswith('repair'):
            return change({'feedback': result, 'resume_action': action, 'reason': result.get('reason') or result.get('summary') or '检查失败'},
                'review_failed' if action in ('prepare', 'review_feature', 'sync_feature') else 'release_failed')
        from .delivery_review import agent_error
        values = {'reason': result.get('reason', '交付已阻塞'), 'resume_status': b['status']}
        if action.startswith('repair') and agent_error(result):
            values |= {'revisions': max(0, b['revisions'] - 1),
                'excluded_repair_rounds': list(dict.fromkeys(b.get('excluded_repair_rounds', []) + [b['round_id']]))}
        return change(values, 'blocked')
    if action == 'prepare':
        with ledger.store.transaction() as db:
            if b.get('feature_requirement_id'):
                association = {'feature_pr_url': result['feature_pr_url'], 'feature_pr_number': result['feature_pr_number'], 'feat_branch': b['feat_branch']}
                requirement = ledger.get('requirements', b['feature_requirement_id'], db)
                if requirement.get('feature_pr_url') and requirement['feature_pr_url'] != association['feature_pr_url']:
                    raise ValueError('一个需求只能关联一个评审 PR')
                ledger.update('requirements', requirement['id'], requirement['version'], association, db=db)
                for ident in result['pending_ids']:
                    run = ledger.get('runs', ident, db)
                    ledger.update('runs', ident, run['version'], association, db=db)
            return ledger.update('deliveries', b['id'], b['version'],
                {k: v for k, v in result.items() if k != 'status'} | {'feature_review_pass': None, 'reason': ''}, 'code_review', db)
    if action == 'review_feature':
        proof = {k: result[k] for k in ('head_sha', 'base_sha')}
        return change({'feature_review_pass': proof | {'round_id': b['round_id']}, 'reason': ''}, 'merging_feature')
    if action == 'merge_feature':
        with ledger.store.transaction() as db:
            current = ledger.get('deliveries', b['id'], db)
            entry = result['merged_feature']
            history = [r for r in b.get('feature_prs', []) if r['pr_number'] != entry['pr_number']] + [entry]
            values = {k: v for k, v in result.items() if k not in ('status', 'merged')}
            remaining = [ident for ident in b['run_ids'] if ident not in result['integrated_ids']]
            values |= {'feature_prs': history, 'pending_ids': [], 'feature_pr_url': None, 'feature_pr_number': None, 'feature_requirement_id': None,
                'feature_head_sha': None, 'feature_base_sha': None, 'feature_review_pass': None,
                'review_pass': {'head_sha': result['head_sha'], 'base_sha': b['base_sha'], 'feature_reviews': history},
                'validation_pass': None, 'reason': '等待 23:30 封板后统一验证'}
            updated = ledger.update('deliveries', b['id'], current['version'], values, 'preparing' if remaining else 'collecting', db)
            for ident in result['integrated_ids']:
                run = ledger.get('runs', ident, db)
                if run['status'] == 'accepted':
                    ledger.update('runs', ident, run['version'], {'delivery_id': b['id']}, 'delivered', db)
                    req = ledger.get('requirements', run['requirement_id'], db)
                    ledger.update('requirements', req['id'], req['version'], {'delivery_id': b['id']}, 'delivered', db)
            return updated
    if action == 'repair_feature':
        return change({'feature_head_sha': result.get('head_sha'), 'feature_review_pass': None, 'reason': ''}, 'preparing' if b.get('resume_action') == 'prepare' else 'syncing_feature')
    if action == 'sync_feature':
        return change({'feature_head_sha': result['head_sha'], 'feature_base_sha': result['base_sha'], 'feature_review_pass': None, 'reason': ''}, 'code_review')
    if action == 'sync_release':
        return change({'head_sha': result['head_sha'], 'base_sha': result['base_sha'],
            'release_sync_pass': {'head_sha': result['head_sha'], 'base_sha': result['base_sha']},
            'validation_pass': None, 'reason': ''}, 'validating')
    if action == 'review_release':
        return change({'review_pass': {'head_sha': result['head_sha'], 'base_sha': result['base_sha'], 'round_id': b['round_id']}, 'reason': ''}, 'validating')
    if action == 'repair_release':
        return change({'head_sha': result['head_sha'], 'review_pass': None, 'validation_pass': None, 'reason': ''}, 'syncing_release')
    if action == 'validate_release':
        return change({'validation_pass': {'head_sha': result['head_sha'], 'base_sha': result['base_sha'], 'validation_id': b['validation_id']}, 'reason': ''}, 'merging')
    if action == 'merge_release':
        return mark_online(ledger, b, result)


def assign_requirement(scheduler, batch):
    """为下一项需求冻结唯一 feat 分支和 PR；返修与跨日复用同一关联。"""
    if batch.get('feature_pr_url') or batch.get('feature_requirement_id'):
        return batch
    ledger = scheduler.ledger
    pending = [ident for ident in batch['run_ids'] if ident not in batch.get('integrated_ids', [])]
    if not pending:
        return batch
    run = ledger.get('runs', pending[0])
    requirement = ledger.get('requirements', run['requirement_id'])
    branch = requirement.get('feat_branch') or 'feat-' + hashlib.sha256(requirement['id'].encode()).hexdigest()[:24]
    return scheduler.change('deliveries', batch, {'feature_requirement_id': requirement['id'], 'feat_branch': branch,
        'feature_pr_url': requirement.get('feature_pr_url'), 'feature_pr_number': requirement.get('feature_pr_number'),
        'pending_ids': [run['id']], 'feature_head_sha': None, 'feature_base_sha': None, 'feature_review_pass': None})


def tick_product(scheduler, product, now):
    from .delivery import create_batch, config, repair_queue, ZONE
    from reviewers import selection, normalize, snapshot
    import uuid
    ledger = scheduler.ledger
    batches = sorted([b for b in ledger.list('deliveries') if b['product_id'] == product['id'] and b['status'] not in ('online', 'cancelled')], key=lambda b: (b['day'], b.get('sequence', 1)))
    for b in batches:
        pending = b.get('pending_result')
        if b.get('call') or pending:
            if now >= b['cutoff'] and not b.get('frozen'):
                b = scheduler.change('deliveries', b, {'frozen': True})
            result = pending['result'] if pending else scheduler.call_result(b)
            if result is not None:
                action = pending['action'] if pending else b['call']['action']
                if not pending:
                    b = scheduler.change('deliveries', b, {'pending_result': {'action': action, 'result': result}})
                if b.get('call'):
                    b = scheduler.consume('deliveries', b, result)
                complete(scheduler, b, result, action)
                b = ledger.get('deliveries', b['id'])
                scheduler.change('deliveries', b, {'pending_result': None})
            return
    if product['status'] != 'active':
        return
    local = dt.datetime.fromtimestamp(now, ZONE)
    review_window = (local.hour, local.minute) < (23, 30)
    if review_window:
        candidates = [r for r in reversed(ledger.list('runs')) if r['product_id'] == product['id'] and r['status'] == 'accepted' and not r.get('delivery_id')]
        if candidates:
            batch = create_batch(ledger, product, now, bootstrap=product.get('git_migration', {}).get('status') != 'completed')
            if not batch.get('frozen') and batch['status'] in ('collecting', 'preparing'):
                for run in candidates:
                    ledger.update('runs', run['id'], run['version'], {'delivery_id': batch['id']})
                batch = scheduler.change('deliveries', batch, {'run_ids': list(dict.fromkeys(batch['run_ids'] + [r['id'] for r in candidates])), 'reason': ''}, 'preparing')
    batches = sorted([b for b in ledger.list('deliveries') if b['product_id'] == product['id'] and b['status'] not in ('online', 'cancelled')], key=lambda b: (b['day'], b.get('sequence', 1)))
    if not batches:
        return
    feature_states = {'preparing', 'code_review', 'review_failed', 'repairing_feature', 'syncing_feature', 'merging_feature'}
    b = None
    for candidate in batches:
        if now >= candidate['cutoff'] and not candidate.get('frozen'):
            candidate = scheduler.change('deliveries', candidate, {'frozen': True})
        if candidate.get('frozen') and (candidate['status'] in feature_states or candidate['status'] == 'blocked' and candidate.get('resume_status') in feature_states):
            defer_pending(scheduler, candidate)
            return
        candidate, waiting = repair_queue(scheduler, candidate, product, now)
        if not waiting:
            b = candidate
            break
    if b is None:
        return
    if b['status'] == 'collecting':
        if not b.get('frozen'):
            return
        b = scheduler.change('deliveries', b, {}, 'syncing_release')
    if b['status'] == 'blocked' or now < b.get('next_attempt', 0):
        return
    # 兼容旧版留下的 release 复审或验证队列，重新同步后只做统一验证。
    if b['status'] == 'reviewing_release' or b['status'] in ('validating', 'merging') and not b.get('release_sync_pass'):
        b = scheduler.change('deliveries', b, {'validation_pass': None, 'reason': ''}, 'syncing_release')
    if b['status'] == 'preparing':
        b = assign_requirement(scheduler, b)
    choice = config(product)
    if b['status'] in ('review_failed', 'release_failed'):
        b = scheduler.change('deliveries', b, {}, 'repairing_feature' if b['status'] == 'review_failed' else 'repairing_release')
    action = {'preparing': 'prepare', 'code_review': 'review_feature', 'merging_feature': 'merge_feature',
        'syncing_feature': 'sync_feature', 'repairing_feature': 'repair_feature', 'syncing_release': 'sync_release',
        'reviewing_release': 'review_release', 'repairing_release': 'repair_release', 'validating': 'validate_release', 'merging': 'merge_release'}[b['status']]
    if action.startswith('review') and not review_window:
        if b.get('reason') != '23:30 后不开启新代码评审，等待次日评审窗口':
            scheduler.change('deliveries', b, {'reason': '23:30 后不开启新代码评审，等待次日评审窗口'})
        return
    if scheduler.wait_for_off_peak('deliveries',b,product,action,now=now):
        return
    if action.startswith(('review', 'repair')):
        selected = copy.deepcopy(choice['fixer' if action.startswith('repair') else 'reviewer'])
        selection(selected)
        actual = snapshot(normalize(scheduler.store.settings()) | {'reviewer_mode': 'unified', 'reviewer_unified': selected}, 'acceptance')
        selected |= {'bin': selected.get('bin', actual['bin']), 'reasoning_effort': selected.get('reasoning_effort', 'medium')}
        target = feature_request({'record': b})['record'] if action.endswith('feature') else b
        skill_version = json.loads((Path(__file__).resolve().parents[1] / 'vendor/open-code-review/lock.json').read_text())
        row = ledger.create('code_reviews', {'product_id': product['id'], 'delivery_id': b['id'], 'title': b['title'] + (' · Code Review' if action.startswith('review') else ' · 修复评审问题'),
            'phase': action, 'selection': selected, 'skill_version': skill_version, 'pr_url': target.get('pr_url'),
            'pr_number': target.get('pr_number'), 'head_sha': target.get('head_sha'), 'base_sha': target.get('base_sha'), 'started_at': now}, 'running')
        b = scheduler.change('deliveries', b, {'selection': selected, 'round_id': row['id'], 'revisions': b['revisions'] + action.startswith('repair')})
    else:
        b = scheduler.change('deliveries', b, {'round_id': None, **({'validation_id': str(uuid.uuid4())} if action == 'validate_release' else {})})
    # 未合入 release 的需求已延期，不能再拿它们的验收条件阻塞本次上线。
    run_ids = b.get('integrated_ids', []) if action.endswith('release') else b['run_ids']
    if b.get('feature_requirement_id') and (action == 'prepare' or action.endswith('feature')):
        run_ids = [ident for ident in run_ids if ledger.get('runs', ident)['requirement_id'] == b['feature_requirement_id']][:1]
    command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/autopilot-adapter.py'), 'delivery']
    scheduler.start_call('deliveries', b, product, action, command,
        {'runs': [ledger.get('runs', ident) for ident in run_ids],
         'requirements': [ledger.get('requirements', ledger.get('runs', ident)['requirement_id']) for ident in run_ids]}, timeout=14400)
