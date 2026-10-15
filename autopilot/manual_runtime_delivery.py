"""登记已验证的手工运行时发行，并关闭尚未开始的同一需求任务。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time

from review_core import Conflict
from .project import manifest_hash
from .store import redact


def _read(path):
    return json.loads(Path(path).read_text())


def _fingerprint(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _proof(state, manifest_path):
    """核对已完成更新、当前候选身份、产物完整性及真实检查日志。"""
    state = Path(state).resolve()
    manifest_path = Path(manifest_path).resolve()
    releases = state/'autopilot/manual-releases'
    if not manifest_path.is_relative_to(releases) or manifest_path.name != 'manifest.json':
        raise ValueError('手工发行清单必须位于本实例的受管手工发行目录')
    manifest = _read(manifest_path)
    release_id = manifest.get('release_id')
    if not isinstance(release_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{1,180}', release_id):
        raise ValueError('发行标识无效')
    artifact = Path(manifest.get('artifact', '')).resolve()
    if (manifest.get('version') != 1 or manifest.get('database_compatibility') != 'backward-compatible'
            or artifact != manifest_path.parent/'artifact' or artifact.parent.name != release_id):
        raise ValueError('发行清单版本、产物路径或数据兼容声明无效')
    current = state/'platform/current'
    if not current.is_symlink() or current.resolve() != artifact:
        raise Conflict('正式运行版本与此手工发行不一致')
    marker = _read(artifact/'autopilot-release.json')
    if any(marker.get(key) != manifest.get(key) for key in ('product_id', 'commit', 'release_id')):
        raise ValueError('正式运行标记与发行清单不一致')
    if manifest_hash(artifact) != manifest.get('artifact_hash'):
        raise ValueError('已验证的手工发行产物发生变化')
    checks = manifest.get('checks')
    if not isinstance(checks, list) or not checks or not any(check.get('required', True) for check in checks):
        raise ValueError('手工发行缺少必需验证回执')
    names = set()
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get('name'), str) or not check['name'] or check['name'] in names:
            raise ValueError('发行检查名称无效或重复')
        names.add(check['name'])
        if check.get('required', True) and (check.get('status') != 'pass' or check.get('exit_code') != 0):
            raise ValueError('手工发行的必需验证未通过')
        log = Path(check.get('log', '')).resolve()
        if (not log.is_relative_to(manifest_path.parent/'checks') or not log.is_file()
                or check.get('cwd') != str(artifact) or _fingerprint(log) != check.get('log_sha256')):
            raise ValueError('发行验证日志身份或内容哈希不一致')
    job_path = state/'autopilot/updates'/(release_id+'.json')
    job = _read(job_path)
    if (job.get('status') != 'completed' or not job.get('completed_at')
            or Path(job.get('manifest', '')).resolve() != manifest_path
            or job.get('product_id') != manifest.get('product_id')
            or job.get('expected_commit') != manifest.get('commit')):
        raise Conflict('手工发行尚未通过独立更新器的切换及健康观察')
    # 读取及哈希计算期间如出现另一版切换，不把旧候选登记为当前交付。
    if current.resolve() != artifact or _read(current/'autopilot-release.json') != marker:
        raise Conflict('验证交付证据期间正式版本已变化')
    return manifest, job_path, checks


def _acceptance(requirement, acceptance_checks, checks):
    """每条原验收条件须映射到清单中实际通过的检查，不靠总括结论。"""
    expected = requirement.get('acceptance')
    if not isinstance(expected, list) or not expected or not all(isinstance(item, str) and item for item in expected):
        raise ValueError('原需求缺少可逐条验证的验收条件')
    if len(set(expected)) != len(expected):
        raise ValueError('原需求验收条件重复，需先核对原需求')
    if not isinstance(acceptance_checks, list) or len(acceptance_checks) != len(expected):
        raise ValueError('手工发行未覆盖全部原验收条件')
    actual = [row.get('acceptance') for row in acceptance_checks if isinstance(row, dict)]
    if len(actual) != len(expected) or len(set(actual)) != len(actual) or set(actual) != set(expected):
        raise ValueError('手工发行验收映射与原需求不一致')
    passed = {row['name'] for row in checks if row.get('status') == 'pass' and row.get('exit_code') == 0}
    for row in acceptance_checks:
        linked = row.get('checks')
        if (row.get('status') != 'pass' or not isinstance(linked, list) or not linked
                or any(not isinstance(name, str) or name not in passed for name in linked)):
            raise ValueError('原需求验收条件未关联实际通过的发行检查')
    return redact(acceptance_checks)


def register_manual_runtime_delivery(ledger, requirement_id, run_id, manifest_path, acceptance_checks):
    """以平台事务登记本机手工交付；不伪造 Git 合入或自动开发成功。"""
    manifest, job_path, checks = _proof(ledger.store.state, manifest_path)
    identity = {'release_id':manifest['release_id'], 'artifact_hash':manifest['artifact_hash'],
                'manifest':str(Path(manifest_path).resolve()), 'update_job':str(job_path),
                'commit':manifest['commit'], 'artifact':manifest['artifact']}
    with ledger.store.transaction() as db:
        requirement = ledger.get('requirements', requirement_id, db)
        run = ledger.get('runs', run_id, db)
        if (requirement['product_id'] != manifest['product_id'] or run['product_id'] != requirement['product_id']
                or run.get('requirement_id') != requirement_id or requirement.get('run_id') != run_id):
            raise Conflict('发行与需求、排队任务的项目或绑定关系不一致')
        mapped = _acceptance(requirement, acceptance_checks, checks)
        previous = requirement.get('manual_runtime_delivery')
        if previous:
            if (all(previous.get(key) == value for key, value in identity.items())
                    and previous.get('acceptance_checks') == mapped
                    and requirement['status'] == 'delivered' and run['status'] == 'cancelled'
                    and run.get('manual_runtime_delivery') == previous):
                return {'requirement':requirement, 'run':run, 'replayed':True}
            raise Conflict('已有不同的手工交付记录，禁止覆盖历史')
        if (requirement['status'] != 'queued' or run['status'] != 'queued'
                or run.get('budget_reserved') is not False or run.get('receipts') != []
                or run.get('call') or run.get('review_id') or run.get('control')
                or run.get('execution_seconds', 0) != 0 or run.get('revisions', 0) != 0
                or any(run.get(key) for key in ('workspace', 'commit', 'accepted_at', 'online_at', 'merge_sha', 'manifest'))):
            raise Conflict('仅可关闭尚未执行且未占预算的同一需求排队任务')
        current = Path(ledger.store.state)/'platform/current'
        if current.resolve() != Path(manifest['artifact']).resolve():
            raise Conflict('登记交付前正式版本已变化')
        proof = identity | {'acceptance_checks':mapped, 'at':time.time(),
                            'kind':'manual_runtime_delivery', 'git_delivery':'not_recorded',
                            'verification':'independent_updater_completed_and_required_checks_passed'}
        reason = ('该排队任务由已验证的手工运行时发行覆盖；发行 ' + manifest['release_id']
                  + '；证据 ' + str(Path(manifest_path).resolve()) + '；Git交付未登记。')
        run = ledger.update('runs', run_id, run['version'], {'manual_runtime_delivery':proof,
                            'reason':reason, 'control':None}, 'cancelled', db=db)
        requirement = ledger.update('requirements', requirement_id, requirement['version'],
                      {'manual_runtime_delivery':proof, 'reason':reason}, 'delivered', db=db)
        ledger.store.event('autopilot_manual_runtime_delivered', detail={
            'product_id':requirement['product_id'], 'requirement_id':requirement_id,
            'run_id':run_id, 'release_id':manifest['release_id'], 'artifact_hash':manifest['artifact_hash'],
            'manifest':str(Path(manifest_path).resolve()), 'git_delivery':'not_recorded'}, db=db)
        return {'requirement':requirement, 'run':run, 'replayed':False}
