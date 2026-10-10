"""提取模型执行错误，保留可操作原因及是否适合自动重试。"""
import json
import re

from .store import redact
from error_messages import failure_fields, sanitize


def verification_failure(result):
    """依赖下载的明确网络故障保留验证阶段，不能作为代码缺陷消耗返修次数。"""
    if result.get('status') != 'fail':
        return result
    failed = [c for c in result.get('checks', []) if c.get('status') != 'pass' and c.get('required', True)]
    if not failed or any(not c.get('name', '').startswith('install-') for c in failed):
        return result
    network = re.compile(r'\b(?:ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|ENETUNREACH|ENOTFOUND)\b')
    for check in failed:
        detail = '\n'.join(str(check.get(k, '')) for k in ('reason', 'log_tail', 'diagnostic'))
        if not network.search(detail):
            return result
    return result | {'status': 'blocked', 'failure_kind': 'dependency_network', 'retryable': True}


def codex_failure(root, returncode):
    """从 Codex 最终失败事件提取原因；已恢复的重连事件不覆盖成功结果。"""
    terminal = ''
    latest = ''
    try:
        with (root / 'trace.jsonl').open(errors='replace') as trace:
            for line in trace:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get('type')
                if kind == 'turn.completed':
                    terminal = latest = ''
                    continue
                if kind not in ('turn.failed', 'error'):
                    continue
                error = event.get('error')
                message = error.get('message') if isinstance(error, dict) else error
                message = message or event.get('message')
                if not isinstance(message, str) or not message.strip():
                    continue
                message = sanitize(message)[:4000]
                if kind == 'turn.failed':
                    terminal = message
                else:
                    latest = message
    except OSError:
        pass
    if not returncode and not terminal:
        return None
    detail = terminal or latest
    if not detail:
        try:
            with (root / 'stderr.log').open('rb') as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - 8000))
                detail = sanitize(stream.read().decode(errors='replace'))[-4000:]
        except OSError:
            pass
    detail = detail or f'Codex 模型执行失败，退出码 {returncode}'
    fields = failure_fields(detail, 'Codex 模型服务')
    code = fields['error_info']['code']
    return {'status': 'blocked', 'failure_kind': 'agent_execution',
        'error_code': code, 'retryable': code not in (
            'QUOTA_EXHAUSTED', 'MODEL_BALANCE_INSUFFICIENT', 'AUTH_REQUIRED', 'ACCESS_DENIED'),
        **fields, 'detail': detail, 'exit_code': returncode, 'evidence': str(root)}


def harness_failure(root, returncode):
    """优先读取结构化终止事件；失败回执不能被空 final 掩盖。"""
    errors = []
    for line in (root / 'trace.jsonl').read_text(errors='replace').splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        reason = event.get('reason')
        if isinstance(reason, dict) and reason.get('kind') == 'error':
            errors.append(reason.get('error') or reason)
        elif event.get('type') == 'error':
            errors.append(event.get('error') or event)
    if not returncode and not errors:
        return None
    stderr = (root / 'stderr.log').read_text(errors='replace')[-4000:]
    detail = redact(json.dumps(errors[-1], ensure_ascii=False) if errors else stderr)
    balance = bool(re.search(r'Insufficient Balance|余额不足|"status"\s*:\s*402', detail, re.I))
    return {'status': 'blocked', 'failure_kind': 'agent_execution',
        'error_code': 'MODEL_BALANCE_INSUFFICIENT' if balance else 'HARNESS_EXECUTION_ERROR',
        'retryable': not balance,
        **failure_fields(detail, '模型服务', 'MODEL_BALANCE_INSUFFICIENT' if balance else None),
        'detail': detail, 'exit_code': returncode, 'evidence': str(root)}
