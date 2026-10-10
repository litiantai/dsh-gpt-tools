"""版本化项目能力与通用适配协议；旧项目通过显式兼容边界保留。"""
from pathlib import Path
import hashlib
import json

VERSION = 1
# 通用扫描/应用所支持的唯一适配协议版本；接入能力预检与后端兜底共用同一常量。
SUPPORTED_ADAPTER_VERSION = VERSION
CAPABILITIES = {'probe', 'inspect', 'verify', 'idle', 'publish', 'observe', 'rollback',
                'master_sync', 'final_acceptance', 'recover-runtime', 'investigate'}


def generic(product):
    return product.get('adapter_spec', {}).get('kind') == 'command'


def scan_compatible(product):
    """通用扫描/验收的统一能力门槛：协议版本、类型与 verify 能力缺一不可。

    前端预检与后端扫描/应用入口必须使用同一规则，避免只校验 kind/capabilities
    而让协议版本错配的项目进入隔离工作区。legacy 项目显式返回 False。
    """
    spec = product.get('adapter_spec')
    if not isinstance(spec, dict):
        return False
    if spec.get('version') != SUPPORTED_ADAPTER_VERSION:
        return False
    if spec.get('kind') != 'command':
        return False
    capabilities = spec.get('capabilities')
    return isinstance(capabilities, list) and 'verify' in capabilities


def validate(config):
    if config.get('functional_findings_policy', 'block') not in ('block', 'backlog'):
        raise ValueError('功能缺陷处理策略必须为 block 或 backlog')
    if config.get('test_execution', 'isolated') not in ('isolated', 'local'):
        raise ValueError('测试执行方式必须为 local 或 isolated')
    spec = config.get('adapter_spec')
    if spec is None:
        return
    if not isinstance(spec, dict) or spec.get('version') != VERSION or spec.get('kind') not in ('command', 'legacy'):
        raise ValueError('不支持的项目适配协议版本')
    if not isinstance(spec.get('capabilities'), list) or not set(spec['capabilities']) <= CAPABILITIES:
        raise ValueError('项目能力声明无效')
    settings = config.get('project_config', {})
    if settings.get('version', VERSION) != VERSION:
        raise ValueError('不支持的项目配置版本')
    for key in ('install', 'build', 'test', 'start', 'browser'):
        commands = settings.get('commands', {}).get(key, [])
        if not isinstance(commands, list):
            raise ValueError('项目命令必须为参数数组列表')
        for cmd in commands:
            if not isinstance(cmd, list) or not cmd or not all(isinstance(s, str) and s for s in cmd):
                raise ValueError('项目命令必须为非空参数数组')
    for name, argv in settings.get('acceptance_checks', {}).items():
        if not isinstance(name, str) or not name or not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
            raise ValueError('验收检查须声明名称和命令参数数组')
    if 'test_start' in settings:
        starts = settings['test_start']
        if (not isinstance(starts, list) or len(starts) != 1 or not isinstance(starts[0], list)
                or not starts[0] or not all(isinstance(s, str) and s for s in starts[0])):
            raise ValueError('本机测试实例必须指定唯一启动命令')
    ports=settings.get('test_ports', [])
    if not isinstance(ports, list) or any(p != 'test-loopback' and (type(p) is not int or not 1 <= p <= 65535 or p in (13081,13083,13084)) for p in ports):
        raise ValueError('测试端口配置无效或占用正式服务端口')
    for path in settings.get('readonly_paths', []):
        from .probes import safe_path
        if not safe_path(path):
            raise ValueError('只读接口路径无效')


def manifest_hash(root):
    root = Path(root).resolve()
    rows = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            if not path.resolve().is_relative_to(root):
                raise ValueError('产物含越界符号链接')
            rows.append((str(path.relative_to(root)), 'link:' + str(path.readlink())))
        elif path.is_file():
            rows.append((str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest()))
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def migrate(ledger):
    """仅补充兼容元数据；原字段、标识及证据引用原样保留。"""
    with ledger.store.transaction() as db:
        for row in db.execute('SELECT * FROM auto_products').fetchall():
            item = ledger.decode(row)
            if item.get('schema_version', 0) >= 1:
                continue
            changes = {'schema_version': 1, 'compatibility': {'status': 'legacy', 'original_fields_preserved': True}}
            if not item.get('adapter_spec'):
                changes['adapter_spec'] = {'version': 1, 'kind': 'legacy', 'capabilities': sorted(CAPABILITIES)}
            ledger.update('products', item['id'], item['version'], changes, db=db)
