"""源码声明的隔离场景准备及实际响应绑定，不伪造业务台账或执行结果。"""
import json
from pathlib import Path
import re
import uuid

from .inspection_session import LocalApiSession, configuration
from .probes import safe_path
from .store import redact

TOKEN = re.compile(r'\{([a-z][a-z0-9_]*)\}')


def validate_bindings(items):
    """限制响应引用数量、类型及公开字段，重复名称在请求前拒绝。"""
    if not isinstance(items,list) or len(items)>20:
        raise ValueError('场景响应引用须为不超过20项的数组')
    names=set()
    for item in items:
        if not isinstance(item,dict) or set(item)!={'name','pointer'} or not isinstance(item.get('name'),str) or not isinstance(item.get('pointer'),str) or not re.fullmatch(r'[a-z][a-z0-9_]*',item['name']) or not re.fullmatch(r'/(?:[0-9]+/)*(?:id|version|status|name)',item['pointer']) or item['name'] in names:
            raise ValueError('场景响应绑定仅允许不重复的公开标识、版本和状态')
        names.add(item['name'])


def resolve(value, bindings):
    """替换控制器已保存的实际响应值，未知引用直接受阻。"""
    if isinstance(value, dict):
        output={}
        for key, item in value.items():
            if key in ('api_body','expected_json') and item:
                try:
                    parsed=json.loads(item)
                except json.JSONDecodeError:
                    parsed=json.loads(re.sub(r'(?<=[\s:,\[])\{([a-z][a-z0-9_]*)\}(?=[\s,}\]])',lambda m:json.dumps(m[0]),item))
                output[key]=json.dumps(resolve(parsed,bindings),ensure_ascii=False)
            else:
                output[key]=resolve(item,bindings)
        return output
    if isinstance(value, list):
        return [resolve(v, bindings) for v in value]
    if not isinstance(value, str):
        return value
    match = TOKEN.fullmatch(value)
    if match and match[1] in bindings:
        return bindings[match[1]]
    def replacement(match):
        if match[1] not in bindings:
            raise ValueError('场景引用尚未准备：'+match[1])
        return str(bindings[match[1]])
    return TOKEN.sub(replacement, value)


def bind(step, response, bindings):
    """仅保存真实 JSON 响应的公开标识及版本，禁止凭据绑定。"""
    items=step.get('response_bindings', [])
    validate_bindings(items)
    for item in items:
        name = item['name']; pointer = item['pointer']
        if name in bindings:
            raise ValueError('场景引用不能重复绑定：'+name)
        value = json.loads(response['text'])
        for part in pointer.lstrip('/').split('/'):
            value = value[int(part)] if isinstance(value, list) else value[part]
        if not isinstance(value, (str, int, bool)) or len(str(value)) > 200:
            raise ValueError('场景引用必须为简短的公开值')
        bindings[name] = value


def configuration_for(workspace):
    path = Path(workspace)/'.autopilot.json'
    if path.is_symlink():
        raise ValueError('项目配置不能是符号链接')
    config = json.loads(path.read_text()) if path.is_file() else {}
    steps = config.get('inspection_setup', [])
    if not isinstance(steps, list) or len(steps) > 30:
        raise ValueError('场景准备步骤须为不超过30项的数组')
    for step in steps:
        if not isinstance(step, dict) or set(step)-{'title','path','method','body','response_bindings'}:
            raise ValueError('场景准备字段无效')
        if not step.get('title') or not safe_path(step.get('path','')) or step.get('method') not in ('GET','POST'):
            raise ValueError('场景准备仅允许声明同源读取和登记操作')
        if step.get('method') == 'POST' and step['path'] not in ('/api/products','/api/requirements','/api/signals'):
            raise ValueError('场景准备不能启动执行、发布或外部投递')
        validate_bindings(step.get('response_bindings', []))
    return steps


def prepare(workspace, product, instance, root):
    """在正式路线执行前，通过真实管理接口登记暂停项目、需求和信号。"""
    steps = configuration_for(workspace)
    if not steps:
        return {'bindings':{}, 'receipt':None}
    if instance.get('instance_role') != 'isolated':
        raise ValueError('场景准备只允许隔离实例')
    path = Path(root)/'inspection-data-preparation.json'
    if path.exists():
        raise ValueError('已有场景准备回执，须先核对实际记录，不能重复提交')
    client = LocalApiSession(instance['origin'])
    session = client.initialize(configuration(workspace, product))
    bindings = {'workspace':str(Path(workspace).resolve())}
    receipt = {'purpose':'inspection_data_prepare','session':session,'steps':[], 'status':'running'}
    def save():
        path.write_text(json.dumps(redact(receipt), ensure_ascii=False, indent=2)+'\n')
    save()
    try:
        for source in steps:
            step = resolve(source, bindings)
            operation_id = str(uuid.uuid4())
            receipt['steps'].append({'title':step['title'],'path':step['path'],'method':step['method'],
                'body':step.get('body'),'operation_id':operation_id,'status':'submitted'})
            save()
            if step['path']=='/api/products' and step['method']=='POST':
                config = step.get('body',{}).get('config',{})
                if config.get('automation_disabled') is not True or config.get('git'):
                    raise ValueError('准备项目必须停用自动调度和自动交付')
            response = client.request(step['path'], step['method'], step.get('body'), operation_id=operation_id)
            receipt['steps'][-1].update(response=response, status='completed' if response['ok'] else 'blocked')
            save()
            if not response['ok']:
                raise ValueError('场景准备接口失败：HTTP '+str(response['status']))
            bind(source, response, bindings)
        receipt.update(status='pass', bindings=bindings)
        save()
        return {'bindings':bindings, 'receipt':str(path)}
    except Exception:
        receipt['status']='blocked';save()
        raise
