"""将合并放行绑定到完整检查回执、需求范围、策略与 Git 版本。"""
import hashlib
import json
from pathlib import Path
import uuid

from .call import atomic
from .verification_findings import acceptance_targets, inconsistent_pass, unrelated_failure

VERSION = 1


def context(request, head, base):
    """仅绑定影响放行的配置，状态时间等调度字段不使证据失效。"""
    product, record = request['product'], request['record']
    requirements = request.get('requirements') or [request.get('requirement') or {}]
    return {'version': VERSION, 'product_id': product['id'], 'delivery_id': record['id'],
            'head_sha': head, 'base_sha': base,
            'flow': record.get('flow'), 'review_target': record.get('review_target'),
            'policy': product.get('functional_findings_policy', 'block'),
            'requirements': sorted([{key: r.get(key) for key in ('id', 'title', 'goal', 'scope', 'acceptance', 'resolution_probes')}
                                    for r in requirements], key=lambda r: str(r.get('id'))),
            'project_config': product.get('project_config', {}),
            'test_execution': product.get('test_execution'), 'git': product.get('git', {})}


def rejection(request, result):
    """不能靠修改 required 标记把本次需求失败或未知失败转换为通过。"""
    if not isinstance(result, dict):
        return '缺少通过的完整交付检查回执'
    checks = result.get('checks')
    if result.get('status') != 'pass' or not isinstance(checks, list) or not checks:
        return '缺少通过的完整交付检查回执'
    if not any(c.get('required', True) for c in checks if isinstance(c, dict)):
        return '交付回执没有必需检查'
    for check in checks:
        if not isinstance(check, dict) or check.get('required', True) and check.get('status') != 'pass':
            return '交付必需检查尚未全部通过'
        if check.get('name') == '独立业务验证' and isinstance(check.get('evidence'), dict):
            evidence = check['evidence']
            # 无业务需求的导入批次由 verify 生成摘要基线条件；其逐项覆盖已在适配器内核对。
            if evidence.get('status') != check.get('status') or inconsistent_pass(
                    request, evidence, check_coverage=bool(acceptance_targets(request))):
                return '独立业务验证结论与原始逐项证据不一致'
        if check.get('name') == '独立业务验证' and check.get('status') != 'pass':
            evidence = check.get('evidence')
            if (request['product'].get('functional_findings_policy') != 'backlog'
                    or request['record'].get('flow') != 'review_before_release'
                    or request['record'].get('review_target') == 'feature'
                    or check.get('status') != 'fail' or check.get('disposition') != 'backlog'
                    or not check.get('requirement_id') or not isinstance(evidence, dict)
                    or not unrelated_failure(request, evidence)):
                return '独立业务验收失败涉及本次需求或归属不明，禁止以入池代替修复'
    return ''


def certify(request, result, head, base):
    """控制器保存不可复用到其他提交或策略的原始放行依据。"""
    if result.get('status') != 'pass':
        return result
    reason = rejection(request, result)
    if reason:
        return result | {'status': 'blocked', 'reason': reason}
    from .delivery_worker import paths
    folder = paths(request)[1] / 'merge-validations'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (str(uuid.uuid4()) + '.json')
    atomic(path, {'context': context(request, head, base), 'result': result})
    return result | {'merge_gate': {'version': VERSION, 'path': str(path),
                                   'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}}


def verify(request, head, base):
    """临合并重新读取完整证据；旧版、缺失、变更或失败证据一律阻断。"""
    from .delivery_worker import paths
    gate = (request['record'].get('validation_pass') or {}).get('gate')
    if not isinstance(gate, dict) or gate.get('version') != VERSION:
        return '旧版或缺失的验收放行证据，必须重新验证'
    try:
        path = Path(gate['path']).resolve()
        folder = (paths(request)[1] / 'merge-validations').resolve()
        if path.parent != folder:
            return '验收放行证据不属于当前交付批次'
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != gate.get('sha256'):
            return '验收放行证据发生变化，必须重新验证'
        saved = json.loads(content)
        if saved['context'] != context(request, head, base):
            return '需求、交付策略或提交版本已变化，必须重新验证'
        return rejection(request, saved['result'])
    except (KeyError, OSError, ValueError, TypeError):
        return '验收放行证据无法读取，必须重新验证'
