"""隔离实例的本机会话初始化声明，与人工账号登录条件分开。"""
import json
from pathlib import Path

from .probes import safe_path


def validate(value):
    """只允许同源 GET 初始化及受保护接口探测，不接收账号或凭据。"""
    if not isinstance(value, dict) or set(value) != {'path', 'probe_path'}:
        raise ValueError('本机会话初始化须声明 path 和 probe_path')
    if any(not isinstance(value[k], str) or not safe_path(value[k]) for k in value):
        raise ValueError('本机会话路径必须为同源绝对路径')
    return dict(value)


def configuration(workspace, product):
    """以当轮提交的源码声明为准，兼容已保存的项目配置。"""
    path = Path(workspace)/'.autopilot.json'
    if path.is_symlink():
        raise ValueError('项目配置不能是符号链接')
    declared = json.loads(path.read_text()) if path.is_file() else {}
    value = declared.get('session_bootstrap', product.get('project_config', {}).get('session_bootstrap'))
    return validate(value) if value is not None else None
