"""只读上线断言：固定同源 GET 与 JSON 字段判断，不执行模型提供的代码。"""
import re


def validate(probes):
    if not isinstance(probes,list):
        raise ValueError('上线验证必须为只读探针列表')
    for probe in probes:
        if isinstance(probe,str):
            path=probe
        elif isinstance(probe,dict):
            path=probe.get('path','')
            if probe.get('operator') not in ('equals','not_equals','not_empty','not_contains') or not isinstance(probe.get('pointer'),str):
                raise ValueError('上线验证断言无效')
        else:
            raise ValueError('上线验证探针无效')
        if not re.fullmatch(r'/ths-octop(?:-[a-z-]+)?/api/[a-zA-Z0-9/_-]+',path) or '..' in path:
            raise ValueError('上线验证探针不是允许的只读路径')


def check(probes,fetch):
    validate(probes)
    if not probes:
        return False
    for probe in probes:
        data=fetch(probe if isinstance(probe,str) else probe['path'])
        if data.get('ok') is not True:
            return False
        if isinstance(probe,str):
            continue
        values=[data]
        for part in probe['pointer'].strip('/').split('/'):
            part=part.replace('~1','/').replace('~0','~')
            next_values=[]
            for value in values:
                if part=='*' and isinstance(value,list):
                    next_values.extend(value)
                elif isinstance(value,dict) and part in value:
                    next_values.append(value[part])
            values=next_values
        if not values:
            return False
        expected=probe.get('expected')
        for value in values:
            op=probe['operator']
            if op=='equals' and value!=expected or op=='not_equals' and value==expected:
                return False
            if op=='not_empty' and not value:
                return False
            if op=='not_contains' and (not isinstance(value,(list,str)) or expected in value):
                return False
    return True
