"""将已验收任务的历史分支证据传入交付验收，保留各版本的验证边界。"""
import hashlib
import json
from pathlib import Path
import subprocess

from .call import atomic
from .workspace import git


def collect(request, workspace, head, folder):
    """复制通过的任务回执，复核原始质量证据并关联已合入的 feat PR。"""
    history = []
    batch = request['record']
    integrated = set(batch.get('integrated_ids', []))
    requirements = {r['id'] for r in request.get('requirements', [])}
    if not integrated or not requirements or not request.get('runs'):
        return history
    trusted = Path(request['state_root']).resolve()
    for run in request.get('runs', []):
        if run['id'] not in integrated or run.get('requirement_id') not in requirements:
            continue
        receipt = next((r for r in reversed(run.get('receipts', []))
                        if r.get('action') == 'verify' and r.get('result', {}).get('status') == 'pass'), None)
        if receipt is None:
            continue
        result = receipt['result']
        evidence = folder / 'history' / hashlib.sha256(run['id'].encode()).hexdigest()[:24]
        evidence.mkdir(parents=True, exist_ok=True)
        atomic(evidence / 'task-receipt.json', receipt)
        item = {'run_id': run['id'], 'requirement_id': run['requirement_id'],
                'branch': (result.get('instance') or {}).get('branch'), 'commit': run.get('commit'),
                'verified_at': receipt.get('at'), 'receipt_file': str(evidence / 'task-receipt.json'),
                'receipt_sha256': checksum(evidence / 'task-receipt.json'),
                'instance': result.get('instance'), 'checks': [],
                'scope': '历史 feat 验证，仅证明该提交当时通过；当前 release 及后续修复仍须独立验收。'}
        try:
            proof = result['quality']
            path = trusted_file(proof['receipt'], trusted)
            if checksum(path) != proof['receipt_hash']:
                raise ValueError('历史质量回执哈希不符')
            saved = json.loads(path.read_text())
            plan = trusted_file(path.with_name('plan.json'), trusted)
            if checksum(plan) != saved['plan_hash']:
                raise ValueError('历史质量计划哈希不符')
            copied_receipt = evidence / 'quality-receipt.json'
            copied_plan = evidence / 'quality-plan.json'
            copied_receipt.write_bytes(path.read_bytes())
            copied_plan.write_bytes(plan.read_bytes())
            item.update(quality_receipt_file=str(copied_receipt), quality_receipt_sha256=proof['receipt_hash'],
                        quality_plan_file=str(copied_plan), quality_plan_sha256=saved['plan_hash'])
            identity = {k: run.get(k) for k in ('commit', 'source_digest')}
            if saved['status'] != 'pass' or any(saved.get(k) != v for k, v in identity.items()):
                raise ValueError('历史质量回执与已验收任务版本不符')
            steps = json.loads(plan.read_text())['steps']
            if not steps or len(steps) != len(saved['checks']):
                raise ValueError('历史质量回执缺少步骤')
            instance = result.get('instance') or {}
            if instance.get('execution') != 'local' or instance.get('commit') != run.get('commit'):
                raise ValueError('历史本机实例与已验收提交不符')
            for index, (step, check) in enumerate(zip(steps, saved['checks'])):
                log = trusted_file(check['log'], trusted)
                if (check.get('status') != 'pass' or check.get('exit_code') != 0
                        or check.get('step_id') != step['step_id']
                        or check.get('declared_command') != step['command']
                        or check.get('commit') != run.get('commit')
                        or check.get('branch') not in (None, instance.get('branch'))
                        or checksum(log) != check['log_sha256']):
                    raise ValueError('历史质量步骤或日志失效')
                copy = evidence / f'check-{index}.log'
                copy.write_bytes(log.read_bytes())
                item['checks'].append({k: check.get(k) for k in ('name', 'status', 'branch', 'commit')}
                                      | {'log': str(copy), 'log_sha256': checksum(copy)})
            feature = next((p for p in batch.get('feature_prs', []) if run['id'] in p.get('run_ids', [])), None)
            if feature is None:
                raise ValueError('历史任务缺少已合入 feat PR 的关联')
            git(workspace, 'merge-base', '--is-ancestor', feature['merge_sha'], head)
            item['integration'] = feature
            item['business_verification'] = [c.get('evidence') for c in result.get('checks', [])
                                             if c.get('name') == '独立业务验证']
            for index, business in enumerate(item['business_verification']):
                if not isinstance(business, dict) or business.get('status') != 'pass':
                    raise ValueError('历史独立业务验证未通过')
                for key in ('diff_file', 'facts_file'):
                    if not business.get(key):
                        continue
                    original = trusted_file(business[key], trusted)
                    if key == 'diff_file' and checksum(original) != business.get('diff_sha256'):
                        raise ValueError('历史业务差异哈希不符')
                    copy = evidence / f'business-{index}-{original.name}'
                    copy.write_bytes(original.read_bytes())
                    business = business | {key: str(copy), key+'_sha256':checksum(copy)}
                item['business_verification'][index] = business
            item['integrity'] = {'status': 'pass', 'reason': '原始回执、计划和日志哈希匹配，feat 合并提交属于当前 release 历史'}
        except (KeyError, OSError, ValueError, subprocess.CalledProcessError) as exc:
            item['integrity'] = {'status': 'blocked', 'reason': str(exc)}
        history.append(item)
    return history


def checksum(path):
    """读取证据文件的 SHA-256 摘要。"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def trusted_file(path, root):
    """拒绝控制器证据目录之外的历史文件引用。"""
    path = Path(path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('历史文件不在控制器证据目录内')
    return path
