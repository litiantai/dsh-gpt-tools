"""公开平台更新阻塞原因，并向独立更新器提交可审计的手动结束观察请求。"""
import json
from pathlib import Path
import time

from review_core import Conflict
from .call import atomic


def active_job(state):
    state = Path(state)
    drain = state/'autopilot/update-drain.json'
    if not drain.exists():
        return None
    path = Path(json.loads(drain.read_text())['job']).resolve()
    if path.parent != (state/'autopilot/updates').resolve() or path.suffix != '.json':
        raise ValueError('更新锁引用的任务路径无效')
    return path, json.loads(path.read_text())


def status(state):
    try:
        active = active_job(state)
        if not active:
            return {'active': False}
        path, job = active
        config = json.loads((Path(state)/'platform/updater/config.json').read_text())
        duration = config.get('observation_seconds', 1800)
        until = job.get('healthy_since', time.time()) + duration
        request = Path(state)/'autopilot/update-requests'/path.name
        pending = request.exists() and json.loads(request.read_text()).get('status') == 'queued'
        labels = {'draining': '等待活动任务结束', 'stopping': '停止旧服务', 'switching': '切换版本',
                  'checking': '检查新版本健康', 'observing': '更新观察中', 'rollback_check': '检查回滚健康',
                  'rolling_back': '正在回滚', 'rollback_retry': '等待重试回滚', 'blocked': '更新受阻'}
        return {'active': True, 'job_id': path.stem, 'phase': job['status'],
                'label': labels.get(job['status'], '平台更新中'), 'commit': job['expected_commit'],
                'reason': job.get('reason', ''), 'started_at': job.get('started'),
                'ends_at': until if job['status'] == 'observing' else None,
                'remaining_seconds': max(0, int(until-time.time())) if job['status'] == 'observing' else None,
                'release_requested': pending, 'can_release': job['status'] == 'observing' and not pending,
                'impact': '所有项目的新任务派发、重试和配置修改暂缓；查询和停止操作仍可使用。'}
    except (OSError, ValueError, KeyError) as exc:
        return {'active': True, 'phase': 'unknown', 'label': '更新状态异常', 'reason': str(exc),
                'can_release': False, 'impact': '更新锁仍在，需核对更新记录后恢复。'}


def request_release(state, body):
    active = active_job(state)
    if not active:
        raise Conflict('当前没有阻塞派发的平台更新，请刷新状态')
    path, job = active
    if body.get('job_id') != path.stem or body.get('commit') != job.get('expected_commit'):
        raise Conflict('更新版本已变化，请刷新后重试')
    if job['status'] != 'observing':
        raise Conflict('仅可手动结束健康观察，切换、启动及回滚阶段不能解除')
    request = Path(state)/'autopilot/update-requests'/path.name
    if not request.exists():
        request.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic(request, {'status': 'queued', 'job_id': path.stem, 'expected_commit': job['expected_commit'],
                         'operation_id': body['operation_id'], 'requested_at': time.time(),
                         'reason': '用户通过本机管理页面手动结束更新观察'})
    return status(state)
