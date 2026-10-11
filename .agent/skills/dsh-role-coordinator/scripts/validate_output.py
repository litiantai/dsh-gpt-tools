"""校验协调输出使用的 JSON Schema 子集；JSON stdin/stdout，无状态副作用。"""
import json
import sys


def validate(value, schema, path='$'):
    types = schema.get('type')
    names = types if isinstance(types, list) else [types] if types else []
    matches = {'object':isinstance(value,dict), 'array':isinstance(value,list), 'string':isinstance(value,str),
               'integer':type(value) is int, 'number':type(value) in (int,float), 'boolean':type(value) is bool, 'null':value is None}
    if names and not any(matches.get(name,False) for name in names):
        raise ValueError(path + ' 类型不符合契约')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(path + ' 值不在允许范围')
    if isinstance(value,dict):
        for key in schema.get('required',[]):
            if key not in value:
                raise ValueError(path + ' 缺少字段 ' + key)
        properties = schema.get('properties',{})
        if schema.get('additionalProperties') is False and set(value)-set(properties):
            raise ValueError(path + ' 包含未登记字段')
        for key,item in value.items():
            if key in properties:
                validate(item,properties[key],path+'.'+key)
    if isinstance(value,list):
        for i,item in enumerate(value):
            validate(item,schema.get('items',{}),path+'['+str(i)+']')
    if type(value) in (int,float):
        if 'minimum' in schema and value < schema['minimum'] or 'maximum' in schema and value > schema['maximum']:
            raise ValueError(path + ' 数值超出允许范围')


if __name__ == '__main__':
    payload=json.load(sys.stdin)
    try:
        validate(payload['result'],payload['schema'])
        print(json.dumps({'status':'pass'}))
    except ValueError as exc:
        print(json.dumps({'status':'blocked','reason':str(exc)},ensure_ascii=False))
        sys.exit(1)
