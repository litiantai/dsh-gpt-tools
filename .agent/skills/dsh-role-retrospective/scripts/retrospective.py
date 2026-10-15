#!/usr/bin/env python3
"""收集复盘证据、生成 HTML 报告，并按明确授权去重登记改进需求。"""
from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
from html import escape
import json
from pathlib import Path
import re
import shutil
import sys
import uuid
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener


def clean(value):
    text = str('' if value is None else value)
    text = re.sub(r'(?i)(Bearer\s+)[\w.\-/+=]+', r'\1[已隐藏]', text)
    return re.sub(r'(?i)((?:api[_-]?key|token|password|cookie|authorization|credential|secret)\s*[=:]\s*)(?:"[^"\n]*"|\x27[^\x27\n]*\x27|[^\s,;}]+)', r'\1[已隐藏]', text)


def e(value):
    return escape(clean(value), quote=True)


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def rows(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def coverage_rows(value):
    if isinstance(value, list):
        return rows(value)
    if not isinstance(value, dict):
        return []
    gaps = value.get('gaps', [])
    result = []
    for kind, source in value.get('sources', {}).items():
        notes = [str(gap.get('detail', '')) for gap in gaps if gap.get('source') in (kind, 'all')]
        result.append({'source_kind': kind, 'read_count': source.get('records', 0),
                       'in_window_count': source.get('window_records', 0),
                       'failed_count': source.get('failed_count', source.get('problem_records')),
                       'status': 'complete' if source.get('available') and not notes else 'incomplete',
                       'notes': notes})
    return result


def identifiers(item):
    keys = ('source_ids', 'signal_ids', 'evidence_ids', 'requirement_ids')
    return [(key, str(value)) for key in keys for value in item.get(key, []) if isinstance(value, (str, int))]


LABELS = {'runs': '任务执行与验证', 'reviews': '方案与验收审查', 'signals': '巡检与发现信号',
          'inspections': '巡检记录', 'inspection_rounds': '巡检轮次', 'deliveries': '代码交付与发布验证',
          'code_reviews': '代码评审', 'daily_reports': '归因与复盘执行', 'requirements': '需求池',
          'evidence': '检查证据', 'products': '项目监测与主实例同步', 'inspection_plans': '巡检方案设计',
          'coordinators': '协调与执行权限', 'research_jobs': '竞品研究', 'events': '状态转换历史',
          'current_progress': '当前执行与等待', 'notifications': '邮件投递', 'scans': '接入扫描',
          'test_chains': '测试链', 'test_chain_versions': '测试链版本', 'test_chain_runs': '测试链执行',
          'test_chain_baselines': '测试链基线', 'requirement_groups': '跨项目需求组',
          'design_versions': '设计交付', 'evaluations': '评测任务', 'releases': '发布批次'}
STATES = {'pending': '待处理', 'queued': '已排队', 'blocked': '已阻塞', 'waiting_for_reply': '等待协作回复',
          'awaiting_external': '等待外部条件', 'investigating': '调查中', 'planning': '制定方案',
          'plan_review': '方案审查', 'developing': '开发中', 'verifying': '独立验证',
          'acceptance_review': '业务验收', 'delivered': '已交付未上线', 'online': '已上线',
          'resolved': '已记录恢复', 'completed': '已完成', 'unknown': '证据不足', 'failed': '执行失败',
          'fail': '未通过', 'observed': '仅完成观察', 'pass': '已通过', 'revise': '需要返修',
          'new': '窗口内新发生', 'ongoing': '窗口内持续', 'recovered': '窗口内有恢复证据',
          'not_observed': '窗口内未观察到', 'history_incomplete': '历史记录不完整',
          'historical_failed': '历史失败记录，当前影响待核对', 'abnormal': '执行异常待归因',
          'sent': 'SMTP 已接受', 'smtp_accepted': 'SMTP 已接受', 'partial': '部分收件人失败',
          'retry_wait': '投递失败等待重试'}


def render_report(evidence, analysis=None, conversion=None):
    analysis = analysis or {}
    project, period = evidence.get('project', {}), evidence.get('period', {})
    coverage = coverage_rows(evidence.get('coverage'))
    for row in coverage:
        kind = row.get('source_kind')
        statistics = evidence.get('statistics', {})
        statuses = statistics.get('receipt_status_counts', {}).get(kind, {})
        if kind == 'inspections':
            statuses = statistics.get('inspection_status_counts', {})
        elif kind == 'reviews':
            statuses = statistics.get('review_decisions', {})
        if statuses:
            row['failed_count'] = sum(int(statuses.get(key, 0) or 0) for key in ('fail', 'failed', 'blocked', 'revise', 'error', 'partial', 'retry_wait'))
    problems = rows(evidence.get('problem_items'))
    blockers = rows(evidence.get('current_blockers'))
    requirements = {r['id']: r for r in rows(evidence.get('requirements')) if r.get('id')}
    source_total = sum(int(row.get('in_window_count', row.get('window_count', 0)) or 0) for row in coverage)
    occurrences = sum(int(row.get('occurrences', 1) or 1) for row in problems)
    complete = bool(coverage) and all(row.get('status') in ('complete', 'pass', 'ok') for row in coverage)
    if isinstance(evidence.get('coverage'), dict):
        complete = complete and evidence['coverage'].get('complete') is True
    summary = analysis.get('summary') or ('统计窗口发现异常与返修记录，需要结合当前进展处理。' if problems else
              '当前已收集记录未发现异常；请同时核对数据覆盖和当前卡点。')

    def paragraph(value):
        return f'<p>{e(value)}</p>'

    def status(value):
        return STATES.get(str(value), str(value or '未记录'))

    def provenance(item):
        values = identifiers(item)
        if not values:
            return '<p class="muted">缺少可追溯标识，需要补齐证据。</p>'
        content = ''.join(f'<div><b>{e(key)}</b> <code>{e(value)}</code></div>' for key, value in values[:24])
        tail = f'<p>另有 {len(values)-24} 个标识，完整清单见证据 JSON。</p>' if len(values) > 24 else ''
        return '<details><summary>来源与证据</summary>' + content + tail + '</details>'

    def links(item):
        ids = item.get('requirement_ids', [])
        return ''.join(f'<p class="requirement">关联需求：{e(requirements.get(ident, {}).get("title") or ident)} · '
                       f'{e(status(requirements.get(ident, {}).get("status")))} <code>{e(ident)}</code></p>' for ident in ids)

    def issue_card(item, current=False):
        source = item.get('source_kind') or 'unknown'
        count = int(item.get('occurrences', 1) or 1)
        window_status = item.get('window_status')
        time_bases = {'receipt_at', 'evidence_at', 'finished_at', 'created', 'last_seen', 'current_snapshot', 'event_at'}
        if window_status in time_bases:
            historical = sorted({status(observation.get('historical_status')) for observation in rows(item.get('observations')) if observation.get('historical_status')})
            window_status = ' / '.join(historical) or '异常记录，历史状态详见来源'
        current_status = item.get('current_status') or item.get('status')
        parts = [f'<article class="issue"><div class="row"><span class="badge">{e(LABELS.get(source, source))}</span>',
                 f'<span class="muted">{count:,} 次记录</span></div>',
                 f'<h3>{e(item.get("title") or item.get("summary") or "未命名问题")}</h3>']
        if not current and window_status:
            parts.append(f'<p class="muted">统计窗口：{e(status(window_status))} · 当前：{e(status(current_status))}</p>')
        elif current_status:
            parts.append(f'<p class="muted">当前：{e(status(current_status))}</p>')
        reason = item.get('reason') or item.get('cause')
        if reason:
            parts.append('<div class="reason"><b>观察与原因</b>' + paragraph(reason) + '</div>')
        if item.get('action') or item.get('next_action'):
            parts.append('<div class="action"><b>下一步</b>' + paragraph(item.get('action') or item['next_action']) + '</div>')
        if 'attempts_lifetime' in item:
            parts.append(paragraph('通知累计尝试 ' + str(item['attempts_lifetime']) + ' 次（不是当日独立故障数）'
                                   + ('；下次重试：' + str(item['next_attempt_at']) if item.get('next_attempt_at') else '')))
        parts.extend([links(item), provenance(item), '</article>'])
        return ''.join(parts)

    def grouped_cards(items, current=False):
        groups = {}
        for item in items:
            groups.setdefault(item.get('source_kind') or 'unknown', []).append(item)
        order = {key: index for index, key in enumerate(('runs', 'requirements', 'reviews', 'deliveries', 'products', 'inspections', 'inspection_rounds', 'daily_reports', 'signals'))}
        result = ''
        for kind, entries in sorted(groups.items(), key=lambda pair: order.get(pair[0], 99)):
            opened = ' open' if current and kind in ('runs', 'requirements') else ''
            count = sum(int(item.get('occurrences', 1) or 1) for item in entries)
            result += f'<details class="source-group"{opened}><summary>{e(LABELS.get(kind, kind))} · {len(entries)} 组 / {count:,} 条发生记录</summary>' + ''.join(issue_card(item, current) for item in entries) + '</details>'
        return result

    source_rows = ''.join('<tr>' + ''.join(f'<td>{value}</td>' for value in (
        e(LABELS.get(row.get('source_kind', row.get('source', '')), row.get('source_kind', row.get('source', '')))),
        e(row.get('read_count', row.get('total_records', 0))), e(row.get('in_window_count', row.get('window_count', 0))),
        e(row.get('failed_count', row.get('problem_count'))) if row.get('failed_count', row.get('problem_count')) is not None else '—', e(row.get('status')),
        e('; '.join(row.get('notes', [])) if isinstance(row.get('notes'), list) else row.get('notes', '')))) + '</tr>' for row in coverage)
    coverage_html = '<div class="table-wrap"><table><thead><tr><th>来源</th><th>读取记录</th><th>窗口内记录</th><th>异常事件</th><th>覆盖</th><th>说明</th></tr></thead><tbody>' + source_rows + '</tbody></table></div><p class="muted">异常事件按执行回执、巡检判定和审查结论计量；信号另计新增与待归因数量，其异常不重复冒充一次独立执行失败。未建立该来源的异常事件口径时显示 —，表示未统计。</p>'
    if isinstance(evidence.get('coverage'), dict) and evidence['coverage'].get('gaps'):
        coverage_html += '<details><summary>历史完整性与缺失来源</summary><ul>' + ''.join(
            f'<li>{e(gap.get("source"))}：{e(gap.get("detail"))}（{e(gap.get("count", 1))}）</li>'
            for gap in rows(evidence['coverage']['gaps'])) + '</ul></details>'
    candidates = rows(analysis.get('requirement_candidates'))
    assessed = []
    for item in rows(analysis.get('problem_items')):
        enriched = dict(item)
        if item.get('source_id') and not item.get('source_ids'):
            enriched['source_ids'] = [item['source_id']]
        if item.get('disposition'):
            enriched['action'] = str(item.get('next_action') or '') + '（处理判定：' + str(item['disposition']) + '）'
        assessed.append(enriched)
    assessed_html = ''.join(issue_card(item) for item in assessed)
    results = rows((conversion or {}).get('results'))
    results_by_title = {row.get('title'): row for row in results}
    converted_html = ''
    for candidate in candidates:
        result = results_by_title.get(candidate.get('title'), {})
        state = {'created': '已加入需求池', 'reused': '复用已有需求', 'failed': '入池失败', 'skipped': '未转化'}.get(result.get('action'), '候选需求，尚未入池')
        converted_html += '<article class="issue"><span class="badge">' + e(state) + '</span><h3>' + e(candidate.get('title')) + '</h3>'
        converted_html += paragraph(candidate.get('impact')) + '<ul>' + ''.join(f'<li>{e(item)}</li>' for item in candidate.get('acceptance', [])) + '</ul>'
        if result.get('requirement_id'):
            converted_html += '<p class="requirement">需求 <code>' + e(result['requirement_id']) + '</code> · ' + e(status(result.get('status'))) + '</p>'
        if result.get('reason'):
            converted_html += paragraph(result['reason'])
        if result.get('advancement'):
            converted_html += paragraph(result['advancement'])
        converted_html += '</article>'
    if not converted_html:
        converted_html = '<p class="muted">未提出新需求。已有关联需求的事项继续跟踪；证据不足的事项补证后再判定。</p>'
    if conversion is not None:
        converted_html = '<p>实际转换：新增 ' + e(conversion.get('created', 0)) + '，复用 ' + e(conversion.get('reused', 0)) + '，未转化 ' + e(conversion.get('skipped', 0)) + '，失败 ' + e(conversion.get('failed', 0)) + '，进入开发队列 ' + e(conversion.get('queued', 0)) + '。</p>' + converted_html
        converted_html += ''.join('<p class="warning">' + e(result.get('title', '转换控制器')) + '：' + e(result.get('reason', '')) + '</p>' for result in results if result.get('action') == 'failed' and result.get('title') not in {c.get('title') for c in candidates})
    actions_html = ''
    for label, key in (('经验与改进', 'lessons'), ('下一步行动', 'next_actions')):
        values = analysis.get(key, [])
        if values:
            actions_html += f'<h3>{label}</h3><ul>' + ''.join(f'<li>{e(value)}</li>' for value in values) + '</ul>'
    accomplishments = '<ul>' + ''.join(f'<li>{e(value)}</li>' for value in analysis.get('accomplishments', [])) + '</ul>' if analysis.get('accomplishments') else '<p class="muted">未提供完成事实的独立复盘结论，可在完整分类统计中核对原始状态。</p>'
    date = period.get('date') or period.get('day') or ''
    snapshot = period.get('snapshot_at') or period.get('generated_at') or evidence.get('generated_at')
    snapshot_html = f'<p>当前状态快照：{e(snapshot)}</p>' if snapshot else '<p>当前状态快照时间未记录；当前判断需重新核对。</p>'
    warning = '' if complete else '<p class="warning">来源覆盖或历史证据不完整，不能据此认定没有问题。缺口见下方覆盖表。</p>'
    statistics = evidence.get('statistics', {})
    stats_html = '<details><summary>完整分类统计</summary><pre>' + e(json.dumps(statistics, ensure_ascii=False, indent=2)) + '</pre></details>'
    task_html = ''
    tasks = rows(evidence.get('task_items'))
    if tasks:
        entries = ''
        for task in tasks:
            receipt_lines = ''.join(f'<p>{e(r.get("action"))} · {e(status(r.get("status")))} · {e(r.get("at"))}<br>{e(r.get("reason"))}</p>' for r in rows(task.get('receipts')))
            progress = task.get('current_progress') or {}
            entries += f'<article class="issue"><h3>{e(task.get("title"))}</h3><p>当前：{e(status(task.get("status")))} · 窗口内有记录：{"是" if task.get("observed_in_period") else "未观察到"}</p>'
            if progress.get('reason') or task.get('reason'):
                entries += paragraph(progress.get('reason') or task.get('reason'))
            entries += f'<p class="muted">任务 <code>{e(task.get("id"))}</code> · 需求 <code>{e(task.get("requirement_id"))}</code> · {e(status(task.get("requirement_status")))}</p>'
            entries += ('<details><summary>窗口内执行与验证回执</summary>' + receipt_lines + '</details>') if receipt_lines else ''
            entries += '</article>'
        task_html = f'<section><h2>全部任务与当前进展</h2><p class="muted">共 {len(tasks)} 项项目任务，{sum(bool(t.get("observed_in_period")) for t in tasks)} 项在统计窗口有可靠记录；未观察到不等于没有执行。</p><details><summary>任务清单与回执</summary>{entries}</details></section>'
    window_label = '北京时间自然日' if period.get('window_kind') in (None, 'natural_day') else '北京时间调度窗口'
    window_html = f'<p>统计窗口：{e(period.get("start"))} 至 {e(period.get("end"))}（开始含，结束不含）</p>' if period.get('start') and period.get('end') else ''
    mail_html = ''
    notifications = rows(evidence.get('notification_items'))
    if notifications:
        mail_rows = ''.join('<tr>' + ''.join('<td>' + e(value) + '</td>' for value in (
            item.get('title'), status(item.get('current_status')), status(item.get('window_receipt_status')),
            item.get('receipt_at') or '未保存可靠时间', item.get('attempts_lifetime', 0),
            str(item.get('accepted_count', 0)) + '/' + str(item.get('requested_count', 0)),
            item.get('next_attempt_at') or '—', item.get('error') or '—')) + '</tr>' for item in notifications)
        mail_html = '<section><h2>邮件投递与运行卡点</h2><p class="muted">累计尝试是该通知的总尝试次数，不等于当日故障次数。SMTP 接受仅确认服务器接受，不证明收件箱收到；常规排队不计故障。</p><details><summary>完整投递清单</summary><div class="table-wrap"><table><thead><tr><th>通知</th><th>当前状态</th><th>窗口回执</th><th>回执时间</th><th>累计尝试</th><th>已接受/请求人数</th><th>下次重试</th><th>当前原因</th></tr></thead><tbody>' + mail_rows + '</tbody></table></div></details></section>'
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(project.get('name'))} · 复盘与总结 · {e(date)}</title>
<style>body{{margin:0;background:#eef2f6;color:#24364c;font:15px/1.8 -apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif}}main{{max-width:1000px;margin:28px auto;background:#fff;border:1px solid #dbe3ed;border-radius:12px;overflow:hidden}}header{{background:#172b4d;color:white;padding:30px 36px}}header p{{color:#c5d3e5;margin:6px 0}}h1{{font-size:28px;line-height:1.5;margin:8px 0}}h2{{font-size:21px;color:#172b4d;margin:0 0 16px}}h3{{font-size:16px;line-height:1.7;margin:10px 0;overflow-wrap:anywhere}}section{{padding:30px 36px;border-bottom:1px solid #e8edf3}}p{{margin:8px 0;white-space:pre-wrap;overflow-wrap:anywhere}}.metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:18px 0}}.metric{{background:#f1f5fa;border:1px solid #e0e7f0;padding:16px;border-radius:8px}}.metric b{{display:block;font-size:26px;color:#172b4d}}.muted{{font-size:13px;color:#64748b}}.warning,.reason{{background:#fff6e6;border-left:3px solid #d99b39;padding:13px 16px;border-radius:5px}}.action{{background:#edf4fb;padding:13px 16px;border-radius:5px;margin-top:10px}}.issue{{border:1px solid #dce4ed;border-radius:8px;padding:18px;margin:0 0 14px}}.row{{display:flex;justify-content:space-between;gap:12px}}.badge{{background:#eaf1fb;color:#355e96;border-radius:4px;font-size:12px;padding:3px 9px}}.requirement{{font-size:13px;background:#edf7f0;padding:8px 12px;border-radius:5px}}details{{font-size:12px;color:#64748b;margin-top:12px}}summary{{cursor:pointer;font-size:13px}}code,pre{{font-size:11px;white-space:pre-wrap;word-break:break-all}}table{{width:100%;border-collapse:collapse;font-size:12px}}td,th{{text-align:left;vertical-align:top;padding:10px;border-bottom:1px solid #e4eaf1;overflow-wrap:anywhere}}th{{background:#f4f7fa}}.table-wrap{{overflow:auto}}footer{{padding:20px 36px;color:#64748b;font-size:12px}}a{{color:#426aa5}}@media(max-width:700px){{main{{margin:8px;border-radius:7px}}header,section{{padding:22px 18px}}h1{{font-size:23px}}.metrics{{grid-template-columns:repeat(2,1fr)}}footer{{padding:18px}}}}</style></head>
<body><main><header><p>{e(project.get('name'))} · 研发复盘</p><h1>复盘与总结</h1><p>{e(date)} · {window_label}，历史窗口与当前状态分别记录</p>{window_html}{snapshot_html}</header>
<section><h2>结论与处理重点</h2>{paragraph(summary)}{warning}<div class="metrics"><div class="metric"><b>{source_total:,}</b>窗口内各来源记录</div><div class="metric"><b>{len(problems):,}</b>异常与返修记录组</div><div class="metric"><b>{occurrences:,}</b>各组发生记录合计</div><div class="metric"><b>{len(blockers):,}</b>当前等待与待处理组</div></div><p class="muted">同一事件可能在多种来源中留痕；各来源记录数不可当作独立业务缺陷数量。任务卡点、待调查需求与待归因信号分别展示，已交付与已上线分别表述。</p>{stats_html}</section>
<section><h2>数据覆盖与证据缺口</h2>{coverage_html}</section>
<section><h2>完成与推进</h2>{accomplishments}</section>
{task_html}
{mail_html}
{('<section><h2>重点问题的判断与处理建议</h2>' + assessed_html + '</section>') if assessed_html else ''}
<section><h2>统计窗口内的问题、返修与异常</h2>{grouped_cards(problems) or '<p>已收集来源暂无异常记录；请核对覆盖是否完整。</p>'}</section>
<section><h2>当前未解决事项与卡点</h2><p class="muted">反映收集时刻的实际状态；进入等待或不再出现告警不代表业务问题已修复。</p>{grouped_cards(blockers, True) or '<p>已收集的当前记录暂无明确卡点。</p>'}</section>
<section><h2>转化需求与处理回执</h2>{converted_html}</section><section><h2>经验与后续行动</h2>{actions_html or '<p>结合上述来源证据继续补齐原因、范围和验收条件。</p>'}</section>
<footer>依据本机台账和真实回执生成。<a href="evidence.json">完整证据 JSON</a> · <a href="analysis.json">复盘结论与候选需求</a></footer></main></body></html>'''


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, '本机管理接口不允许重定向', headers, fp)


class LocalApi:
    def __init__(self, origin):
        parsed = urlsplit(origin)
        if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost') or not parsed.port or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
            raise ValueError('origin 必须是带端口的本机 HTTP 地址')
        self.origin = origin.rstrip('/')
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()), NoRedirect())
        with self.opener.open(self.origin + '/api/bootstrap', timeout=15) as response:
            self.csrf = json.load(response)['csrf']

    def call(self, path, body=None):
        headers = {'Accept': 'application/json', 'Origin': self.origin}
        data = None
        if body is not None:
            headers.update({'Content-Type': 'application/json', 'X-CSRF-Token': self.csrf})
            data = json.dumps(body, ensure_ascii=False).encode()
        request = Request(self.origin + '/api' + path, data=data, headers=headers)
        with self.opener.open(request, timeout=30) as response:
            return json.load(response)


def apply_requirements(evidence, analysis, api, queue=False):
    project_id = evidence['project']['id']
    project = api.call('/products/' + project_id)
    if project['id'] != project_id:
        raise ValueError('目标项目不匹配')
    known = rows(evidence.get('requirements'))
    candidates = rows(analysis.get('requirement_candidates'))
    results = []
    valid_signals = {ident for item in rows(evidence.get('problem_items')) + rows(evidence.get('current_blockers')) for ident in item.get('signal_ids', [])}
    for candidate in candidates:
        title = str(candidate.get('title') or '').strip()
        record = {'title': title}
        existing_id = candidate.get('existing_requirement_id')
        matching = next((item for item in known if item.get('id') == existing_id), None) if existing_id else None
        if existing_id and matching is None:
            raise ValueError('指定的已有需求不属于当前证据项目：' + str(existing_id))
        matching = matching or next((item for item in known if str(item.get('title', '')).strip().casefold() == title.casefold()), None)
        if matching:
            current = api.call('/requirements/' + matching['id'])
            if current.get('product_id') != project_id:
                raise ValueError('已有需求属于其他项目')
            results.append(record | {'action': 'reused', 'requirement_id': current['id'], 'status': current['status']})
            continue
        acceptance = candidate.get('acceptance')
        if not title or not isinstance(acceptance, list) or not acceptance or any(not isinstance(value, str) or not value.strip() for value in acceptance):
            raise ValueError('候选需求必须有标题和可验收条件')
        if not all(isinstance(candidate.get(key), str) and candidate[key].strip() for key in ('evidence', 'impact', 'reproduction')):
            raise ValueError('候选需求缺少证据、影响或复现条件')
        signals = candidate.get('signal_ids') or []
        if not isinstance(signals, list) or not signals or any(ident not in valid_signals for ident in signals):
            raise ValueError('候选需求引用了当前项目证据以外的信号')
        if candidate.get('in_scope') is not True:
            results.append(record | {'action': 'skipped', 'reason': '候选范围尚未确认，保留在报告中'})
            continue
        # 明确的现有需求关联优先于新标题，避免同一问题换标题重复入池。
        linked = [item for item in known if set(item.get('signal_ids', [])) & set(signals)]
        if linked:
            current = api.call('/requirements/' + linked[0]['id'])
            results.append(record | {'action': 'reused', 'requirement_id': current['id'], 'status': current['status']})
            continue
        fingerprint = hashlib.sha256(json.dumps([project_id, title.casefold(), sorted(signals)], ensure_ascii=False).encode()).hexdigest()
        marker = '复盘需求标识：' + fingerprint
        prior = next((item for item in known if marker in str(item.get('evidence', ''))), None)
        if prior:
            results.append(record | {'action': 'reused', 'requirement_id': prior['id'], 'status': prior['status']})
            continue
        trace = '\n'.join([candidate['evidence'], '复现条件：' + candidate['reproduction'],
                           '来源信号：' + ', '.join(signals), marker])
        body = {'operation_id': str(uuid.uuid5(uuid.NAMESPACE_URL, fingerprint)), 'product_id': project_id,
                'title': title, 'evidence': trace, 'impact': candidate['impact'], 'acceptance': acceptance,
                'priority': candidate.get('priority', 2)}
        try:
            pending_signal = None
            if candidate.get('priority') == 0:
                for ident in signals:
                    signal = api.call('/signals/' + ident)
                    if signal.get('product_id') != project_id:
                        raise ValueError('候选来源信号属于其他项目')
                    if signal.get('requirement_id') or signal.get('requirement_ids'):
                        existing = signal.get('requirement_id') or signal['requirement_ids'][0]
                        requirement = api.call('/requirements/' + existing)
                        results.append(record | {'action': 'reused', 'requirement_id': requirement['id'], 'status': requirement['status']})
                        break
                    if signal.get('status') == 'pending':
                        pending_signal = signal
                        break
                else:
                    requirement = None
                if results and results[-1].get('title') == title:
                    continue
            if pending_signal:
                requirement = api.call('/signals/' + pending_signal['id'] + '/promote',
                                       {key: body[key] for key in ('operation_id', 'title', 'evidence', 'impact', 'acceptance')} |
                                       {'version': pending_signal['version']})
            else:
                requirement = api.call('/requirements', body)
            result = record | {'action': 'created', 'requirement_id': requirement['id'], 'status': requirement['status']}
            known.append(requirement)
            if queue and candidate.get('classification') == 'development':
                run = api.call('/requirements/' + requirement['id'] + '/queue',
                               {'operation_id': str(uuid.uuid5(uuid.NAMESPACE_URL, fingerprint + '/queue'))})
                result.update(status='queued', run_id=run['id'])
            results.append(result)
        except (HTTPError, TimeoutError, OSError) as exc:
            results.append(record | {'action': 'failed', 'outcome_unknown': True,
                                     'reason': '写操作结果需核对，不自动换 UUID 重试：' + clean(exc)})
            break
    return {'project_id': project_id, 'results': results, 'created': sum(row['action'] == 'created' for row in results),
            'reused': sum(row['action'] == 'reused' for row in results)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    collect_parser = commands.add_parser('collect', help='只读收集指定日期和项目的完整证据')
    for key in ('state-dir', 'product-id', 'date', 'output'):
        collect_parser.add_argument('--' + key, required=True)
    collect_parser.add_argument('--origin')
    collect_parser.add_argument('--start', help='自定义窗口开始，有时区 ISO 时间')
    collect_parser.add_argument('--end', help='自定义窗口结束，有时区 ISO 时间')
    render_parser = commands.add_parser('render', help='生成可分享的独立 HTML 报告')
    render_parser.add_argument('--evidence', required=True)
    render_parser.add_argument('--analysis')
    render_parser.add_argument('--conversion')
    render_parser.add_argument('--output', required=True)
    apply_parser = commands.add_parser('apply', help='明确写动作：去重加入需求池，默认不启动任务')
    for key in ('evidence', 'analysis', 'origin', 'output'):
        apply_parser.add_argument('--' + key, required=True)
    apply_parser.add_argument('--queue', action='store_true', help='在已有执行授权下让开发候选进入原有任务队列')
    args = parser.parse_args()
    if args.command == 'collect':
        from collect_evidence import collect
        value = collect(args.state_dir, args.product_id, args.date, origin=args.origin, start=args.start, end=args.end)
        save(args.output, value)
        print(json.dumps({'output': str(Path(args.output).resolve()), 'coverage': value.get('coverage'), 'summary': value.get('summary')}, ensure_ascii=False))
    elif args.command == 'render':
        evidence = load(args.evidence)
        analysis = load(args.analysis) if args.analysis else {}
        conversion = load(args.conversion) if args.conversion else None
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_report(evidence, analysis, conversion), encoding='utf-8')
        if Path(args.evidence).resolve() != (path.parent / 'evidence.json').resolve():
            shutil.copy2(args.evidence, path.parent / 'evidence.json')
        save(path.parent / 'analysis.json', analysis)
        print(str(path.resolve()))
    else:
        result = apply_requirements(load(args.evidence), load(args.analysis), LocalApi(args.origin), queue=args.queue)
        save(args.output, result)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
