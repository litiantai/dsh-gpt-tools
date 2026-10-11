"""本机邮件通知：凭据隔离、项目订阅、事件触发与可复算的邮件简报。

安全边界：
- SMTP 授权码只写入 ``DSH_HOME/supervisor/credentials/email``（目录 700、文件 600，
  原子替换），绝不进入项目台账、操作日志、事件或邮件正文；接口只回布尔状态。
- 自动化测试使用假 SMTP 或临时回环 SMTP 服务，不访问外部邮箱、不使用真实凭据。
- 发送在调度器单线程内完成，每个 tick 最多一封，不新开线程或常驻子进程。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html as html_module
import json
import os
import re
import smtplib
import tempfile
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from zoneinfo import ZoneInfo

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

def credential_file():
    """SMTP 授权码本机文件，与项目源码和业务台账隔离。"""
    return Path(os.environ.get('DSH_HOME', str(Path.home() / '.dsh'))) / 'supervisor/credentials/email'


def credential():
    """只在内存中读取授权码，不写台账、不回显。"""
    path = credential_file()
    try:
        value = path.read_text().strip()
    except OSError:
        return None
    return value or None


def credential_configured():
    return credential_file().is_file()


def save_credential(code):
    """原子替换授权码；失败保留已有凭据。"""
    value = (code or '').strip()
    if not value or len(value) > 512 or any(ch in value for ch in '\r\n'):
        raise ValueError('邮箱授权码格式无效')
    path = credential_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix='.email-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


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
    return value


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
    return config


def validate_structure(value):
    """供 project.validate 使用：拒绝结构错误的持久化配置。"""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('通知配置必须为对象')
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
    if not isinstance(changes, dict):
        raise ValueError('通知配置必须为对象')
    known = {'enabled', 'sender', 'recipients', 'events', 'smtp', 'morning_hour'}
    if set(changes) - known:
        raise ValueError('通知配置字段无效')
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


def collect(ledger, product, start, end, now, context):
    pid = product['id']
    runs = ledger.scoped('runs', pid)
    requirements = ledger.scoped('requirements', pid)
    deliveries = ledger.scoped('deliveries', pid)
    releases = ledger.scoped('releases', pid)
    problems = [item for item in ledger.scoped('problems', pid) if item.get('status') == 'open']
    research = ledger.scoped('research_jobs', pid)
    competitors = ledger.scoped('competitors', pid)
    accepted = [run for run in runs if _inside(run.get('accepted_at'), start, end)]
    online = [row for row in deliveries if _inside(row.get('merged_at'), start, end)]
    release_rows = [row for row in releases if _inside(row.get('created'), start, end) or _inside(row.get('updated'), start, end)]
    found = [row for row in requirements if _inside(row.get('created'), start, end)]
    started = [row for row in runs if _inside(row.get('created'), start, end)]
    issues = []
    for run in runs:
        if run['status'] == 'blocked':
            issues.append({'source': 'task', 'id': run['id'], 'title': run.get('title', '未命名任务'),
                           'cause': run.get('reason', '未说明'), 'action': '核对原因后重试或补充条件',
                           'requirement_id': run.get('requirement_id')})
    for row in deliveries:
        if row['status'] in ('blocked', 'release_failed'):
            issues.append({'source': 'delivery', 'id': row['id'], 'title': row.get('branch') or row.get('title', '交付批次'),
                           'cause': row.get('reason', '未说明'), 'action': '处理代码交付阻塞后继续'})
    open_problems = [{'id': problem['id'], 'summary': problem.get('summary', ''), 'cause': problem.get('cause', ''),
                      'occurrences': problem.get('occurrences', 1)} for problem in problems]
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
    if issues:
        todo.append(f'{len(issues)} 项阻塞或复盘问题需要人工处理。')
    for row in new_competitors[:5]:
        todo.append(f'评估竞品动态：{row["name"]}')
    suggestions = [f'复盘问题「{_safe(problem.get("summary", ""), 60)}」并纳入下一轮需求候选，完成后再复验。'
                   for problem in problems[:5]]
    if not suggestions:
        suggestions.append('当前没有未解决的复盘问题；保持巡检、需求发现与代码评审节奏。')
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
            'blocked': len(issues),
        },
        'done': [{'id': run['id'], 'title': run.get('title', ''), 'status': run['status'],
                  'at': run.get('accepted_at')} for run in accepted],
        'online_detail': [{'id': row['id'], 'title': row.get('branch') or row.get('title', ''),
                           'at': row.get('merged_at')} for row in online],
        'issues': issues,
        'open_problems': open_problems,
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
    if left.date() == right.date() or (right - left) == dt.timedelta(days=1):
        return left.strftime('%Y-%m-%d 00:00–24:00')
    return left.strftime('%Y-%m-%d %H:%M') + ' 至 ' + right.strftime('%Y-%m-%d %H:%M')


def render_text(subject, data):
    lines = [subject, '', '一、概览指标',
             f'- 统计窗口：{_window_label(*data["window"])}（Asia/Shanghai）',
             f'- 新增需求：{data["stats"]["requirements_found"]}',
             f'- 启动任务：{data["stats"]["runs_started"]}',
             f'- 业务验收通过：{data["stats"]["accepted"]}',
             f'- 交付上线：{data["stats"]["online"]}',
             f'- 发布批次：{data["stats"]["releases"]}',
             f'- 当前排队：{data["stats"]["queued"]}',
             '', *([data['focus'], ''] if data.get('focus') else []),
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
        lines.append(f'- [复盘] {problem["summary"]}（累计 {problem["occurrences"]} 次，持续跟踪）')
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
    issues += [f'[复盘] {problem["summary"]}（累计 {problem["occurrences"]} 次，持续跟踪）' for problem in data['open_problems']]
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


def record_problems(ledger, product, issues):
    """把未解决问题结构化落库，供后续需求发现复算引用。"""
    created = []
    now = time.time()
    for issue in issues:
        summary = (str(issue.get('title') or '未命名问题') + '：' + str(issue.get('cause') or '未说明'))[:500]
        ident = 'problem-' + hashlib.sha256(summary.encode()).hexdigest()[:32]
        requirement_id = issue.get('requirement_id')
        signal_ids = []
        if requirement_id:
            try:
                requirement = ledger.get('requirements', requirement_id)
            except KeyError:
                requirement = {}
            signal_ids = [value for value in requirement.get('signal_ids', []) if isinstance(value, str)]
        try:
            old = ledger.get('problems', ident)
        except KeyError:
            created.append(ledger.create('problems', {
                'product_id': product['id'], 'summary': summary,
                'cause': str(issue.get('cause') or '')[:1000], 'source': issue.get('source', ''),
                'status': 'open', 'occurrences': 1, 'first_seen': now, 'last_seen': now,
                'run_id': issue.get('id') if issue.get('source') == 'task' else None,
                'requirement_id': requirement_id, 'signal_ids': signal_ids,
            }, 'open', ident=ident))
            continue
        ledger.update('problems', old['id'], old['version'],
                      {'last_seen': now, 'occurrences': old.get('occurrences', 1) + 1,
                       'cause': str(issue.get('cause') or '')[:1000],
                       'requirement_id': requirement_id or old.get('requirement_id'),
                       'signal_ids': signal_ids or old.get('signal_ids', [])})
    return created


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
    context = {'day': day, 'window': [start, end], 'title': day.isoformat()}
    subject, payload = render(ledger, product, 'morning_retro', now, context)
    record_problems(ledger, product, collect(ledger, product, start, end, now, context)['issues'])
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

def _finish(ledger, item, status, error, now, receipt=None, attempts=None):
    changes = {'error': _safe(error), 'next_attempt_at': 0}
    if receipt is not None:
        changes['receipt'] = receipt
    if attempts is not None:
        changes['attempts'] = attempts
    return ledger.update('notifications', item['id'], item['version'], changes, status)


def send(scheduler, item, now):
    ledger = scheduler.ledger
    product = ledger.get('products', item['product_id'])
    config = normalize(product)
    if not config['sender'] or not config['recipients']:
        return _finish(ledger, item, 'blocked', '发件人或收件人未配置完整，请先在项目设置的通知页补齐', now)
    code = credential()
    if not code:
        return _finish(ledger, item, 'blocked', '本机尚未配置邮箱授权码，请先在通知页验证并保存', now)
    payload = item.get('payload') or {}
    previous = item.get('receipt') or {}
    recipients = previous.get('requested') or config['recipients']
    accepted = list(previous.get('to', []))
    pending = [address for address in recipients if address not in accepted]
    receipt = {'requested': recipients, 'to': accepted, 'refused': previous.get('refused', {}),
               'message_id': previous.get('message_id'), 'at': now}
    try:
        note = build_message(config['sender'], recipients, item['subject'],
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
            receipt['refused'] = {address: {'code': int(value[0]),
                'reason': _safe(value[1].decode(errors='replace') if isinstance(value[1], bytes) else str(value[1]))}
                for address, value in refused.items()}
            accepted.extend(address for address in pending if address not in refused)
        finally:
            try:
                server.quit()
            except Exception:
                pass
    except smtplib.SMTPAuthenticationError:
        return _finish(ledger, item, 'blocked', '邮箱认证失败，请重新核对发件人与授权码；原凭据未被覆盖', now, receipt=receipt)
    except (smtplib.SMTPException, OSError, ValueError) as exc:
        attempts = item.get('attempts', 0) + 1
        if attempts >= MAX_ATTEMPTS:
            return _finish(ledger, item, 'partial' if accepted else 'failed', f'邮件发送连续失败 {attempts} 次：{_safe(exc)}', now, receipt=receipt, attempts=attempts)
        delay = min(BACKOFF_SECONDS * (2 ** (attempts - 1)), 3600)
        ledger.update('notifications', item['id'], item['version'],
                      {'attempts': attempts, 'next_attempt_at': now + delay, 'error': _safe(exc), 'receipt': receipt})
        ledger.store.event('autopilot_notification_retry', detail={'id': item['id'], 'attempts': attempts})
        return False
    if receipt['refused']:
        attempts = item.get('attempts', 0) + 1
        reason = f'SMTP 已接受 {len(accepted)}/{len(recipients)} 位收件人，{len(receipt["refused"])} 位被拒收'
        if attempts >= MAX_ATTEMPTS:
            return _finish(ledger, item, 'partial' if accepted else 'failed', reason + '；重试次数已用尽', now,
                           receipt=receipt, attempts=attempts)
        ledger.update('notifications', item['id'], item['version'],
                      {'attempts': attempts, 'next_attempt_at': now + min(BACKOFF_SECONDS * 2**(attempts - 1), 3600),
                       'error': reason + '；将仅重试未接受的收件人', 'receipt': receipt})
        return False
    ledger.store.event('autopilot_notification_sent', detail={'id': item['id'], 'kind': item['kind']})
    return _finish(ledger, item, 'sent', '', now, receipt=receipt)


def process(scheduler, now, enabled_products=None):
    ledger = scheduler.ledger
    # 测试简报是用户显式触发的人工验证动作，不受「启用邮件通知」总开关影响；
    # 其余事件仍只投递已启用的项目，避免关闭通知后继续外发。
    due = [item for item in ledger.list('notifications')
           if item['status'] == 'queued' and (item.get('next_attempt_at') or 0) <= now
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

def get(ledger, product_id):
    product = ledger.get('products', product_id)
    config = normalize(product)
    records = sorted(ledger.scoped('notifications', product_id),
                     key=lambda item: (item['created'], item['id']), reverse=True)
    recent = [{
        'id': item['id'], 'kind': item['kind'], 'subject': item['subject'], 'status': item['status'],
        'created': item['created'], 'updated': item['updated'], 'attempts': item.get('attempts', 0),
        'error': item.get('error', ''), 'receipt': item.get('receipt'),
    } for item in records[:20]]
    stats = {status: sum(1 for item in records if item['status'] == status)
                 for status in ('queued', 'sent', 'partial', 'failed', 'blocked')}
    return {
        'enabled': config['enabled'],
        'sender_masked': mask(config['sender']),
        'sender_configured': bool(config['sender']),
        'credential_configured': credential_configured(),
        'recipients': config['recipients'],
        'events': config['events'],
        'smtp': config['smtp'],
        'morning_hour': config['morning_hour'],
        'recent': recent,
        'stats': stats,
    }


def _requeue_blocked(ledger, product_id):
    """修正配置或凭据后，给此前阻塞的通知一次重新投递机会。"""
    for item in ledger.scoped('notifications', product_id):
        if item['status'] == 'blocked':
            ledger.update('notifications', item['id'], item['version'],
                          {'next_attempt_at': 0, 'error': ''}, 'queued')


def configure(ledger, product_id, body):
    changes = body.get('config') if isinstance(body.get('config'), dict) else {}
    current = ledger.get('products', product_id)
    merged = validate(merge(normalize(current), changes))
    code = body.get('credential')
    if code is not None:
        if not isinstance(code, str) or not code.strip():
            raise ValueError('邮箱授权码不能为空')
        if not merged['sender']:
            raise ValueError('验证授权码前请先填写发件人邮箱')
        verify(merged['smtp'], merged['sender'], code.strip())
        save_credential(code.strip())
    with ledger.store.transaction() as db:
        latest = ledger.get('products', product_id, db)
        if body.get('version') is not None and body['version'] != latest['version']:
            from review_core import Conflict
            raise Conflict('记录已更新，请刷新后重试')
        ledger.update('products', product_id, latest['version'], {'notifications': merged}, db=db)
    _requeue_blocked(ledger, product_id)
    ledger.store.event('autopilot_notifications_configured',
                       detail={'product_id': product_id, 'enabled': merged['enabled'],
                               'events': [key for key, value in merged['events'].items() if value],
                               'recipients': len(merged['recipients']),
                               'credential_configured': credential_configured()})
    return get(ledger, product_id)


def add_recipients(ledger, product_id, body):
    values = body.get('recipients')
    if values is None:
        values = [body.get('recipient')]
    if not isinstance(values, list):
        raise ValueError('收件人必须为列表')
    current = ledger.get('products', product_id)
    config = normalize(current)
    for value in values:
        if value in (None, ''):
            continue
        address = email(value, '收件人邮箱')
        if address not in config['recipients']:
            config['recipients'].append(address)
    if len(config['recipients']) > MAX_RECIPIENTS:
        raise ValueError(f'收件人数量不能超过 {MAX_RECIPIENTS} 个')
    with ledger.store.transaction() as db:
        latest = ledger.get('products', product_id, db)
        ledger.update('products', product_id, latest['version'], {'notifications': config}, db=db)
    return get(ledger, product_id)


def send_test(ledger, product_id):
    product = ledger.get('products', product_id)
    config = normalize(product)
    if not config['sender']:
        raise ValueError('请先配置发件人邮箱')
    if not config['recipients']:
        raise ValueError('请先添加至少一个收件人邮箱')
    if not credential_configured():
        raise ValueError('请先验证并保存邮箱授权码')
    now = time.time()
    subject, payload = render(ledger, product, 'test', now, {})
    return ledger.create('notifications', {
        'product_id': product_id, 'kind': 'test', 'event_key': f'test:{int(now * 1000)}',
        'subject': subject, 'payload': payload, 'attempts': 0, 'next_attempt_at': 0,
        'receipt': None, 'error': '',
    }, 'queued')
