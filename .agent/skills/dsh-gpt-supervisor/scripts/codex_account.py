"""通过实际执行器读取当前账户额度，不用历史失败或认证文件推断可用性。"""
import json
import os
from pathlib import Path
import subprocess
import time


def account_status(cfg, selected=None, cwd=None):
    """只读查询实时账户与所有额度窗口；查询失败明确返回未知，不复用旧快照。"""
    from reviewers import normalize
    from review_core import stop_review_process
    from error_messages import sanitize
    cfg = normalize(cfg)
    binary = (selected or {}).get('bin') or cfg['codex_bin']
    value = {'provider': 'codex', 'source': 'codex-app-server', 'bin': binary,
        'auth_home': str(Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser()),
        'status': 'unknown', 'checked_at': time.time(), 'fetched_at': None,
        'account_id': None, 'plan_type': None, 'ordinary_usage_allowed': None,
        'rate_limits': {}, 'error': None}
    proc = None
    try:
        proc = subprocess.Popen([cfg['node_bin'], cfg['reviewer_helper']],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True, cwd=cwd)
        out, err = proc.communicate(json.dumps({'provider': 'codex', 'action': 'account', 'bin': binary}), timeout=25)
        if proc.returncode:
            raise ValueError(f'Codex 账户查询失败（退出码 {proc.returncode}）：' + sanitize(err)[-2000:])
        raw = json.loads(out)
        if not isinstance(raw, dict) or not isinstance(raw.get('limits'), dict):
            raise ValueError('Codex 账户查询返回无效结果')
        account, limits = raw.get('account'), raw['limits']
        if account is not None and not isinstance(account, dict):
            raise ValueError('Codex 账户查询返回无效账户')
        allowed = limits.get('ordinaryUsageAllowed')
        value.update(account_id=limits.get('accountId'), auth_mode=(account or {}).get('type'),
            plan_type=(account or {}).get('planType'),
            ordinary_usage_allowed=allowed if isinstance(allowed, bool) else None)
        buckets = limits.get('rateLimitsByLimitId')
        if not isinstance(buckets, dict) or not buckets:
            legacy = limits.get('rateLimits')
            buckets = {legacy.get('limitId') or 'codex': legacy} if isinstance(legacy, dict) else {}
        for key, bucket in buckets.items():
            if not isinstance(bucket, dict):
                continue
            row = {k: bucket.get(k) for k in ('limitId', 'limitName', 'normalModelSlug', 'planType', 'rateLimitReachedType')}
            for window in ('primary', 'secondary'):
                data = bucket.get(window)
                row[window] = ({k: data.get(k) for k in ('usedPercent', 'windowDurationMins', 'resetsAt')}
                    if isinstance(data, dict) else None)
            credits = bucket.get('credits')
            row['credits'] = ({k: credits.get(k) for k in ('hasCredits', 'unlimited', 'balance')}
                if isinstance(credits, dict) else None)
            value['rate_limits'][key] = row
        # Credits and model-specific limits can change the meaning of 100% used.
        # Only the service's explicit permission decides the current status.
        value['status'] = ('unauthenticated' if account is None else
            'available' if allowed is True else 'limited' if allowed is False else 'unknown')
        value['fetched_at'] = time.time()
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        value['error'] = sanitize(str(exc))
    finally:
        if proc is not None and not stop_review_process(proc):
            value.update(status='unknown', error='Codex 账户查询进程未确认停止')
    return sanitize(value)
