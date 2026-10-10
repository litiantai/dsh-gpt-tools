"""扫描来源的稳定标识；本地工作区与远程提交分别保留验证记录。"""
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def repository_key(source):
    """合并本地路径别名及 HTTPS 地址的常见等价写法。"""
    source = source.strip()
    if source.startswith('https://'):
        address = urlsplit(source)
        host = address.hostname or ''
        if ':' in host:
            host = '[' + host + ']'
        if address.port and address.port != 443:
            host += ':' + str(address.port)
        path = address.path.rstrip('/')
        if path.endswith('.git'):
            path = path[:-4]
        return urlunsplit(('https', host, path, '', ''))
    return str(Path(source).resolve())
