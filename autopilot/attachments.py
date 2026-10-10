"""项目附件验证、隔离落盘与正文提取；解析失败保留明确状态。"""
import base64
import hashlib
import io
import uuid
import zipfile
from pathlib import Path
from xml.etree import ElementTree

MAX_SIZE = 10 * 1024 * 1024
MIMES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp',
         '.pdf': 'application/pdf', '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
         '.txt': 'text/plain', '.md': 'text/markdown'}


def upload(ledger, product_id, body):
    name = Path(str(body.get('name', ''))).name
    suffix = Path(name).suffix.lower()
    if suffix not in MIMES:
        raise ValueError('仅支持 PNG、JPEG、WebP、PDF、DOCX、TXT、Markdown')
    raw = base64.b64decode(body.get('data', ''), validate=True)
    if not raw or len(raw) > MAX_SIZE:
        raise ValueError('单文件必须为 1 字节至 10 MB')
    signatures = {'.png': raw.startswith(b'\x89PNG\r\n\x1a\n'), '.jpg': raw.startswith(b'\xff\xd8\xff'),
                  '.jpeg': raw.startswith(b'\xff\xd8\xff'), '.webp': raw[:4] == b'RIFF' and raw[8:12] == b'WEBP',
                  '.pdf': raw.startswith(b'%PDF-'), '.docx': raw.startswith(b'PK')}
    if suffix in signatures and not signatures[suffix]:
        raise ValueError('文件内容与扩展名不符')
    ident = str(uuid.uuid4())
    root = ledger.store.state / 'autopilot' / 'attachments' / product_id
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / (ident + suffix)
    with path.open('xb') as out:
        path.chmod(0o600)
        out.write(raw)
    value = {'product_id': product_id, 'name': name, 'mime': MIMES[suffix], 'size': len(raw),
             'sha256': hashlib.sha256(raw).hexdigest(), 'filename': path.name}
    status = 'ready'
    try:
        if suffix in ('.txt', '.md'):
            value['text'] = raw.decode('utf-8-sig')
        elif suffix == '.docx':
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                entry = archive.getinfo('word/document.xml')
                if entry.file_size > 20 * 1024 * 1024:
                    raise ValueError('解压后文档超过 20 MB')
                xml = archive.read(entry)
                if b'<!DOCTYPE' in xml or b'<!ENTITY' in xml:
                    raise ValueError('不支持包含实体定义的文档')
                tree = ElementTree.fromstring(xml)
                value['text'] = '\n'.join(''.join(p.itertext()) for p in tree.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p'))
        elif suffix == '.pdf':
            try:
                from pypdf import PdfReader
            except ImportError as exc:
                raise ValueError('PDF 解析依赖未安装，请安装 requirements.txt') from exc
            reader = PdfReader(io.BytesIO(raw))
            if reader.is_encrypted or len(reader.pages) > 200:
                raise ValueError('不支持加密 PDF 或超过 200 页的 PDF')
            value['text'] = '\n'.join(page.extract_text() or '' for page in reader.pages)
            if not value['text'].strip():
                raise ValueError('PDF 无可提取正文，请提供截图或文本版')
        if len(value.get('text', '')) > 100000:
            raise ValueError('提取正文超过 10 万字符，请拆分文档')
    except Exception as exc:
        status = 'parse_failed'
        value.pop('text', None)
        value['reason'] = str(exc)
    return ledger.create('attachments', value, status, ident=ident)


def read(ledger, product_id, ident):
    item = ledger.get('attachments', ident)
    if item['product_id'] != product_id:
        raise KeyError('附件不属于此项目')
    root = (ledger.store.state / 'autopilot' / 'attachments' / product_id).resolve()
    path = (root / item['filename']).resolve()
    if not path.is_relative_to(root):
        raise ValueError('附件路径无效')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != item['sha256']:
        raise ValueError('附件内容已改变')
    return item, path
