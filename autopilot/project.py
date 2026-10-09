"""版本化项目能力与通用适配协议；旧项目通过显式兼容边界保留。"""
from pathlib import Path
import hashlib
import json

VERSION = 1
CAPABILITIES = {'probe', 'inspect', 'verify', 'idle', 'publish', 'observe', 'rollback',
                'master_sync', 'final_acceptance', 'recover-runtime', 'investigate'}


def generic(product):
    return product.get('adapter_spec', {}).get('kind') == 'command'


def validate(config):
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
