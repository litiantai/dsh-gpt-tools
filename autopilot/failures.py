"""提取模型执行错误，保留可操作原因及是否适合自动重试。"""
import json
import re

from .store import redact
from error_messages import failure_fields, sanitize


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


def verification_failure(result, trusted_root=None):
    """按真实失败阶段分类，网络故障不消耗代码返修，断言失败不冒充服务超时。"""
    if result.get('status') not in ('fail', 'blocked') or result.get('failure_kind') == 'agent_execution':
        return result
    from .verification_control import failed_checks, check_detail, failed_test_ids, passed_test_ids
    failed = failed_checks(result)
    if not failed:
        return result | {'failure_kind': 'business', 'retryable': False} if result.get('status') == 'fail' else result
    nested = [c.get('evidence') if isinstance(c.get('evidence'),dict) else c for c in failed]
    if all(c.get('status') == 'blocked' for c in failed) and all(n.get('failure_kind') == 'agent_execution' for n in nested):
        return result | {'status':'blocked','failure_kind':'agent_execution',
                         'retryable':all(n.get('retryable') is True for n in nested)}
    details = [(c, check_detail(c, trusted_root)) for c in failed]
    if all(c.get('name', '').startswith('install-') for c in failed) and all(
            re.search(r'\b(?:ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|ENETUNREACH|ENOTFOUND)\b', text)
            for c, text in details):
        return result | {'status': 'blocked', 'failure_kind': 'dependency_network', 'retryable': True}
    kind, code = 'business', None
    for check, text in details:
        name = check.get('name', '')
        if name.startswith(('build-', 'compile-', 'typecheck-')) or re.search(r'error TS\d+', text):
            kind, code = 'build', 'BUILD_FAILED'
            break
        if re.search(r'AssertionError|Error: expect\(|expect\(locator\).*failed|FAILED \(failures=|\bnot ok \d+', text):
            kind, code = 'test_assertion', 'TEST_ASSERTION_FAILED'
            break
        if name.startswith(('test-', 'browser-')):
            kind, code = 'test_execution', 'TEST_EXECUTION_FAILED'
        elif check.get('status') == 'blocked' and not check.get('failure_kind'):
            kind, code = 'verification_evidence', 'VERIFICATION_EVIDENCE_MISSING'
    changes = {'failure_kind': kind, 'retryable': False,
               'status': 'blocked' if all(c.get('status') == 'blocked' for c in failed) else result['status'],
               'failed_test_ids': sorted(set(test for _, text in details for test in failed_test_ids(text))),
               'passed_test_ids': sorted(set(test for _, text in details for test in passed_test_ids(text)))}
    if code:
        changes.update(failure_fields('\n'.join(text for _, text in details), code=code))
    return result | changes


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
