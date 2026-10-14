"""检查任务依赖和需求验收覆盖；从 JSON stdin 读取，不修改文件。"""
from collections import deque
import json
import sys


def _strings(value):
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def validate(value):
    if not isinstance(value, dict):
        return {'status': 'fail', 'problems': ['任务检查输入必须为对象']}
    tasks = value.get('tasks', [])
    problems = []
    if not isinstance(tasks, list) or not tasks:
        return {'status': 'fail', 'problems': ['缺少实施任务']}
    required = value.get('required_acceptance_ids', [])
    if not _strings(required):
        problems.append('required_acceptance_ids 必须为非空文本项列表')
        required = []
    by_id = {}
    covered = set()
    dependencies = {}
    for task in tasks:
        ident = task.get('id') if isinstance(task, dict) else None
        if not isinstance(ident, str) or not ident.strip() or ident in by_id:
            problems.append('任务标识缺失或重复')
            continue
        by_id[ident] = task
        if any(not isinstance(task.get(key), str) or not task[key].strip() for key in ('scope', 'verification')):
            problems.append('任务缺少修改范围或完成检查：' + ident)
        deps = task.get('depends_on', [])
        if not _strings(deps):
            problems.append('depends_on 必须为非空文本项列表：' + ident)
            deps = []
        dependencies[ident] = set(deps)
        acceptance = task.get('acceptance_ids', [])
        if not _strings(acceptance):
            problems.append('acceptance_ids 必须为非空文本项列表：' + ident)
        else:
            covered.update(acceptance)

    # 使用拓扑检查而非递归，较长任务链也能验证。
    dependents = {ident: [] for ident in by_id}
    indegree = {}
    for ident, deps in dependencies.items():
        for dependency in deps:
            if dependency not in by_id:
                problems.append('依赖任务不存在：' + dependency)
            else:
                dependents[dependency].append(ident)
        indegree[ident] = sum(dependency in by_id for dependency in deps)
    ready = deque(ident for ident, count in indegree.items() if count == 0)
    visited = 0
    while ready:
        ident = ready.popleft()
        visited += 1
        for dependent in dependents[ident]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    if visited != len(by_id):
        problems.append('任务依赖成环')
    missing = set(required) - covered
    if missing:
        problems.append('验收条件未落实到任务：' + '、'.join(sorted(missing)))
    return {'status': 'fail' if problems else 'pass', 'problems': problems}


if __name__ == '__main__':
    try:
        result = validate(json.load(sys.stdin))
    except (ValueError, UnicodeError):
        result = {'status': 'fail', 'problems': ['输入不是有效 JSON']}
    print(json.dumps(result, ensure_ascii=False))
