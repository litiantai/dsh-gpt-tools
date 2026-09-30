"""Behavioral tests use isolated sessions and a deterministic local review executable."""

import concurrent.futures
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import dashboard_server as dashboard
from review_core import Engine, Store, Conflict, inventory, validate_result


def wait_for(condition, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = condition()
        if result:
            return result
        time.sleep(0.04)
    raise AssertionError("Timed out waiting for condition")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "source.txt").write_text("unchanged")
        self.sid = "session-dashboard-test"
        self.log = self.home / "sessions" / "workspace" / self.sid / "session.jsonl"
        self.log.parent.mkdir(parents=True)
        self.log.write_text(
            json.dumps(
                {
                    "type": "session/header",
                    "id": self.sid,
                    "cwd": str(self.project),
                    "seq": 0,
                }
            )
            + "\n"
        )
        self.fake = self.root / "fake-codex"
        self.fake.write_text(f"""#!{sys.executable}
import json,sys,time
from pathlib import Path
prompt=sys.stdin.read()
time.sleep(float(Path(__file__).with_suffix('.delay').read_text()))
result={{"decision":"approve","summary":"已检查","instruction":"继续下一步","checks":["test passed"],"issues":[]}}
Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps(result))
""")
        self.fake.chmod(0o700)
        self.fake.with_suffix(".delay").write_text(".05")
        self.store = Store(
            self.state,
            dict(
                home=str(self.home),
                codex_bin=str(self.fake),
                review_timeout=3,
                handoff_timeout=5,
            ),
        )
        self.engine = Engine(self.store)

    def tearDown(self):
        self.engine.stop()
        self.tmp.cleanup()

    def packet(self):
        return dict(
            request_id=str(uuid.uuid4()),
            session_id=self.sid,
            cwd=str(self.project),
            phase="plan",
            scope=["source.txt"],
            summary="核对测试文件",
        )

    def complete(self, rid):
        return wait_for(
            lambda: (
                self.store.get(rid)
                if self.store.get(rid)["status"]
                not in ("queued", "running", "awaiting_human")
                else None
            )
        )

    def test_automatic_handoff_cached_and_conflict(self):
        packet = self.packet()
        rid = self.engine.submit(packet)
        row = self.complete(rid)
        self.assertEqual(row["result"]["decision"], "approve")
        self.assertTrue(row["result"]["pause_proof"]["pause_verified"])
        self.assertEqual(self.engine.submit(packet), rid)
        self.assertEqual(
            json.loads((self.state / "quota.json").read_text())["count"], 1
        )
        with self.assertRaises(Conflict):
            self.engine.submit(packet | {"summary": "different"})

    def test_manual_approval_and_stale_version(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO modes VALUES (?,?)", (self.sid, "manual"))
        rid = self.engine.submit(self.packet())
        row = wait_for(
            lambda: (
                self.store.get(rid)
                if self.store.get(rid)["status"] == "awaiting_human"
                else None
            )
        )
        with self.assertRaises(Conflict):
            self.store.control(rid, "decision", row["version"] - 1, "approve", "继续")
        self.store.control(rid, "decision", row["version"], "revise", "请修改测试")
        self.assertEqual(self.complete(rid)["result"]["decision"], "revise")
        with self.assertRaises(Conflict):
            self.store.control(rid, "decision", row["version"], "approve", "继续")

    def test_takeover_wins_before_completion(self):
        self.fake.with_suffix(".delay").write_text(".6")
        rid = self.engine.submit(self.packet())
        row = wait_for(
            lambda: (
                self.store.get(rid)
                if self.store.get(rid)["status"] == "running"
                else None
            )
        )
        self.store.control(rid, "takeover", row["version"])
        wait_for(lambda: self.store.get(rid)["status"] == "awaiting_human")
        self.assertIsNone(self.store.get(rid)["result"])
        self.store.cancel(rid)
        self.assertEqual(self.complete(rid)["result"]["decision"], "blocked")

    def test_manual_rechecks_source_changes(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO modes VALUES (?,?)", (self.sid, "manual"))
        rid = self.engine.submit(self.packet())
        row = wait_for(
            lambda: (
                self.store.get(rid)
                if self.store.get(rid)["status"] == "awaiting_human"
                else None
            )
        )
        (self.project / "source.txt").write_text("changed while waiting")
        self.store.control(rid, "decision", row["version"], "approve", "继续")
        result = self.complete(rid)["result"]
        self.assertEqual(result["decision"], "blocked")
        self.assertFalse(result["pause_proof"]["pause_verified"])

    def test_new_steps_fail_pause_proof(self):
        self.fake.with_suffix(".delay").write_text(".4")
        rid = self.engine.submit(self.packet())
        wait_for(lambda: (self.state / "reviews" / rid / "before.json").exists())
        with self.log.open("a") as log:
            log.write(json.dumps({"seq": 1, "type": "step/start"}) + "\n")
        self.assertEqual(self.complete(rid)["result"]["decision"], "blocked")

    def test_observation_never_claims_pause(self):
        rid = self.engine.submit(self.packet(), "observation")
        result = self.complete(rid)["result"]
        self.assertFalse(result["pause_proof"]["pause_verified"])
        self.assertTrue(result["pause_proof"]["observation"])

    def test_cancel_and_timeout(self):
        self.fake.with_suffix(".delay").write_text("5")
        rid = self.engine.submit(self.packet())
        wait_for(lambda: self.store.get(rid)["status"] == "running")
        self.store.cancel(rid)
        self.assertEqual(self.complete(rid)["status"], "cancelled")
        self.engine.stop()
        cfg = self.store.settings() | {"review_timeout": 0.1}
        with self.store.connect() as db:
            db.execute("UPDATE settings SET value=?", (json.dumps(cfg),))
        rid = self.engine.submit(self.packet())
        self.assertIn("超时", self.complete(rid)["result"]["summary"])

    def test_manual_deadline(self):
        cfg = self.store.settings() | {"handoff_timeout": 0.5}
        with self.store.connect() as db:
            db.execute("UPDATE settings SET value=?", (json.dumps(cfg),))
            db.execute("INSERT INTO modes VALUES (?,?)", (self.sid, "manual"))
        rid = self.engine.submit(self.packet())
        self.assertEqual(self.complete(rid)["status"], "blocked")

    def test_quota_not_bypassed_by_retry(self):
        cfg = self.store.settings() | {"max_per_day": 1}
        with self.store.connect() as db:
            db.execute("UPDATE settings SET value=?", (json.dumps(cfg),))
        self.complete(self.engine.submit(self.packet()))
        result = self.complete(self.engine.submit(self.packet()))["result"]
        self.assertIn("额度", result["summary"])

    def test_recovery_keeps_terminal_results(self):
        rid = self.engine.submit(self.packet())
        completed = self.complete(rid)
        packet = self.packet()
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO reviews(id,session_id,packet,mode,status,created,updated,deadline,owner) VALUES (?,?,?,'handoff','running',?,?,?,?)",
                (
                    packet["request_id"],
                    self.sid,
                    json.dumps(packet),
                    time.time(),
                    time.time(),
                    time.time() + 10,
                    99999999,
                ),
            )
        self.store.recover()
        self.assertEqual(self.store.get(rid)["result"], completed["result"])
        self.assertEqual(self.store.get(packet["request_id"])["status"], "blocked")

    def test_scope_escape_and_invalid_phase(self):
        for extra in ({"scope": ["../"]}, {"phase": "invalid"}):
            with self.assertRaises(ValueError):
                self.engine.submit(self.packet() | extra)
        with self.assertRaises(ValueError):
            validate_result(
                dict(decision="done", summary="", instruction="", checks=[], issues=[]),
                "plan",
            )

    def test_connector_delivery_no_duplicate_and_offline(self):
        app = dashboard.Dashboard(self.state)
        oid = str(uuid.uuid4())
        with self.assertRaises(Conflict):
            app.mutate(
                f"/sessions/{self.sid}/messages", dict(operation_id=oid, text="hello")
            )
        app.connector({"home": str(self.home), "reports": []})
        body = dict(operation_id=oid, text="hello")
        first = app.operation(f"/sessions/{self.sid}/messages", body)
        self.assertEqual(first, app.operation(f"/sessions/{self.sid}/messages", body))
        claimed = app.connector({"home": str(self.home), "reports": []})["commands"]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(
            app.connector({"home": str(self.home), "reports": []})["commands"], []
        )
        app.connector(
            {"home": str(self.home), "reports": [{"id": oid, "status": "accepted"}]}
        )
        app.connector(
            {"home": str(self.home), "reports": [{"id": oid, "status": "unknown"}]}
        )
        with self.store.connect() as db:
            self.assertEqual(
                db.execute("SELECT status FROM commands WHERE id=?", (oid,)).fetchone()[
                    0
                ],
                "accepted",
            )
        app.connector(
            {"home": str(self.home), "reports": [{"id": oid, "status": "consumed"}]}
        )
        with self.store.connect() as db:
            self.assertEqual(
                db.execute("SELECT status FROM commands WHERE id=?", (oid,)).fetchone()[
                    0
                ],
                "consumed",
            )
        with self.assertRaises(Conflict):
            app.operation(
                f"/sessions/{self.sid}/messages", body | {"text": "different"}
            )

    def test_handoff_cancelled_before_steer(self):
        self.fake.with_suffix(".delay").write_text("3")
        rid = self.engine.submit(self.packet())
        app = dashboard.Dashboard(self.state)
        app.connector({"home": str(self.home)})

        def acknowledge():
            wait_for(lambda: self.store.get(rid)["status"] == "cancelled")
            with self.store.connect() as db:
                db.execute("UPDATE reviews SET delivered=1 WHERE id=?", (rid,))

        thread = threading.Thread(target=acknowledge)
        thread.start()
        app.operation(
            f"/sessions/{self.sid}/messages",
            dict(operation_id=str(uuid.uuid4()), text="新指令"),
        )
        thread.join()
        self.assertEqual(self.store.get(rid)["result"]["decision"], "blocked")
        self.assertEqual(len(app.connector({"home": str(self.home)})["commands"]), 1)

    def test_source_directory_mismatch(self):
        app = dashboard.Dashboard(self.state)
        app.connector({"home": str(self.root)})
        with self.assertRaises(Conflict):
            app.mutate(
                f"/sessions/{self.sid}/messages",
                dict(operation_id=str(uuid.uuid4()), text="hello"),
            )

    def test_session_inventory_and_corrupt_log(self):
        self.assertEqual(inventory(self.store)[0]["id"], self.sid)
        self.log.write_text("broken")
        self.assertEqual(inventory(self.store)[0]["status"], "unavailable")

    def test_subagent_readonly_and_fork_is_regular(self):
        header = json.loads(self.log.read_text().splitlines()[0])
        header.update(origin="subagent", delegationDepth=1)
        self.log.write_text(json.dumps(header) + "\n")
        self.assertTrue(inventory(self.store)[0]["subagent"])
        with self.assertRaises(ValueError):
            self.engine.submit(self.packet())
        header.pop("origin")
        header["delegationDepth"] = 0
        header["parentSession"] = "ordinary-fork"
        self.log.write_text(json.dumps(header) + "\n")
        self.assertFalse(inventory(self.store)[0]["subagent"])

    def test_cached_legacy_result_never_runs_model(self):
        packet = self.packet()
        dest = self.state / "reviews" / packet["request_id"]
        dest.mkdir(parents=True)
        (dest / "request.json").write_text(json.dumps(packet))
        result = dict(
            decision="blocked",
            summary="legacy",
            instruction="停止",
            checks=[],
            issues=[],
        )
        (dest / "result.json").write_text(json.dumps(result))
        rid = self.engine.submit(packet)
        self.assertEqual(self.store.get(rid)["result"], result)
        self.assertFalse((self.state / "quota.json").exists())

    def test_partial_trailing_event_keeps_session_visible(self):
        with self.log.open("a") as stream:
            stream.write('{"type":')
        self.assertEqual(inventory(self.store)[0]["id"], self.sid)
        self.assertIsNone(inventory(self.store)[0]["error"])

    def test_lost_connector_ack_expires_without_redelivery(self):
        app = dashboard.Dashboard(self.state)
        app.connector({"home": str(self.home)})
        oid = str(uuid.uuid4())
        app.operation(
            f"/sessions/{self.sid}/messages", dict(operation_id=oid, text="test")
        )
        self.assertEqual(len(app.connector({"home": str(self.home)})["commands"]), 1)
        with self.store.connect() as db:
            db.execute("UPDATE commands SET updated=?", (time.time() - 30,))
        app.connector_status()
        with self.store.connect() as db:
            self.assertEqual(
                db.execute("SELECT status FROM commands").fetchone()[0], "unknown"
            )
        self.assertEqual(app.connector({"home": str(self.home)})["commands"], [])

    def test_http_auth_csrf_and_operation(self):
        app = dashboard.Dashboard(self.state)
        server = dashboard.ThreadingHTTPServer(
            ("127.0.0.1", 0), dashboard.handler_for(app, 0, ROOT / "dashboard/dist")
        )
        port = server.server_address[1]
        server.RequestHandlerClass = dashboard.handler_for(
            app, port, ROOT / "dashboard/dist"
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{port}"
        try:
            with self.assertRaises(HTTPError) as caught:
                urlopen(origin + "/api/overview")
            self.assertEqual(caught.exception.code, 401)
            caught.exception.close()
            with urlopen(origin + "/api/bootstrap") as response:
                csrf = json.load(response)["csrf"]
                cookie = response.headers["Set-Cookie"].split(";")[0]
            with urlopen(
                Request(origin + "/api/overview", headers={"Cookie": cookie})
            ) as response:
                self.assertEqual(json.load(response)["sessions"], 1)
            request = Request(
                origin + "/api/service/stop",
                data=json.dumps({"operation_id": str(uuid.uuid4())}).encode(),
                headers={
                    "Cookie": cookie,
                    "Origin": "https://untrusted.example",
                    "X-CSRF-Token": csrf,
                },
            )
            with self.assertRaises(HTTPError) as caught:
                urlopen(request)
            self.assertEqual(caught.exception.code, 403)
            caught.exception.close()
            with self.assertRaises(HTTPError) as caught:
                urlopen(
                    Request(
                        origin + "/api/service/stop",
                        data=b"{}",
                        headers={"Cookie": cookie},
                    )
                )
            self.assertEqual(caught.exception.code, 403)
            caught.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_bridge_http_real_transport(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        proc = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "dsh-gpt-supervisor/scripts/bridge.py"),
                "run",
                "--state-dir",
                str(self.state),
                "--port",
                str(port),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            wait_for(lambda: (self.state / "pid").exists())
            key = (self.state / "bridge.key").read_text()
            packet = self.packet()
            request = Request(
                f"http://127.0.0.1:{port}/handoff",
                data=json.dumps(packet).encode(),
                headers={"Authorization": "Bearer " + key},
            )
            with urlopen(request, timeout=8) as response:
                result = json.load(response)
            self.assertEqual(result["decision"], "approve")
            wait_for(lambda: self.store.get(packet["request_id"])["delivered"])
        finally:
            proc.terminate()
            proc.communicate(timeout=8)


if __name__ == "__main__":
    unittest.main()
