"""区分候选版本验收与发布后的正式实例效果复验。"""
from copy import deepcopy


PRE_RELEASE_INSTRUCTION = (
    '当前为发布前候选版本验收（pre_release），判定对象是当前 feat/release 提交及其测试证据。'
    '原需求的代码、功能、兼容性和测试条件仍须逐项验证；缺陷返回 fail，必需证据不足返回 blocked。'
    '验收条件中要求正式实例已更新、正式接口返回新能力或提供上线效果回执的部分，'
    '属于发布后复验，由控制器在正常发布并核对运行版本后执行原始 resolution_probes。'
    '正式实例此时仍是旧版本属于预期，不能仅因缺少发布后回执而阻塞候选验收，'
    '也不能要求开发进程提前部署。候选测试通过不能充当正式实例验证通过；'
    '发布后条件保持 pending，禁止删改原验收条件或探针、宣称已上线或问题已解决。'
    '缺少有效上线探针时须明确报告证据缺口，不得将上线条件当作已满足。'
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
