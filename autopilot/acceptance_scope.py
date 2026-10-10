"""区分候选版本验收与发布后的正式实例效果复验。"""
from .role_skills import rule as skill_rule
from copy import deepcopy


PRE_RELEASE_INSTRUCTION = (
    skill_rule('acceptance_scope-acceptance-1')
)


def pre_release(requirement, record, instance=None):
    """提供控制器确定的阶段边界，保留原探针供发布后执行。"""
    return {
        'stage': 'pre_release',
        'candidate': {
            'commit': record.get('commit'),
            'branch': record.get('branch'),
            'test_instance': deepcopy(instance),
        },
        'post_release': {
            'stage': 'post_release',
            'status': 'pending',
            'resolution_probes': deepcopy((requirement or {}).get('resolution_probes', [])),
            'prerequisite': '正常发布完成并核对正式实例运行版本后，由控制器执行原需求效果断言',
        },
    }
