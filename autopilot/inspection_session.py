"""隔离实例的本机会话初始化声明，与人工账号登录条件分开。"""
import json
import http.cookiejar
from pathlib import Path
import re
import uuid
import urllib.error
import urllib.request

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


class LocalApiSession:
    """仅在受管隔离实例建立本机会话，凭据只保存在内存中。"""
    def __init__(self, origin):
        if not re.fullmatch(r'http://(?:127\.0\.0\.1|localhost):\d+', origin):
            raise ValueError('接口取证只能访问受管本机实例')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        self.origin = origin
        self.csrf = None
        self.client = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, path, method='GET', body=None, timeout=45, operation_id=None, private=False):
        if not safe_path(path) or method not in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE'):
            raise ValueError('接口请求路径或方法无效')
        headers = {}
        if method != 'GET':
            body = dict(body or {})
            body.setdefault('operation_id', operation_id or str(uuid.uuid4()))
            headers['Content-Type'] = 'application/json'
            if self.csrf:
                headers['X-CSRF-Token'] = self.csrf
        request = urllib.request.Request(self.origin + path,
            data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method)
        try:
            response = self.client.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            content = response.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024:
                raise ValueError('接口证据超过 1 MB')
            text=content.decode('utf-8', errors='replace')
            if not private:
                from .store import redact
                def scrub(value):
                    if isinstance(value,dict):
                        return {k:'[redacted]' if re.search(r'(?i)csrf',k) else scrub(v) for k,v in value.items()}
                    if isinstance(value,list):
                        return [scrub(v) for v in value]
                    return value
                try:
                    text=json.dumps(redact(scrub(json.loads(text))),ensure_ascii=False)
                except json.JSONDecodeError:
                    text=redact(text)
            return {'ok':200 <= response.code < 300, 'status':response.code,'text':text}

    def initialize(self, spec):
        spec = validate(spec)
        initialized = self.request(spec['path'], timeout=10, private=True)
        if not initialized['ok']:
            raise ValueError(f"本机会话初始化失败：HTTP {initialized['status']}")
        self.csrf = json.loads(initialized['text']).get('csrf')
        probe = self.request(spec['probe_path'], timeout=10)
        if not probe['ok']:
            raise ValueError(f"本机会话探测失败：HTTP {probe['status']}")
        return {'session_ready':True, 'bootstrap_status':initialized['status'], 'probe_status':probe['status']}
