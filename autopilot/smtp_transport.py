"""QQ SMTP 在 macOS 假 IP 代理断开 TLS 时使用独立物理接口连接。"""
from __future__ import annotations

import ipaddress
import json
from pathlib import Path
import smtplib
import socket
import ssl
import subprocess
import sys
from urllib.request import Request, urlopen


def _eligible(host, port):
    """只处理已确认的本机假 IP 场景，不改变系统或代理配置。"""
    if sys.platform != 'darwin' or host.lower().rstrip('.') not in ('smtp.qq.com', 'mp.mail.qq.com') or port != 465:
        return False
    if not Path('/tmp/verge/verge-mihomo.sock').exists():
        return False
    try:
        addresses = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        fake = ipaddress.ip_network('198.18.0.0/15')
        return bool(addresses) and all(ipaddress.ip_address(row[4][0]) in fake for row in addresses)
    except OSError:
        return False


def _interface():
    """读取默认物理接口；只对本次 SMTP socket 绑定该接口。"""
    output = subprocess.check_output(['/sbin/route', '-n', 'get', 'default'], text=True, timeout=3)
    names = [line.split(':', 1)[1].strip() for line in output.splitlines() if line.strip().startswith('interface:')]
    if len(names) != 1 or not names[0].startswith('en'):
        raise OSError('无法确认 SMTP 物理出口接口')
    return socket.if_nametoindex(names[0])


def _addresses(host):
    """通过 HTTPS DNS 获取公开地址，最终 TLS 仍验证原邮箱域名。"""
    last = None
    for origin in ('https://dns.alidns.com/resolve', 'https://cloudflare-dns.com/dns-query'):
        try:
            with urlopen(Request(origin + '?name=' + host + '&type=A', headers={'Accept': 'application/dns-json'}), timeout=8) as response:
                result = json.load(response)
            addresses = list(dict.fromkeys(row.get('data') for row in result.get('Answer', [])
                if row.get('type') == 1 and isinstance(row.get('data'), str)))
            addresses = [address for address in addresses if ipaddress.ip_address(address).version == 4 and ipaddress.ip_address(address).is_global]
            if result.get('Status') == 0 and addresses:
                return addresses
        except (OSError, ValueError) as exc:
            last = exc
    raise OSError('无法解析 SMTP 的公开地址') from last


def _direct_socket(host, port, timeout):
    """macOS IP_BOUND_IF 来自系统 SDK，仅影响创建的这一条连接。"""
    interface = _interface()
    last = None
    for address in _addresses(host):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            sock.setsockopt(socket.IPPROTO_IP, 25, interface)
            sock.connect((address, port))
            return sock
        except OSError as exc:
            last = exc
            sock.close()
    raise OSError('SMTP 物理出口连接失败') from last


class DirectSMTPSSL(smtplib.SMTP_SSL):
    """保留 SMTP 协议和原域名证书验证，仅改变底层 socket 路由。"""
    def _get_socket(self, host, port, timeout):
        sock = _direct_socket(host, port, timeout)
        try:
            return self.context.wrap_socket(sock, server_hostname=self._host)
        except Exception:
            sock.close()
            raise


def connect_ssl(host, port, *, timeout=15, factory=None):
    """常规 SMTP 优先；确认代理假 IP 的 TLS EOF 后仅对本连接降级到物理出口。"""
    factory = factory or smtplib.SMTP_SSL
    try:
        return factory(host, port, timeout=timeout)
    except ssl.SSLEOFError as original:
        if not _eligible(host, port):
            raise
        try:
            return DirectSMTPSSL(host, port, timeout=timeout, context=ssl.create_default_context())
        except (OSError, ValueError, subprocess.SubprocessError) as fallback:
            raise original from fallback
