"""校验需求文档的最低可实施内容；从 JSON stdin 读取，不修改文件。"""
import json
import sys


def validate(document):
    if not isinstance(document, dict):
        return {'status': 'fail', 'problems': ['需求必须为对象'], 'ready_for_confirmation': False}
    problems = []
    for key in ('title', 'goal', 'scenario', 'scope', 'evidence', 'impact'):
        if not isinstance(document.get(key), str) or not document[key].strip():
            problems.append('缺少有效字段：' + key)
    for key in ('acceptance', 'questions'):
        value = document.get(key)
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
            problems.append('字段必须为非空文本项列表：' + key)
    if not document.get('acceptance'):
        problems.append('缺少可观察验收条件')
    return {'status': 'fail' if problems else 'pass', 'problems': problems,
            'ready_for_confirmation': not problems and not document.get('questions')}


if __name__ == '__main__':
    try:
        result = validate(json.load(sys.stdin))
    except (ValueError, UnicodeError):
        result = {'status': 'fail', 'problems': ['输入不是有效 JSON'], 'ready_for_confirmation': False}
    print(json.dumps(result, ensure_ascii=False))
