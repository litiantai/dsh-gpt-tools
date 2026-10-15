"""研发通知的邮件 HTML：只渲染简报白名单字段，兼容 Gmail 的表格布局。"""

import datetime as dt
from html import escape
from zoneinfo import ZoneInfo


ZONE = ZoneInfo('Asia/Shanghai')
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',Arial,sans-serif"
TEXT = 'font-size:14px;line-height:1.75;color:#334155;overflow-wrap:anywhere;word-break:break-word;'
SMALL = 'font-size:12px;line-height:1.65;color:#64748b;overflow-wrap:anywhere;word-break:break-all;'
REPORTS = {
    'run_report': '运行节点报告', 'alert': '告警', 'quota_exhausted': '额度用尽提示',
    'competitor': '竞品发现与分析', 'morning_retro': '昨日研发简报', 'test': '通知测试简报',
}


def _e(value):
    """转义明确允许展示的文本，不将对象或任意数据键转换为页面内容。"""
    return escape(str(value), quote=True) if isinstance(value, (str, int, float)) else ''


def _count(value):
    """规范化台账计数，避免非数字内容进入数量或宽度属性。"""
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _number(value):
    """为计数添加千分位。"""
    return f'{_count(value):,}'


def _timestamp(value):
    """安全读取可选时间戳。"""
    try:
        return dt.datetime.fromtimestamp(float(value), ZONE)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _stamp(value):
    """用北京时间展示精确到秒的记录时间。"""
    stamp = _timestamp(value)
    return stamp.strftime('%Y-%m-%d %H:%M:%S') if stamp else '时间未记录'


def _window(value):
    """区分自然日与跨日运行窗口，不将任意区间误写成全天。"""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return '统计窗口未记录'
    left, right = (_timestamp(item) for item in value)
    if not left or not right:
        return '统计窗口未记录'
    if right < left:
        raise ValueError('统计窗口结束时间不能早于开始时间')
    if left.time() == dt.time.min and right.time() == dt.time.min and right - left == dt.timedelta(days=1):
        return left.strftime('%Y-%m-%d 00:00–24:00')
    precision = '%Y-%m-%d %H:%M:%S' if any((left.second, right.second, left.microsecond, right.microsecond)) else '%Y-%m-%d %H:%M'
    return left.strftime(precision) + ' 至 ' + right.strftime(precision)


def _rows(value):
    """只接受结构化列表中的记录对象。"""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, (list, tuple)) else []


def _table(content, style='', **attributes):
    """统一使用展示表格，关键布局不依赖头部样式表。"""
    attrs = ''.join(f' {key}="{_e(value)}"' for key, value in attributes.items())
    return f'<table role="presentation" border="0" cellpadding="0" cellspacing="0"{attrs} style="{style}">{content}</table>'


def _badge(label, tone='blue'):
    """为事实状态添加克制的文字标签。"""
    foreground, background = {
        'blue': ('#1d4ed8', '#eff6ff'), 'green': ('#166534', '#edf7ef'),
        'amber': ('#92400e', '#fff7e6'), 'gray': ('#475569', '#f1f5f9'),
    }.get(tone, ('#475569', '#f1f5f9'))
    return f'<span style="display:inline-block;padding:3px 8px;border-radius:4px;font-size:11px;line-height:1.5;font-weight:600;color:{foreground};background-color:{background};">{_e(label)}</span>'


def _empty(text='无'):
    """展示没有记录的区块，保留明确的空状态。"""
    return f'<p style="margin:0;padding:13px 16px;border:1px dashed #dbe2ea;border-radius:6px;{SMALL}">{_e(text)}</p>'


def _section(title, content, note=''):
    """创建简报的六个一级区块。"""
    note_html = f'<p style="margin:0 0 14px;{SMALL}">{_e(note)}</p>' if note else ''
    return f'<tr><td class="section-pad" style="padding:26px 28px 0;"><h2 style="margin:0 0 12px;font-size:16px;line-height:1.5;font-weight:700;color:#172b4d;">{_e(title)}</h2>{note_html}{content}</td></tr>'


def _record(title, label, ident='', tone='green', body=''):
    """将完整标题、事实标签和次级记录标识分层。"""
    record_id = f'<p style="margin:6px 0 0;{SMALL}">记录 {_e(ident)}</p>' if ident else ''
    return _table(f'<tr><td style="padding:15px 16px;">{_badge(label, tone)}<p style="margin:8px 0 0;font-size:14px;line-height:1.75;font-weight:600;color:#253858;overflow-wrap:anywhere;word-break:break-word;">{_e(title)}</p>{record_id}{body}</td></tr>',
                  'width:100%;margin:0 0 10px;border:1px solid #e2e8f0;border-radius:7px;', width='100%')


def _metrics(stats):
    """六项指标在桌面分两行，移动端可增强为两列。"""
    cards = []
    for key, label in (
        ('requirements_found', '新增需求'), ('runs_started', '启动任务'),
        ('accepted', '业务验收通过'), ('online', '交付上线'),
        ('releases', '发布批次'), ('queued', '当前排队'),
    ):
        cards.append(_table(f'<tr><td style="padding:4px;"><div style="padding:14px 12px;background-color:#f3f6fa;border:1px solid #e8edf4;border-radius:6px;"><p style="margin:0;font-size:24px;line-height:1.25;font-weight:700;color:#17365d;">{_number(stats.get(key))}</p><p style="margin:5px 0 0;font-size:12px;line-height:1.5;color:#64748b;">{label}</p></div></td></tr>',
                            'display:inline-table;width:33.333%;vertical-align:top;', width='33.333%', **{'class': 'metric-card'}))
    return _table('<tr><td style="font-size:0;line-height:0;">' + ''.join(cards) + '</td></tr>', 'width:100%;', width='100%')


def _issue(issue):
    """将阻塞原因与下一步分成独立卡片，不混合事实与行动。"""
    source = {'task': '任务阻塞', 'delivery': '交付阻塞'}.get(issue.get('source'), issue.get('source') or '阻塞')
    panels = []
    for label, value, background in (
        ('阻塞原因', issue.get('cause') or '未说明', '#fffaf1'),
        ('下一步', issue.get('action') or '未说明', '#f4f7fc'),
    ):
        panels.append(f'<td class="split-cell" width="50%" valign="top" style="width:50%;padding:4px;"><div style="padding:12px;background-color:{background};border-radius:5px;"><p style="margin:0 0 4px;font-size:11px;line-height:1.5;color:#64748b;font-weight:600;">{label}</p><div style="{TEXT}white-space:pre-wrap;">{_e(value)}</div></div></td>')
    body = _table('<tr>' + ''.join(panels) + '</tr>', 'width:100%;margin-top:10px;', width='100%')
    if issue.get('requirement_id'):
        body += f'<p style="margin:6px 0 0;{SMALL}">关联需求 {_e(issue["requirement_id"])}</p>'
    return _record(issue.get('title') or '未命名问题', source, issue.get('id'), 'amber', body)


def _problem(problem, label, tone='amber', extra=''):
    """保留复盘问题、原因、累计次数及可确认的历史事实。"""
    body = ''
    if problem.get('cause'):
        body += f'<p style="margin:8px 0 0;{TEXT}white-space:pre-wrap;">问题原因：{_e(problem["cause"])}</p>'
    if problem.get('occurrences') is not None:
        body += f'<p style="margin:8px 0 0;{SMALL}">累计 {_number(problem["occurrences"])} 次' + ('，持续跟踪' if tone == 'amber' else '') + '</p>'
    if extra:
        body += f'<p style="margin:8px 0 0;{SMALL}">{_e(extra)}</p>'
    if problem.get('history_incomplete'):
        body += f'<p style="margin:8px 0 0;{SMALL}">历史不完整，仅展示可确认的区间。</p>'
    return _record(problem.get('summary') or problem.get('title') or '未命名复盘问题', label, problem.get('id'), tone, body)


def _problems(data):
    """按统计窗口显示阻塞、持续跟踪、恢复和不完整历史。"""
    pieces = [_issue(row) for row in _rows(data.get('issues'))]
    pieces.extend(_problem(row, '复盘 · 持续跟踪') for row in _rows(data.get('open_problems')))
    window = data.get('window') or []
    window_end = _timestamp(window[1]) if len(window) == 2 else None
    for row in _rows(data.get('resolved_problems')):
        closed = _timestamp(row.get('closed_at'))
        if isinstance(row.get('status_label'), str) and row['status_label']:
            label = row['status_label']
        elif closed and window_end:
            label = '窗口结束后、截至复盘时间已恢复' if closed >= window_end else '窗口内已恢复'
        else:
            label = '已恢复（恢复时间未记录）'
        extra = '恢复时间：' + _stamp(row['closed_at']) + '（北京时间）' if closed else ''
        pieces.append(_problem(row, label, 'green', extra))
    for row in _rows(data.get('history_problems')):
        observed = {'open': '问题观察', 'resolved': '恢复观察'}.get(row.get('observed_status'), '发生观察')
        extra = row.get('history_label') if isinstance(row.get('history_label'), str) and row['history_label'] else f'历史不完整，仅能确认 {_stamp(row.get("observed_at"))}（北京时间）的{observed}，不能还原持续区间。'
        pieces.append(_problem(row, '历史观察', 'gray', extra))
    if data.get('history_incomplete'):
        pieces.append(f'<p style="margin:10px 0 0;{SMALL}">历史不完整，仅展示可确认的区间。</p>')
    return ''.join(pieces) or _empty()


def _quota(data):
    """格式化额度与使用率；无上限的额度不制造百分比。"""
    cards = []
    for label, used_key, limit_key, exhausted_key in (
        ('开发 Token', 'tokens_used', 'tokens_limit', 'tokens_exhausted'),
        ('今日需求', 'development_used', 'development_limit', 'development_exhausted'),
        ('需求发现', 'discovery_used', 'discovery_limit', 'discovery_exhausted'),
    ):
        used, limit = _count(data.get(used_key)), _count(data.get(limit_key))
        ratio = used / limit * 100 if limit else None
        exhausted = bool(data.get(exhausted_key)) or bool(limit and used >= limit)
        status = '已用尽' if exhausted and limit else '不限' if not limit else f'{ratio:.1f}%'
        color = '#b7791f' if exhausted and limit else '#426aa5'
        bar = ''
        if ratio is not None:
            filled = min(100, max(0, ratio))
            cells = f'<td width="{filled:.2f}%" bgcolor="{color}" style="height:5px;font-size:0;line-height:0;">&nbsp;</td>' if filled else ''
            if filled < 100:
                cells += f'<td width="{100-filled:.2f}%" bgcolor="#e8edf3" style="height:5px;font-size:0;line-height:0;">&nbsp;</td>'
            bar = _table('<tr>' + cells + '</tr>', 'width:100%;margin-top:12px;background-color:#e8edf3;border-radius:3px;', width='100%')
        percentage = f' · {ratio:.1f}%' if ratio is not None and exhausted else ''
        note = '未设置上限' if not limit else f'使用率 {ratio:.1f}%'
        content = f'<tr><td style="padding:15px 16px;">' + _table(f'<tr><td valign="top" style="{TEXT}font-weight:600;">{label}</td><td align="right" valign="top">{_badge(status + percentage, "amber" if exhausted and limit else "gray")}</td></tr>', 'width:100%;', width='100%')
        content += f'<p style="margin:8px 0 0;font-size:18px;line-height:1.5;color:#172b4d;font-weight:600;">{_number(used)} <span style="font-size:13px;font-weight:400;color:#64748b;">/ {_number(limit) if limit else "不限"}</span></p>{bar}<p style="margin:7px 0 0;{SMALL}">{note}</p></td></tr>'
        cards.append(_table(content, 'width:100%;margin:0 0 10px;border:1px solid #e2e8f0;border-radius:7px;', width='100%'))
    return ''.join(cards)


def _competitors(data):
    """将登记竞品名称与调研任务的状态和结论分层。"""
    names, jobs = _rows(data.get('new')), _rows(data.get('jobs'))
    if not names and not jobs:
        return _empty()
    content = ''
    if names:
        content += '<h3 style="margin:0 0 10px;font-size:13px;line-height:1.5;color:#64748b;">新登记竞品</h3>'
        for row in names:
            content += f'<p style="margin:0 0 8px;padding:10px 14px;border-left:3px solid #6585b6;background-color:#f5f8fc;{TEXT}font-weight:600;">{_e(row.get("name"))}</p>'
    if jobs:
        content += '<h3 style="margin:18px 0 10px;font-size:13px;line-height:1.5;color:#64748b;">调研任务</h3>'
        for row in jobs:
            label = '发现竞品' if row.get('action') == 'find_competitors' else '竞品分析'
            status = {'completed': '完成', 'failed': '失败'}.get(row.get('status'), row.get('status') or '状态未记录')
            body = f'<p style="margin:8px 0 0;{SMALL}">新增 {_number(row.get("found"))} 个候选</p>'
            if row.get('reason'):
                body += f'<div style="margin:8px 0 0;{TEXT}white-space:pre-wrap;">{_e(row["reason"])}</div>'
            content += _record(label + '任务', status, tone='green' if status == '完成' else 'amber', body=body)
    return content


def _actions(values, label, background):
    """分别展示当前待办和自进化建议，保留完整内容。"""
    values = [value for value in values if isinstance(value, (str, int, float))] if isinstance(values, (list, tuple)) else []
    items = ''.join(f'<tr><td width="16" valign="top" style="padding:5px 0;font-size:14px;line-height:1.75;color:#7690af;">•</td><td style="padding:5px 0;{TEXT}white-space:pre-wrap;">{_e(value)}</td></tr>' for value in values)
    return _table(f'<tr><td style="padding:15px 16px;"><h3 style="margin:0 0 7px;font-size:13px;line-height:1.5;color:#253858;">{label}</h3>{_table(items, "width:100%;", width="100%") if items else _empty()}</td></tr>',
                  f'width:100%;margin:0 0 10px;background-color:{background};border-radius:7px;', width='100%')


def _comprehensive(data):
    """邮件正文说明全量统计与重点卡点，全部证据保留在 HTML 附件。"""
    facts = data.get('comprehensive') or {}
    summary = facts.get('summary') or {}
    coverage = facts.get('coverage') or {}
    content = f'<p style="margin:0 0 12px;{TEXT}">{_e(summary.get("statement"))}</p>'
    metrics = (
        ('任务失败或阻塞回执', summary.get('run_failure_receipts')),
        ('受影响任务', summary.get('runs_with_problem_receipts')),
        ('巡检阻塞记录', summary.get('inspection_blocked_records')),
        ('归并历史问题组', summary.get('problem_groups')),
        ('当前任务卡点组', summary.get('current_run_blocker_groups')),
        ('当前需求等待组', summary.get('current_requirement_wait_groups')),
        ('待归因信号组', summary.get('current_signal_attribution_groups')),
    )
    cells = ''.join(f'<tr><td style="padding:6px 0;{TEXT}">{_e(label)}</td><td align="right" style="padding:6px 0;{TEXT}font-weight:700;">{_number(value) if value is not None else "—"}</td></tr>' for label, value in metrics)
    content += _table(cells, 'width:100%;margin:0 0 16px;', width='100%')
    history = sorted(_rows(facts.get('problem_items')), key=lambda row: _count(row.get('occurrences')), reverse=True)
    current = _rows(facts.get('current_blockers'))
    focused = [row for row in current if row.get('source_kind') != 'signals']
    for title, rows, limit in (('统计窗口内的历史失败与返修', history, 12), ('截至复盘时的任务卡点与需求等待', focused, 18)):
        content += f'<h3 style="margin:20px 0 10px;font-size:14px;color:#253858;">{title}</h3>'
        for row in rows[:limit]:
            body = f'<div style="margin:8px 0 0;{TEXT}white-space:pre-wrap;">{_e(row.get("reason") or "原因未记录")}</div>'
            if row.get('occurrences') is not None:
                body += f'<p style="margin:6px 0 0;{SMALL}">发生 {_number(row["occurrences"])} 次</p>'
            ids = row.get('requirement_ids') or []
            if ids:
                body += f'<p style="margin:6px 0 0;{SMALL}">关联需求 {_e(", ".join(ids))}</p>'
            content += _record(row.get('title') or '待核对事项', row.get('current_status') or '证据待核对', tone='amber', body=body)
        if not rows:
            content += _empty('当前可用证据中没有该类记录；覆盖缺口须单独核对。')
        if len(rows) > limit:
            content += f'<p style="margin:10px 0;{SMALL}">正文展示 {limit} 组重点，共 {_number(len(rows))} 组；全部分组与逐条证据见 HTML 附件。</p>'
    content += f'<h3 style="margin:20px 0 10px;font-size:14px;color:#253858;">来源覆盖与智能归因</h3><p style="margin:0 0 10px;{TEXT}">{"已覆盖全部可用来源。" if coverage.get("complete") else "来源存在覆盖缺口，不能断言没有问题。"}</p>'
    for row in _rows(coverage.get('gaps')):
        content += f'<p style="margin:5px 0;{SMALL}">{_e(row.get("source"))}：{_e(row.get("detail") or row.get("code"))}</p>'
    if data.get('analysis_pending'):
        content += f'<p style="margin:12px 0 0;{TEXT}">智能复盘等待异步执行。早七先送达完整事实报告；归因完成后，已有需求补充证据，新增可验收改进去重入池并沿研发流程推进。</p>'
    analysis = facts.get('analysis') or {}
    conversion = facts.get('conversion') or {}
    if analysis.get('status') == 'pass':
        content += f'<h3 style="margin:20px 0 10px;font-size:14px;color:#253858;">智能复盘结论与需求转化</h3><p style="margin:0 0 10px;{TEXT}white-space:pre-wrap;">{_e(analysis.get("summary"))}</p>'
        for row in _rows(analysis.get('problem_items')):
            body = f'<p style="margin:8px 0 0;{TEXT}">原因：{_e(row.get("cause"))}</p><p style="margin:6px 0 0;{TEXT}">下一步：{_e(row.get("next_action"))}</p>'
            content += _record(row.get('title') or '复盘发现', row.get('disposition') or '待跟踪', tone='blue', body=body)
        counters = '新增 ' + _number(conversion.get('created')) + ' · 复用 ' + _number(conversion.get('reused')) + ' · 入队 ' + _number(conversion.get('queued')) + ' · 待补证 ' + _number(conversion.get('skipped')) + ' · 失败 ' + _number(conversion.get('failed'))
        content += f'<p style="margin:14px 0 10px;{TEXT}font-weight:600;">需求转化回执：{counters}</p>'
        labels = {'created': '已新增需求', 'reused': '已复用需求', 'skipped': '待补证或待归因', 'failed': '转化失败'}
        for row in _rows(conversion.get('results')):
            body = f'<p style="margin:8px 0 0;{TEXT}">{_e(row.get("advancement") or row.get("reason") or row.get("status"))}</p>'
            content += _record(row.get('title') or '改进候选', labels.get(row.get('action'), '待跟踪'), row.get('requirement_id'), tone='green' if row.get('action') in ('created', 'reused') else 'amber', body=body)
        content += _actions(analysis.get('next_actions') or [], '复盘后续行动', '#f3f6fa')
    content += f'<p style="margin:12px 0 0;{SMALL}">随信 HTML 附件包含全部任务、巡检、审查、历史问题和当前卡点。最新结论及入池结果可在研发日报查看。报告 {_e(data.get("report_id"))}</p>'
    return content


def render_html(subject, data):
    """生成六节研发简报，完整保留白名单内容并转义所有动态文本。"""
    kind = data.get('kind')
    product = data.get('product') or '项目未命名'
    report_label = REPORTS.get(kind, '研发通知')
    title = subject
    prefix = f'[{product}] '
    if isinstance(subject, str) and subject.startswith(prefix):
        title = subject[len(prefix):]
    stats = data.get('stats') if isinstance(data.get('stats'), dict) else {}
    quota = data.get('quota') if isinstance(data.get('quota'), dict) else {}
    competitors = data.get('competitors') if isinstance(data.get('competitors'), dict) else {}
    done = ''.join(_record(row.get('title') or row.get('id') or '未命名需求', '业务验收通过', row.get('id')) for row in _rows(data.get('done')))
    done += ''.join(_record(row.get('title') or row.get('id') or '交付批次', '已合入主分支', row.get('id')) for row in _rows(data.get('online_detail')))
    done_title = '二、昨日完成' if kind == 'morning_retro' else '二、本次统计窗口完成'
    problem_note = '统计窗口内的阻塞、问题与可确认的恢复记录。'
    if data.get('problem_status_at') is not None:
        problem_note += f'问题状态核对截至 {_stamp(data["problem_status_at"])}（北京时间）。'
    quota_note = '额度按北京时间自然日计量。' + (f'额度日期：{quota["day"]}。' if isinstance(quota.get('day'), str) else '')
    todo = data.get('todo') or ['按巡检与需求发现节奏继续推进；暂无额外汇总事项。']
    sections = _section('一、概览指标', _metrics(stats), '统计窗口内的活动；当前排队为报告生成时的队列状态。')
    sections += _section(done_title, done or _empty())
    if isinstance(data.get('comprehensive'), dict):
        sections += _section('三、完整复盘：问题与卡点', _comprehensive(data), '历史发生、当前等待和覆盖缺口分别核对，完整证据随信附上。')
    else:
        sections += _section('三、阻塞与问题', _problems(data), problem_note)
    sections += _section('四、额度使用', _quota(quota), quota_note)
    sections += _section('五、竞品动态', _competitors(competitors))
    sections += _section('六、今日待办与自进化建议', _actions(todo, '当前待办', '#f3f6fa') + _actions(data.get('suggestions'), '自进化建议', '#f6f6fb'), '当前待办与统计窗口内的历史记录分别展示。')
    focus = f'<div style="margin-top:18px;padding:13px 16px;border-left:3px solid #5e83b8;border-radius:4px;background-color:#eff4fb;{TEXT}white-space:pre-wrap;">{_e(data["focus"])}</div>' if data.get('focus') else ''
    day = f'<p style="margin:6px 0 0;{SMALL}">报告日期：{_e(data["day"])}</p>' if data.get('day') else ''
    header = f'<tr><td class="section-pad" bgcolor="#172b4d" style="padding:22px 28px;background-color:#172b4d;"><p style="margin:0;font-size:16px;line-height:1.5;font-weight:700;color:#ffffff;">研发控制台</p><p style="margin:5px 0 0;font-size:10px;line-height:1.5;letter-spacing:1.5px;color:#b5c7e2;">DEEPSEEK ↔ GPT · 本机研发记录</p></td></tr>'
    header += f'<tr><td class="section-pad" style="padding:25px 28px 0;">' + _table(f'<tr><td style="font-size:12px;line-height:1.65;color:#64748b;overflow-wrap:anywhere;word-break:break-word;">项目 · {_e(product)}</td><td align="right" valign="top">{_badge(report_label)}</td></tr>', 'width:100%;', width='100%')
    header += f'<h1 style="margin:14px 0 14px;font-size:21px;line-height:1.55;font-weight:700;color:#172b4d;overflow-wrap:anywhere;word-break:break-word;">{_e(title)}</h1><div style="padding:12px 14px;background-color:#f8fafc;border:1px solid #e5eaf0;border-radius:6px;"><p style="margin:0;font-size:11px;line-height:1.5;color:#64748b;">统计窗口 · 北京时间（Asia/Shanghai）</p><p style="margin:5px 0 0;font-size:13px;line-height:1.7;color:#334155;">{_e(_window(data.get("window")))}</p>{day}</div>{focus}</td></tr>'
    footer = '<tr><td class="section-pad" style="padding:26px 28px;"><p style="margin:0;padding-top:16px;border-top:1px solid #e8edf3;font-size:11px;line-height:1.8;color:#94a3b8;">本简报由本机台账复算生成，未使用任何外部数据源。</p></td></tr>'
    shell = _table(header + sections + footer, 'width:100%;max-width:680px;background-color:#ffffff;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;', width='680', align='center', **{'class': 'email-shell'})
    canvas = _table(f'<tr><td align="center" class="canvas-pad" style="padding:28px 12px;">{shell}</td></tr>', 'width:100%;background-color:#edf1f5;', width='100%')
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{_e(subject)}</title>
<style>@media only screen and (max-width:600px){{.canvas-pad{{padding:12px 8px!important}}.section-pad{{padding-left:18px!important;padding-right:18px!important}}.metric-card{{width:50%!important}}.split-cell{{display:block!important;width:auto!important}}.email-shell{{border-radius:6px!important}}}}</style></head>
<body style="margin:0;padding:0;width:100%;background-color:#edf1f5;font-family:{FONT};-webkit-text-size-adjust:100%;">{canvas}</body></html>'''
