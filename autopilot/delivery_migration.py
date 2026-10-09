"""将未合并的旧交付批次迁移到先 Code Review 再合入 release 的流程。"""
import datetime as dt
import fcntl
from pathlib import Path
import sqlite3
import time
from urllib.parse import quote

from review_core import Conflict
from .call import atomic
from .delivery import ZONE
from .delivery_worker import paths, network_git, journal
from .github import GitHub
from .workspace import git


def migrate(ledger, batch):
    product = ledger.get('products', batch['product_id'])
    if batch.get('flow') == 'review_before_release':
        return batch
    if product['status'] != 'paused' or batch.get('call') or batch['status'] in ('online', 'cancelled'):
        raise Conflict('迁移交付流程前须暂停项目，并等待当前交付调用结束；已上线批次不可迁移')
    request = {'state_root': str(ledger.store.state / 'autopilot'), 'product': product, 'record': batch}
    root, folder, repo, old_workspace = paths(request)
    operation = folder / 'review-flow-migration.json'
    with (root / 'delivery.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = journal(operation)
        if not old:
            backup = folder / 'before-review-flow.sqlite3'
            with ledger.store.connect() as source, sqlite3.connect(backup) as destination:
                source.backup(destination)
            backup.chmod(0o600)
            old = {'batch': batch, 'backup': str(backup), 'started_at': time.time(),
                'archived_branch': 'codex/legacy-' + batch['branch']}
            atomic(operation, old)
        gh = GitHub(batch['git_url'])
        base = network_git(repo, 'ls-remote', 'origin', 'refs/heads/' + batch['base_branch']).split()[0]
        expected = old['batch']['head_sha']
        branch = batch['branch']
        archived = old['archived_branch']
        remote = network_git(repo, 'ls-remote', 'origin', 'refs/heads/' + branch)
        saved = network_git(repo, 'ls-remote', 'origin', 'refs/heads/' + archived)
        if not saved:
            if not remote or remote.split()[0] != expected:
                raise Conflict('原 release 已变化，不能自动迁移；已保留备份')
            pr = gh.api('/pulls/' + str(old['batch']['pr_number']))
            if pr.get('merged'):
                raise Conflict('旧 PR 已合并，不能重新初始化 release')
            gh.api('/branches/' + quote(branch, safe='') + '/rename', 'POST', {'new_name': archived})
            saved = network_git(repo, 'ls-remote', 'origin', 'refs/heads/' + archived)
        if not saved or saved.split()[0] != expected:
            raise Conflict('旧 release 归档提交核对失败，停止迁移')
        if git(old_workspace, 'branch', '--show-current') == branch:
            git(old_workspace, 'branch', '-m', archived)
        if git(repo, 'rev-parse', 'refs/heads/' + batch['feat_branch']) != expected:
            raise Conflict('feat 与原交付提交不一致，保留工作区等待核对')
        cutoff = dt.datetime.strptime(batch['day'], '%Y%m%d').replace(tzinfo=ZONE, hour=23, minute=30).timestamp()
        # The original feat already contains the captured baseline and accepted patches.
        atomic(folder / 'staged.json', {'snapshot_done': True, 'migrated_from': old['batch']['pr_url']})
        with ledger.store.transaction() as db:
            current = ledger.get('deliveries', batch['id'], db)
            if current.get('call'):
                raise Conflict('迁移期间出现运行调用，停止更新台账')
            for ident in current['run_ids']:
                run = ledger.get('runs', ident, db)
                if run['status'] == 'delivered':
                    ledger.update('runs', ident, run['version'], {'delivery_migration': '待 feat → release Code Review'}, 'accepted', db)
                    req = ledger.get('requirements', run['requirement_id'], db)
                    ledger.update('requirements', req['id'], req['version'], {}, 'accepted', db)
            values = {'flow': 'review_before_release', 'cutoff': cutoff, 'frozen': False, 'manual_close_at': None,
                'legacy_prs': [{'pr_url': old['batch']['pr_url'], 'pr_number': old['batch']['pr_number'], 'branch': archived, 'head_sha': expected}],
                'pr_url': None, 'pr_number': None, 'head_sha': base, 'base_sha': base, 'integrated_ids': [],
                'feature_pr_url': None, 'feature_pr_number': None, 'feature_head_sha': expected, 'feature_base_sha': base,
                'feature_review_pass': None, 'review_pass': None, 'validation_pass': None, 'round_id': None,
                'feedback': None, 'resume_status': None, 'next_attempt': 0, 'reason': '', 'revisions': 0,
                'workspace': str(folder / 'release-workspace')}
            updated = ledger.update('deliveries', batch['id'], current['version'], values, 'preparing', db)
            p = ledger.get('products', product['id'], db)
            ledger.update('products', p['id'], p['version'], {'delivery_flow': 'review_before_release'}, db=db)
        atomic(operation, old | {'completed_at': time.time(), 'base_sha': base})
        return updated
