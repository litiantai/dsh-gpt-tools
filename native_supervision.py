"""Durable native-plugin gates. A release is separate from the review verdict."""
import getpass
import json
import time
import uuid
from pathlib import Path
from review_core import ACTIVE, Conflict, stamp


class NativeSupervision:
    def __init__(self, app):
        self.app = app
        self.store = app.store

    def get(self, gate_id):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM native_gates WHERE id=?", (gate_id,)).fetchone()
        if not row:
            with self.store.connect() as db:
                cancelled = db.execute("SELECT id FROM native_cancellations WHERE id=?", (gate_id,)).fetchone()
            if cancelled:
                return dict(id=gate_id, status="cancelled", reason="原任务已取消", online=False, ready=0,
                            packet={}, review=None, owner=None, session_id=None, review_id=None, override=None)
            raise KeyError("监管审批点不存在")
        gate = dict(row)
        for key in ("packet", "override"):
            gate[key] = json.loads(gate[key]) if gate[key] else None
        try:
            review = self.store.get(gate["review_id"]) if gate["review_id"] else None
        except KeyError:
            review = None
            gate["status"] = "blocked"
            gate["reason"] = "审查提交中断，请重试"
        gate["review"] = review
        gate["online"] = time.time() - gate["heartbeat"] < 15
        if gate["status"] not in ("cancelled", "consumed") and review:
            if review["status"] in ACTIVE:
                gate["status"] = "waiting"
            elif review["result"] and review["result"]["decision"] == "blocked":
                gate["status"] = "blocked"
                gate["reason"] = review["result"]["summary"]
        if gate["override"] and gate["status"] not in ("cancelled", "consumed"):
            gate["status"] = "released"
        return gate

    def update(self, gate_id, **values):
        with self.store.connect() as db:
            db.execute("UPDATE native_gates SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?", (*values.values(), gate_id))

    def submit(self, gate_id, request_id):
        gate = self.get(gate_id)
        status = self.app.service_status()
        if not status.get("running") or not status.get("compatible"):
            self.update(gate_id, status="blocked", reason="审查服务未运行；启动服务后重试，或人工放行当前审批点")
            return
        packet = gate["packet"] | {"request_id": request_id, "native_gate_id": gate_id}
        self.update(gate_id, review_id=request_id, status="waiting", reason="")
        try:
            self.app.engine.submit(packet, "native_handoff")
        except Exception as exc:
            # A durable review may exist even when its worker failed to start.
            try:
                self.store.get(request_id)
            except KeyError:
                self.update(gate_id, review_id=None)
            self.update(gate_id, status="blocked", reason=str(exc))

    def connector(self, body):
        if Path(body.get("home", "")).resolve() != Path(self.store.settings()["home"]).resolve():
            raise Conflict("插件会话目录不匹配")
        action = body.get("action")
        if action == "enable":
            self.app.service(True)
            return {"enabled": True, "protocol": "native-v1"}
        sid = body.get("session_id")
        if action == "state":
            session = self.app.session(sid)
            if session["subagent"]:
                raise ValueError("子代理不单独监管")
            state = body.get("state")
            if state not in ("planning", "developing", "waiting", "blocked", "verified", "manual_released", "cancelled"):
                raise ValueError("无效监管状态")
            with self.store.connect() as db:
                db.execute("INSERT OR REPLACE INTO native_sessions VALUES (?,?,?,?,?)", (sid, state, body.get("reason", "")[:2000], body.get("gate_id"), time.time()))
            self.app.cache = (0, [])
            return {"ok": True}
        gate_id = body["gate_id"]
        uuid.UUID(gate_id)
        owner = body["owner"]
        uuid.UUID(owner)
        with self.app.lock:
            if action == "open":
                with self.store.connect() as db:
                    if db.execute("SELECT id FROM native_cancellations WHERE id=?", (gate_id,)).fetchone():
                        return self.get(gate_id)
                packet = body["packet"]
                session = self.app.session(packet["session_id"])
                if session["subagent"] or session.get("error"):
                    raise ValueError("此会话不能进入监管")
                if packet.get("phase") not in ("plan", "checkpoint", "acceptance"):
                    raise ValueError("无效阶段")
                if not isinstance(packet.get("summary"), str) or len(packet["summary"]) > 20000:
                    raise ValueError("摘要最长 20000 字")
                if type(packet.get("pause_seq")) is not int or packet["pause_seq"] < 0:
                    raise ValueError("暂停位置必须为非负事件序号")
                packet = dict(session_id=session["id"], cwd=session["cwd"], scope=["."], phase=packet["phase"], summary=packet["summary"], pause_seq=packet["pause_seq"])
                with self.store.transaction() as db:
                    old = db.execute("SELECT * FROM native_gates WHERE id=?", (gate_id,)).fetchone()
                    if old:
                        if json.loads(old["packet"]) != packet:
                            raise Conflict("审批点 ID 已用于不同请求")
                        if old["owner"] != owner and time.time() - old["heartbeat"] < 15:
                            raise Conflict("审批点仍由另一插件实例持有")
                        db.execute("UPDATE native_gates SET owner=?,heartbeat=?,ready=? WHERE id=?", (owner, time.time(), int(body.get("ready") is True), gate_id))
                    else:
                        pending = db.execute("SELECT id FROM native_gates WHERE session_id=? AND status NOT IN ('consumed','cancelled')", (session["id"],)).fetchone()
                        if pending:
                            raise Conflict("此会话仍有未消费的审批点")
                        db.execute("INSERT INTO native_gates(id,session_id,packet,status,reason,owner,heartbeat,ready,created) VALUES (?,?,?,?,?,?,?,?,?)", (gate_id, session["id"], json.dumps(packet, ensure_ascii=False), "blocked" if body.get("ready") is True else "waiting_background", body.get("reason", ""), owner, time.time(), int(body.get("ready") is True), time.time()))
                if old and old["owner"] != owner and old["status"] not in ("consumed", "cancelled"):
                    previous = self.get(gate_id)
                    if previous["review"] and previous["review"]["status"] in ACTIVE:
                        self.store.cancel(previous["review_id"], "插件恢复后必须重新验证暂停状态")
                    self.update(gate_id, status="blocked", override=None, reason="插件恢复后，请重新审查当前审批点")
                if not old and body.get("ready") is True and not body.get("reason"):
                    self.submit(gate_id, gate_id)
                elif not old:
                    self.update(gate_id, reason=body.get("reason") or "后台任务尚未结束")
            else:
                if action == "cancel":
                    with self.store.connect() as db:
                        exists = db.execute("SELECT id FROM native_gates WHERE id=?", (gate_id,)).fetchone()
                        if not exists:
                            db.execute("INSERT OR IGNORE INTO native_cancellations VALUES (?,?)", (gate_id, time.time()))
                            return dict(id=gate_id, status="cancelled", reason="原任务已取消", review=None)
                gate = self.get(gate_id)
                if gate["owner"] != owner and not (action == "cancel" and not gate["online"]):
                    raise Conflict("插件审批点持有者已改变")
                if action == "poll":
                    self.update(gate_id, heartbeat=time.time(), ready=int(body.get("ready") is True))
                    if gate["status"] == "waiting_background" and body.get("ready") is True:
                        self.submit(gate_id, gate_id)
                    if not body.get("ready") and gate["review"] and gate["review"]["status"] in ACTIVE:
                        self.store.cancel(gate["review_id"], "后台活动未停止，监管继续阻塞")
                elif action == "cancel":
                    if gate["status"] == "consumed":
                        return gate
                    if gate["review"] and gate["review"]["status"] in ACTIVE:
                        self.store.cancel(gate["review_id"], "原任务取消或监管插件卸载")
                    self.update(gate_id, status="cancelled", reason="原任务取消或监管插件卸载", override=None)
                    self.store.event("native_cancelled", gate["session_id"], gate["review_id"])
                elif action == "hold":
                    if gate["status"] in ("consumed", "cancelled"):
                        raise Conflict("审批点已结束")
                    if gate["review"] and not gate["review"]["execution_done"]:
                        raise Conflict("审查进程尚未结束")
                    self.update(gate_id, status="blocked", reason="同一阶段已连续返修 3 次，请人工处理后重试或放行")
                elif action == "ack":
                    if gate["status"] == "consumed":
                        return gate
                    if gate["status"] == "cancelled":
                        raise Conflict("审批点已取消")
                    review = gate["review"]
                    if review and not review["execution_done"]:
                        raise Conflict("审查进程尚未结束")
                    if gate["status"] == "blocked" or (not gate["override"] and not (review and review["result"] and review["result"]["decision"] != "blocked")):
                        raise Conflict("审批点尚未通过")
                    if not gate["ready"]:
                        raise Conflict("后台活动未结束")
                    self.update(gate_id, status="consumed")
                    if review:
                        with self.store.connect() as db:
                            db.execute("UPDATE reviews SET delivered=1 WHERE id=?", (review["id"],))
                    self.store.event("native_consumed", gate["session_id"], gate["review_id"])
                else:
                    raise ValueError("未知插件监管操作")
            return self.get(gate_id)

    def control(self, gate_id, action, body):
        gate = self.get(gate_id)
        review = gate["review"]
        if gate["status"] != "blocked" or not gate["online"]:
            raise Conflict("仅能处理在线插件持有的阻塞审批点")
        if not gate["ready"]:
            raise Conflict("后台任务或子代理未结束；请先停止或等待它们结束")
        if review and not review["execution_done"]:
            raise Conflict("审查进程尚未停止，请稍后重试")
        if body.get("review_id") != gate["review_id"]:
            raise Conflict("审查轮次已改变，请刷新")
        if action == "retry":
            self.app.service(True)
            self.submit(gate_id, body["operation_id"])
        elif action == "release":
            reason = body.get("reason", "")
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
                raise ValueError("请填写人工放行原因（最多 2000 字）")
            override = dict(reason=reason.strip(), at=stamp(), actor=f"本机管理会话 ({getpass.getuser()})", gate_id=gate_id, review_id=gate["review_id"])
            self.update(gate_id, override=json.dumps(override, ensure_ascii=False), status="released")
            self.store.event("native_released", gate["session_id"], gate["review_id"], override)
        else:
            raise ValueError("未知监管操作")
        return self.get(gate_id)
