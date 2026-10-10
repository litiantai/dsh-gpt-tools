"""按 PR 汇总代码交付，并从不可变轮次证据还原尚未完成的问题。"""
import hashlib
import json
import threading
import time
from datetime import datetime

from .call import atomic
from .github import GitHub


def pr_url(value):
    """去除评论锚点，确保同一 PR 只出现一次。"""
    return str(value or '').split('#')[0].rstrip('/')


def pending_issues(deliveries, rounds, dispatch=None, requirements=()):
    """修复成功仅关闭启动前的问题；失败、运行异常和未完成轮次保留问题。"""
    batches = {b['id']: b for b in deliveries}
    opened = {}
    events = list(rounds)
    # 统一验证/合并冲突没有模型评审轮次，同样可能需要修复。
    for b in deliveries:
        for receipt in b.get('receipts', []):
            action = receipt['action']
            if action.startswith(('review', 'repair')):
                continue
            result = receipt.get('result', {})
            deferred = action.startswith('validate') and any(
                check.get('status') == 'fail' and check.get('disposition') == 'backlog'
                for check in result.get('checks', []))
            if result.get('status') == 'fail' or deferred:
                events.append({'id': receipt['call_id'], 'delivery_id': b['id'], 'phase': action,
                    'pr_url': b.get('feature_pr_url') if action in ('prepare', 'sync_feature') else b.get('pr_url'),
                    'created': receipt['at'], 'finished_at': receipt['at'], 'updated': receipt['at'],
                    'status': 'fail', 'result': result})
    for row in sorted(events, key=lambda r: (r.get('finished_at') or r['updated'], r['created'], r['id'])):
        b = batches.get(row['delivery_id'], {})
        url = pr_url(row.get('pr_url'))
        # 没有 PR 的准备冲突只归属于该批次，不能误挂到之后的另一个 PR。
        target = url or 'delivery:' + row['delivery_id'] + (':feature' if row.get('phase', '').endswith('feature') or row.get('phase') == 'prepare' else ':release')
        phase, status = row.get('phase', ''), row['status']
        is_repair = phase.startswith('repair')
        if status == 'pass' and (is_repair or phase.startswith('review')):
            before = row.get('started_at', row['created'])
            opened = {k: v for k, v in opened.items() if v['target'] != target or v['created'] > before}
        if status == 'fail' and not is_repair:
            result = row.get('result') or {}
            issues = result.get('issues')
            if not issues and phase.startswith('validate'):
                issues = []
                for check in result.get('checks', []):
                    if check.get('status') != 'fail' or not (check.get('required', True) or check.get('disposition') == 'backlog'):
                        continue
                    proof = check.get('evidence')
                    proof = proof if isinstance(proof, dict) else {}
                    issues.append({'content': proof.get('reason') or check.get('reason') or check.get('name') or '检查未通过',
                                   'verification': proof})
            issues = issues or [{'content': result.get('reason') or row.get('reason') or result.get('summary') or '检查未通过'}]
            for issue in issues:
                if not isinstance(issue, dict):
                    issue = {'content': str(issue)}
                key = hashlib.sha256(json.dumps([target, issue.get('path', ''), issue.get('content', '')], ensure_ascii=False).encode()).hexdigest()
                old = opened.get(key)
                linked = []
                proof = issue.get('verification') or {}
                if proof.get('status') == 'fail' and proof.get('reason') and result.get('head_sha'):
                    for requirement in requirements:
                        if requirement.get('source') != 'verification_finding' or requirement.get('product_id') != b.get('product_id'):
                            continue
                        if any(occurrence.get('delivery_id') == row['delivery_id']
                               and occurrence.get('commit') == result['head_sha']
                               and ' '.join((occurrence.get('verification', {}).get('reason') or '').split()) == ' '.join(proof['reason'].split())
                               for occurrence in requirement.get('verification_occurrences', [])):
                            linked.append(requirement)
                requirement = max(linked, key=lambda r: r.get('created', 0), default=None)
                if requirement and requirement['status'] in ('completed', 'online') and requirement.get('updated', 0) < row['updated']:
                    requirement = None
                if (requirement and requirement['status'] in ('completed', 'online')
                        and requirement.get('updated', 0) >= row['updated']):
                    opened.pop(key, None)
                    continue
                opened[key] = {'id': key, 'target': target, 'delivery_id': row['delivery_id'], 'pr_url': url,
                    'title': issue.get('content', '检查未通过'), 'path': issue.get('path', ''), 'start_line': issue.get('start_line'),
                    'severity': issue.get('severity', ''), 'status': 'pending', 'review_id': row['id'],
                    'created': old['created'] if old else row['updated'], 'updated': row['updated'],
                    'evidence': proof.get('evidence') or result.get('report_url') or result.get('evidence'), 'batch_title': b.get('title', ''),
                    **({'requirement': {k: requirement[k] for k in ('id', 'title', 'status', 'updated') if k in requirement}}
                       if requirement else {})}
        if is_repair and status != 'pass':
            for item in opened.values():
                if item['target'] == target and item['created'] <= row.get('started_at', row['created']):
                    item.update(status='repairing' if status == 'running' else 'blocked', repair_id=row['id'],
                        updated=row['updated'], reason=row.get('reason', ''), selection=row.get('selection'))
    for item in opened.values():
        if item.get('requirement'):
            item.update(status='backlog', call={}, retryable=False,
                        reason='该验收缺陷已转入需求池，修复进度以关联需求为准。',
                        retry_reason='请在关联需求中查看和处理，无需重试原交付批次',
                        updated=max(item['updated'], item['requirement'].get('updated', 0)))
    return sorted(opened.values(), key=lambda r: r['created'], reverse=True)


def review_status(pr, rounds, issues):
    """已合并优先；未完成问题进入修复，修复后的新提交须重新评审。"""
    if pr['status'] == 'merged':
        return 'merged'
    if any(i['pr_url'] == pr['pr_url'] for i in issues):
        return 'fixing'
    completed = [r for r in rounds if pr_url(r.get('pr_url')) == pr['pr_url']
                 and r['status'] in ('pass', 'fail')]
    latest = max(completed, key=lambda r: r.get('finished_at') or r['updated'], default=None)
    if latest and latest.get('phase', '').startswith('review') and latest['status'] == 'pass':
        proof = latest.get('result', {})
        head, base = proof.get('head_sha') or latest.get('head_sha'), proof.get('base_sha') or latest.get('base_sha')
        if head and base and head == pr.get('head_sha') and base == pr.get('base_sha'):
            return 'approved'
    return 'unreviewed'


def separate_prs(prs, batches, runs, requirements):
    """依台账的需求关系列出需求 PR；release PR 独立进入待合并视图。"""
    by_url = {pr['pr_url']: pr for pr in prs}
    by_run = {run['id']: run for run in runs}
    by_requirement = {requirement['id']: requirement for requirement in requirements}
    release_urls, feature_runs = {}, {}

    def feature(url, run_ids):
        url = pr_url(url)
        if url:
            feature_runs.setdefault(url, set()).update(run_ids)

    for batch in batches:
        if batch.get('pr_url'):
            release_urls[pr_url(batch['pr_url'])] = batch
        for pr in prs:
            if (pr.get('head_branch') and pr.get('head_branch') == batch.get('branch')
                    and pr.get('base_branch') == batch.get('base_branch')):
                release_urls[pr['pr_url']] = batch
        for entry in batch.get('feature_prs', []):
            feature(entry.get('pr_url'), entry.get('run_ids', []))
        pending = batch.get('pending_ids', [rid for rid in batch.get('run_ids', []) if rid not in batch.get('integrated_ids', [])])
        feature(batch.get('feature_pr_url'), pending)
        for receipt in batch.get('receipts', []):
            result = receipt.get('result', {})
            feature(result.get('feature_pr_url'), result.get('pending_ids', []))
            merged = result.get('merged_feature') or {}
            feature(merged.get('pr_url'), merged.get('run_ids', []))
    for run in runs:
        feature(run.get('feature_pr_url'), [run['id']])

    rows = {}
    for url, run_ids in feature_runs.items():
        # release 链路不得被错误关联成某个需求的独立 PR。
        if url in release_urls or url not in by_url:
            continue
        for run_id in sorted(run_ids):
            run = by_run.get(run_id)
            requirement = by_requirement.get(run.get('requirement_id')) if run else None
            if not requirement:
                continue
            registered = pr_url(requirement.get('feature_pr_url'))
            if registered and registered != url:
                continue
            key = requirement['id']
            if key in rows and rows[key]['updated'] > by_url[url]['updated']:
                continue
            rows[key] = by_url[url] | {'id': key, 'requirement_id': requirement['id'],
                'requirement_title': requirement['title']}
    for row in rows.values():
        row['shared_pr'] = sum(other['pr_url'] == row['pr_url'] for other in rows.values()) > 1
    release_prs = [by_url[url] | {'delivery_id': batch['id'], 'delivery_status': batch['status'],
                    'reason': batch.get('reason', '')} for url, batch in release_urls.items() if url in by_url]
    return sorted(rows.values(), key=lambda r: r['updated'], reverse=True), sorted(release_prs, key=lambda r: r['updated'], reverse=True)


class DeliveryBoard:
    """缓存完整 GitHub PR 清单，网络失败保留上次确认状态并报告过期。"""
    def __init__(self, ledger):
        self.ledger = ledger
        self.lock = threading.Lock()
        self.attempted = {}
        self.errors = {}

    def get(self, product):
        ident = product['id']
        with self.ledger.store.connect() as db:
            data = {kind: [self.ledger.decode(r) for r in db.execute(
                f'SELECT * FROM auto_{kind} WHERE product_id=? ORDER BY created', (ident,))]
                for kind in ('deliveries', 'code_reviews', 'runs', 'requirements')}
        batches, rounds = data['deliveries'], data['code_reviews']
        known = {}
        for b in batches:
            for entry in [b, {'pr_url': b.get('feature_pr_url')}, *b.get('feature_prs', [])]:
                url = pr_url(entry.get('pr_url'))
                if url:
                    merged = bool(entry.get('merge_sha')) or entry is b and b['status'] == 'online'
                    known[url] = {'id': url, 'pr_url': url, 'number': url.rsplit('/', 1)[-1],
                        'title': b['title'], 'status': 'merged' if merged else 'unknown', 'updated': b['updated']}
        for row in rounds:
            url = pr_url(row.get('pr_url'))
            if url and url not in known:
                known[url] = {'id': url, 'pr_url': url, 'number': url.rsplit('/', 1)[-1],
                    'title': row.get('title', ''), 'status': 'unknown', 'updated': row['updated']}
        repository = product.get('git', {}).get('url')
        cache_key = hashlib.sha256(str(repository).encode()).hexdigest()
        path = self.ledger.store.state / 'autopilot/pr-status' / (cache_key + '.json')
        with self.lock:
            try:
                saved = json.loads(path.read_text())
            except (OSError, ValueError):
                saved = {'prs': [], 'checked_at': None}
            if repository and time.monotonic() - self.attempted.get(cache_key, -60) >= 60:
                self.attempted[cache_key] = time.monotonic()
                try:
                    gh, prs, page = GitHub(repository), [], 1
                    while True:
                        chunk = gh.api(f'/pulls?state=all&per_page=100&page={page}')
                        for pr in chunk:
                            url = pr_url(pr['html_url'])
                            prs.append({'id': url, 'pr_url': url, 'number': pr['number'], 'title': pr['title'],
                                'status': 'merged' if pr.get('merged_at') else 'closed' if pr['state'] == 'closed' else 'open',
                                'draft': pr.get('draft', False), 'head_branch': pr['head']['ref'], 'base_branch': pr['base']['ref'],
                                'head_sha': pr['head'].get('sha'), 'base_sha': pr['base'].get('sha'),
                                'updated': datetime.fromisoformat(pr['updated_at'].replace('Z', '+00:00')).timestamp()})
                        if len(chunk) < 100:
                            break
                        page += 1
                    saved = {'prs': prs, 'checked_at': time.time()}
                    path.parent.mkdir(parents=True, exist_ok=True)
                    atomic(path, saved)
                    self.errors.pop(cache_key, None)
                except (OSError, ValueError) as exc:
                    self.errors[cache_key] = 'PR 状态同步失败，保留上次结果；未确认的状态显示为未知。'
            for pr in saved['prs']:
                known[pr['id']] = pr
        issues = pending_issues(batches, rounds, requirements=data['requirements'])
        prs = [pr | {'git_status': pr['status'], 'status': review_status(pr, rounds, issues)} for pr in known.values()]
        requirement_prs, release_prs = separate_prs(prs, batches, data['runs'], data['requirements'])
        return {'prs': requirement_prs, 'release_prs': release_prs,
            'issues': issues, 'checked_at': saved['checked_at'],
            'sync_error': self.errors.get(cache_key)}
