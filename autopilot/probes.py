"""只读上线断言：固定同源 GET 与 JSON 字段判断，不执行模型提供的代码。"""
import re


def safe_path(path):
    return isinstance(path, str) and bool(re.fullmatch(r"/[a-zA-Z0-9/_-]*", path)) and "//" not in path


def validate(probes, product=None, assertions=False):
    if not isinstance(probes,list):
        raise ValueError('上线验证必须为只读探针列表')
    for probe in probes:
        if isinstance(probe,str):
            if assertions:
                raise ValueError('业务验收必须提供字段断言，不能仅检查接口可达')
            path=probe
        elif isinstance(probe,dict):
            path=probe.get('path','')
            if probe.get('operator') not in ('equals','not_equals','not_empty','not_contains') or not isinstance(probe.get('pointer'),str):
                raise ValueError('上线验证断言无效')
        else:
            raise ValueError('上线验证探针无效')
        if product and isinstance(path, str) and path.startswith('check:'):
            if path[6:] not in product.get('project_config', {}).get('acceptance_checks', {}):
                raise ValueError('验收检查未在项目配置中登记')
            continue
        allowed = product.get('project_config', {}).get('readonly_paths', []) if product else None
        valid = safe_path(path) and path in allowed if allowed is not None else bool(re.fullmatch(r'/ths-octop(?:-[a-z-]+)?/api/[a-zA-Z0-9/_-]+', path))
        if not valid:
            raise ValueError('上线验证探针不是允许的只读路径')


def check(probes,fetch,product=None):
    validate(probes,product,assertions=product is not None)
    if not probes:
        return False
    for probe in probes:
        data=fetch(probe if isinstance(probe,str) else probe['path'])
        if product is None and data.get('ok') is not True:
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
