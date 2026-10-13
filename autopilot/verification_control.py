"""验证失败身份、用例进展与同版本重复复验控制。"""
import hashlib
import json
from pathlib import Path
import re


def failed_checks(result):
    """只提取必需检查的真实失败或阻塞。"""
    return [c for c in result.get('checks', []) if isinstance(c, dict)
            and c.get('required', True) and c.get('status') in ('fail', 'blocked')]


def check_detail(check, trusted_root=None):
    """保留原始诊断，按需读取控制器证据目录内的日志末尾。"""
    text = '\n'.join(str(check.get(k, '')) for k in ('reason', 'log_tail', 'diagnostic', 'evidence'))
    if trusted_root and check.get('log'):
        path = Path(check['log']).resolve()
        if path.is_relative_to(Path(trusted_root).resolve()) and path.is_file():
            with path.open('rb') as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - 200000))
                text += '\n' + stream.read().decode(errors='replace')
    return re.sub(r'\x1b\[[0-9;]*m', '', text)


def failed_test_ids(text):
    """提取 unittest、Playwright 和 TAP 的失败用例身份。"""
    result = re.findall(r'(?m)^(?:FAIL|ERROR):\s+([^\n]+)', text)
    result += re.findall(r'(?m)^\s*\d+\)\s+(tests/[^\n]+)', text)
    result += re.findall(r'(?m)^\s*not ok \d+\s*-\s*([^\n]+)', text)
    return sorted(set(re.sub(r':\d+:\d+(?=\s|$)', '', value).strip() for value in result))


def passed_test_ids(text):
    """用实际通过行证明旧失败已修好，不把消失或删除的用例当作通过。"""
    result = re.findall(r'(?m)^([^\n]+\([^\n]+\)) \.\.\. ok\s*$', text)
    result += re.findall(r'(?m)^\s*[✓✔]\s+\d+\s+(tests/[^\n]+?)\s+\([\d.]+[ms]+\)\s*$', text)
    return sorted(set(re.sub(r':\d+:\d+(?=\s|$)', '', value).strip() for value in result))


def identity(record, product, action):
    """绑定候选、基线和验证配置；验证器升级或配置变化允许重新取得证据。"""
    data = {k: record.get(k) for k in ('commit', 'source_digest', 'head_sha', 'base_sha', 'feature_head_sha', 'feature_base_sha')}
    if not any(data.values()):
        return None
    data.update(action=action, adapter=product.get('adapter'), config=product.get('project_config'),
                adapter_spec=product.get('adapter_spec'))
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def repeat_reason(record, product, action):
    """同候选与验证配置的已知缺陷要求先修复，执行或网络故障可原阶段重试。"""
    if action not in ('verify', 'validate', 'validate_release'):
        return ''
    key = identity(record, product, action)
    if not key:
        return ''
    for receipt in reversed(record.get('receipts', [])):
        if receipt.get('action') != action:
            continue
        from .failures import verification_failure
        result = verification_failure(receipt.get('result') or {})
        if result.get('status') != 'fail' or result.get('retryable') is True:
            return ''
        previous = receipt.get('verification_identity')
        # 历史回执没有执行身份时，只使用控制器记录的候选身份，保守禁止无变化复验。
        if previous == key or previous is None and not any(r.get('action') in ('develop', 'repair', 'repair_release', 'repair_feature')
                for r in record.get('receipts', [])[record['receipts'].index(receipt) + 1:]):
            return '候选代码与验证配置未变化，已知缺陷不能重复复验；请先修复或取得新的验证依据'
        return ''
    return ''


def outcome(record):
    """从最新控制器验证回执抽取检查、具体失败用例和验证身份。"""
    for receipt in reversed(record.get('receipts', [])):
        if receipt.get('action') not in ('verify', 'validate', 'validate_release'):
            continue
        result = receipt.get('result') or {}
        return {'receipt_id': receipt.get('call_id'),
                'checks': {str(c.get('id') or c.get('name')): c.get('status') for c in result.get('checks', []) if c.get('required',True) and (c.get('id') or c.get('name'))},
                'failed_tests': result.get('failed_test_ids', []), 'passed_tests': result.get('passed_test_ids', []),
                'failure_kind': result.get('failure_kind'), 'retryable': result.get('retryable'),
                'verification_identity': receipt.get('verification_identity')}
    return {'receipt_id': None, 'checks': {}, 'failed_tests': []}
