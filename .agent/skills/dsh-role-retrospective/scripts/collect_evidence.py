#!/usr/bin/env python3
"""以只读 SQLite 快照采集项目自然日复盘证据，不初始化或修改工作台。

历史发生时间与当前状态分别输出。计数表示证据记录或已确认发生次数，
不代表独立业务缺陷数；重复信号的累计次数跨日时只报告可确认的下界。
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import datetime as dt
import hashlib
import http.cookiejar
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo


ZONE = ZoneInfo("Asia/Shanghai")
KINDS = ("products", "runs", "inspections", "inspection_rounds", "inspection_plans", "signals",
         "deliveries", "code_reviews", "daily_reports", "requirements", "evidence",
         "coordinators", "research_jobs", "releases", "notifications", "problems", "scans",
         "test_chains", "test_chain_versions", "test_chain_runs", "test_chain_baselines",
         "requirement_groups", "design_versions", "evaluations")
BAD = {"fail", "failed", "blocked", "error", "abnormal", "revise", "rejected"}
WAIT = {"blocked", "failed", "waiting_for_reply", "needs_human", "pending_confirmation",
        "awaiting_human", "paused", "pausing", "cancelling"}
TERMINAL = {"completed", "accepted", "delivered", "online", "cancelled", "rolled_back", "pass"}
SECRET_KEY = re.compile(r"(?i)password|cookie|authorization|api.?key|(?:^|_)token(?:$|_)")
SECRET_TEXT = re.compile(
    r"(?i)(bearer\s+)[\w.\-/+=]+|((?:token|password|cookie|authorization|api[_-]?key)\s*[=:]\s*)[^\s,;]+")


def _safe(value):
    """递归脱敏；保留完整证据 ID 和内容，不截断关联集合。"""
    if isinstance(value, dict):
        return {str(k): "[redacted]" if SECRET_KEY.search(str(k)) else _safe(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, str):
        return SECRET_TEXT.sub(lambda m: (m[1] or m[2]) + "[redacted]", value)
    return value


def _object(value):
    """解析可选 JSON 对象，损坏的内容由调用方标记覆盖缺口。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            result = json.loads(value)
            return result if isinstance(result, dict) else {}
        except (ValueError, TypeError):
            pass
    return {}


def _time(value):
    """解析 epoch 或有时区 ISO 时间；无时区时间不能证明自然日归属。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if value > 0 else None
    if isinstance(value, str) and value:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.timestamp() if parsed.tzinfo is not None else None
        except ValueError:
            return None
    return None


def _iso(value):
    """将可靠时间转为北京时间；未知时间保留 null。"""
    value = _time(value)
    return dt.datetime.fromtimestamp(value, ZONE).isoformat() if value else None


def _ids(value):
    """规范化一个 ID 或 ID 数组。"""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return list(dict.fromkeys(x for x in value if isinstance(x, str) and x))
    return []


def _references(value, singular, plural):
    """提取嵌套结构中的显式关联 ID，不把证据文件路径当台账 ID。"""
    found = []
    if isinstance(value, dict):
        found.extend(_ids(value.get(singular)))
        found.extend(_ids(value.get(plural)))
        for key, child in value.items():
            if key not in (singular, plural) and isinstance(child, (dict, list)):
                found.extend(_references(child, singular, plural))
    elif isinstance(value, list):
        for child in value:
            found.extend(_references(child, singular, plural))
    return list(dict.fromkeys(found))


def _reason(value):
    """优先采用明确失败原因，保留原始措辞用于审计归并。"""
    value = _object(value)
    for key in ("reason", "summary", "judgement", "actual", "error", "instruction"):
        text = value.get(key)
        if isinstance(text, str) and text.strip():
            return " ".join(_safe(text).split())
    issues = value.get("issues")
    if isinstance(issues, list):
        return "；".join(_safe(x) for x in issues if isinstance(x, str))
    return "原记录未提供具体原因"


def _failure_kind(value, status):
    """尊重显式归因；阻塞或待补证不会默认归类为业务缺陷。"""
    value = _object(value)
    explicit = value.get("failure_kind") or _object(value.get("evidence")).get("failure_kind")
    if explicit:
        return str(explicit)
    if status == "revise":
        return "review_revision"
    if status in ("blocked", "abnormal"):
        return "execution_blocked"
    return "unclassified_failure"


def _cause(reason):
    """只规范已明确的运行噪声；共享实例准备失败按实际失败测试归并。"""
    stable = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<record-id>", reason, flags=re.I)
    stable = re.sub(r"Ran (\d+) tests in [\d.]+s", r"Ran \1 tests in <elapsed>s", stable)
    stable = re.sub(r"\([\d.]+m\)", "(<elapsed>m)", stable)
    if reason.startswith("共享隔离实例准备失败"):
        tests = sorted(set(re.findall(r"(?:FAIL|ERROR): (test_[A-Za-z0-9_]+)", reason)))
        browser = sorted(set(re.findall(r"tests/browser/[\w./-]+\.spec\.ts:\d+:\d+", reason)))
        if tests or browser:
            signature = tests + browser
            return "共享隔离实例准备失败：测试未通过：" + "、".join(signature), "inspection_environment"
    return stable, None


def _chunks(values, size=100):
    """按 SQLite 绑定参数上限拆分集合，不对记录总量限流。"""
    values = list(values)
    for index in range(0, len(values), size):
        yield values[index:index + size]


class _NoRedirect(HTTPRedirectHandler):
    """本机证据 API 不允许重定向到其他站点。"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _current_progress(origin, product_id):
    """仅 GET 本机 bootstrap/runs；会话 Cookie 只存于内存、不写输出。"""
    parsed = urlsplit(origin)
    if (parsed.scheme not in ("http", "https") or parsed.hostname not in ("localhost", "127.0.0.1", "::1")
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/")):
        raise ValueError("origin 必须为无凭据、无路径的本机 HTTP(S) origin")
    opener = build_opener(_NoRedirect(), HTTPCookieProcessor(http.cookiejar.CookieJar()))
    base = origin.rstrip("/")
    with opener.open(Request(base + "/api/bootstrap", method="GET"), timeout=10) as response:
        response.read()  # 不解析或回传 CSRF/Cookie。
    with opener.open(Request(base + "/api/runs", method="GET"), timeout=15) as response:
        payload = json.load(response)
    rows = payload if isinstance(payload, list) else payload.get("items", payload.get("runs", []))
    if not isinstance(rows, list):
        raise ValueError("runs API 返回结构无效")
    return {row["id"]: _safe(row.get("current_progress")) for row in rows
            if isinstance(row, dict) and row.get("product_id") == product_id and row.get("id")}


def collect(state_dir, product_id, date, origin=None, *, start=None, end=None, current_progress=None):
    """采集指定项目和北京时间自然日的只读、可溯源复盘证据。

    state_dir 可为包含 dashboard.sqlite3 的目录或数据库文件。
    origin 仅用于补充 GET /runs 的当前进度，数据库仍以完整项目查询为准。
    """
    day = dt.date.fromisoformat(str(date))
    snapshot_at = dt.datetime.now(ZONE).isoformat()
    if (start is None) != (end is None):
        raise ValueError("自定义窗口必须同时提供 start/end")
    custom_window = start is not None
    if custom_window:
        lower, upper = _time(start), _time(end)
        if lower is None or upper is None or upper <= lower:
            raise ValueError("统计窗口必须使用有时区时间或 epoch，且 end 大于 start")
        start = dt.datetime.fromtimestamp(lower, ZONE)
        end = dt.datetime.fromtimestamp(upper, ZONE)
    else:
        start = dt.datetime.combine(day, dt.time(), ZONE)
        end = start + dt.timedelta(days=1)
    lower, upper = start.timestamp(), end.timestamp()
    path = Path(state_dir).expanduser().resolve()
    database = path if path.is_file() else path / "dashboard.sqlite3"
    if not database.is_file():
        raise FileNotFoundError(f"只读台账不存在：{database}")
    coverage = {"complete": True, "gaps": [], "sources": {},
                "time_policy": "历史发生仅依据事件 at、回执 at、显式完成时间及 created/last_seen；updated 仅为当前记录更新。",
                "count_policy": "记录、回执、信号次数分别计数；阻塞不等于业务缺陷，跨日累计信号次数仅确认下界。"}

    def gap(source, code, detail, count=1):
        for item in coverage["gaps"]:
            if item["source"] == source and item["code"] == code and item["detail"] == detail:
                item["count"] += count
                return
        coverage["gaps"].append({"source": source, "code": code, "detail": detail, "count": count})
        coverage["complete"] = False

    def in_window(value):
        moment = _time(value)
        return moment is not None and lower <= moment < upper

    rows = {}
    events = []
    reviews = []
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for kind in KINDS:
            table = "auto_" + kind
            available = table in tables
            coverage["sources"][kind] = {"available": available, "records": 0, "window_records": 0}
            rows[kind] = []
            if not available:
                gap(kind, "missing_table", f"缺少 {table}，此来源无法核实")
                continue
            columns = {r[1] for r in connection.execute(f"PRAGMA table_info({table})")}
            if "product_id" not in columns or "id" not in columns:
                gap(kind, "missing_scope", "缺少项目或记录 ID，未读取无法隔离项目的数据")
                continue
            query = f"SELECT * FROM {table} WHERE product_id=?"
            if kind == "products":
                query = f"SELECT * FROM {table} WHERE id=?"
            for raw in connection.execute(query, (product_id,)):
                raw = dict(raw)
                try:
                    data = json.loads(raw.get("data") or "{}")
                    if not isinstance(data, dict):
                        raise ValueError("非对象")
                except (ValueError, TypeError):
                    data = {}
                    gap(kind, "invalid_json", "记录 JSON 损坏，保留行元数据但无法判断内容")
                # 列元数据为权威状态；JSON 不能改变项目隔离或行 ID。
                row = data | {k: v for k, v in raw.items() if k != "data"}
                if kind == "notifications":
                    receipt = _object(row.get("receipt"))
                    refused = receipt.get("refused") if isinstance(receipt.get("refused"), dict) else {}
                    # 投递证据只保留状态、计数与脱敏原因，不读取正文、附件或账户凭据。
                    row = {k: row[k] for k in ("id", "product_id", "status", "version", "created", "updated",
                        "kind", "subject", "error", "attempts", "next_attempt_at", "run_id", "report_id") if k in row}
                    row["title"] = row.get("subject") or "邮件通知"
                    row["receipt"] = {"at": receipt.get("at"), "requested_count": len(receipt.get("requested") or []),
                        "accepted_count": len(receipt.get("to") or []), "refused_count": len(refused),
                        "refusal_reasons": [_reason(_object(value)) for value in refused.values()]}
                if not _time(row.get("created")):
                    gap(kind, "missing_created_time", "记录缺少可靠 created，无法确认首次出现的自然日")
                rows[kind].append(row)
            rows[kind].sort(key=lambda r: (_time(r.get("created")) or 0, str(r["id"])))
            coverage["sources"][kind]["records"] = len(rows[kind])
            coverage["sources"][kind]["window_records"] = sum(in_window(r.get("created")) for r in rows[kind])

        all_ids = {r["id"] for kind, values in rows.items() for r in values if kind != "products"}
        run_ids = {r["id"] for r in rows["runs"]}
        review_ids = set()
        for run in rows["runs"]:
            for key in ("review_id", "acceptance_review_id", "repair_plan_review_id"):
                review_ids.update(_ids(run.get(key)))
            review_ids.update(_references(run.get("last_review_result"), "review_id", "review_ids"))
        for evidence in rows["evidence"]:
            review_ids.update(_ids(evidence.get("review_id")))

        json_sql = bool(connection.execute("SELECT json_valid('{}')").fetchone()[0])
        if "reviews" in tables:
            selected = {}
            for column, identifiers in (("session_id", run_ids), ("id", review_ids)):
                for group in _chunks(identifiers):
                    marks = ",".join("?" for _ in group)
                    for raw in connection.execute(f"SELECT * FROM reviews WHERE {column} IN ({marks})", group):
                        selected[raw["id"]] = dict(raw)
            if json_sql:
                for raw in connection.execute("SELECT * FROM reviews WHERE json_valid(packet) AND json_extract(packet,'$.product_id')=?", (product_id,)):
                    selected[raw["id"]] = dict(raw)
                for group in _chunks(run_ids):
                    marks = ",".join("?" for _ in group)
                    for raw in connection.execute(f"SELECT * FROM reviews WHERE json_valid(packet) AND json_extract(packet,'$.run_id') IN ({marks})", group):
                        selected[raw["id"]] = dict(raw)
            for raw in selected.values():
                raw["packet"] = _object(raw.get("packet"))
                raw["result"] = _object(raw.get("result"))
                explicit = raw["packet"].get("product_id")
                if explicit and explicit != product_id:
                    gap("reviews", "conflicting_project_link", "审查关联存在冲突，已排除")
                    continue
                reviews.append(raw)
            review_ids.update(r["id"] for r in reviews)
            unlinked = connection.execute("SELECT count(*) FROM reviews").fetchone()[0] - len(reviews)
            coverage["sources"]["reviews"] = {"available": True, "records": len(reviews), "window_records": sum(in_window(r.get("created")) for r in reviews)}
            if unlinked:
                gap("reviews", "unassigned_project_records", "其余审查无本项目明确 ID 关联，未按工作目录猜测归属；不纳入本项目统计", unlinked)
        else:
            coverage["sources"]["reviews"] = {"available": False, "records": 0, "window_records": 0}
            gap("reviews", "missing_table", "缺少正式 reviews 表，无法核实方案/验收审查")

        if "events" in tables:
            selected = {}
            if json_sql:
                for raw in connection.execute("SELECT * FROM events WHERE json_valid(detail) AND json_extract(detail,'$.product_id')=?", (product_id,)):
                    selected[raw["id"]] = dict(raw)
                # 一个 JSON 集合绑定完整项目 ID，避免千条上限和逐批全表扫描。
                keys = ("id", "run_id", "requirement_id", "inspection_id", "delivery_id", "signal_id", "report_id")
                membership = " OR ".join(f"json_extract(detail,'$.{key}') IN (SELECT value FROM json_each(?))" for key in keys)
                scope_ids = json.dumps(sorted(all_ids))
                query = f"SELECT * FROM events WHERE json_valid(detail) AND ({membership})"
                for raw in connection.execute(query, (scope_ids,) * len(keys)):
                    selected[raw["id"]] = dict(raw)
            for column, identifiers in (("session_id", run_ids), ("review_id", review_ids)):
                for group in _chunks(identifiers):
                    marks = ",".join("?" for _ in group)
                    for raw in connection.execute(f"SELECT * FROM events WHERE {column} IN ({marks})", group):
                        selected[raw["id"]] = dict(raw)
            for raw in selected.values():
                raw["detail"] = _object(raw.get("detail"))
                explicit = raw["detail"].get("product_id")
                if explicit and explicit != product_id:
                    continue
                events.append(raw)
            events.sort(key=lambda r: (_time(r.get("at")) or 0, r["id"]))
            coverage["sources"]["events"] = {"available": True, "records": len(events), "window_records": sum(in_window(r.get("at")) for r in events)}
        else:
            coverage["sources"]["events"] = {"available": False, "records": 0, "window_records": 0}
            gap("events", "missing_table", "缺少原始事件，无法重建状态和审查决策发生历史")
    finally:
        connection.close()

    products = rows["products"]
    if not products:
        raise ValueError("指定项目不存在或项目表不可用")
    product = products[0]
    index = {kind: {r["id"]: r for r in values} for kind, values in rows.items()}
    transitions = defaultdict(list)
    review_events = defaultdict(list)
    for event in events:
        detail = event["detail"]
        if detail.get("kind") in rows and detail.get("id") in index[detail["kind"]]:
            transitions[(detail["kind"], detail["id"])].append(event)
        if event.get("review_id") in review_ids:
            review_events[event["review_id"]].append(event)

    signal_requirements = defaultdict(set)
    record_requirements = defaultdict(set)
    for requirement in rows["requirements"]:
        for sid in _ids(requirement.get("signal_ids")):
            if sid in index["signals"]:
                signal_requirements[sid].add(requirement["id"])
            else:
                gap("requirements", "missing_signal_reference", "需求关联信号不在本项目已读证据中")
        for key in ("source_id", "run_id", "inspection_id", "delivery_id"):
            for ident in _ids(requirement.get(key)):
                if ident in all_ids:
                    record_requirements[ident].add(requirement["id"])

    progress = {}
    if current_progress is not None:
        if not isinstance(current_progress, dict):
            raise ValueError("current_progress 必须为本项目任务 ID 到只读投影的映射")
        progress = {ident: _safe(value) for ident, value in current_progress.items() if ident in run_ids}
        coverage["sources"]["current_progress"] = {"available": True, "records": len(progress), "window_records": None}
        if run_ids - set(progress):
            gap("current_progress", "projection_omitted_runs", "只读投影未覆盖全部任务；缺失项采用保守状态判断", len(run_ids - set(progress)))
    elif origin:
        try:
            progress = _current_progress(origin, product_id)
            missing = run_ids - set(progress)
            coverage["sources"]["current_progress"] = {"available": True, "records": len(progress), "window_records": None}
            if missing:
                gap("current_progress", "api_omitted_runs", "GET /runs 未覆盖全部项目任务；完整数据库记录仍保留，缺失项采用保守状态判断", len(missing))
        except Exception as exc:
            gap("current_progress", "api_unavailable", f"本机当前进度 API 不可用（{type(exc).__name__}），使用只读台账状态")
    else:
        gap("current_progress", "not_requested", "未提供 origin；不推断未显式记录的额度、执行槽或调度等待条件")

    def links(record, result=None):
        # 实体旧回执和其他批次结果不是本次问题的关联证据。
        signal_ids = _ids(record.get("signal_id")) + _ids(record.get("signal_ids"))
        signal_ids += _references(record.get("evidence"), "signal_id", "signal_ids")
        signal_ids += _references(result, "signal_id", "signal_ids")
        if record.get("source") == "inspection" and record.get("id") in index["signals"]:
            signal_ids.append(record["id"])
        signal_ids += _ids(record.get("exception_signal_ids"))
        signal_ids += _ids(record.get("attribution_signal_ids"))
        signal_ids = sorted(set(sid for sid in signal_ids if sid in index["signals"]))
        requirement_ids = set(_ids(record.get("requirement_id")) + _ids(record.get("requirement_ids")))
        if record.get("id") in index["requirements"]:
            requirement_ids.add(record["id"])
        requirement_ids.update(_references(result, "requirement_id", "requirement_ids"))
        requirement_ids.update(record_requirements.get(record.get("id"), set()))
        for sid in signal_ids:
            requirement_ids.update(signal_requirements[sid])
            signal = index["signals"][sid]
            requirement_ids.update(_references(signal, "requirement_id", "requirement_ids"))
        for rid in _ids(record.get("run_ids")) + _ids(record.get("run_id")):
            if rid in index["runs"]:
                requirement_ids.update(_ids(index["runs"][rid].get("requirement_id")))
        requirement_ids = sorted(rid for rid in requirement_ids if rid in index["requirements"])
        evidence_ids = set(_ids(record.get("evidence_id")) + _ids(record.get("evidence_ids")))
        evidence_ids.update(_references(result, "evidence_id", "evidence_ids"))
        if record.get("id") in index["inspections"]:
            evidence_ids.update(_ids(record.get("steps")))
        for sid in signal_ids:
            evidence_ids.update(_references(index["signals"][sid], "evidence_id", "evidence_ids"))
        return signal_ids, requirement_ids, sorted(evidence_ids)

    groups = {}
    current = {}

    def add(bucket, kind, record, result, status, moment, *, code=None, component=None,
            event_key=None, count=1, count_scope="confirmed", event_id=None, receipt_id=None,
            window_status="confirmed_in_period", reason=None, failure_kind=None):
        result = _object(result)
        raw_reason = reason or _reason(result)
        reason, shared_component = _cause(raw_reason)
        workflow_sources = {"products", "inspection_plans", "inspection_rounds", "daily_reports", "coordinators", "research_jobs"}
        component = str(shared_component or component or record.get("component") or record.get("route_id")
                        or record.get("requirement_id") or (product_id if kind in workflow_sources else record.get("id")))
        code = str(code or result.get("error_code") or record.get("code") or result.get("code") or kind.upper())
        failure = failure_kind or _failure_kind(result, status)
        source = "inspection" if kind == "inspections" else str(record.get("source") or kind)
        if source == "inspection" and kind in ("inspections", "signals"):
            code = "INSPECTION_EVIDENCE"
        # 仅明确来源、组件、代码和相同原因可合并；不会按相似标题猜测同一缺陷。
        key = (source, code, component, reason, failure)
        signals, requirements, evidence = links(record, result)
        ident = hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()[:20]
        if key not in bucket:
            bucket[key] = {"id": "problem-" + ident, "title": record.get("title") or record.get("summary") or reason,
                "source_kind": kind, "source_ids": [], "evidence_ids": [], "signal_ids": [], "requirement_ids": [],
                "reason": reason, "window_status": window_status, "current_status": record.get("status", "unknown"),
                "failure_kind": failure, "occurrences": 0, "last_seen": None, "code": code, "component": component,
                "count_scope": count_scope, "observations": [], "requirement_statuses": {}, "_counts": {},
                "association_scope": "显式 ID/来源记录/信号的已有需求关联用于追溯；同一日报或轮次关联不证明该需求已覆盖本问题"}
        item = bucket[key]
        item.setdefault("source_kinds", [])
        if kind not in item["source_kinds"]:
            item["source_kinds"].append(kind)
        item.setdefault("reason_variants", [])
        if raw_reason != reason and raw_reason not in item["reason_variants"]:
            item["reason_variants"].append(raw_reason)
        for name, values in (("source_ids", [record["id"]]), ("evidence_ids", evidence),
                             ("signal_ids", signals), ("requirement_ids", requirements)):
            item[name] = sorted(set(item[name]) | set(values))
        item["requirement_statuses"].update({rid: index["requirements"][rid]["status"] for rid in requirements})
        stamp = _iso(moment)
        if stamp and (item["last_seen"] is None or stamp > item["last_seen"]):
            item["last_seen"] = stamp
        identity = event_key or receipt_id or event_id or record["id"]
        item["_counts"][str(identity)] = max(item["_counts"].get(str(identity), 0), count)
        item["occurrences"] = sum(item["_counts"].values())
        if count_scope != "confirmed":
            item["count_scope"] = count_scope
        item["observations"].append({"source_id": record["id"], "at": stamp, "historical_status": status,
            "current_status": record.get("status"), "event_id": event_id, "receipt_id": receipt_id,
            "current_record_updated": _iso(record.get("updated")), "time_basis": window_status})
        return item

    def outcome(record, kind):
        """优先原始状态事件；完成时间只在无跨日快照覆盖时使用当前结果。"""
        history = [e for e in transitions[(kind, record["id"])] if in_window(e.get("at"))
                   and (e["detail"].get("to") or e["detail"].get("status")) not in (None, "running", "queued", "pending")]
        if history:
            event = history[-1]
            return event["detail"].get("to") or event["detail"].get("status"), event["at"], "original_event", str(event["id"])
        moment = record.get("finished_at")
        if in_window(moment):
            if _time(record.get("updated")) and _time(record["updated"]) >= upper:
                gap(kind, "historical_payload_overwritten", "记录在窗口后更新且缺少原始状态事件，完成发生可确认，但当时状态/原因不能仅用当前快照证明")
                return None, moment, "finished_in_period_status_unverified", None
            return record.get("status"), moment, "finished_at", None
        if in_window(record.get("created")):
            gap(kind, "missing_outcome_time", "记录在自然日内创建，但缺少可靠完成/状态发生时间；不把当前 updated 当历史证据")
        return None, None, "unknown", None

    # 全量 evidence 是主源；实体 receipts 有最近 100 条裁剪，仅用于关联和补充。
    call_owners = {}
    merged_receipts = {}
    for kind, records in rows.items():
        if kind == "evidence":
            continue
        for record in records:
            receipts = record.get("receipts") if isinstance(record.get("receipts"), list) else []
            for number, receipt in enumerate(receipts):
                if not isinstance(receipt, dict):
                    continue
                identity = receipt.get("call_id") or receipt.get("evidence_id") or f"{record['id']}:{number}"
                merged_receipts[identity] = (kind, record, receipt)
                if receipt.get("call_id"):
                    call_owners[receipt["call_id"]] = (kind, record)
            if _object(record.get("call")).get("id"):
                call_owners[record["call"]["id"]] = (kind, record)
            if len(receipts) >= 100:
                gap(kind, "entity_receipts_truncated", "实体回执保留最近 100 条；已改用完整 auto_evidence 主源，仍不能证明数据库更早记录从未删除")
    ownerless_evidence = Counter()
    for evidence in rows["evidence"]:
        call_id = evidence.get("call_id")
        phase = evidence.get("phase")
        if not call_id or not phase:
            continue  # 巡检步骤/正式审查证据不是独立执行回执。
        owner = call_owners.get(call_id)
        if not owner and evidence.get("run_id") in index["runs"]:
            owner = ("runs", index["runs"][evidence["run_id"]])
        if owner:
            kind, record = owner
        else:
            # 保留平台同步、协调、研究等执行证据；缺少实体关联时不猜任务或需求。
            kind = {"master_sync": "products", "probe": "products", "final_acceptance": "products",
                    "daily_attribution": "daily_reports", "daily_acceptance": "daily_reports", "daily_retrospective": "daily_reports",
                    "coordinate": "coordinators", "inspection_design": "inspection_plans", "inspection_run": "inspection_rounds",
                    "analyze_competitors": "research_jobs", "find_competitors": "research_jobs"}.get(phase, "execution_evidence")
            record = dict(evidence, title=phase + " 执行异常", component=product_id)
            if in_window(evidence.get("at")):
                ownerless_evidence[phase] += 1
        result = _object(evidence.get("details"))
        result = dict(result, status=result.get("status") or evidence.get("status"))
        if not result.get("reason") and not result.get("summary"):
            result["reason"] = evidence.get("judgement") or evidence.get("actual")
        merged_receipts[call_id] = (kind, record, {"call_id": call_id, "action": phase,
            "at": evidence.get("at") or evidence.get("created"), "evidence_id": evidence["id"], "result": result})
    for phase, count in ownerless_evidence.items():
        gap("evidence", "missing_execution_owner", f"{phase} 的原始回执有效，但已裁剪或未保存所属实体 ID；仅按项目/阶段/原因归并，不猜测需求关联", count)

    receipt_counts = defaultdict(Counter)
    phase_counts = defaultdict(Counter)
    for problem in rows['problems']:
        episodes = problem.get('episodes')
        if not isinstance(episodes, list):
            # 旧台账只确认观测点，不能从最后状态反推整段持续时间。
            seen = _time(problem.get('first_seen'))
            if seen is not None and in_window(seen):
                add(groups, 'problems', problem, {}, 'blocked', seen,
                    reason=problem.get('cause') or problem.get('summary'),
                    window_status='history_incomplete', code='PROBLEM_OBSERVATION')
                gap('problems', 'missing_episodes', '旧问题台账没有完整发生与恢复区间，仅保留可确认观测')
            continue
        for number, episode in enumerate(episodes):
            if not isinstance(episode, dict):
                continue
            opened, closed = _time(episode.get('opened_at')), _time(episode.get('closed_at'))
            if opened is None or opened >= upper or closed is not None and closed <= lower:
                continue
            observations = sorted([o for o in episode.get('observations', [])
                                   if isinstance(o, dict) and _time(o.get('at')) is not None], key=lambda o: _time(o['at']))
            if not observations:
                observations = [dict(episode, at=opened)]
            for pos, observation in enumerate(observations):
                observed = _time(observation['at'])
                until = _time(observations[pos + 1]['at']) if pos + 1 < len(observations) else closed
                if observed >= upper or until is not None and until <= lower:
                    continue
                cause = observation.get('cause') or episode.get('cause') or problem.get('cause') or '历史原因未记录'
                if closed is not None:
                    label = '窗口结束后、截至复盘时间已恢复' if closed >= upper else '窗口内已恢复'
                    cause += '；问题台账标记“' + label + '”，业务验收和上线证据需分别核对。'
                record = dict(problem, title=observation.get('summary') or episode.get('summary') or problem.get('summary'))
                add(groups, 'problems', record, {}, 'blocked', max(observed, lower),
                    reason=cause, code='PROBLEM_EPISODE', event_key=str(number) + ':' + str(pos),
                    window_status='episode_overlap')
    run_failures = 0
    failed_run_ids = set()
    failed_receipt_ids = set()
    ordered_receipts = sorted(merged_receipts.values(), key=lambda x: _time(x[2].get("at")) or 0)
    for kind, record, receipt in ordered_receipts:
        if not _time(receipt.get("at")):
            gap(kind, "untimed_receipt", "回执缺少可靠 at，不能归入指定自然日")
            continue
        if not in_window(receipt["at"]):
            continue
        result = _object(receipt.get("result"))
        status = result.get("status") or result.get("decision") or "unknown"
        receipt_counts[kind][status] += 1
        phase_counts[receipt.get("action") or "unknown"][status] += 1
        checks = [c for c in result.get("checks", []) if isinstance(c, dict)
                  and (c.get("status") in BAD or c.get("passed") is False)]
        if status not in BAD and not checks:
            continue
        receipt_id = receipt.get("call_id") or receipt.get("evidence_id")
        failed_receipt_ids.add(receipt_id)
        if kind == "runs":
            failed_run_ids.add(record["id"])
            if status in BAD:
                run_failures += 1
        refs = dict(result)
        if receipt.get("evidence_id"):
            refs["evidence_id"] = receipt["evidence_id"]
        item = add(groups, kind, record, refs, status, receipt["at"],
            code=result.get("error_code") or receipt.get("action") or kind.upper(),
            component=product_id if kind == "execution_evidence" else None,
            receipt_id=receipt_id, window_status="evidence_at" if receipt.get("evidence_id") in index["evidence"] else "receipt_at",
            reason=_reason(result) if status in BAD else "父回执通过，但存在未通过的检查：" + "；".join(_reason(c) for c in checks))
        item.setdefault("phases", [])
        if receipt.get("action") not in item["phases"]:
            item["phases"].append(receipt.get("action"))
        item.setdefault("failed_checks", []).extend(_safe({k: c.get(k) for k in ("id", "name", "status", "reason", "failure_kind", "evidence_id")}) for c in checks)
        later_passes = [r for k, owner, r in ordered_receipts if owner["id"] == record["id"]
                        and r.get("action") == receipt.get("action") and _object(r.get("result")).get("status") == "pass"
                        and _time(r.get("at")) and _time(r["at"]) > _time(receipt["at"])]
        if later_passes:
            passed = later_passes[-1]
            item.setdefault("later_progress_evidence", []).append({"action": receipt.get("action"),
                "receipt_id": passed.get("call_id"), "evidence_id": passed.get("evidence_id"), "at": _iso(passed["at"]),
                "meaning": "后续同阶段已有通过回执；不自动证明每个旧问题均已修复"})

    inspection_counts = Counter()
    for kind in ("inspections", "code_reviews", "scans", "test_chain_runs", "evaluations", "requirement_groups", "design_versions"):
        for record in rows[kind]:
            status, moment, basis, event_id = outcome(record, kind)
            if kind == "inspections" and status:
                inspection_counts[status] += 1
            if status not in BAD:
                continue
            result = _object(record.get("result")) or record
            add(groups, kind, record, result, status, moment, event_id=event_id,
                event_key="inspection:" + record["id"] if kind == "inspections" else None,
                window_status=basis, code="BUSINESS_ROUTE" if kind == "inspections" and record.get("route_id") else kind.upper())

    notification_counts = Counter()
    notification_items = []
    notification_retry_events = 0
    for note in rows["notifications"]:
        receipt = note["receipt"]
        moment = receipt.get("at")
        retries = [event for event in events if event.get("kind") == "autopilot_notification_retry"
                   and event["detail"].get("id") == note["id"] and in_window(event.get("at"))]
        notification_retry_events += len(retries)
        status = "smtp_accepted" if note["status"] in ("sent", "smtp_accepted") else note["status"]
        error = note.get("error") or ("；".join(receipt.get("refusal_reasons", [])) if receipt.get("refused_count") else "")
        if status == "queued" and (error or note.get("attempts", 0)):
            status = "retry_wait"
        event_id = None
        basis = "receipt_at"
        if not _time(moment):
            status_at, moment, basis, event_id = outcome(note, "notifications")
            if status_at:
                status = "smtp_accepted" if status_at == "sent" else status_at
        if in_window(moment):
            notification_counts[status] += 1
            receipt_counts["notifications"][status] += 1
            if status in ("partial", "failed", "blocked", "retry_wait"):
                historical_error = error
                if _time(note.get("updated")) and _time(note["updated"]) >= upper:
                    historical_error = "窗口内邮件投递异常已确认，错误详情在窗口后更新；请核对原始投递回执"
                    gap("notifications", "historical_error_overwritten", "邮件在窗口后更新，不能把当前错误原因倒填历史投递")
                item = add(groups, "notifications", note, {}, status, moment,
                    code="SMTP_DELIVERY", component=product_id, event_id=event_id,
                    event_key="notification:" + note["id"] + ":latest-receipt", window_status=basis,
                    reason=historical_error or "邮件投递未完成，原记录没有错误详情", failure_kind="notification_delivery")
                item["attempts_lifetime"] = note.get("attempts", 0)
                item.setdefault("notification_attempts", {})[note["id"]] = note.get("attempts", 0)
                item["accepted_count"] = receipt.get("accepted_count", 0)
                item["requested_count"] = receipt.get("requested_count", 0)
        elif retries:
            add(groups, "notifications", note, {}, "retry_wait", retries[-1]["at"],
                code="SMTP_RETRY", component=product_id, count=len(retries), event_key="notification:" + note["id"] + ":retry-events",
                window_status="event_at", reason="邮件投递发生重试；当时的错误详情未完整保存", failure_kind="notification_delivery")
        retrying = note["status"] == "queued" and (bool(error) or note.get("attempts", 0) > 0)
        if note["status"] in ("partial", "failed", "blocked") or retrying:
            item = add(current, "notifications", note, {}, note["status"], None,
                code="SMTP_DELIVERY", component=product_id, window_status="current_snapshot_only",
                reason=error or "邮件投递未完成，需要核对发送条件", failure_kind="notification_delivery")
            item["attempts_lifetime"] = note.get("attempts", 0)
            item.setdefault("notification_attempts", {})[note["id"]] = note.get("attempts", 0)
            item["next_attempt_at"] = _iso(note.get("next_attempt_at"))
            item["action"] = "按现有退避规则重试并核对 SMTP 网络、端口、加密方式与认证；已接受不等于收件箱收到"
        notification_items.append({"id": note["id"], "title": note["title"], "kind": note.get("kind"),
            "current_status": note["status"], "window_receipt_status": status if in_window(moment) else None,
            "receipt_at": _iso(receipt.get("at")), "error": error, "attempts_lifetime": note.get("attempts", 0),
            "next_attempt_at": _iso(note.get("next_attempt_at")), "requested_count": receipt.get("requested_count"),
            "accepted_count": receipt.get("accepted_count"), "refused_count": receipt.get("refused_count"),
            "retry_events_in_period": [{"at": _iso(ev["at"]), "attempt": ev["detail"].get("attempts")} for ev in retries]})

    signals_created = 0
    signal_lifetime = 0
    for signal in rows["signals"]:
        first, last = _time(signal.get("created")), _time(signal.get("last_seen"))
        created_here, last_here = in_window(first), in_window(last)
        signals_created += int(created_here)
        if not created_here and not last_here:
            continue
        count = signal.get("count", 1)
        count = count if isinstance(count, int) and count > 0 else 1
        signal_lifetime += count
        evidence = _object(signal.get("evidence"))
        status = evidence.get("status") or signal.get("status")
        if status not in BAD:
            continue
        if last and last >= upper:
            gap("signals", "latest_evidence_outside_period", "信号窗口内首次出现，但最新 evidence 在窗口后被覆盖；仅能确认出现，不能把当前失败详情倒填昨日")
            continue
        scope = "confirmed"
        occurrences = count if created_here and (last_here or count == 1) else 1
        if count > occurrences:
            scope = "lower_bound; lifetime count crosses period"
            gap("signals", "cumulative_count_without_occurrence_times", "信号累计次数跨越窗口，缺少逐次时间；窗口内次数仅为已确认下界")
        linked_inspection = index["inspections"].get(signal.get("inspection_id"))
        extra = dict(signal)
        extra["signal_ids"] = [signal["id"]]
        if linked_inspection:
            extra["evidence_ids"] = _ids(linked_inspection.get("steps"))
            extra["component"] = linked_inspection.get("route_id") or signal.get("component")
        item = add(groups, "signals", extra, evidence, status, last if last_here else first,
            event_key="inspection:" + linked_inspection["id"] if linked_inspection else None,
            count=occurrences, count_scope=scope, window_status="last_seen" if last_here else "created",
            component=signal.get("component") or (linked_inspection or {}).get("route_id"))
        if linked_inspection and linked_inspection["id"] not in item["source_ids"]:
            item["source_ids"].append(linked_inspection["id"])
        item.setdefault("lifetime_occurrences_by_signal", {})[signal["id"]] = count

    review_decisions = Counter()
    for review in reviews:
        timed = [e for e in review_events[review["id"]] if in_window(e.get("at")) and e["detail"].get("decision")]
        result = review["result"]
        if not timed:
            if in_window(review.get("created")) and result.get("decision"):
                gap("reviews", "missing_decision_time", "审查窗口内提交，但缺少原始决策事件；created 不证明当前决策发生在当日")
            continue
        for event in timed:
            decision = event["detail"]["decision"]
            review_decisions[decision] += 1
            if decision not in BAD:
                continue
            run = index["runs"].get(review.get("session_id")) or index["runs"].get(review["packet"].get("run_id"))
            record = dict(review, title=review["packet"].get("summary") or "正式方案/验收审查")
            if run:
                record["requirement_id"] = run.get("requirement_id")
                record["run_id"] = run["id"]
            reason = _reason(result) if result.get("decision") == decision else "原始审查事件要求修改/阻塞；当前结果已改变，历史原因未保存"
            add(groups, "reviews", record, result, decision, event["at"],
                code=review["packet"].get("phase") or "REVIEW", event_id=str(event["id"]),
                component=(run or {}).get("requirement_id") or review.get("session_id"),
                window_status="original_review_event", reason=reason)

    # 归因/复盘只读文字与来源 ID 以 receipts 的 at 为准，不用报告 day/updated 代替发生时间。
    daily_outputs = []
    for report in rows["daily_reports"]:
        selected_receipts = [r for kind, owner, r in ordered_receipts
                             if kind == "daily_reports" and owner["id"] == report["id"] and in_window(r.get("at"))]
        if selected_receipts or in_window(report.get("created")):
            daily_outputs.append({"id": report["id"], "day": report.get("day"), "status": report["status"],
                "created": _iso(report.get("created")), "current_record_updated": _iso(report.get("updated")),
                "signal_ids": _ids(report.get("attribution_signal_ids")), "requirement_ids": _ids(report.get("requirement_ids")),
                "outputs": [{"action": r.get("action"), "at": _iso(r["at"]), "evidence_id": r.get("evidence_id"),
                    "result": _safe({k: _object(r.get("result")).get(k) for k in ("status", "reason", "summary", "attributions", "requirements", "problems", "lessons", "next_actions")})}
                    for r in selected_receipts]})
    orphan_daily = [r for kind, owner, r in ordered_receipts if kind == "daily_reports"
                    and owner["id"] not in index["daily_reports"] and in_window(r.get("at"))]
    if orphan_daily:
        daily_outputs.append({"id": "unassigned-daily-executions", "day": day.isoformat(),
            "status": "historical_evidence_owner_unknown", "signal_ids": [], "requirement_ids": [],
            "outputs": [{"action": r.get("action"), "at": _iso(r["at"]), "evidence_id": r.get("evidence_id"),
                         "result": _safe({k: _object(r.get("result")).get(k) for k in ("status", "reason", "summary", "attributions", "requirements", "problems", "lessons", "next_actions")})}
                        for r in orphan_daily],
            "coverage_note": "完整证据已保留，但所属日报实体关联因裁剪不可核实；不得猜测对应信号批次"})

    task_receipts = defaultdict(list)
    for kind, owner, receipt in ordered_receipts:
        if kind == "runs" and in_window(receipt.get("at")):
            result = _object(receipt.get("result"))
            task_receipts[owner["id"]].append({"action": receipt.get("action"), "at": _iso(receipt.get("at")),
                "call_id": receipt.get("call_id"), "evidence_id": receipt.get("evidence_id"),
                "status": result.get("status"), "reason": _reason(result)})
    task_items = []
    for run in rows["runs"]:
        req = index["requirements"].get(run.get("requirement_id"), {})
        historical = [ev for ev in transitions[("runs", run["id"])] if in_window(ev.get("at"))]
        observed = bool(task_receipts[run["id"]] or historical or in_window(run.get("created"))
                        or any(in_window(run.get(key)) for key in ("finished_at", "accepted_at", "online_at")))
        task_items.append({"id": run["id"], "title": run.get("title") or req.get("title") or run["id"],
            "status": run["status"], "reason": run.get("reason"), "requirement_id": run.get("requirement_id"),
            "requirement_status": req.get("status"), "created": _iso(run.get("created")),
            "observed_in_period": observed, "receipts": task_receipts[run["id"]],
            "current_progress": progress.get(run["id"]) or run.get("current_progress"),
            "historical_statuses": sorted({ev["detail"].get("to") or ev["detail"].get("status")
                for ev in historical if ev["detail"].get("to") or ev["detail"].get("status")})})
    active_runs = []
    for run in rows["runs"]:
        current_progress = progress.get(run["id"]) or _object(run.get("current_progress"))
        call = _object(run.get("call"))
        if current_progress.get("running") or call:
            active_runs.append({"id": run["id"], "status": run["status"], "action": call.get("action"),
                                "current_progress": current_progress or {"kind": "call", "running": True, "reason": "执行中，等待回执"}})
            continue
        if run["status"] in TERMINAL:
            continue
        explicit_wait = current_progress.get("kind") not in (None, "call", "dispatch") and current_progress.get("running") is not True
        if not (run["status"] in WAIT or run.get("uncertain") or run.get("pending_result") or run.get("revision_exhausted") or explicit_wait):
            continue
        reason = current_progress.get("reason") or run.get("reason") or ("等待核对执行结果/补齐证据" if run.get("uncertain") or run.get("pending_result") else "等待回复或人工处理")
        item = add(current, "runs", run, run, run["status"], None,
            window_status="current_snapshot_only", code=current_progress.get("kind") or run["status"],
            reason=reason, failure_kind="workflow_wait" if run["status"] != "failed" else "unclassified_failure")
        item["current_progress"] = current_progress or None
        item["revision_exhausted"] = bool(run.get("revision_exhausted"))
        item["last_seen"] = None
        item["occurrences"] = 1
        item["count_scope"] = "current records; not historical occurrences"
    historical_failed_records = []
    for kind in ("inspection_rounds", "daily_reports", "deliveries", "code_reviews", "scans", "test_chain_runs", "evaluations", "requirement_groups"):
        for record in rows[kind]:
            if record["status"] not in WAIT or record.get("call"):
                continue
            delivery = index["deliveries"].get(record.get("delivery_id"))
            if (delivery and delivery["status"] in TERMINAL) or record.get("finished_at") or kind == "code_reviews":
                historical_failed_records.append({"source_kind": kind, "id": record["id"],
                    "current_status": "historical_failed", "record_status": record["status"],
                    "owner_status": delivery.get("status") if delivery else None,
                    "meaning": "已结束的历史失败/阻塞记录；不据此判断当前执行受阻，未有后续验证时保留待核实"})
                continue
            add(current, kind, record, _object(record.get("result")) or record, record["status"], None,
                window_status="current_snapshot_only", reason=_reason(record), failure_kind="workflow_wait")
    for requirement in rows["requirements"]:
        status = requirement["status"]
        investigation = requirement.get("classification") in ("investigation", "environment")
        if status not in ("awaiting_external", "waiting_for_reply", "needs_human", "pending_confirmation") and not (investigation and status in ("pending", "queued", "investigating")):
            continue
        if requirement.get("call"):
            continue
        reason = requirement.get("reason") or requirement.get("investigation_reason") or {
            "awaiting_external": "等待外部条件或外部证据", "pending_confirmation": "等待需求范围/证据确认",
            "pending": "待调查，尚未形成已确认业务缺陷", "queued": "调查需求已入池，等待调查"}.get(status, "等待调查或协作回复")
        add(current, "requirements", requirement, requirement, status, None,
            window_status="current_snapshot_only", code=status, reason=reason, failure_kind="workflow_wait")
    for signal in rows["signals"]:
        evidence = _object(signal.get("evidence"))
        if not (signal.get("attribution_pending") or signal["status"] in ("abnormal", "pending")) or evidence.get("status") not in BAD:
            continue
        linked = index["inspections"].get(signal.get("inspection_id")) or {}
        extra = dict(signal, signal_ids=[signal["id"]], evidence_ids=_ids(linked.get("steps")))
        add(current, "signals", extra, evidence, evidence.get("status"), None,
            component=signal.get("component") or linked.get("route_id"),
            event_key="inspection:" + linked["id"] if linked else None,
            window_status="current_snapshot_only", failure_kind="attribution_pending")

    def finish(bucket):
        result = list(bucket.values())
        for item in result:
            item.pop("_counts", None)
        result.sort(key=lambda r: (-r["occurrences"], r["source_kind"], r["id"]))
        return _safe(result)

    problem_items, blockers = finish(groups), finish(current)
    requirements = []
    for requirement in rows["requirements"]:
        requirements.append(_safe({"id": requirement["id"], "title": requirement.get("title"), "status": requirement["status"],
            "classification": requirement.get("classification"), "reason": requirement.get("reason"),
            "source": requirement.get("source"), "source_id": requirement.get("source_id"),
            "signal_ids": _ids(requirement.get("signal_ids")), "evidence_ids": _references(requirement, "evidence_id", "evidence_ids"),
            "created": _iso(requirement.get("created")), "created_in_period": in_window(requirement.get("created")),
            "current_record_updated": _iso(requirement.get("updated")),
            "run_ids": [r["id"] for r in rows["runs"] if r.get("requirement_id") == requirement["id"]]}))
    if rows["signals"] and not rows["evidence"]:
        gap("evidence", "no_original_evidence_records", "存在信号但无原始 evidence 行，最新信号快照不能替代完整历史证据")
    if rows["runs"]:
        missing_receipts = sum(not r.get("receipts") and r["status"] in WAIT for r in rows["runs"])
        if missing_receipts:
            gap("runs", "blocker_without_receipt_history", "当前阻塞任务缺少历史回执，无法证明当日失败细节", missing_receipts)
    statistics = {"records": {k: len(v) for k, v in rows.items()},
        "by_status": {k: dict(Counter(r.get("status", "unknown") for r in v)) for k, v in rows.items()},
        "receipt_status_counts": {k: dict(v) for k, v in receipt_counts.items()},
        "phase_status_counts": {k: dict(v) for k, v in phase_counts.items()},
        "run_failure_receipts": run_failures, "failed_run_ids": sorted(failed_run_ids),
        "failed_receipt_ids": sorted(failed_receipt_ids), "inspection_status_counts": dict(inspection_counts),
        "review_decisions": dict(review_decisions), "signals_created": signals_created,
        "historical_failed_records": historical_failed_records,
        "signals_lifetime_counts_of_window_records": signal_lifetime, "active_runs": active_runs,
        "requirements_created": sum(r["created_in_period"] for r in requirements)}
    statistics["notification_receipt_status_counts"] = dict(notification_counts)
    statistics["notification_retry_events_in_period"] = notification_retry_events
    summary = {"problem_groups": len(problem_items), "current_blocker_groups": len(blockers),
        "current_run_blocker_groups": sum(i["source_kind"] == "runs" for i in blockers),
        "current_requirement_wait_groups": sum(i["source_kind"] == "requirements" for i in blockers),
        "current_signal_attribution_groups": sum(i["source_kind"] == "signals" for i in blockers),
        "current_notification_wait_groups": sum(i["source_kind"] == "notifications" for i in blockers),
        "run_failure_receipts": run_failures, "runs_with_problem_receipts": len(failed_run_ids),
        "inspection_records_with_outcomes": sum(inspection_counts.values()),
        "inspection_blocked_records": inspection_counts["blocked"], "new_signal_records": signals_created,
        "requirements_created": statistics["requirements_created"], "coverage_complete": coverage["complete"],
        "statement": f"确认任务失败/阻塞回执 {run_failures} 条；巡检阻塞 {inspection_counts['blocked']} 条为执行/证据阻塞记录，不能称为同等数量业务缺陷。问题按明确来源、组件、代码和原因归并；覆盖缺口另列。"}
    if notification_items:
        summary["statement"] += f" 邮件窗口投递异常回执 {sum(notification_counts[key] for key in ('partial', 'failed', 'blocked', 'retry_wait'))} 条；当前投递待处理 {summary['current_notification_wait_groups']} 组。SMTP 接受不等于收件箱收到。"
    return _safe({"schema_version": "1.0", "generated_at": snapshot_at, "project": {"id": product_id, "name": product.get("name") or product.get("title") or product_id,
            "status": product.get("status"), "source": product.get("source")},
        "period": {"date": day.isoformat(), "timezone": "Asia/Shanghai", "start": start.isoformat(), "end": end.isoformat(),
                   "start_epoch": lower, "end_epoch": upper, "bounds": "[start,end)", "snapshot_at": snapshot_at,
                   "window_kind": "custom" if custom_window else "natural_day"},
        "coverage": coverage, "summary": summary, "statistics": statistics,
        "problem_items": problem_items, "current_blockers": blockers, "requirements": requirements,
        "daily_reports": daily_outputs, "task_items": task_items, "notification_items": notification_items})


def main(argv=None):
    """命令行输出 JSON；仅显式 --output 时写报告证据文件。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--product-id", required=True)
    parser.add_argument("--date", required=True, help="北京时间自然日 YYYY-MM-DD")
    parser.add_argument("--origin", help="可选本机 API origin，用于补充 current_progress")
    parser.add_argument("--start", help="自定义窗口开始，有时区 ISO 时间")
    parser.add_argument("--end", help="自定义窗口结束，有时区 ISO 时间；右侧不含")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = collect(args.state_dir, args.product_id, args.date, args.origin, start=args.start, end=args.end)
    content = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(content, encoding="utf-8")
    else:
        print(content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
