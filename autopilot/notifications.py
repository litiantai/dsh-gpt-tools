"""本机邮件通知：凭据隔离、项目订阅、事件触发与可复算的邮件简报。

安全边界：
- SMTP 授权码按「项目 + 发件账户 + 不可变版本」隔离写入本机凭据目录（目录 700、
  文件 600），只有 SQLite 配置提交的引用才生效；旧作用域和全局文件仅作只读兼容来源。
  授权码绝不进入项目台账、操作日志、事件或邮件正文；接口只回布尔状态。
- 自动化测试只通过注入的假 SMTP 客户端验证行为，不联网、不使用真实凭据。
- 发送在调度器单线程内完成，每个 tick 最多一封，不新开线程或常驻子进程。
"""
from __future__ import annotations

import datetime as dt
import copy
import hashlib
import html as html_module
import json
import math
import os
import re
import smtplib
import time
import uuid
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from zoneinfo import ZoneInfo

from review_core import Conflict
from .store import DEFAULTS, SECRET

ZONE = ZoneInfo('Asia/Shanghai')
EVENTS = ('run_report', 'alert', 'quota_exhausted', 'competitor', 'morning_retro')
REPORT_STATUSES = ('plan_review', 'accepted', 'delivered', 'online', 'completed')
TLS_CHOICES = ('ssl', 'starttls', 'none')
MAX_RECIPIENTS = 50
MAX_ATTEMPTS = 5
BACKOFF_SECONDS = 60
SMTP_TIMEOUT = 20
MORNING_HOUR = 7
EMAIL = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
HOST = re.compile(r'^[A-Za-z0-9.-]{1,253}$')
SECTIONS = (
    ('一、概览指标', '概览指标'),
    ('二、昨日完成', '昨日完成'),
    ('三、阻塞与问题', '阻塞与问题'),
    ('四、额度使用', '额度使用'),
    ('五、竞品动态', '竞品动态'),
    ('六、今日待办与自进化建议', '今日待办与自进化建议'),
)
KIND_LABELS = {
    'run_report': '运行节点报告',
    'alert': '告警',
    'quota_exhausted': '额度用尽提示',
    'competitor': '竞品发现-分析',
    'morning_retro': '每日复盘简报',
    'test': '通知测试',
}


# ---------------------------------------------------------------- 账户与凭据

SCOPE = re.compile(r'^[0-9A-Za-z._-]{1,64}$')
REFERENCE = re.compile(r'^[0-9a-f]{32}$')
LEGACY_CREDENTIAL = object()
PUBLIC_CONFIG_FIELDS = {'enabled', 'sender', 'recipients', 'events', 'smtp', 'morning_hour'}


def credential_dir():
    """作用域凭据目录：按项目与发件账户隔离存储。"""
    return Path(os.environ.get('DSH_HOME', str(Path.home() / '.dsh'))) / 'supervisor/credentials/smtp'


def _scope(product_id):
    """把项目 ID 收敛为安全目录名，拒绝路径分隔符与父目录引用。"""
    value = str(product_id or '')
    if value not in ('.', '..') and SCOPE.fullmatch(value):
        return value
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def credential_path(product_id, sender):
    """作用域凭据文件：只以发件人哈希入文件名，不泄露完整账号。"""
    digest = hashlib.sha256(str(sender or '').encode()).hexdigest()[:32]
    return credential_dir() / _scope(product_id) / (digest + '.code')


def credential_file(product_id=None, sender=None, *, credential_ref=LEGACY_CREDENTIAL):
    """SMTP 授权码本机文件，与项目源码和业务台账隔离。

    未传引用时返回旧路径供兼容读取；显式引用只能指向该账户的不可变版本。
    """
    if credential_ref is not LEGACY_CREDENTIAL:
        if (not isinstance(credential_ref, str) or not REFERENCE.fullmatch(credential_ref)
                or product_id is None or not sender):
            return None
        legacy = credential_path(product_id, sender)
        return legacy.with_suffix('') / (credential_ref + '.code')
    if product_id is not None and sender:
        return credential_path(product_id, sender)
    return Path(os.environ.get('DSH_HOME', str(Path.home() / '.dsh'))) / 'supervisor/credentials/email'


def _read_credential(path):
    if path is None:
        return None
    try:
        value = path.read_text().strip()
    except (OSError, UnicodeError):
        return None
    return value or None


def _fsync_directory(path):
    """持久化目录项，确保引用提交前新凭据及其父目录已落盘。"""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _secure_dir(*paths):
    for path in paths:
        missing = []
        ancestor = path
        while not ancestor.exists():
            missing.append(ancestor)
            ancestor = ancestor.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700, exist_ok=True)
            _fsync_directory(directory.parent)
        os.chmod(path, 0o700)
        _fsync_directory(path)


def _write_credential(path, value):
    """只创建新版本，不替换现有文件；未提交版本可安全留作不生效的孤儿。"""
    _secure_dir(credential_dir(), path.parent.parent, path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(value)
        output.flush()
        os.fsync(output.fileno())
    _fsync_directory(path.parent)


def credential(product_id=None, sender=None, *, credential_ref=LEGACY_CREDENTIAL):
    """只在内存中读取授权码，不写台账、不回显。

    显式引用缺失、无效或为空时绝不回退。只有无引用的历史配置可只读回退旧凭据。
    """
    if credential_ref is not LEGACY_CREDENTIAL:
        return _read_credential(credential_file(product_id, sender, credential_ref=credential_ref))
    if product_id is None or not sender:
        return _read_credential(credential_file())
    path = credential_path(product_id, sender)
    value = _read_credential(path)
    if value:
        return value
    return _read_credential(credential_file())


def credential_configured(product_id=None, sender=None, *, credential_ref=LEGACY_CREDENTIAL):
    """只读核对当前引用的凭据内容；空文件或不可读文件不算已配置。"""
    return bool(credential(product_id, sender, credential_ref=credential_ref))


def credential_from_config(product_id, config):
    """从一次配置快照解析凭据，避免发送时混用新旧账户和授权码。"""
    if not config.get('sender'):
        return None
    reference = config.get('credential_ref', LEGACY_CREDENTIAL)
    return credential(product_id, config['sender'], credential_ref=reference)


def _credential_value(code):
    if not isinstance(code, str) or not code.strip():
        raise ValueError('邮箱授权码不能为空')
    value = code.strip()
    if len(value) > 512 or any(ch in value for ch in '\r\n'):
        raise ValueError('邮箱授权码格式无效')
    return value


def save_credential(product_id, sender, code):
    """持久化新凭据版本并返回引用，生效时机由配置事务决定。"""
    value = _credential_value(code)
    reference = uuid.uuid4().hex
    path = credential_file(product_id, sender, credential_ref=reference)
    _write_credential(path, value)
    return reference


# ---------------------------------------------------------------- 配置校验

def defaults():
    return {'enabled': False, 'sender': '', 'recipients': [],
            'events': {key: False for key in EVENTS},
            'smtp': {'host': 'smtp.qq.com', 'port': 465, 'tls': 'ssl'},
            'morning_hour': MORNING_HOUR}


def normalize(product):
    """把任意历史/缺失配置收敛为完整结构，供读取与触发共用。"""
    raw = product.get('notifications') if isinstance(product.get('notifications'), dict) else {}
    value = defaults()
    value['enabled'] = bool(raw.get('enabled'))
    value['sender'] = raw.get('sender') if isinstance(raw.get('sender'), str) else ''
    value['recipients'] = [item for item in raw.get('recipients', []) if isinstance(item, str)]
    events = raw.get('events') if isinstance(raw.get('events'), dict) else {}
    value['events'] = {key: bool(events.get(key)) for key in EVENTS}
    smtp = raw.get('smtp') if isinstance(raw.get('smtp'), dict) else {}
    value['smtp'] = {
        'host': smtp.get('host') if isinstance(smtp.get('host'), str) and smtp.get('host') else 'smtp.qq.com',
        'port': smtp.get('port') if type(smtp.get('port')) is int else 465,
        'tls': smtp.get('tls') if smtp.get('tls') in TLS_CHOICES else 'ssl',
    }
    value['morning_hour'] = raw.get('morning_hour') if type(raw.get('morning_hour')) is int else MORNING_HOUR
    value['config_version'] = raw.get('config_version', 0)
    if 'credential_ref' in raw:
        # 必须保留显式空值/无效值，绝不能因收敛而恢复历史全局回退。
        value['credential_ref'] = raw['credential_ref']
    _validate_internal(value)
    return value


def _validate_internal(config):
    version = config.get('config_version', 0)
    if type(version) is not int or version < 0:
        raise ValueError('通知配置版本无效')
    reference = config.get('credential_ref')
    if reference is not None and (not isinstance(reference, str) or not REFERENCE.fullmatch(reference)):
        raise ValueError('通知凭据引用无效')


def validate_public_changes(changes):
    """所有客户端写入口共用白名单，内部引用及版本只能由服务端生成。"""
    if not isinstance(changes, dict):
        raise ValueError('通知配置必须为对象')
    if set(changes) - PUBLIC_CONFIG_FIELDS:
        raise ValueError('通知配置字段无效，凭据引用和配置版本由服务端管理')
    return changes


def email(value, field='邮箱'):
    """拒绝换行、逗号、空格等头注入字符，只接受单一地址。"""
    if not isinstance(value, str):
        raise ValueError(f'{field}必须为文本')
    value = value.strip()
    if not value or len(value) > 254 or any(ch in value for ch in '\r\n,; \t'):
        raise ValueError(f'{field}格式无效，不能包含换行、空格、逗号或分号')
    if not EMAIL.fullmatch(value):
        raise ValueError(f'{field}格式无效')
    return value


def validate(config):
    if not isinstance(config, dict):
        raise ValueError('通知配置必须为对象')
    if type(config.get('enabled')) is not bool:
        raise ValueError('通知开关必须为布尔值')
    if config.get('sender'):
        config['sender'] = email(config['sender'], '发件人邮箱')
    if config.get('enabled') and not config.get('sender'):
        raise ValueError('启用通知前请填写发件人邮箱')
    recipients = []
    for item in config.get('recipients', []):
        value = email(item, '收件人邮箱')
        if value not in recipients:
            recipients.append(value)
    if len(recipients) > MAX_RECIPIENTS:
        raise ValueError(f'收件人数量不能超过 {MAX_RECIPIENTS} 个')
    config['recipients'] = recipients
    if config.get('enabled') and not recipients:
        raise ValueError('启用通知前请至少添加一个收件人邮箱')
    smtp = config.get('smtp')
    if not isinstance(smtp, dict) or not isinstance(smtp.get('host'), str) or not HOST.fullmatch(smtp['host']):
        raise ValueError('SMTP 服务器地址无效')
    if type(smtp.get('port')) is not int or not 1 <= smtp['port'] <= 65535:
        raise ValueError('SMTP 端口必须为 1–65535 的整数')
    if smtp.get('tls') not in TLS_CHOICES:
        raise ValueError('SMTP 加密方式无效')
    events = config.get('events')
    if not isinstance(events, dict):
        raise ValueError('通知事件必须为对象')
    config['events'] = {key: bool(events.get(key)) for key in EVENTS}
    hour = config.get('morning_hour', MORNING_HOUR)
    if type(hour) is not int or not 0 <= hour <= 23:
        raise ValueError('复盘时间必须为 0–23 的整点')
    config['morning_hour'] = hour
    _validate_internal(config)
    return config


def validate_structure(value):
    """供 project.validate 使用：拒绝结构错误的持久化配置。"""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('通知配置必须为对象')
    _validate_internal(value)
    # normalize 会把历史缺失字段收敛为默认值，因此先按原始结构做类型判断。
    if 'enabled' in value and type(value['enabled']) is not bool:
        raise ValueError('通知开关必须为布尔值')
    if 'sender' in value and not isinstance(value['sender'], str):
        raise ValueError('发件人邮箱必须为文本')
    if 'recipients' in value and not isinstance(value['recipients'], list):
        raise ValueError('收件人必须为列表')
    if 'events' in value and not isinstance(value['events'], dict):
        raise ValueError('通知事件必须为对象')
    if 'smtp' in value and not isinstance(value['smtp'], dict):
        raise ValueError('SMTP 配置必须为对象')
    if 'morning_hour' in value and type(value['morning_hour']) is not int:
        raise ValueError('复盘时间必须为 0–23 的整点')
    return validate(normalize({'notifications': value}))


def merge(existing, changes):
    validate_public_changes(changes)
    result = dict(existing)
    if 'enabled' in changes:
        result['enabled'] = changes['enabled']
    if 'sender' in changes:
        result['sender'] = changes['sender']
    if 'recipients' in changes:
        if not isinstance(changes['recipients'], list):
            raise ValueError('收件人必须为列表')
        result['recipients'] = list(changes['recipients'])
    if 'events' in changes:
        if not isinstance(changes['events'], dict):
            raise ValueError('通知事件必须为对象')
        result['events'] = dict(existing['events']) | changes['events']
    if 'smtp' in changes:
        if not isinstance(changes['smtp'], dict):
            raise ValueError('SMTP 配置必须为对象')
        result['smtp'] = dict(existing['smtp']) | changes['smtp']
    if 'morning_hour' in changes:
        result['morning_hour'] = changes['morning_hour']
    return result


def prepare_configuration(product, changes, *, has_credential=False):
    """在同一账户快照上合并公开字段，并统一约束账户切换与首次启用。"""
    existing = normalize(product)
    result = validate(merge(existing, changes))
    if result['sender'] != existing['sender']:
        if result['sender'] and existing['sender'] and not has_credential:
            raise ValueError('更换发件人时请同时填写并验证新邮箱授权码')
        if result['sender'] and result['enabled'] and not has_credential:
            raise ValueError('首次启用通知前请填写并验证邮箱授权码')
        result['credential_ref'] = None
    return result


def initialize_configuration(changes):
    """新建项目只接受公开配置，关闭状态草稿不继承本机历史账户。"""
    result = prepare_configuration({}, changes)
    result['credential_ref'] = None
    result['config_version'] = 0
    return result


def stage_configuration_credential(product, config, code=None):
    """生成待提交版本，或在成功保存历史配置时迁移旧凭据；不覆盖任何旧文件。"""
    result = dict(config)
    if code is not None:
        result['credential_ref'] = save_credential(product['id'], result['sender'], code)
    elif 'credential_ref' not in result:
        old_code = credential_from_config(product['id'], normalize(product))
        result['credential_ref'] = save_credential(product['id'], result['sender'], old_code) if old_code else None
    return result


def version_configuration(product, config):
    """只有公开配置或有效凭据引用实际变化时递增通知独立版本。"""
    existing = normalize(product)
    result = dict(config)
    result['config_version'] = existing['config_version']
    if result != existing:
        result['config_version'] += 1
    return result


def mask(value):
    """脱敏发件人：191****@qq.com 形式，不含本地完整账号。"""
    if not value or '@' not in value:
        return ''
    local, _, domain = value.partition('@')
    head = local[:3] if len(local) > 3 else local[:1]
    return f'{head}****@{domain}'


# ---------------------------------------------------------------- SMTP 传输

def connect(smtp):
    host, port, mode = smtp['host'], int(smtp['port']), smtp['tls']
    if mode == 'ssl':
        return smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT)
    server = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT)
    if mode == 'starttls':
        server.starttls()
    return server


def verify(smtp, sender, code):
    """登录验证；失败不落盘。"""
    server = connect(smtp)
    try:
        server.login(sender, code)
    finally:
        try:
            server.quit()
        except Exception:
            pass
    return True


def build_message(sender, recipients, subject, text, body_html):
    note = EmailMessage()
    note['From'] = sender
    note['To'] = ', '.join(recipients)
    note['Subject'] = ' '.join(str(subject).split())[:300]
    note['Date'] = formatdate(localtime=True)
    note['Message-ID'] = make_msgid(domain=sender.partition('@')[2] or None)
    note.set_content(text)
    note.add_alternative(body_html, subtype='html')
    return note


def _safe(value, limit=500):
    text = SECRET.sub(lambda m: (m[1] or m[2]) + '[redacted]', str(value or ''))
    return ' '.join(text.split())[:limit]


# ---------------------------------------------------------------- 简报渲染

def window_for_retro(now, hour=MORNING_HOUR):
    """北京时间复盘边界：返回最近一次应复盘的自然日与窗口。"""
    local = dt.datetime.fromtimestamp(now, ZONE)
    boundary = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if local < boundary:
        boundary -= dt.timedelta(days=1)
    day = (boundary - dt.timedelta(days=1)).date()
    start = dt.datetime.combine(day, dt.time.min, ZONE).timestamp()
    return day, start, start + 86400


def _inside(value, start, end):
    return bool(value) and start <= float(value) < end


def _history_time(value):
    """仅接纳可复算的有限时间戳，缺失时间不推测补齐。"""
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return float(value)


def _episode_snapshot(issue):
    return {'summary': _issue_summary(issue), 'cause': str(issue.get('cause') or '')[:1000],
            'title': str(issue.get('title') or '未命名问题'),
            'action': str(issue.get('action') or '核对原因后处理')}


def _new_episode(snapshot, now, occurrence, incomplete=False):
    """每次发生独立保存半开区间及原因观察，首见内容不会被下一次重开覆盖。"""
    return dict(snapshot, opened_at=now, closed_at=None, last_seen=now,
                occurrence=occurrence, history_incomplete=incomplete,
                observations=[dict(snapshot, at=now)])


def _problem_history(problem):
    """只读解析问题历史；旧版多次发生只保留确认时点，绝不补造跨恢复空档的区间。"""
    if isinstance(problem.get('episodes'), list):
        return {'episodes': copy.deepcopy(problem['episodes']),
                'history_facts': copy.deepcopy(problem.get('history_facts') or []),
                'history_incomplete': bool(problem.get('history_incomplete'))}
    first = _history_time(problem.get('first_seen'))
    last = _history_time(problem.get('last_seen'))
    closed = _history_time(problem.get('closed_at'))
    occurrence = problem.get('occurrences', 1)
    snapshot = {'summary': str(problem.get('summary') or '历史问题'),
                'cause': str(problem.get('cause') or ''),
                'title': str(problem.get('summary') or '历史问题').partition('：')[0],
                'action': '核对历史记录后处理'}
    if occurrence == 1 and first is not None and (problem.get('status') == 'open' or closed is not None):
        # 单次发生能还原连续区间，但最新 scalar 原因只在 last_seen 被确认。
        unknown = dict(snapshot, summary=snapshot['title'] + '：当时原因未保留（历史不完整）',
                       cause='当时原因未保留，历史不完整')
        incomplete = last != first
        episode = _new_episode(unknown if incomplete else snapshot, first, 1, incomplete=incomplete)
        episode['last_seen'] = last if last is not None else first
        episode['closed_at'] = closed if problem.get('status') != 'open' else None
        facts = []
        if last is not None and last > first:
            if episode['closed_at'] is None or last < episode['closed_at']:
                episode['observations'].append(dict(snapshot, at=last))
            else:
                # 恢复时点确认的最新内容不描述已经结束的存活区间。
                facts.append(dict(snapshot, at=last, status='resolved', occurrence=1))
        return {'episodes': [episode], 'history_facts': facts, 'history_incomplete': incomplete}
    facts = {}
    if first is not None:
        # 当前 scalar 原因可能属于最后一次发生，不能拿它描述第一轮。
        facts[first] = {'at': first, 'status': 'unknown', 'occurrence': 1,
                        'summary': snapshot['title'] + '：该次原因未保留（历史不完整）',
                        'cause': '该次原因未保留，历史不完整'}
    if last is not None:
        facts[last] = dict(snapshot, at=last, status='open' if problem.get('status') == 'open' else 'unknown',
                           occurrence=occurrence)
    if closed is not None:
        facts[closed] = dict(snapshot, at=closed, status='resolved', occurrence=occurrence)
    return {'episodes': [], 'history_facts': [facts[key] for key in sorted(facts)],
            'history_incomplete': True}


def _interval_overlaps(opened, closed, start, end):
    if opened is None or start >= end:
        return False
    finish = float('inf') if closed is None else closed
    return max(opened, start) < min(finish, end)


def _problem_window_rows(problem, start, end, status_at=None):
    """按发生区间和原因片段投影窗口；关闭状态依据报告截止时点而非当前总状态。"""
    if start >= end:
        return []
    history = _problem_history(problem)
    status_at = end if status_at is None else status_at
    rows = []
    for index, episode in enumerate(history['episodes'], 1):
        opened = _history_time(episode.get('opened_at'))
        closed = _history_time(episode.get('closed_at'))
        if not _interval_overlaps(opened, closed, start, end):
            continue
        observations = [value for value in episode.get('observations', [])
                        if isinstance(value, dict) and _history_time(value.get('at')) is not None]
        if not observations:
            observations = [dict(episode, at=opened)]
        observations.sort(key=lambda value: value['at'])
        for position, observation in enumerate(observations):
            segment_start = max(opened, observation['at'])
            segment_end = observations[position + 1]['at'] if position + 1 < len(observations) else closed
            if closed is not None:
                segment_end = closed if segment_end is None else min(closed, segment_end)
            if not _interval_overlaps(segment_start, segment_end, start, end):
                continue
            rows.append({
                'id': problem['id'], 'source': problem.get('source', ''),
                'source_id': problem.get('source_id') or problem.get('run_id'), 'episode': index,
                'summary': observation.get('summary', episode.get('summary', '')),
                'cause': observation.get('cause', episode.get('cause', '')),
                'title': observation.get('title', episode.get('title', '历史问题')),
                'action': observation.get('action', episode.get('action', '核对原因后处理')),
                'occurrences': episode.get('occurrence', index), 'opened_at': opened, 'closed_at': closed,
                'status': 'resolved' if closed is not None and closed <= status_at else 'open',
                'status_at': status_at,
                'history_incomplete': bool(episode.get('history_incomplete')),
            })
    for fact in history['history_facts']:
        observed = _history_time(fact.get('at'))
        if observed is not None and start <= observed < end:
            if fact.get('status') == 'resolved' and observed == start:
                # 恢复发生在窗口起点不能证明本窗口内仍有问题。
                continue
            rows.append({'id': problem['id'], 'summary': fact.get('summary', '旧版问题记录'),
                         'cause': fact.get('cause', ''), 'status': 'historical',
                         'observed_at': observed, 'observed_status': fact.get('status', 'unknown'),
                         'occurrences': fact.get('occurrence', problem.get('occurrences', 1)),
                         'history_incomplete': True})
    return rows


def _problem_status_label(problem, end):
    """两种正文共用恢复时点措辞，区分统计窗口内和复盘截止前的后续恢复。"""
    if problem['status'] != 'resolved':
        label = '持续跟踪'
    elif problem['closed_at'] >= end:
        label = '窗口结束后、截至复盘时间已恢复'
    else:
        label = '窗口内已恢复'
    if problem.get('history_incomplete'):
        label += '；历史不完整，仅展示可确认的区间'
    return label


def _history_fact_label(problem):
    stamp = dt.datetime.fromtimestamp(problem['observed_at'], ZONE).strftime('%Y-%m-%d %H:%M:%S')
    state = {'open': '问题观察', 'resolved': '恢复观察'}.get(problem.get('observed_status'), '发生观察')
    return f'历史不完整，仅能确认 {stamp} 的{state}，不能还原持续区间'


def _overlaps(problem, start, end):
    """是否存在确实相交的发生区间或窗口内可确认的历史观察时点。"""
    return bool(_problem_window_rows(problem, start, end))


def _current_issues(ledger, product):
    """当前即时问题快照：阻塞/失败任务、交付阻塞、监测未通过。

    与告警事件同源，避免「问题只在复盘时采集」造成状态变化前后逻辑漂移。
    """
    pid = product['id']
    issues = []
    for run in ledger.scoped('runs', pid):
        # blocked/fail/结果不确定的任务都属于需要记录的问题；只收 blocked 会让 fail 任务的
        # 失败原因从简报「阻塞与问题」章节和复盘问题清单中消失。
        if run['status'] in ('blocked', 'fail') or run.get('uncertain'):
            action = '核对原因后重试或补充条件' if run['status'] == 'blocked' else '核对失败原因后修复并重新提交'
            issues.append({'source': 'task', 'id': run['id'], 'title': run.get('title', '未命名任务'),
                           'cause': run.get('reason') or '未说明', 'action': action,
                           'requirement_id': run.get('requirement_id')})
    for row in ledger.scoped('deliveries', pid):
        if row['status'] in ('blocked', 'release_failed'):
            issues.append({'source': 'delivery', 'id': row['id'], 'title': row.get('branch') or row.get('title', '交付批次'),
                           'cause': row.get('reason', '未说明'), 'action': '处理代码交付阻塞后继续'})
    for action in ('probe', 'inspect'):
        result = product.get('last_' + action + '_result')
        if isinstance(result, dict) and (result.get('status') in ('blocked', 'fail') or result.get('monitor_mode') == 'basic'):
            issues.append({'source': action, 'id': action, 'title': action,
                           'cause': result.get('reason') or '未说明', 'action': '核对监测结果后处理'})
    return issues


def collect(ledger, product, start, end, now, context):
    pid = product['id']
    runs = ledger.scoped('runs', pid)
    requirements = ledger.scoped('requirements', pid)
    deliveries = ledger.scoped('deliveries', pid)
    releases = ledger.scoped('releases', pid)
    problems = ledger.scoped('problems', pid)
    research = ledger.scoped('research_jobs', pid)
    competitors = ledger.scoped('competitors', pid)
    accepted = [run for run in runs if _inside(run.get('accepted_at'), start, end)]
    online = [row for row in deliveries if _inside(row.get('merged_at'), start, end)]
    release_rows = [row for row in releases if _inside(row.get('created'), start, end) or _inside(row.get('updated'), start, end)]
    found = [row for row in requirements if _inside(row.get('created'), start, end)]
    started = [row for row in runs if _inside(row.get('created'), start, end)]
    status_at = min(now, end)
    if context.get('_kind') == 'morning_retro':
        status_at = min(now, end + normalize(product)['morning_hour'] * 3600)
    window_problems = [row for problem in problems
                       for row in _problem_window_rows(problem, start, end, status_at)]
    open_problems = [row for row in window_problems if row['status'] == 'open']
    resolved_problems = [row for row in window_problems if row['status'] == 'resolved']
    history_problems = [row for row in window_problems if row['status'] == 'historical']
    issues = [{'source': row['source'], 'id': row['source_id'], 'title': row['title'],
               'cause': row['cause'], 'action': row['action']} for row in open_problems]
    current_issues = _current_issues(ledger, product)
    instantaneous_issues = []
    # 未曾落库的即时问题只证明当前时点状态，不能混入昨天或其他历史窗口。
    if start <= now < end:
        instantaneous_issues = [issue for issue in current_issues
                                if _find_problem(problems, issue, _problem_ident(pid, issue)) is None]
        issues.extend(instantaneous_issues)
    quota = _quota(ledger, product)
    policy = DEFAULTS | product.get('policy', {})
    quota['development_used'] = ledger.budget_used(pid, 'development')
    quota['development_limit'] = policy['runs_per_day']
    quota['discovery_used'] = ledger.budget_used(pid, 'discovery')
    quota['discovery_limit'] = policy['discovery_per_day']
    competitor_rows = []
    for job in research:
        if job['status'] in ('completed', 'failed') and (_inside(job.get('updated'), start, end) or _inside(job.get('created'), start, end)):
            result = job.get('result') if isinstance(job.get('result'), dict) else {}
            competitor_rows.append({'action': job.get('action', 'research'), 'status': job['status'],
                                    'found': len(result.get('competitors', []) or []), 'reason': job.get('reason', '')})
    new_competitors = [{'name': row.get('name') or row.get('title') or row['id'], 'at': row.get('created')}
                       for row in competitors if _inside(row.get('created'), start, end)]
    todo = []
    if quota['tokens_exhausted']:
        todo.append('今日开发 Token 额度已用尽，北京时间次日零点恢复；可在项目设置调整额度。')
    queued = [run for run in runs if run['status'] == 'queued']
    if queued:
        todo.append(f'{len(queued)} 个需求在队列等待调度。')
    if current_issues:
        todo.append(f'当前 {len(current_issues)} 项阻塞或问题需要人工处理。')
    for row in new_competitors[:5]:
        todo.append(f'评估竞品动态：{row["name"]}')
    suggestions = [f'复盘问题「{_safe(problem.get("summary", ""), 60)}」并纳入下一轮需求候选，完成后再复验。'
                   for problem in open_problems[:5]]
    if not suggestions:
        suggestions.append('截至复盘截止时间，本统计窗口没有未解决的问题；保持巡检、需求发现与代码评审节奏。')
    return {
        'day': dt.datetime.fromtimestamp(now, ZONE).date().isoformat(),
        'window': [start, end],
        'product': product.get('name') or product['id'],
        'focus': context.get('focus') or '',
        'stats': {
            'requirements_found': len(found),
            'runs_started': len(started),
            'accepted': len(accepted),
            'online': len(online),
            'releases': len(release_rows),
            'queued': len(queued),
            'blocked': len(current_issues),
        },
        'done': [{'id': run['id'], 'title': run.get('title', ''), 'status': run['status'],
                  'at': run.get('accepted_at')} for run in accepted],
        'online_detail': [{'id': row['id'], 'title': row.get('branch') or row.get('title', ''),
                           'at': row.get('merged_at')} for row in online],
        'issues': issues,
        'open_problems': open_problems,
        'resolved_problems': resolved_problems,
        'history_problems': history_problems,
        'problem_status_at': status_at,
        'quota': quota,
        'competitors': {'jobs': competitor_rows, 'new': new_competitors},
        'todo': todo,
        'suggestions': suggestions,
    }


def _quota(ledger, product):
    from .quota import snapshot
    return dict(snapshot(ledger, product))


def _window_label(start, end):
    left = dt.datetime.fromtimestamp(start, ZONE)
    right = dt.datetime.fromtimestamp(end, ZONE)
    if right < left:
        raise ValueError('统计窗口结束时间不能早于开始时间')
    if left.time() == dt.time.min and right.time() == dt.time.min and (right - left) == dt.timedelta(days=1):
        return left.strftime('%Y-%m-%d 00:00–24:00')
    precision = '%Y-%m-%d %H:%M:%S' if any((left.second, right.second, left.microsecond, right.microsecond)) else '%Y-%m-%d %H:%M'
    return left.strftime(precision) + ' 至 ' + right.strftime(precision)


def render_text(subject, data):
    lines = [subject]
    if data.get('focus'):
        # HTML 正文与纯文本正文必须承载同一 focus；否则告警邮件在纯文本下会丢失失败原因。
        lines += ['', str(data['focus'])]
    lines += ['', '一、概览指标',
             f'- 统计窗口：{_window_label(*data["window"])}（Asia/Shanghai）',
             f'- 新增需求：{data["stats"]["requirements_found"]}',
             f'- 启动任务：{data["stats"]["runs_started"]}',
             f'- 业务验收通过：{data["stats"]["accepted"]}',
             f'- 交付上线：{data["stats"]["online"]}',
             f'- 发布批次：{data["stats"]["releases"]}',
             f'- 当前排队：{data["stats"]["queued"]}',
             '',
             '二、昨日完成']
    if data['done'] or data['online_detail']:
        for row in data['done']:
            lines.append(f'- 需求《{row["title"] or row["id"]}》业务验收通过（记录 {row["id"]}）')
        for row in data['online_detail']:
            lines.append(f'- 交付《{row["title"] or row["id"]}》已合入主分支（记录 {row["id"]}）')
    else:
        lines.append('- 无')
    lines += ['', '三、阻塞与问题']
    if data['issues']:
        for issue in data['issues']:
            lines.append(f'- [{issue["source"]}] {issue["title"]}：{issue["cause"]}；需要：{issue["action"]}')
    else:
        lines.append('- 无')
    for problem in data['open_problems']:
        lines.append(f'- [复盘] {problem["summary"]}（累计 {problem["occurrences"]} 次，{_problem_status_label(problem, data["window"][1])}）')
    for problem in data.get('resolved_problems', []):
        lines.append(f'- [复盘-已恢复] {problem["summary"]}（{_problem_status_label(problem, data["window"][1])}）')
    for problem in data.get('history_problems', []):
        lines.append(f'- [复盘-历史不完整] {problem["summary"]}（{_history_fact_label(problem)}）')
    quota = data['quota']
    lines += ['', '四、额度使用',
              f'- 开发 Token：{quota["tokens_used"]} / {quota["tokens_limit"] or "不限"}{"（已用尽）" if quota["tokens_exhausted"] else ""}',
              f'- 今日需求：{quota["development_used"]} / {quota["development_limit"] or "不限"}',
              f'- 需求发现：{quota["discovery_used"]} / {quota["discovery_limit"] or "不限"}',
              '', '五、竞品动态']
    if data['competitors']['jobs'] or data['competitors']['new']:
        for row in data['competitors']['jobs']:
            label = '发现竞品' if row['action'] == 'find_competitors' else '竞品分析'
            lines.append(f'- {label}任务{("完成" if row["status"] == "completed" else "失败")}：新增 {row["found"]} 个候选'
                         + (f'；{row["reason"]}' if row['reason'] else ''))
        for row in data['competitors']['new']:
            lines.append(f'- 新登记竞品：{row["name"]}')
    else:
        lines.append('- 无')
    lines += ['', '六、今日待办与自进化建议']
    for item in data['todo'] or ['按巡检与需求发现节奏继续推进；暂无额外汇总事项。']:
        lines.append(f'- 待办：{item}')
    for item in data['suggestions']:
        lines.append(f'- 自进化：{item}')
    return '\n'.join(lines)[:100000]


def render_html(subject, data):
    def item_list(values, empty='无'):
        if not values:
            return f'<li>{html_module.escape(empty)}</li>'
        return ''.join(f'<li>{html_module.escape(str(value))}</li>' for value in values)
    stats = data['stats']
    done = [f'需求《{row["title"] or row["id"]}》业务验收通过（记录 {row["id"]}）' for row in data['done']]
    done += [f'交付《{row["title"] or row["id"]}》已合入主分支（记录 {row["id"]}）' for row in data['online_detail']]
    issues = [f'[{issue["source"]}] {issue["title"]}：{issue["cause"]}；需要：{issue["action"]}' for issue in data['issues']]
    issues += [f'[复盘] {problem["summary"]}（累计 {problem["occurrences"]} 次，{_problem_status_label(problem, data["window"][1])}）' for problem in data['open_problems']]
    issues += [f'[复盘-已恢复] {problem["summary"]}（{_problem_status_label(problem, data["window"][1])}）' for problem in data.get('resolved_problems', [])]
    issues += [f'[复盘-历史不完整] {problem["summary"]}（{_history_fact_label(problem)}）' for problem in data.get('history_problems', [])]
    quota = data['quota']
    quota_rows = [f'开发 Token：{quota["tokens_used"]} / {quota["tokens_limit"] or "不限"}'
                  + ('（已用尽）' if quota['tokens_exhausted'] else ''),
                  f'今日需求：{quota["development_used"]} / {quota["development_limit"] or "不限"}',
                  f'需求发现：{quota["discovery_used"]} / {quota["discovery_limit"] or "不限"}']
    competitor = []
    for row in data['competitors']['jobs']:
        label = '发现竞品' if row['action'] == 'find_competitors' else '竞品分析'
        competitor.append(f'{label}任务{("完成" if row["status"] == "completed" else "失败")}：新增 {row["found"]} 个候选'
                          + (f'；{row["reason"]}' if row['reason'] else ''))
    competitor += [f'新登记竞品：{row["name"]}' for row in data['competitors']['new']]
    todo = [f'待办：{value}' for value in (data['todo'] or ['按巡检与需求发现节奏继续推进；暂无额外汇总事项。'])]
    todo += [f'自进化：{value}' for value in data['suggestions']]
    focus = f'<p class="focus">{html_module.escape(data["focus"])}</p>' if data.get('focus') else ''
    body = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>{html_module.escape(subject)}</title>
<style>body{{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1f2329;line-height:1.6}}
h1{{font-size:18px}}h2{{font-size:15px;margin-top:20px;border-bottom:1px solid #e5e6eb;padding-bottom:4px}}
ul{{padding-left:20px;margin:6px 0}}.focus{{background:#f2f3f5;padding:8px 12px;border-radius:4px}}</style></head>
<body><h1>{html_module.escape(subject)}</h1>{focus}
<p>统计窗口：{html_module.escape(_window_label(*data["window"]))}（Asia/Shanghai）</p>
<h2>一、概览指标</h2><ul>
<li>新增需求：{stats["requirements_found"]}</li><li>启动任务：{stats["runs_started"]}</li>
<li>业务验收通过：{stats["accepted"]}</li><li>交付上线：{stats["online"]}</li>
<li>发布批次：{stats["releases"]}</li><li>当前排队：{stats["queued"]}</li></ul>
<h2>二、昨日完成</h2><ul>{item_list(done)}</ul>
<h2>三、阻塞与问题</h2><ul>{item_list(issues)}</ul>
<h2>四、额度使用</h2><ul>{item_list(quota_rows)}</ul>
<h2>五、竞品动态</h2><ul>{item_list(competitor)}</ul>
<h2>六、今日待办与自进化建议</h2><ul>{item_list(todo)}</ul>
<p style="color:#86909c;font-size:12px">本简报由本机台账复算生成，未使用任何外部数据源。</p>
</body></html>'''
    return body[:200000]


def render(ledger, product, kind, now, context=None):
    context = dict(context or {})
    context['_kind'] = kind
    name = product.get('name') or product['id']
    if kind == 'morning_retro':
        day = context.get('day')
        start, end = context['window']
        subject = f'[{name}] 昨日研发简报 · {day.isoformat()}'
    elif kind == 'test':
        stamp = dt.datetime.fromtimestamp(now, ZONE).strftime('%Y-%m-%d %H:%M')
        start = context.get('start', now - 86400)
        end = context.get('end', now + 1)
        subject = f'[{name}] 通知测试简报 · {stamp}'
        context.setdefault('focus', '这是一封通知能力测试邮件，用于确认发件人、收件人与简报渲染均可用。')
    else:
        start = context.get('start', now - 86400)
        end = context.get('end', now + 1)
        subject = f'[{name}] {KIND_LABELS.get(kind, "研发通知")}'
        if context.get('title'):
            subject += f' · {context["title"]}'
    data = collect(ledger, product, start, end, now, context)
    if context.get('focus'):
        data['focus'] = context['focus']
    return subject, {'text': render_text(subject, data), 'html': render_html(subject, data),
                     'day': data['day'], 'window': [start, end], 'kind': kind}


# ---------------------------------------------------------------- 事件触发

def notification_id(product_id, event_key):
    return 'notification-' + hashlib.sha256((str(product_id) + '|' + str(event_key)).encode()).hexdigest()[:32]


def _exists(ledger, ident):
    try:
        ledger.get('notifications', ident)
        return True
    except KeyError:
        return False


def _queue(ledger, product, kind, event_key, now, context, known=None):
    ident = notification_id(product['id'], event_key)
    seen = ident in known if known is not None else _exists(ledger, ident)
    if seen:
        return None
    subject, payload = render(ledger, product, kind, now, context)
    item = ledger.create('notifications', {
        'product_id': product['id'], 'kind': kind, 'event_key': str(event_key),
        'subject': subject, 'payload': payload, 'attempts': 0, 'next_attempt_at': 0,
        'receipt': None, 'error': '',
    }, 'queued', ident=ident)
    if known is not None:
        known.add(ident)
    return item


def _issue_summary(issue):
    return (str(issue.get('title') or '未命名问题') + '：' + str(issue.get('cause') or '未说明'))[:500]


def _issue_source_id(issue):
    value = issue.get('id')
    return None if value in (None, '') else str(value)


def _problem_ident(product_id, issue):
    """问题身份键用 source + source_id，保证同一 run/delivery 只有一条记录。

    ID 必须含项目 ID：不同项目可能出现同名同因问题，否则后一个项目会覆盖前一个项目的
    需求/信号关联，导致问题记录缺失。
    """
    source = str(issue.get('source') or '')
    source_id = _issue_source_id(issue)
    key = f'{source}:{source_id}' if source_id is not None else 'summary:' + _issue_summary(issue)
    return 'problem-' + hashlib.sha256((str(product_id) + '|' + key).encode()).hexdigest()[:32]


def _find_problem(existing, issue, ident):
    """按新身份键查找；回退匹配必须来源身份一致。

    不同 task/run/source_id 即使 summary/cause 完全相同也必须独立落库：禁止用 summary
    回退把带身份的记录合并到一起，否则复用写回会覆盖旧记录的来源关联，损害复盘完整性与
    自进化来源追踪。仅允许历史遗留记录（source_id 为空）按同 task run_id 补齐身份。
    """
    for problem in existing:
        if problem['id'] == ident:
            return problem
    source = str(issue.get('source') or '')
    source_id = _issue_source_id(issue)
    if source_id is None:
        # 无来源身份：同 summary 的无身份记录已由 ident 精确命中，不能再用 summary
        # 回退合并到任何带身份的记录。
        return None
    run_id = source_id if source == 'task' else None
    for problem in existing:
        if source and problem.get('source') and str(problem.get('source')) != source:
            continue
        if str(problem.get('source_id') or '') == source_id:
            return problem
        # 仅历史记录（source_id 为空）可按同 task run_id 补齐身份，禁止跨身份合并。
        if run_id is not None and not problem.get('source_id') and problem.get('run_id') == run_id:
            return problem
    return None


def record_problems(ledger, product, issues, now=None):
    """把未解决问题结构化落库，供后续需求发现复算引用。

    同一 (source, source_id) 只有一条记录：持续 open 只刷新 last_seen，已恢复后再次出现
    才重新打开并累计 occurrences。来源身份不一致的问题各自独立，复用旧记录时只补齐空字段，
    绝不覆盖已有的 source_id/run_id/requirement_id/signal_ids。
    """
    now = time.time() if now is None else now
    created = []
    existing = ledger.scoped('problems', product['id'])
    for issue in issues:
        summary = _issue_summary(issue)
        snapshot = _episode_snapshot(issue)
        ident = _problem_ident(product['id'], issue)
        source = str(issue.get('source') or '')
        source_id = _issue_source_id(issue)
        run_id = source_id if source == 'task' else None
        requirement_id = issue.get('requirement_id')
        signal_ids = []
        if requirement_id:
            try:
                requirement = ledger.get('requirements', requirement_id)
            except KeyError:
                requirement = {}
            signal_ids = [value for value in requirement.get('signal_ids', []) if isinstance(value, str)]
        old = _find_problem(existing, issue, ident)
        if old is None:
            record = ledger.create('problems', {
                'product_id': product['id'], 'summary': summary,
                'cause': str(issue.get('cause') or '')[:1000], 'source': source,
                'source_id': source_id, 'status': 'open', 'occurrences': 1,
                'first_seen': now, 'last_seen': now, 'closed_at': None, 'run_id': run_id,
                'requirement_id': requirement_id, 'signal_ids': signal_ids,
                'episodes': [_new_episode(snapshot, now, 1)], 'history_facts': [],
                'history_incomplete': False,
            }, 'open', ident=ident)
            existing.append(record)
            created.append(record)
            continue
        # 复用已有记录时只补空、不覆盖：不同来源身份的问题不得改写旧记录的
        # source_id/run_id/requirement_id/signal_ids，保证复盘与自进化来源可追溯。
        changes = {'last_seen': now, 'summary': summary,
                   'cause': str(issue.get('cause') or '')[:1000]}
        history = _problem_history(old)
        occurrence = old.get('occurrences', 1)
        if old.get('status') == 'open':
            current_episode = next((episode for episode in reversed(history['episodes'])
                                    if episode.get('closed_at') is None), None)
            if current_episode is None:
                # 旧版多次发生无法确定本轮起点；仅从这次确实观察到问题的时点开始记录。
                current_episode = _new_episode(snapshot, now, occurrence, incomplete=True)
                history['episodes'].append(current_episode)
                history['history_incomplete'] = True
            else:
                observations = current_episode.setdefault('observations', [dict(current_episode, at=current_episode['opened_at'])])
                if not observations or any(observations[-1].get(key) != value for key, value in snapshot.items()):
                    observations.append(dict(snapshot, at=now))
                current_episode['last_seen'] = now
        else:
            occurrence += 1
            history['episodes'].append(_new_episode(snapshot, now, occurrence))
        changes.update(history)
        if source and not old.get('source'):
            changes['source'] = source
        if source_id is not None and not old.get('source_id'):
            changes['source_id'] = source_id
        if run_id is not None and not old.get('run_id'):
            changes['run_id'] = run_id
        if requirement_id and not old.get('requirement_id'):
            changes['requirement_id'] = requirement_id
        if signal_ids and not old.get('signal_ids'):
            changes['signal_ids'] = signal_ids
        if old.get('status') == 'open':
            # 同一持续时间段只刷新 last_seen，不重复累加 occurrences。
            updated = ledger.update('problems', old['id'], old['version'], changes)
        else:
            # 已恢复的问题再次出现：视为新一轮发生，保留首见时间，累计次数并重新打开。
            changes['occurrences'] = occurrence
            changes['closed_at'] = None
            updated = ledger.update('problems', old['id'], old['version'], changes, 'open')
        existing = [updated if problem['id'] == old['id'] else problem for problem in existing]
    return created


def observe_problems(ledger, product, now=None):
    """在问题发生或状态变化时即时持久化，并关闭已恢复的问题。

    不依赖早七复盘时刻，也不依赖通知总开关，保证「昨日发生、早七前恢复」的问题仍以
    open→resolved 的完整区间留存在台账，供复盘按时间窗口读取。
    """
    now = time.time() if now is None else now
    issues = _current_issues(ledger, product)
    created = record_problems(ledger, product, issues, now)
    active_ids, active_sources, active_runs, active_summaries = set(), set(), set(), set()
    for issue in issues:
        active_ids.add(_problem_ident(product['id'], issue))
        source = str(issue.get('source') or '')
        source_id = _issue_source_id(issue)
        if source_id is not None:
            active_sources.add((source, source_id))
        if source == 'task' and source_id is not None:
            active_runs.add(source_id)
        active_summaries.add(_issue_summary(issue))
    resolved = []
    for problem in ledger.scoped('problems', product['id']):
        if problem.get('status') != 'open' or problem['id'] in active_ids:
            continue
        source = str(problem.get('source') or '')
        # 历史记录可能没有 source_id：用 source+source_id、run_id、summary 回退判断是否仍活跃。
        if (source, str(problem.get('source_id'))) in active_sources:
            continue
        if source == 'task' and problem.get('run_id') in active_runs:
            continue
        if not problem.get('source_id') and problem.get('run_id') is None \
                and problem.get('summary') in active_summaries:
            continue
        history = _problem_history(problem)
        current_episode = next((episode for episode in reversed(history['episodes'])
                                if episode.get('closed_at') is None), None)
        if current_episode is not None:
            current_episode['closed_at'] = now
            current_episode['last_seen'] = now
        else:
            # 历史记录没有可靠发生区间时，只增加明确的恢复时点，不连接到全局首见时间。
            history['history_facts'].append({'at': now, 'status': 'resolved',
                'summary': problem.get('summary', '历史问题'), 'cause': problem.get('cause', ''),
                'occurrence': problem.get('occurrences', 1)})
            history['history_incomplete'] = True
        resolved.append(ledger.update('problems', problem['id'], problem['version'],
                                      dict(history, closed_at=now, last_seen=now), 'resolved'))
    return {'created': created, 'resolved': resolved}


def _run_events(ledger, product, now, known=None):
    for run in ledger.scoped('runs', product['id']):
        if run['status'] not in REPORT_STATUSES:
            continue
        key = f'run:{run["id"]}:{run["status"]}' + (f':{run["version"]}' if run['status'] == 'plan_review' else '')
        _queue(ledger, product, 'run_report', key, now, {
            'start': (run.get('created') or now - 86400) - 1, 'end': now + 1,
            'title': run.get('title') or run['id'],
            'focus': f'运行节点：{run.get("title") or run["id"]} 已进入 {run["status"]}。'
                     + (f'原因：{_safe(run.get("reason"), 300)}' if run.get('reason') else ''),
        }, known)


def _alert_events(ledger, product, now, known=None):
    for run in ledger.scoped('runs', product['id']):
        if run['status'] in ('blocked', 'fail') or run.get('uncertain'):
            reason = _safe(run.get('reason'), 300) or '未说明'
            _queue(ledger, product, 'alert', f'alert:run:{run["id"]}:{run["status"]}', now, {
                'start': now - 86400, 'end': now + 1, 'title': run.get('title') or run['id'],
                'focus': f'任务《{run.get("title") or run["id"]}》状态 {run["status"]}：{reason}',
            }, known)
    for row in ledger.scoped('deliveries', product['id']):
        if row['status'] in ('blocked', 'release_failed'):
            _queue(ledger, product, 'alert', f'alert:delivery:{row["id"]}:{row["status"]}', now, {
                'start': now - 86400, 'end': now + 1, 'title': row.get('branch') or row['id'],
                'focus': f'代码交付批次 {row.get("branch") or row["id"]} 状态 {row["status"]}：{_safe(row.get("reason"), 300)}',
            }, known)
    for action in ('probe', 'inspect'):
        result = product.get('last_' + action + '_result')
        at = product.get('last_' + action)
        if not isinstance(result, dict):
            continue
        if result.get('status') in ('blocked', 'fail') or result.get('monitor_mode') == 'basic':
            _queue(ledger, product, 'alert', f'alert:{action}:{int(at or 0)}', now, {
                'start': now - 86400, 'end': now + 1, 'title': action,
                'focus': f'应用{("运行监测" if action == "probe" else "体验巡查")}未通过：{_safe(result.get("reason"), 300)}',
            }, known)


def _quota_events(ledger, product, now, known=None):
    quota = _quota(ledger, product)
    day = quota['day']
    if quota['tokens_exhausted']:
        _queue(ledger, product, 'quota_exhausted', f'quota:tokens:{day}', now, {
            'start': now - 86400, 'end': now + 1, 'title': '开发 Token',
            'focus': f'开发 Token 额度已用尽（{quota["tokens_used"]} / {quota["tokens_limit"]}），'
                     f'北京时间次日零点恢复；可在项目设置调整。',
        }, known)
    policy = DEFAULTS | product.get('policy', {})
    for kind, limit in (('development', policy['runs_per_day']), ('discovery', policy['discovery_per_day'])):
        used = ledger.budget_used(product['id'], kind)
        if limit and used >= limit:
            _queue(ledger, product, 'quota_exhausted', f'quota:{kind}:{day}', now, {
                'start': now - 86400, 'end': now + 1, 'title': kind,
                'focus': f'{("今日需求" if kind == "development" else "需求发现")}额度已用尽（{used} / {limit}），次日零点恢复。',
            }, known)
    try:
        value = json.loads((ledger.store.state / 'quota.json').read_text())
    except (OSError, ValueError):
        value = {}
    limit = ledger.store.settings().get('max_per_day')
    if isinstance(value, dict) and value.get('day') == day and limit and value.get('count', 0) >= limit:
        _queue(ledger, product, 'quota_exhausted', f'quota:review:{day}', now, {
            'start': now - 86400, 'end': now + 1, 'title': '平台审查',
            'focus': f'平台每日审查额度已用尽（{value.get("count")} / {limit}），次日零点恢复。',
        }, known)


def _competitor_events(ledger, product, now, known=None):
    for job in ledger.scoped('research_jobs', product['id']):
        if job['status'] not in ('completed', 'failed'):
            continue
        result = job.get('result') if isinstance(job.get('result'), dict) else {}
        found = len(result.get('competitors', []) or [])
        label = '竞品发现' if job.get('action') == 'find_competitors' else '竞品分析'
        _queue(ledger, product, 'competitor', f'competitor:{job["id"]}:{job["status"]}', now, {
            'start': job.get('created') or now - 86400, 'end': now + 1, 'title': label,
            'focus': f'{label}任务{("完成" if job["status"] == "completed" else "失败")}，新增 {found} 个候选'
                     + (f'；{_safe(job.get("reason"), 300)}' if job.get('reason') else ''),
        }, known)


def _retro_events(ledger, product, now, config, known=None):
    day, start, end = window_for_retro(now, config.get('morning_hour', MORNING_HOUR))
    if end < product.get('created', 0):
        return None
    if now < end:  # 边界前不提前生成
        return None
    event_key = f'retro:{day.isoformat()}'
    ident = notification_id(product['id'], event_key)
    seen = ident in known if known is not None else _exists(ledger, ident)
    if seen:
        return None
    # 先按当前状态落库/关闭问题，再按窗口读取，保证「昨日发生、早七前恢复」的问题可见。
    try:
        observe_problems(ledger, product, now)
    except Exception as exc:
        ledger.store.event('autopilot_notification_error',
                           detail={'product_id': product['id'], 'reason': _safe(exc)})
    context = {'day': day, 'window': [start, end], 'title': day.isoformat()}
    subject, payload = render(ledger, product, 'morning_retro', now, context)
    item = ledger.create('notifications', {
        'product_id': product['id'], 'kind': 'morning_retro', 'event_key': event_key,
        'subject': subject, 'payload': payload, 'attempts': 0, 'next_attempt_at': 0,
        'receipt': None, 'error': '',
    }, 'queued', ident=ident)
    if known is not None:
        known.add(ident)
    return item


def enqueue(ledger, product, config, now, known=None):
    events = config['events']
    if events.get('run_report'):
        _run_events(ledger, product, now, known)
    if events.get('alert'):
        _alert_events(ledger, product, now, known)
    if events.get('quota_exhausted'):
        _quota_events(ledger, product, now, known)
    if events.get('competitor'):
        _competitor_events(ledger, product, now, known)
    if events.get('morning_retro'):
        _retro_events(ledger, product, now, config, known)


# ---------------------------------------------------------------- 投递

def _finish(ledger, item, status, error, now, receipt=None, attempts=None, db=None):
    changes = {'error': _safe(error), 'next_attempt_at': 0}
    if receipt is not None:
        changes['receipt'] = receipt
    if attempts is not None:
        changes['attempts'] = attempts
    return ledger.update('notifications', item['id'], item['version'], changes, status, db=db)


def _refused_map(value):
    """兼容真实 smtplib 的拒收字典与测试替身可能返回的地址集合。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, (list, tuple, set)):
        return {str(item): None for item in value}
    return {}


def _refusal_detail(refused):
    """把 SMTP 拒收字典转成不含凭据的可读原因。"""
    parts = []
    for address, info in (refused or {}).items():
        if isinstance(info, (tuple, list)) and len(info) == 2:
            parts.append(f'{_safe(address, 254)}（{_safe(info[0], 12)} {_safe(info[1], 120)}）')
        else:
            parts.append(_safe(address, 254))
    return '；'.join(parts) or 'SMTP 未返回可用的拒收原因'


def _retry(ledger, item, now, error, delivered, pending, receipt=None):
    """暂时失败或部分拒收：保留已送达收件人，只对未送达地址退避重试。"""
    attempts = item.get('attempts', 0) + 1
    if attempts >= MAX_ATTEMPTS:
        return _finish(ledger, item, 'partial' if delivered else 'failed', f'邮件发送连续失败 {attempts} 次：{error}', now,
                       receipt=receipt, attempts=attempts)
    changes = {'attempts': attempts,
               'next_attempt_at': now + min(BACKOFF_SECONDS * (2 ** (attempts - 1)), 3600),
               'error': _safe(error), 'sent_to': delivered, 'pending_to': pending}
    if receipt is not None:
        changes['receipt'] = receipt
    ledger.update('notifications', item['id'], item['version'], changes)
    ledger.store.event('autopilot_notification_retry',
                       detail={'id': item['id'], 'attempts': attempts, 'pending': len(pending)})
    return False


def _block_for_configuration(ledger, item, config, now, error, receipt=None):
    """配置版本判断与阻塞落库共用事务，避免旧发送覆盖新配置的重入队结果。"""
    with ledger.store.transaction() as db:
        latest_product = ledger.get('products', item['product_id'], db)
        latest_config = normalize(latest_product)
        latest_item = ledger.get('notifications', item['id'], db)
        if latest_item['status'] not in ('queued', 'blocked'):
            return latest_item
        if latest_config != config:
            changes = {'error': '发送期间通知配置已更新，将使用最新配置重试', 'next_attempt_at': 0}
            if receipt is not None:
                changes['receipt'] = receipt
            return ledger.update('notifications', latest_item['id'], latest_item['version'], changes, 'queued', db=db)
        return _finish(ledger, latest_item, 'blocked', error, now, receipt=receipt, db=db)


def _receipt_refusals(value):
    """持久化真实 SMTP 拒收码和原因，同时兼容旧地址列表回执。"""
    rows = _refused_map(value)
    result = {}
    for address, detail in rows.items():
        if isinstance(detail, dict):
            code, reason = detail.get('code', 0), detail.get('reason', '')
        elif isinstance(detail, (tuple, list)) and len(detail) == 2:
            code, reason = detail
        else:
            code, reason = 0, detail
        reason = reason.decode(errors='replace') if isinstance(reason, bytes) else str(reason)
        result[address] = {'code': int(code), 'reason': _safe(reason)}
    return result


def send(scheduler, item, now):
    ledger = scheduler.ledger
    product = ledger.get('products', item['product_id'])
    config = normalize(product)
    # tick 的启用列表可能在扫描后失效；发送采用最新快照，人工测试保留显式投递语义。
    if item.get('kind') != 'test' and not config['enabled']:
        return False
    payload = item.get('payload') or {}
    previous = item.get('receipt') if isinstance(item.get('receipt'), dict) else {}
    recipients = previous.get('requested') or config['recipients']
    delivered = list(previous.get('to', item.get('sent_to') or []))
    if not recipients:
        return _block_for_configuration(ledger, item, config, now, '发件人或收件人未配置完整，请先在项目设置的通知页补齐')
    withdrawn = [address for address in recipients
                 if address not in delivered and address not in config['recipients']]
    pending = [address for address in recipients
               if address not in delivered and address in config['recipients']]
    receipt = {'requested': list(recipients), 'to': delivered,
               'withdrawn': withdrawn,
               'refused': _receipt_refusals(previous.get('refused', {})),
               'message_id': previous.get('message_id'), 'at': now}
    if not pending:
        if withdrawn:
            return _finish(ledger, item, 'cancelled', '尚未接受的收件人已撤销，本次通知停止投递', now, receipt=receipt)
        receipt['refused'] = {}
        return _finish(ledger, item, 'sent', '', now, receipt=receipt)
    if not config['sender']:
        return _block_for_configuration(ledger, item, config, now, '发件人或收件人未配置完整，请先在项目设置的通知页补齐', receipt=receipt)
    code = credential_from_config(item['product_id'], config)
    if not code:
        return _block_for_configuration(ledger, item, config, now, '当前通知配置的邮箱授权码不可用，请先在通知页验证并保存', receipt=receipt)
    try:
        active_recipients = [address for address in recipients if address in config['recipients']]
        note = build_message(config['sender'], active_recipients, item['subject'],
                             payload.get('text', ''), payload.get('html', ''))
        if receipt['message_id']:
            note.replace_header('Message-ID', receipt['message_id'])
        receipt['message_id'] = note['Message-ID']
        server = connect(config['smtp'])
        try:
            server.login(config['sender'], code)
            try:
                refused = server.send_message(note, from_addr=config['sender'], to_addrs=pending)
            except smtplib.SMTPRecipientsRefused as exc:
                refused = exc.recipients
            refused = _refused_map(refused)
            receipt['refused'] = _receipt_refusals(refused)
            delivered.extend(address for address in pending if address not in refused)
        finally:
            try:
                server.quit()
            except Exception:
                pass
    except smtplib.SMTPAuthenticationError:
        return _block_for_configuration(ledger, item, config, now,
            '邮箱认证失败，请重新核对发件人与授权码；原凭据未被覆盖', receipt=receipt)
    except (smtplib.SMTPException, OSError, ValueError) as exc:
        return _retry(ledger, item, now, _safe(exc), delivered, pending, receipt)
    rejected = [address for address in pending if address in refused]
    if not rejected:
        if withdrawn:
            return _finish(ledger, item, 'cancelled', '当前收件人已接受，其余未接受的收件人已撤销', now, receipt=receipt)
        ledger.store.event('autopilot_notification_sent',
                           detail={'id': item['id'], 'kind': item['kind'], 'recipients': len(delivered)})
        return _finish(ledger, item, 'sent', '', now, receipt=receipt)
    # 部分收件人被拒收：不得把全部收件人记为已发送，需保留已送达名单并重试被拒地址。
    receipt['partial'] = bool(delivered)
    ledger.store.event('autopilot_notification_refused',
                       detail={'id': item['id'], 'accepted': len(pending) - len(rejected), 'refused': len(rejected)})
    return _retry(ledger, item, now, '部分收件人被拒收：' + _refusal_detail(refused),
                  delivered, rejected, receipt)


def process(scheduler, now, enabled_products=None):
    ledger = scheduler.ledger
    # 测试简报是用户显式触发的人工验证动作，不受「启用邮件通知」总开关影响；
    # 其余事件仍只投递已启用的项目，避免关闭通知后继续外发。
    # 这里用 by_status 全量查询待发送记录：Ledger.list 只回最近 1000 条，历史增长后
    # 较早的 queued 通知会被挤出而永久无法投递。
    due = [item for item in ledger.by_status('notifications', 'queued')
           if (item.get('next_attempt_at') or 0) <= now
           and (item['kind'] == 'test' or enabled_products is None
                or item['product_id'] in enabled_products)]
    if not due:
        return False
    due.sort(key=lambda item: (item['created'], item['id']))
    send(scheduler, due[0], now)
    return True


def tick(scheduler):
    ledger = scheduler.ledger
    now = time.time()
    enabled = set()
    for product in ledger.list('products'):
        if product.get('automation_disabled'):
            continue
        # 问题落库/关闭与通知总开关无关：只要项目在自动化运行，状态变化就即时持久化。
        try:
            observe_problems(ledger, product, now)
        except Exception as exc:
            ledger.store.event('autopilot_notification_error',
                               detail={'product_id': product['id'], 'reason': _safe(exc)})
        config = normalize(product)
        if not config['enabled']:
            continue
        enabled.add(product['id'])
        try:
            known = {item['id'] for item in ledger.scoped('notifications', product['id'])}
            enqueue(ledger, product, config, now, known)
        except Exception as exc:
            ledger.store.event('autopilot_notification_error',
                               detail={'product_id': product['id'], 'reason': _safe(exc)})
    try:
        return process(scheduler, now, enabled)
    except Exception as exc:
        ledger.store.event('autopilot_notification_error', detail={'reason': _safe(exc)})
        return False


# ---------------------------------------------------------------- 接口支撑

def get(ledger, product_id, db=None):
    """在同一只读数据库快照中返回通知设置、版本和投递状态，不迁移凭据。"""
    if db is None:
        with ledger.store.connect() as connection:
            connection.execute('BEGIN')
            return get(ledger, product_id, db=connection)
    product = ledger.get('products', product_id, db)
    config = normalize(product)
    records = sorted(ledger.scoped('notifications', product_id, db),
                     key=lambda item: (item['created'], item['id']), reverse=True)
    recent = [{
        'id': item['id'], 'kind': item['kind'], 'subject': item['subject'], 'status': item['status'],
        'created': item['created'], 'updated': item['updated'], 'attempts': item.get('attempts', 0),
        'error': item.get('error', ''), 'receipt': item.get('receipt'),
    } for item in records[:20]]
    stats = {status: sum(1 for item in records if item['status'] == status)
             for status in ('queued', 'sent', 'partial', 'failed', 'blocked', 'cancelled')}
    return {
        'enabled': config['enabled'],
        'config_version': config['config_version'],
        'sender_masked': mask(config['sender']),
        'sender_configured': bool(config['sender']),
        'credential_configured': bool(credential_from_config(product_id, config)),
        'recipients': config['recipients'],
        'events': config['events'],
        'smtp': config['smtp'],
        'morning_hour': config['morning_hour'],
        'recent': recent,
        'stats': stats,
    }


def _requeue_blocked(ledger, product_id, db):
    """修正配置或凭据后，给此前阻塞的通知一次重新投递机会。"""
    for item in ledger.scoped('notifications', product_id, db):
        if item['status'] == 'blocked':
            ledger.update('notifications', item['id'], item['version'],
                          {'next_attempt_at': 0, 'error': ''}, 'queued', db=db)


def check_configuration_versions(product, body, baseline=None):
    """保留旧产品版本语义，并对通知独立版本和请求开始快照执行事务内复查。"""
    config = normalize(product)
    if 'config_version' in body:
        version = body['config_version']
        if type(version) is not int or version < 0:
            raise ValueError('通知配置版本无效')
        if version != config['config_version']:
            raise Conflict('通知配置已更新，请刷新后重试')
    if body.get('version') is not None:
        version = body['version']
        if type(version) is not int or version < 1:
            raise ValueError('产品版本无效')
        if version != product['version']:
            raise Conflict('记录已更新，请刷新后重试')
    if baseline is not None and config != baseline:
        raise Conflict('通知配置已更新，请刷新后重试')


def configuration_effects(ledger, product_id, config, db):
    """配置、阻塞通知重入队与操作事件在同一数据库事务中生效。"""
    _requeue_blocked(ledger, product_id, db)
    ledger.store.event('autopilot_notifications_configured',
        detail={'product_id': product_id, 'enabled': config['enabled'],
                'config_version': config['config_version'],
                'events': [key for key, value in config['events'].items() if value],
                'recipients': len(config['recipients']),
                'credential_configured': bool(credential_from_config(product_id, config))}, db=db)


def commit_configuration(ledger, product, config, db):
    """仅保存实际变化，同时复用同事务重入队和事件逻辑。"""
    config = version_configuration(product, config)
    if config != normalize(product):
        product = ledger.update('products', product['id'], product['version'], {'notifications': config}, db=db)
    configuration_effects(ledger, product['id'], config, db)
    return product


def configure(ledger, product_id, body):
    if set(body) - {'config', 'credential', 'config_version', 'version', 'operation_id'}:
        raise ValueError('通知配置请求字段无效')
    changes = body.get('config', {})
    current = ledger.get('products', product_id)
    check_configuration_versions(current, body)
    baseline = normalize(current)
    code = body.get('credential')
    merged = prepare_configuration(current, changes, has_credential=code is not None)
    if code is not None:
        code = _credential_value(code)
        if not merged['sender']:
            raise ValueError('验证授权码前请先填写发件人邮箱')
        verify(merged['smtp'], merged['sender'], code)
    merged = stage_configuration_credential(current, merged, code)
    with ledger.store.transaction() as db:
        latest = ledger.get('products', product_id, db)
        check_configuration_versions(latest, body, baseline)
        commit_configuration(ledger, latest, merged, db)
        return get(ledger, product_id, db=db)


def add_recipients(ledger, product_id, body):
    if set(body) - {'recipient', 'recipients', 'config_version', 'version', 'operation_id'}:
        raise ValueError('通知收件人请求字段无效')
    values = body.get('recipients')
    if values is None:
        values = [body.get('recipient')]
    if not isinstance(values, list):
        raise ValueError('收件人必须为列表')
    addresses = []
    for value in values:
        if value in (None, ''):
            continue
        address = email(value, '收件人邮箱')
        if address not in addresses:
            addresses.append(address)
    with ledger.store.transaction() as db:
        latest = ledger.get('products', product_id, db)
        check_configuration_versions(latest, body)
        config = normalize(latest)
        recipients = list(dict.fromkeys(config['recipients'] + addresses))
        config = prepare_configuration(latest, {'recipients': recipients})
        config = stage_configuration_credential(latest, config)
        commit_configuration(ledger, latest, config, db)
        return get(ledger, product_id, db=db)


def send_test(ledger, product_id):
    product = ledger.get('products', product_id)
    config = normalize(product)
    if not config['sender']:
        raise ValueError('请先配置发件人邮箱')
    if not config['recipients']:
        raise ValueError('请先添加至少一个收件人邮箱')
    if not credential_from_config(product_id, config):
        raise ValueError('请先验证并保存邮箱授权码')
    now = time.time()
    subject, payload = render(ledger, product, 'test', now, {})
    return ledger.create('notifications', {
        'product_id': product_id, 'kind': 'test', 'event_key': f'test:{int(now * 1000)}',
        'subject': subject, 'payload': payload, 'attempts': 0, 'next_attempt_at': 0,
        'receipt': None, 'error': '',
    }, 'queued')
