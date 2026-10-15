"""复盘控制器：完整只读证据、确定性 HTML 与可重放的需求转化。"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid
from zoneinfo import ZoneInfo

from review_core import Conflict
from .store import redact


ZONE = ZoneInfo('Asia/Shanghai')
SKILL = Path(__file__).resolve().parents[1] / 'skills' / 'dsh-role-retrospective'
_MODULES = {}
CANDIDATE_FIELDS = ('title', 'signal_ids', 'existing_requirement_id', 'evidence', 'impact',
                    'reproduction', 'acceptance', 'classification', 'in_scope', 'priority', 'resolution_probes')


def _skill_module(name):
    if name not in _MODULES:
        path = SKILL / 'scripts' / (name + '.py')
        spec = importlib.util.spec_from_file_location('dsh_retrospective_' + name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _MODULES[name] = module
    return _MODULES[name]


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n'


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(content, encoding='utf-8')
    temporary.chmod(0o600)
    temporary.replace(path)


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _directory(scheduler, report):
    ident = str(report['id'])
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,180}', ident) or ident in ('.', '..'):
        raise ValueError('复盘报告 ID 无效')
    return scheduler.root / 'retrospectives' / ident


class _ReadLedger:
    """复用现有只读进度投影，查询完整项目集合而非旧列表的 1000 条。"""
    def __init__(self, ledger):
        self.ledger, self.store = ledger, ledger.store

    def list(self, kind):
        table = self.ledger._table(kind)
        with self.store.connect() as db:
            return [self.ledger.decode(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY created,id')]

    def get(self, *args, **kwargs):
        return self.ledger.get(*args, **kwargs)

    def scoped(self, *args, **kwargs):
        return self.ledger.scoped(*args, **kwargs)

    def budget_used(self, *args, **kwargs):
        return self.ledger.budget_used(*args, **kwargs)


def _register_sources(scheduler, product, report, evidence):
    """为无信号的真实异常保存稳定来源，模型只能引用控制器登记的 ID。"""
    ledger = scheduler.ledger
    normal_waits = {'quota', 'off_peak', 'resource', 'resources', 'project_paused',
                    'platform_update', 'dispatch', 'call', 'control', 'dependency'}
    with scheduler.store.transaction() as db:
        signals = {row['id']: ledger.decode(row) for row in db.execute(
            'SELECT * FROM auto_signals WHERE product_id=?', (product['id'],))}
        requirements = {row['id']: ledger.decode(row) for row in db.execute(
            'SELECT * FROM auto_requirements WHERE product_id=?', (product['id'],))}
        registered = []
        for item in evidence.get('problem_items', []) + evidence.get('current_blockers', []):
            if item.get('source_kind') in ('signals', 'requirements') or item.get('code') in normal_waits:
                continue
            if (item.get('current_progress') or {}).get('kind') in normal_waits:
                continue
            ids = [ident for ident in item.get('signal_ids', []) if ident in signals]
            for rid in item.get('requirement_ids', []):
                known = requirements.get(rid, {})
                ids.extend(ident for ident in known.get('signal_ids', []) if ident in signals)
            if ids:
                item['signal_ids'] = list(dict.fromkeys(ids))
                continue
            if not item.get('id') or not item.get('source_ids') or not item.get('reason'):
                continue
            fingerprint = hashlib.sha256(_json([product['id'], item['id']]).encode()).hexdigest()
            ident = 'retrospective-source-' + str(uuid.uuid5(uuid.NAMESPACE_URL, fingerprint))
            signal = signals.get(ident)
            if signal and signal.get('retrospective_problem_id') != item['id']:
                raise ValueError('复盘来源身份冲突')
            if not signal:
                signal = ledger.create('signals', {
                    'product_id': product['id'], 'source': 'retrospective',
                    'code': 'RETROSPECTIVE_SOURCE_FACT', 'summary': item.get('title') or item['reason'],
                    'retrospective_problem_id': item['id'], 'daily_report_id': report['id'],
                    'evidence': {'status': 'observed', 'source_kind': item.get('source_kind'),
                        'source_ids': item['source_ids'], 'evidence_ids': item.get('evidence_ids', []),
                        'reason': item['reason'], 'failure_kind': item.get('failure_kind'),
                        'window_status': item.get('window_status'), 'current_status': item.get('current_status'),
                        'known_requirement_ids': item.get('requirement_ids', []),
                        'period': evidence.get('period', {})}}, 'reviewed', ident=ident, db=db)
                signals[ident] = signal
            item['signal_ids'] = [ident]
            registered.append({'signal_id': ident, 'problem_id': item['id'],
                               'source_kind': item.get('source_kind'), 'source_ids': item['source_ids']})
    evidence['registered_sources'] = registered


def prepare(scheduler, product, report, *, register_sources=False):
    """保存不可变证据快照；明确区分晚间调度窗口与昨日自然日。"""
    if report.get('product_id') != product['id']:
        raise ValueError('复盘报告与项目不匹配')
    directory = _directory(scheduler, report)
    evidence_path = directory / 'evidence.json'
    if evidence_path.exists():
        expected = (report.get('retrospective_evidence') or {}).get('evidence_hash')
        if expected and _hash(evidence_path) != expected:
            raise ValueError('已冻结复盘证据哈希不匹配，拒绝使用变更后的模型输入')
        evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
        if evidence.get('project', {}).get('id') != product['id']:
            raise ValueError('复盘证据项目不匹配')
        for report_key, period_key in (('window_start', 'start_epoch'), ('cutoff', 'end_epoch')):
            if report.get(report_key) is not None and evidence.get('period', {}).get(period_key) != report[report_key]:
                raise ValueError('已冻结复盘证据与报告窗口不匹配')
    else:
        current = None
        projection_error = None
        try:
            from .run_progress import RunProgress
            projection = RunProgress(_ReadLedger(scheduler.ledger))
            current = {run['id']: projection.current(run) for run in projection.runs if run['product_id'] == product['id']}
        except (OSError, ValueError, KeyError, AttributeError) as exc:
            projection_error = type(exc).__name__ + ': ' + str(exc)
        try:
            evidence = _skill_module('collect_evidence').collect(
                scheduler.store.state, product['id'], report['day'],
                start=report.get('window_start'), end=report.get('cutoff'), current_progress=current)
        except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
            now = dt.datetime.now(ZONE).isoformat()
            evidence = {'schema_version': '1.0', 'generated_at': now,
                'project': {'id': product['id'], 'name': product.get('name', product['id'])},
                'period': {'date': report['day'], 'start_epoch': report.get('window_start'),
                           'end_epoch': report.get('cutoff'), 'snapshot_at': now},
                'coverage': {'complete': False, 'sources': {}, 'gaps': [{
                    'source': 'all', 'code': 'collection_failed', 'count': 1,
                    'detail': '完整证据采集失败：' + type(exc).__name__ + ': ' + str(exc)}]},
                'summary': {'statement': '完整证据采集失败，不能判断是否没有问题。', 'coverage_complete': False},
                'statistics': {}, 'problem_items': [], 'current_blockers': [], 'task_items': [], 'requirements': []}
        if projection_error:
            evidence['coverage']['complete'] = False
            evidence['coverage']['gaps'].append({'source': 'current_progress', 'code': 'projection_failed',
                'detail': '本机只读进度投影失败：' + projection_error, 'count': 1})
        if register_sources:
            _register_sources(scheduler, product, report, evidence)
        _write(evidence_path, _json(redact(evidence, text_limit=None)))
    metadata = {'evidence_path': str(evidence_path), 'evidence_hash': _hash(evidence_path),
                'period': evidence['period'], 'summary': evidence.get('summary', {}),
                'coverage': evidence.get('coverage', {})}
    material = _model_index(evidence)
    return metadata | {'material': material}


def _model_index(evidence):
    """索引覆盖全部对象；重复回执与长原因保留在原始文件按需读取。"""
    material = {key: evidence.get(key) for key in ('project', 'period', 'summary', 'statistics', 'coverage')}
    fields = ('id', 'title', 'source_kind', 'code', 'component', 'occurrences', 'failure_kind',
              'window_status', 'current_status', 'status', 'classification', 'requirement_id',
              'requirement_status', 'observed_in_period', 'signal_ids', 'requirement_ids',
              'current_progress', 'acceptance', 'resolution_probes', 'in_scope')
    for kind in ('problem_items', 'current_blockers', 'task_items', 'notification_items', 'requirements'):
        items = []
        for position, row in enumerate(evidence.get(kind, [])):
            item = {key: row[key] for key in fields if key in row}
            for key in ('title', 'reason', 'evidence', 'impact', 'reproduction'):
                if row.get(key):
                    text = str(row[key])
                    limit = 240 if key == 'title' else 1200
                    item[key] = text[:limit]
                    if len(text) > limit:
                        item[key + '_excerpt'] = True
            for key in ('source_ids', 'evidence_ids', 'signal_ids', 'requirement_ids'):
                if key in row:
                    item[key] = row[key][:6]
                    item[key + '_count'] = len(row[key])
            item['detail_file'] = f'{kind}/{position:06d}.json'
            items.append(item)
        material[kind] = items
    return material


def model_report(report, execution_root):
    """给本次模型复制脱敏证据包；沙箱仅需读已有执行目录，无需读台账。"""
    comprehensive = report.get('comprehensive_evidence') or {}
    path = Path(comprehensive.get('evidence_path', '')).resolve()
    if not path.is_file() or _hash(path) != comprehensive.get('evidence_hash'):
        raise ValueError('模型复盘证据文件或哈希不匹配')
    evidence = json.loads(path.read_text())
    if evidence.get('project', {}).get('id') != report.get('product_id'):
        raise ValueError('模型证据与报告项目不匹配')
    bundle = Path(execution_root) / 'retrospective-evidence'
    bundle.mkdir(mode=0o700)
    _write(bundle / 'evidence.json', _json(evidence))
    index = _model_index(evidence)
    pages = []
    for kind in ('problem_items', 'current_blockers', 'task_items', 'notification_items', 'requirements'):
        entries = index[kind]
        for position, row in enumerate(evidence.get(kind, [])):
            _write(bundle / kind / f'{position:06d}.json', _json(row))
        # 全索引分页保持每份可直接读取；不截断对象数量。
        page, size = [], 0
        for row in entries:
            length = len(_json(row))
            if page and size + length > 60_000:
                name = f'index/{kind}-{len(pages):05d}.json'
                _write(bundle / name, _json(page)); pages.append({'kind': kind, 'path': name, 'count': len(page)})
                page, size = [], 0
            page.append(row); size += length
        if page:
            name = f'index/{kind}-{len(pages):05d}.json'
            _write(bundle / name, _json(page)); pages.append({'kind': kind, 'path': name, 'count': len(page)})
    manifest = {'evidence_hash': comprehensive['evidence_hash'], 'pages': pages,
                'counts': {kind: len(evidence.get(kind, [])) for kind in ('problem_items', 'current_blockers', 'task_items', 'notification_items', 'requirements')}}
    _write(bundle / 'manifest.json', _json(manifest))
    if len(_json(index)) > 450_000:
        for kind in manifest['counts']:
            index[kind] = {'count': manifest['counts'][kind], 'index_pages': [row['path'] for row in pages if row['kind'] == kind]}
    index.update(bundle_path=str(bundle), manifest_path=str(bundle / 'manifest.json'),
                 evidence_hash=comprehensive['evidence_hash'],
                 reading_rule='逐一核对全部问题与任务索引；索引标明 excerpt 或涉及需求转换时，读取 detail_file 的完整证据。分页索引必须全部读取；没有读取的来源不能声称已完成智能归因。证据包只含本项目脱敏资料，不读取原台账或凭据。')
    keys = ('id', 'product_id', 'title', 'day', 'report_type', 'window_start', 'cutoff', 'timezone', 'stats')
    result = {key: report[key] for key in keys if key in report}
    result['comprehensive_evidence'] = index
    result['audits'] = [{key: row.get(key) for key in ('run_id', 'title', 'status', 'reason', 'summary')} for row in report.get('audits', [])]
    return result


def _candidate_error(candidate, valid_signals, known):
    if not isinstance(candidate, dict):
        return '候选需求不是结构化对象'
    ids = candidate.get('signal_ids')
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i for i in ids):
        return '候选缺少真实信号 ID'
    if any(i not in valid_signals or i not in known for i in ids):
        return '候选引用了快照外或其他项目的信号'
    for key in ('title', 'evidence', 'impact', 'reproduction'):
        if not isinstance(candidate.get(key), str) or not candidate[key].strip():
            return '候选缺少 ' + key
    acceptance = candidate.get('acceptance')
    if not isinstance(acceptance, list) or not acceptance or not all(isinstance(x, str) and x.strip() for x in acceptance):
        return '候选缺少可验收条件'
    if candidate.get('classification') not in ('development', 'investigation', 'environment'):
        return '候选分类无效'
    if candidate.get('in_scope') is not True:
        return '候选不在已授权项目范围内'
    if type(candidate.get('priority', 2)) is not int or candidate.get('priority', 2) < 0:
        return '候选优先级无效'
    return None


def convert(scheduler, product, report, evidence, analysis, *, queue=True):
    """控制器原子去重并沿原确认、探针和调度规则推进，不执行模型写库命令。"""
    if evidence.get('project', {}).get('id') != product['id'] or report['product_id'] != product['id']:
        raise ValueError('转化证据、报告与项目不匹配')
    candidates = analysis.get('requirement_candidates', [])
    if not isinstance(candidates, list):
        raise ValueError('requirement_candidates 必须为列表')
    result = {'created': 0, 'reused': 0, 'skipped': 0, 'failed': 0, 'queued': 0, 'results': []}
    valid_signals = {ident for item in evidence.get('problem_items', []) + evidence.get('current_blockers', [])
                     for ident in item.get('signal_ids', [])}
    ledger = scheduler.ledger
    queue_ids = []
    with scheduler.store.transaction() as db:
        known = {r['id']: ledger.decode(r) for r in db.execute('SELECT * FROM auto_signals WHERE product_id=?', (product['id'],))}
        requirements = {r['id']: ledger.decode(r) for r in db.execute('SELECT * FROM auto_requirements WHERE product_id=?', (product['id'],))}
        initial_requirement_ids = set(requirements)
        titles = {r['title'].strip().casefold(): r['id'] for r in requirements.values()}
        for raw in candidates:
            row = {'title': str(raw.get('title', '')) if isinstance(raw, dict) else '', 'action': 'skipped'}
            reason = _candidate_error(raw, valid_signals, known)
            if analysis.get('status') != 'pass':
                reason = '复盘归因未通过；保留候选补证，不创建或推进需求'
            target = raw.get('existing_requirement_id') if isinstance(raw, dict) else None
            if target and target not in requirements:
                reason = '显式已有需求不存在或不属于本项目'
            if reason:
                row['reason'] = reason
                result['skipped'] += 1
                result['results'].append(row)
                continue
            candidate = redact({key: raw[key] for key in CANDIDATE_FIELDS if key in raw})
            ids = sorted(set(candidate['signal_ids']))
            title = candidate['title'].strip().casefold()
            fingerprint = hashlib.sha256(_json([product['id'], ids, title]).encode()).hexdigest()
            stable_id = 'retrospective-' + str(uuid.uuid5(uuid.NAMESPACE_URL, fingerprint))
            linked = sorted({ident for sid in ids for ident in known[sid].get('requirement_ids', [])
                            + ([known[sid]['requirement_id']] if known[sid].get('requirement_id') else [])
                            + ([known[sid]['manual_requirement_id']] if known[sid].get('manual_requirement_id') else [])
                            if ident in initial_requirement_ids})
            if not linked:
                linked = sorted(r['id'] for r in requirements.values() if r['id'] in initial_requirement_ids and set(r.get('signal_ids', [])) & set(ids))
            existing_id = target or titles.get(title) or (stable_id if stable_id in requirements else None)
            if not existing_id and linked:
                row.update(reason='关联信号已有需求，但候选标题不同且缺少明确已有需求 ID；补齐同因关联后再转化',
                           suggested_requirement_ids=linked, signal_ids=ids)
                result['skipped'] += 1
                result['results'].append(row)
                continue
            if existing_id:
                item = requirements[existing_id]
                added = [sid for sid in ids if sid not in item.get('signal_ids', [])]
                evidence_key = hashlib.sha256(_json([report['id'], ids, candidate['evidence']]).encode()).hexdigest()
                traces = item.get('additional_evidence', [])
                if not any(t.get('retrospective_fingerprint') == evidence_key for t in traces if isinstance(t, dict)):
                    item = ledger.update('requirements', item['id'], item['version'], {
                        'signal_ids': list(dict.fromkeys(item.get('signal_ids', []) + ids)),
                        'additional_evidence': traces + [{'signal_ids': ids, 'evidence': candidate['evidence'],
                            'report_id': report['id'], 'at': time.time(), 'retrospective_fingerprint': evidence_key}]}, db=db)
                row['action'] = 'reused'
                result['reused'] += 1
            else:
                if candidate['classification'] == 'development':
                    from .probes import validate
                    from .project import generic
                    try:
                        validate(candidate.get('resolution_probes'), product if generic(product) else None, assertions=generic(product))
                        if not candidate.get('resolution_probes'):
                            raise ValueError('缺少原问题效果验证条件')
                    except ValueError as exc:
                        candidate.update(classification='investigation', reason='需补充验收证据后转开发：' + str(exc))
                guarded = any(known[sid].get('confirmation_required') or known[sid].get('source') in ('chat', 'competitor') for sid in ids)
                if guarded:
                    from .intake import draft
                    candidate.setdefault('goal', candidate['title'])
                    candidate.setdefault('scope', candidate['impact'])
                    item = draft(ledger, product['id'], candidate, 'retrospective', ','.join(ids), db)
                    item = ledger.update('requirements', item['id'], item['version'], {'signal_ids': ids,
                        'daily_report_id': report['id'], 'requirement_day': report['day'],
                        'retrospective_fingerprint': fingerprint}, db=db)
                else:
                    item = ledger.create('requirements', candidate | {'product_id': product['id'],
                        'source': 'retrospective', 'signal_ids': ids, 'requirement_day': report['day'],
                        'daily_report_id': report['id'], 'retrospective_fingerprint': fingerprint},
                        'pending', ident=stable_id, db=db)
                row['action'] = 'created'
                result['created'] += 1
            requirements[item['id']] = item
            titles[title] = item['id']
            for sid in ids:
                signal = ledger.get('signals', sid, db)
                links = list(dict.fromkeys(signal.get('requirement_ids', []) + ([signal['requirement_id']] if signal.get('requirement_id') else []) + [item['id']]))
                if signal.get('requirement_ids') != links:
                    known[sid] = ledger.update('signals', sid, signal['version'], {'requirement_id': links[0], 'requirement_ids': links}, db=db)
            row.update(requirement_id=item['id'], status=item['status'], classification=item.get('classification'),
                       signal_ids=ids, fingerprint=fingerprint)
            if item.get('reason'):
                row['reason'] = item['reason']
            if queue and item['status'] == 'pending' and item.get('classification') == 'development' and item.get('in_scope') is True:
                queue_ids.append(item['id'])
            elif item.get('classification') in ('investigation', 'environment') and item['status'] == 'pending':
                row['advancement'] = '按现有调查调度、额度和低峰规则自动推进'
            result['results'].append(row)
    for ident in dict.fromkeys(queue_ids):
        try:
            item = ledger.get('requirements', ident)
            scheduler.control.queue(item)
            latest = ledger.get('requirements', ident)
            result['queued'] += 1
            for row in result['results']:
                if row.get('requirement_id') == ident:
                    row.update(status=latest['status'], run_id=latest.get('run_id'), advancement='已进入原开发任务队列')
        except (Conflict, ValueError, KeyError, OSError) as exc:
            for row in result['results']:
                if row.get('requirement_id') == ident:
                    row['queue_error'] = str(exc)
                    row['advancement'] = '需求已入池，原调度门槛暂不允许排队：' + str(exc)
    return result


def finish(scheduler, product, report, result=None, *, auto_apply=True, queue=True):
    """分析失败也输出完整事实报告；只有通过的候选可由控制器入池。"""
    prepared = prepare(scheduler, product, report)
    evidence_path = Path(prepared['evidence_path'])
    evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
    analysis = redact(result or {}, text_limit=None)
    if not isinstance(analysis, dict):
        analysis = {}
    if not analysis.get('summary'):
        analysis['summary'] = evidence.get('summary', {}).get('statement') or '证据覆盖未核实，不能判断是否没有问题。'
    elif evidence.get('problem_items') or evidence.get('current_blockers'):
        facts = evidence.get('summary', {}).get('statement', '')
        if facts and facts not in analysis['summary']:
            analysis['summary'] = facts + '\n' + analysis['summary']
    if analysis.get('status') != 'pass':
        analysis.setdefault('status', 'blocked')
        analysis.setdefault('reason', '智能归因尚未完成，以下保留完整台账事实。')
        analysis['summary'] += '\n分析状态：' + analysis['status'] + '；' + analysis['reason']
    conversion = {'created': 0, 'reused': 0, 'skipped': 0, 'failed': 0, 'queued': 0, 'results': []}
    if auto_apply:
        try:
            conversion = convert(scheduler, product, report, evidence, analysis, queue=queue)
        except (OSError, ValueError, KeyError, Conflict) as exc:
            conversion.update(failed=1, results=[{'action': 'failed', 'title': '需求转换控制器', 'reason': str(exc)}])
            analysis.setdefault('next_actions', []).append('需求转换失败，保留同一输入重试：' + str(exc))
    elif analysis.get('requirement_candidates'):
        conversion['skipped'] = len(analysis['requirement_candidates'])
        conversion['results'] = [{'title': c.get('title', ''), 'action': 'skipped', 'reason': '仅生成事实报告，需求归因待执行'} for c in analysis['requirement_candidates'] if isinstance(c, dict)]
    directory = evidence_path.parent
    analysis_path, conversion_path, report_path = [directory / name for name in ('analysis.json', 'conversion-receipt.json', 'report.html')]
    _write(analysis_path, _json(analysis))
    _write(conversion_path, _json(conversion))
    _write(directory / 'conversion.json', _json(conversion))
    _write(report_path, _skill_module('retrospective').render_report(evidence, analysis, conversion))
    html = {'path': str(report_path), 'sha256': _hash(report_path), 'size': report_path.stat().st_size,
            'evidence_path': str(evidence_path), 'analysis_path': str(analysis_path), 'conversion_path': str(conversion_path)}
    return {'html_report': html, 'retrospective_evidence': {k: v for k, v in prepared.items() if k != 'material'},
            'conversion': conversion, 'retrospective': analysis, 'path': html['path'], 'sha256': html['sha256']}


snapshot = prepare
finalize = finish
