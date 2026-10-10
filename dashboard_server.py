#!/usr/bin/env python3
"""Local dashboard HTTP API and static host. Python standard library only."""

from __future__ import annotations
import hashlib
import argparse, datetime, fcntl, hmac, json, mimetypes, os, secrets, signal, subprocess, sys, threading, time, uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote
from urllib.request import urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "dsh-gpt-supervisor/scripts"))
from review_core import (
    Store,
    Engine,
    Conflict,
    ACTIVE,
    alive,
    inventory,
    private_key,
    read_log,
    session_log,
    stamp,
)

from native_supervision import NativeSupervision
import reviewers
from autopilot.api import Control
from error_messages import present_errors, failure_fields


# 仓库扫描 API 契约版本。当前运行代码声明自己实现的能力；不兼容变更时递增，
# 前端据此在调用 /api/scans 前核对，避免新版页面连接旧版服务时反复提交无效扫描。
REPOSITORY_SCAN_API_VERSION = 1


def runtime_identity(root=ROOT):
    """返回运行身份，并声明当前运行代码版本的仓库扫描能力。

    能力以正在提供请求的代码为准，而不是磁盘上的 release marker，因此
    “旧后端 + 新 marker” 不可能误报支持。旧字段保持原样，只新增 capabilities。
    """
    marker = Path(root) / "autopilot-release.json"
    try:
        payload = json.loads(marker.read_text()) if marker.exists() else {}
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("product_id", None)
    payload.setdefault("commit", None)
    payload.setdefault("release_id", None)
    existing = payload.get("capabilities")
    payload["capabilities"] = {
        **(existing if isinstance(existing, dict) else {}),
        "repository_scans": REPOSITORY_SCAN_API_VERSION,
    }
    return payload


class Dashboard:
    def __init__(self, state):
        self.store = Store(state)
        self.store.recover()
        self.engine = Engine(self.store)
        self.browser_key = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self.connector_key = private_key(self.store.state / "connector.key")
        self.lock = threading.RLock()
        self.cache = (0, [])
        self.bridge = None
        self.native = NativeSupervision(self)
        self.autopilot = Control(self.store)

    def sessions(self):
        with self.lock:
            if time.monotonic() - self.cache[0] > 3:
                self.cache = (time.monotonic(), inventory(self.store))
            return self.cache[1]

    def session(self, sid):
        item = next((x for x in self.sessions() if x["id"] == sid), None)
        if not item:
            raise KeyError("会话不存在")
        return item

    def expire_commands(self):
        with self.store.connect() as db:
            db.execute(
                "UPDATE commands SET status='unknown',detail='连接回执超时，送达待核实' WHERE status='dispatching' AND updated<?",
                (time.time() - 20,),
            )
            db.execute(
                "UPDATE commands SET status='failed',detail='插件未及时接收指令' WHERE status='pending' AND created<?",
                (time.time() - 15,),
            )

    def connector_status(self):
        self.expire_commands()
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM connector WHERE id=1").fetchone()
        value = dict(row) if row else dict(home=None, last_seen=None)
        with self.store.connect() as db:
            cap = db.execute("SELECT response FROM operations WHERE id='connector-capabilities'").fetchone()
        value["native_supervision"] = bool(cap and json.loads(cap[0]).get("native_supervision"))
        value["online"] = bool(row and time.time() - row["last_seen"] < 10)
        value["home_matches"] = bool(
            row
            and Path(row["home"]).expanduser().resolve()
            == Path(self.store.settings()["home"]).expanduser().resolve()
        )
        return value

    def service_status(self):
        try:
            status = json.loads((self.store.state / "status.json").read_text())
        except (OSError, ValueError):
            status = {}
        status["running"] = bool(
            status.get("pid")
            and alive(status["pid"])
            and self.health(status.get("port"))
        )
        status["compatible"] = status.get("protocol") == "dashboard-v1"
        return status

    def health(self, port):
        if not port:
            return False
        try:
            with urlopen(f"http://127.0.0.1:{int(port)}/health", timeout=0.5) as r:
                return json.load(r).get("ok") is True
        except Exception:
            return False

    def service(self, start):
        with self.lock:
            status = self.service_status()
            if start:
                if status["running"]:
                    if not status["compatible"]:
                        raise Conflict(
                            "当前目录由旧版服务使用，请先用原 CLI 停止旧服务"
                        )
                    return status
                cfg = self.store.settings()
                if self.health(cfg["bridge_port"]):
                    raise Conflict("桥接端口已被其他服务占用，请检查设置")
                command = [
                    sys.executable,
                    str(ROOT / "dsh-gpt-supervisor/scripts/bridge.py"),
                    "run",
                    "--state-dir",
                    str(self.store.state),
                    "--port",
                    str(cfg["bridge_port"]),
                ]
                with (self.store.state / "server.log").open("a") as log:
                    self.bridge = subprocess.Popen(
                        command, stdout=log, stderr=log, start_new_session=True
                    )
                for _ in range(40):
                    if self.bridge.poll() is not None:
                        raise ValueError("桥接服务启动失败，请检查 server.log 和依赖")
                    if self.health(cfg["bridge_port"]):
                        self.store.event("service_started")
                        return self.service_status()
                    time.sleep(0.1)
                raise ValueError("桥接服务启动超时")
            if status["running"]:
                if not status["compatible"]:
                    raise Conflict("旧版服务需通过原 CLI 停止")
                # Verify this exact bridge identity before signalling a stored PID.
                with urlopen(
                    f"http://127.0.0.1:{status['port']}/health", timeout=1
                ) as response:
                    live = json.load(response)
                if (
                    live.get("pid") != status["pid"]
                    or live.get("protocol") != "dashboard-v1"
                ):
                    raise Conflict("进程身份变化，拒绝停止")
                os.kill(status["pid"], signal.SIGTERM)
                for _ in range(60):
                    if self.bridge:
                        self.bridge.poll()
                    if not (self.store.state / "pid").exists() and not self.health(
                        status["port"]
                    ):
                        break
                    time.sleep(0.1)
                if self.health(status["port"]):
                    raise Conflict("监工正在退出，请稍后刷新")
            self.engine.stop()
            self.store.event("service_stopped")
            return self.service_status()

    def history(self):
        result = []
        directory = Path(self.store.settings()["history_dir"]).expanduser() / "reviews"
        with self.store.connect() as db:
            current = [
                self.store.decode(r)
                for r in db.execute(
                    "SELECT * FROM reviews ORDER BY created DESC LIMIT 500"
                )
            ]
        known = {r["id"] for r in current}
        for path in directory.glob("*/result.json"):
            if path.parent.name in known:
                continue
            try:
                record = json.loads(path.read_text())
                packet = json.loads((path.parent / "request.json").read_text())
                result.append(
                    dict(
                        id=path.parent.name,
                        session_id=packet.get("session_id"),
                        packet=packet,
                        mode="historical",
                        status=(
                            "blocked"
                            if record.get("decision") == "blocked"
                            else "completed"
                        ),
                        version=0,
                        manual=0,
                        created=path.stat().st_mtime,
                        updated=path.stat().st_mtime,
                        result=record,
                        suggestion=None,
                        human=None,
                        delivered=None,
                    )
                )
            except (OSError, ValueError):
                continue
        return sorted(current + result, key=lambda r: r["created"], reverse=True)[:500]

    def get(self, path):
        if path == "/platform-update":
            from autopilot.platform_update import status
            return status(self.store.state)
        if path == "/runtime-identity":
            return runtime_identity()
        if path.startswith('/reviews/') and path.endswith('/context'):
            from autopilot.record_context import record_context
            ident = path.split('/')[2]
            return record_context(self.autopilot.ledger, 'reviews', ident, self.get('/reviews/' + ident))
        if path == '/autopilot/workbench-empty':
            return {'sessions': [], 'reviews': [], 'events': []}
        if path.startswith('/products/') and path.endswith('/workbench'):
            product = self.autopilot.ledger.get('products', path.split('/')[2])
            roots = [Path(product['source']).resolve()]
            runs = [r for r in self.autopilot.ledger.list('runs') if r['product_id'] == product['id']]
            roots += [Path(r['workspace']).resolve() for r in runs if r.get('workspace')]
            def belongs(cwd):
                if not cwd:
                    return False
                directory = Path(cwd).resolve()
                return any(directory == root or directory.is_relative_to(root) for root in roots)
            sessions = [s for s in self.sessions() if belongs(s.get('cwd'))]
            session_ids = {s['id'] for s in sessions} | {r['id'] for r in runs}
            reviews = [r for r in self.history() if r['session_id'] in session_ids or belongs(r.get('packet', {}).get('cwd'))]
            review_ids = {r['id'] for r in reviews}
            events = [e for e in self.get('/events') if e.get('session_id') in session_ids or e.get('review_id') in review_ids
                      or e.get('detail', {}).get('product_id') == product['id']]
            return {'sessions': [s | {'product_id':product['id']} for s in sessions],
                    'reviews': [r | {'product_id':product['id'], 'title':r.get('packet', {}).get('summary') or r['id']} for r in reviews],
                    'events': [e | {'product_id':product['id'], 'title':e.get('kind'), 'status':'recorded', 'updated':e.get('at')} for e in events]}
        if self.autopilot.handles(path):
            return self.autopilot.get(path)
        cfg = self.store.settings()
        if path == "/bootstrap":
            return {"csrf": self.csrf}
        if path == "/overview":
            with self.store.connect() as db:
                counts = dict(
                    db.execute(
                        "SELECT status,count(*) FROM reviews GROUP BY status"
                    ).fetchall()
                )
                errors = [
                    dict(r)
                    for r in db.execute(
                        "SELECT id,session_id,result,updated FROM reviews WHERE status='blocked' ORDER BY updated DESC LIMIT 5"
                    )
                ]
            try:
                quota = json.loads((self.store.state / "quota.json").read_text())
            except (OSError, ValueError):
                quota = {}
            day = datetime.datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
            return dict(
                service=self.service_status(),
                connector=self.connector_status(),
                sessions=len(self.sessions()),
                counts=counts,
                quota=dict(
                    used=quota.get("count", 0) if quota.get("day") == day else 0,
                    limit=cfg["max_per_day"],
                    day=day,
                ),
                errors=errors,
            )
        if path == "/sessions":
            return self.sessions()
        if path.startswith("/sessions/"):
            sid = path.split("/")[2]
            item = dict(self.session(sid))
            log = session_log(Path(cfg["home"]).expanduser(), sid)
            self.expire_commands()
            try:
                item["events"] = read_log(log)[-80:] if log else []
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                item["events"] = []
                item["error"] = str(exc)
            with self.store.connect() as db:
                item["commands"] = [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM commands WHERE session_id=? ORDER BY created DESC LIMIT 50",
                        (sid,),
                    )
                ]
            return item
        if path == "/reviewers/codex/account":
            from codex_account import account_status
            return account_status(cfg)
        if path.startswith("/reviewers/") and path.endswith("/models"):
            return reviewers.catalog(cfg, self.store.state, path.split("/")[2])
        if path == "/reviews":
            return self.history()
        if path.startswith("/reviews/"):
            rid = path.split("/")[2]
            row = next((r for r in self.history() if r["id"] == rid), None)
            if not row:
                raise KeyError("审查不存在")
            base = (
                Path(cfg["history_dir"])
                if row["mode"] == "historical"
                else self.store.state
            )
            directory = (base / "reviews" / rid).resolve()
            if not directory.is_relative_to((base / "reviews").resolve()):
                raise ValueError("无效路径")
            row["logs"] = {}
            for filename in ("stderr.log", "trace.jsonl"):
                try:
                    with (directory / filename).open("rb") as stream:
                        stream.seek(0, 2)
                        stream.seek(max(0, stream.tell() - 24000))
                        row["logs"][filename] = stream.read().decode(errors="replace")
                except OSError:
                    row["logs"][filename] = ""
            return row
        if path == "/events":
            with self.store.connect() as db:
                events = [
                    dict(r) | {"detail": json.loads(r["detail"])}
                    for r in db.execute(
                        "SELECT * FROM events ORDER BY id DESC LIMIT 500"
                    )
                ]
            legacy = Path(cfg["history_dir"]) / "events.jsonl"
            if legacy.exists():
                with legacy.open("rb") as stream:
                    stream.seek(0, 2)
                    offset = max(0, stream.tell() - 200000)
                    stream.seek(offset)
                    if offset:
                        stream.readline()
                    for index, line in enumerate(stream):
                        try:
                            old = json.loads(line)
                            events.append(
                                dict(
                                    id=f"legacy-{offset}-{index}",
                                    at=old.get("at", ""),
                                    kind=old.get("kind", "historical"),
                                    session_id=old.get(
                                        "session_id", old.get("session")
                                    ),
                                    review_id=old.get("request_id"),
                                    detail=old,
                                )
                            )
                        except ValueError:
                            continue
            return sorted(events, key=lambda event: event["at"], reverse=True)[:500]
        if path.startswith("/native-gates/"):
            return self.native.get(path.split("/")[2])
        if path == "/settings":
            return cfg | {
                "state_dir": str(self.store.state),
                "connector": self.connector_status(),
            }
        raise KeyError("接口不存在")

    def mutate(self, path, body):
        if path == "/platform-update/release":
            from autopilot.platform_update import request_release
            return request_release(self.store.state, body)
        if self.autopilot.handles(path):
            return self.autopilot.mutate(path, body)
        if path.startswith("/reviewers/") and path.endswith("/models/refresh"):
            cfg = self.store.settings()
            # Preview unsaved executable/profile fields without persisting credentials or settings.
            for key in ("codex_bin", "claude_bin", "harness_bin", "harness_profile"):
                if key in body:
                    if not isinstance(body[key], str) or not body[key].strip():
                        raise ValueError(f"{key} 不能为空")
                    cfg[key] = body[key].strip()
            return reviewers.catalog(cfg, self.store.state, path.split("/")[2], True)
        if path == "/service/start":
            return self.service(True)
        if path == "/service/stop":
            return self.service(False)
        if path == "/settings":
            cfg = self.store.settings()
            for key in ("model", "home", "history_dir", "codex_bin", "claude_bin", "harness_bin", "harness_profile"):
                if key in body:
                    if not isinstance(body[key], str) or not body[key].strip():
                        raise ValueError(f"{key} 不能为空")
                    cfg[key] = body[key].strip()
            for key in ("reviewer_mode", "reviewer_unified", "reviewer_stages"):
                if key in body:
                    cfg[key] = body[key]
            if "model" in body and "reviewer_unified" not in body and cfg["reviewer_unified"]["provider"] == "codex":
                cfg["reviewer_unified"] = {"provider": "codex", "model": cfg["model"]}
            reviewers.validate_config(cfg)
            if cfg["reviewer_unified"]["provider"] == "codex":
                cfg["model"] = cfg["reviewer_unified"]["model"]
            for key, lo, hi in (
                ("review_timeout", 1, 450),
                ("max_per_day", 1, 10000),
                ("bridge_port", 1024, 65535),
            ):
                if key in body:
                    if type(body[key]) is not int or not lo <= body[key] <= hi:
                        raise ValueError(f"{key} 必须为 {lo}–{hi} 的整数")
                    cfg[key] = body[key]
            old = self.store.settings()
            changed = any(cfg[k] != old[k] for k in ("home", "bridge_port"))
            with self.store.connect() as db:
                busy = db.execute(
                    "SELECT count(*) FROM reviews WHERE status IN ('queued','running','awaiting_human')"
                ).fetchone()[0]
                pending = db.execute(
                    "SELECT count(*) FROM commands WHERE status IN ('pending','dispatching')"
                ).fetchone()[0]
            if changed and (busy or pending or self.service_status()["running"]):
                raise Conflict("请先停止监工并等待当前操作结束，再更改会话目录或端口")
            for key in ("home", "history_dir"):
                cfg[key] = str(Path(cfg[key]).expanduser().resolve())
                if not Path(cfg[key]).is_dir():
                    raise ValueError(f"{key} 目录不存在")
            with self.store.connect() as db:
                db.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(cfg),))
            self.cache = (0, [])
            self.store.event(
                "settings_updated",
                detail={"changed": [k for k in cfg if cfg[k] != old.get(k)]},
            )
            return cfg
        parts = path.strip("/").split("/")
        if parts[0] == "native-gates" and len(parts) == 3:
            return self.native.control(parts[1], parts[2], body)
        if parts[0] == "sessions" and len(parts) == 3:
            sid, action = parts[1:]
            session = self.session(sid)
            if session["subagent"] or session["error"]:
                raise ValueError("此会话只读或数据不可用")
            if action == "approval-mode":
                mode = body.get("mode")
                if mode not in ("auto", "manual"):
                    raise ValueError("无效审批模式")
                with self.store.connect() as db:
                    db.execute("INSERT OR REPLACE INTO modes VALUES (?,?)", (sid, mode))
                self.cache = (0, [])
                self.store.event("approval_mode_changed", sid, detail={"mode": mode})
                return {"mode": mode}
            if action in ("messages", "stop"):
                connector = self.connector_status()
                if not connector["online"] or not connector["home_matches"]:
                    raise Conflict("连接插件离线或会话目录不匹配，无法发送")
                content = body.get("text", "")
                if not isinstance(content, str):
                    raise ValueError("指令必须为文本")
                content = content.strip()
                if action == "messages" and (not content or len(content) > 20000):
                    raise ValueError("指令须为 1–20000 字")
                with self.store.connect() as db:
                    rows = db.execute(
                        "SELECT id FROM reviews WHERE session_id=? AND mode='handoff' AND status IN ('queued','running','awaiting_human')",
                        (sid,),
                    ).fetchall()
                for row in rows:
                    try:
                        self.store.cancel(
                            row["id"], "人工干预，原审查停止并保持 blocked"
                        )
                    except Conflict:
                        pass
                # Wait for handoff transport acknowledgement before dispatching new input.
                for row in rows:
                    for _ in range(60):
                        current = self.store.get(row["id"])
                        if current["delivered"] and current["execution_done"]:
                            break
                        time.sleep(0.1)
                    if not (
                        self.store.get(row["id"])["delivered"]
                        and self.store.get(row["id"])["execution_done"]
                    ):
                        raise Conflict(
                            "原交接响应尚未确认送达，已取消审查；请核对会话后重试指令"
                        )
                now = time.time()
                cid = body["operation_id"]
                with self.store.connect() as db:
                    db.execute(
                        "INSERT INTO commands VALUES (?,?,?,?,?,?,?,?)",
                        (
                            cid,
                            sid,
                            "steer" if action == "messages" else "stop",
                            content,
                            "pending",
                            now,
                            now,
                            "",
                        ),
                    )
                self.store.event(
                    "command_submitted", sid, detail={"command_id": cid, "kind": action}
                )
                return {"id": cid, "status": "pending"}
        if path == "/reviews":
            sid = body["session_id"]
            session = self.session(sid)
            if not (
                self.service_status()["running"] and self.service_status()["compatible"]
            ):
                raise Conflict("请先启动新版监工；旧版服务须先通过原 CLI 停止")
            packet = dict(
                session_id=sid,
                cwd=session["cwd"],
                phase=body.get("phase", "checkpoint"),
                scope=body.get("scope", ["."]),
                summary=body.get("summary", ""),
                request_id=body["operation_id"],
            )
            return {"id": self.engine.submit(packet, "observation")}
        if parts[0] == "reviews" and len(parts) == 3:
            rid, action = parts[1:]
            if action == "retry":
                if not (
                    self.service_status()["running"]
                    and self.service_status()["compatible"]
                ):
                    raise Conflict("请先启动新版监工；旧版服务须先通过原 CLI 停止")
                old = self.store.get(rid)
                if old["mode"] == "native_handoff":
                    raise Conflict("插件审查请在对应审批点重试")
                if old["status"] in ACTIVE:
                    raise Conflict("当前审查尚未结束")
                packet = old["packet"] | {"request_id": body["operation_id"]}
                return {"id": self.engine.submit(packet, "observation", parent=rid)}
            if action == "cancel":
                self.store.cancel(rid)
            elif action in ("takeover", "decision"):
                self.store.control(
                    rid,
                    action,
                    body.get("version"),
                    body.get("decision"),
                    body.get("instruction"),
                )
            else:
                raise KeyError("操作不存在")
            return self.store.get(rid)
        raise KeyError("接口不存在")

    def operation(self, path, body):
        if (self.store.state/'autopilot/update-drain.json').exists() and path not in ('/service/stop', '/platform-update/release') and not path.endswith(('/cancel','/pause','/stop')):
            raise Conflict('平台正在更新，暂缓新写入与任务派发')
        oid = body.get("operation_id", "")
        uuid.UUID(oid)
        fingerprint = json.dumps([path, body], sort_keys=True, ensure_ascii=False)
        if path.rstrip('/').endswith(('/git-token', '/intelligence/attachments', '/notifications/configure')):
            # Idempotency must never persist the credential request body.
            fingerprint = 'sha256:' + hashlib.sha256(fingerprint.encode()).hexdigest()
        with self.lock:
            with self.store.connect() as db:
                row = db.execute(
                    "SELECT * FROM operations WHERE id=?", (oid,)
                ).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise Conflict("操作 ID 已被不同请求使用")
                response = json.loads(row["response"])
                if response.get("_pending"):
                    raise Conflict("该操作的结果待核实，请刷新状态；不会自动重复执行")
                return response
            # Reserve before any side effect; a process crash must never duplicate a message.
            with self.store.connect() as db:
                db.execute(
                    "INSERT INTO operations VALUES (?,?,?)",
                    (oid, fingerprint, '{"_pending":true}'),
                )
            try:
                result = self.mutate(path, body)
            except Exception:
                # Keep reservation: callers retry only after checking actual state.
                raise
            with self.store.connect() as db:
                db.execute(
                    "UPDATE operations SET response=? WHERE id=?",
                    (json.dumps(result, ensure_ascii=False), oid),
                )
            return result

    def connector(self, body):
        home = body.get("home")
        if not isinstance(home, str) or not Path(home).is_absolute():
            raise ValueError("插件必须报告绝对会话目录")
        now = time.time()
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO operations VALUES ('connector-capabilities','capabilities',?)", (json.dumps({"native_supervision": body.get("native_supervision") is True}),))
        matches = Path(home).resolve() == Path(self.store.settings()["home"]).resolve()
        with self.store.transaction() as db:
            db.execute("INSERT OR REPLACE INTO connector VALUES (1,?,?)", (home, now))
            for report in body.get("reports", []):
                status = report.get("status")
                if status not in ("accepted", "consumed", "failed", "unknown"):
                    raise ValueError("无效送达状态")
                old = db.execute(
                    "SELECT * FROM commands WHERE id=?", (report.get("id"),)
                ).fetchone()
                if (
                    old
                    and old["status"] in ("dispatching", "unknown", "accepted")
                    and matches
                ):
                    # Do not regress accepted/consumed state due to repeated reports.
                    if old["status"] == "accepted" and status in ("unknown", "failed"):
                        continue
                    db.execute(
                        "UPDATE commands SET status=?,updated=?,detail=? WHERE id=?",
                        (status, now, str(report.get("detail", ""))[:2000], old["id"]),
                    )
                    if old["status"] != status:
                        self.store.event(
                            "command_" + status,
                            old["session_id"],
                            detail={"command_id": old["id"]},
                            db=db,
                        )
            db.execute(
                "UPDATE commands SET status='unknown',detail='连接回执超时，送达待核实' WHERE status='dispatching' AND updated<?",
                (now - 20,),
            )
            db.execute(
                "UPDATE commands SET status='failed',detail='插件未及时接收指令' WHERE status='pending' AND created<?",
                (now - 15,),
            )
            commands = []
            if matches:
                for row in db.execute(
                    "SELECT * FROM commands WHERE status='pending' ORDER BY created LIMIT 10"
                ).fetchall():
                    commands.append(dict(row))
                    db.execute(
                        "UPDATE commands SET status='dispatching',updated=? WHERE id=?",
                        (now, row["id"]),
                    )
        return {
            "commands": commands,
            "home_matches": matches,
            "acknowledged": [
                r["id"]
                for r in body.get("reports", [])
                if matches and r.get("status") in ("consumed", "failed")
            ],
        }


def handler_for(app, port, dist):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(
            self,
            code,
            data,
            content_type="application/json; charset=utf-8",
            cookie=False,
        ):
            if not isinstance(data,bytes) and not self.path.startswith('/api/connector/'):
                data=present_errors(data)
            raw = (
                json.dumps(data, ensure_ascii=False).encode()
                if not isinstance(data, bytes)
                else data
            )
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            if cookie:
                self.send_header(
                    "Set-Cookie",
                    f"dsh_dashboard={app.browser_key}; HttpOnly; SameSite=Strict; Path=/",
                )
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def handle_api(self):
            try:
                if self.headers.get("Host") not in (
                    f"127.0.0.1:{port}",
                    f"localhost:{port}",
                ):
                    self.send(403, {"error": "Host rejected"})
                    return
                path = unquote(urlparse(self.path).path)
                if path in ("/api/connector/poll", "/api/connector/native") and self.command == "POST":
                    if not hmac.compare_digest(
                        self.headers.get("Authorization", ""),
                        "Bearer " + app.connector_key,
                    ):
                        self.send(403, {"error": "插件认证失败"})
                        return
                    self.send(200, app.connector(self.body()) if path.endswith("/poll") else app.native.connector(self.body()))
                    return
                origin = self.headers.get("Origin")
                if origin and origin not in (
                    f"http://127.0.0.1:{port}",
                    f"http://localhost:{port}",
                    "http://127.0.0.1:5173",
                    "http://localhost:5173",
                ):
                    self.send(403, {"error": "Origin rejected"})
                    return
                if path == "/api/runtime-identity" and self.command == "GET":
                    self.send(200, app.get("/runtime-identity"))
                    return
                if path == "/api/bootstrap" and self.command == "GET":
                    # Cross-site fetches cannot mint a local browser session.
                    if self.headers.get("Sec-Fetch-Site") == "cross-site":
                        self.send(403, {"error": "Cross-site request rejected"})
                        return
                    self.send(200, app.get("/bootstrap"), cookie=True)
                    return
                if path.startswith("/api/"):
                    cookies = SimpleCookie(self.headers.get("Cookie", ""))
                    if not cookies.get("dsh_dashboard") or not hmac.compare_digest(
                        cookies["dsh_dashboard"].value, app.browser_key
                    ):
                        self.send(401, {"error": "本地会话已失效，请刷新页面"})
                        return
                    if self.command == "GET":
                        self.send(200, app.get(path[4:] + (("?" + urlparse(self.path).query) if "/intelligence" in path and urlparse(self.path).query else "")))
                        return
                    if not hmac.compare_digest(
                        self.headers.get("X-CSRF-Token", ""), app.csrf
                    ):
                        self.send(403, {"error": "CSRF 校验失败"})
                        return
                    self.send(200, app.operation(path[4:], self.body()))
                    return
                if self.command != "GET":
                    self.send(404, {"error": "接口不存在"})
                    return
                target = (dist / path.lstrip("/")).resolve()
                if not target.is_relative_to(dist.resolve()):
                    self.send(403, {"error": "无效路径"})
                    return
                if not target.is_file():
                    target = dist / "index.html"
                if not target.exists():
                    self.send(
                        503,
                        {"error": "前端尚未构建，请执行 npm install && npm run build"},
                    )
                    return
                self.send(
                    200,
                    target.read_bytes(),
                    mimetypes.guess_type(target.name)[0] or "application/octet-stream",
                )
            except Conflict as exc:
                fields=failure_fields(exc,'管理服务')
                self.send(409, {**fields,"error": fields['reason']})
            except KeyError as exc:
                fields=failure_fields(exc,'管理服务')
                self.send(404, {**fields,"error": fields['reason']})
            except (ValueError, TypeError) as exc:
                fields=failure_fields(exc,'管理服务')
                self.send(400, {**fields,"error": fields['reason']})
            except Exception as exc:
                app.store.event("api_error", detail={"error": str(exc)})
                fields=failure_fields(exc,'管理服务')
                self.send(500, {**fields,"error": fields['reason']})

        def body(self):
            size = int(self.headers.get("Content-Length", "0"))
            limit = 14 * 1024 * 1024 if urlparse(self.path).path.endswith("/intelligence/attachments") else 100000
            if not 0 < size <= limit:
                raise ValueError("请求内容大小无效")
            value = json.loads(self.rfile.read(size))
            if not isinstance(value, dict):
                raise ValueError("需要 JSON 对象")
            return value

        do_GET = handle_api
        do_POST = handle_api
        do_PUT = handle_api

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=13084)
    parser.add_argument(
        "--state-dir",
        default=os.environ.get(
            "DSH_SUPERVISOR_STATE",
            str(
                Path(os.environ.get("DSH_HOME", str(Path.home() / ".dsh")))
                / "supervisor"
            ),
        ),
    )
    args = parser.parse_args()
    state = Path(args.state_dir).expanduser()
    state.mkdir(parents=True, exist_ok=True)
    with (state / "dashboard.lock").open("a") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("该状态目录的管理服务已运行")
        app = Dashboard(state)
        server = ThreadingHTTPServer(
            ("127.0.0.1", args.port),
            handler_for(app, args.port, ROOT / "dashboard/dist"),
        )

        def shutdown(*_):
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        print(f"Dashboard: http://127.0.0.1:{args.port}", flush=True)
        try:
            server.serve_forever()
        finally:
            app.engine.stop()
            if app.bridge and app.bridge.poll() is None:
                app.service(False)
            server.server_close()


if __name__ == "__main__":
    main()
