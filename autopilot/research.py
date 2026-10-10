"""公开竞品来源采集；对每次连接及重定向固定经过校验的公网地址。"""
import hashlib
import http.client
import ipaddress
import socket
import ssl
import time
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit, urljoin


class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script','style','noscript'):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ('script','style','noscript'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def normalize_url(url):
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError('来源链接无效')
    parts = urlsplit(url)
    if parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password or parts.port not in (None,80,443):
        raise ValueError('来源必须为不含凭据的公开 HTTP(S) 链接')
    if parts.hostname.lower() == 'localhost' or parts.hostname.lower().endswith(('.localhost','.local')):
        raise ValueError('竞品来源不得访问本机或局域网')
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise ValueError('竞品来源必须为公网地址')
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or '/', parts.query, ''))


def fetch(url):
    original = normalize_url(url)
    current = original
    for _ in range(5):
        parts = urlsplit(current)
        port = parts.port or (443 if parts.scheme == 'https' else 80)
        addresses = {a[4][0] for a in socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)}
        if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
            raise ValueError('来源解析到了非公网地址')
        address = sorted(addresses)[0]
        connection = http.client.HTTPConnection(parts.hostname, port, timeout=10)
        connection.sock = socket.create_connection((address, port), timeout=10)
        if parts.scheme == 'https':
            connection.sock = ssl.create_default_context().wrap_socket(connection.sock, server_hostname=parts.hostname)
        try:
            connection.request('GET', urlunsplit(('', '', parts.path or '/', parts.query, '')),
                               headers={'User-Agent': 'DshResearch/1.0', 'Accept': 'text/html,text/plain', 'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            if response.status in (301,302,303,307,308):
                current = normalize_url(urljoin(current, response.getheader('Location', '')))
                continue
            if response.status != 200:
                raise ValueError(f'来源返回 HTTP {response.status}')
            mime = response.getheader('Content-Type', '').lower()
            if not any(t in mime for t in ('text/html','text/plain','application/xhtml')):
                raise ValueError('来源不是可读取的 HTML 或文本页面')
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError('来源页面超过 2 MB')
            parser = Text()
            parser.feed(raw.decode('utf-8', errors='replace'))
            content = '\n'.join(parser.parts)
            if len(content.strip()) < 40:
                raise ValueError('来源正文不足，可能需要登录或浏览器渲染')
            return {'url': original, 'final_url': current, 'fetched_at': time.time(), 'status': 'pass',
                    'fingerprint': hashlib.sha256(content.encode()).hexdigest(), 'content': content[:30000]}
        finally:
            connection.close()
    raise ValueError('来源重定向次数过多')


def collect(competitors):
    snapshots = []
    for competitor in competitors:
        for url in competitor['urls']:
            try:
                value = fetch(url)
            except Exception as exc:
                value = {'url': url, 'fetched_at': time.time(), 'status': 'failed', 'reason': str(exc)}
            snapshots.append(value | {'competitor_id': competitor['id']})
    return snapshots
