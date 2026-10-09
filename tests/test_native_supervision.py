"""Native gate protocol against the real review worker with a local fake model."""
import json
import threading
import time
import uuid
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest.mock import patch
import unittest
import test_dashboard as fixtures
from test_dashboard import wait_for
from dashboard_server import Dashboard, ThreadingHTTPServer, handler_for, ROOT
from review_core import Conflict


class NativeTests(unittest.TestCase):
    # Reuse the isolated session and executable fixtures, not their test methods.
    def setUp(self):
        fixtures.Fixture.setUp(self)
        self.app = Dashboard(self.state)
        self.app.service_status = lambda: {"running": True, "compatible": True}
        self.app.service = lambda start: self.app.service_status()
        self.owner = str(uuid.uuid4())

    def tearDown(self):
        self.app.engine.stop()
        fixtures.Fixture.tearDown(self)

    def request(self, action="open", gate_id=None, **extra):
        return self.app.native.connector(dict(action=action, home=str(self.home), owner=self.owner,
            gate_id=gate_id or str(uuid.uuid4()), ready=True,
            packet=dict(session_id=self.sid, phase="plan", summary="测试方案", pause_seq=0), **extra))

    def finished(self, gate):
        wait_for(lambda: self.app.native.get(gate["id"])["review"]["execution_done"])
        return self.app.native.get(gate["id"])

    def test_native_dedup_and_consume(self):
        gate = self.request()
        duplicate = self.request(gate_id=gate["id"])
        self.assertEqual(gate["review_id"], duplicate["review_id"])
        gate = self.finished(gate)
        self.assertTrue(gate["review"]["result"]["pause_proof"]["pause_verified"])
        self.request("ack", gate["id"])
        self.request("ack", gate["id"])
        self.assertEqual(self.app.native.get(gate["id"])["status"], "consumed")
        self.assertEqual(json.loads((self.state / "quota.json").read_text())["count"], 1)

    def test_blocked_release_keeps_failed_verdict_and_audit(self):
        self.app.service_status = lambda: {"running": False, "compatible": True}
        gate = self.request()
        self.assertEqual(gate["status"], "blocked")
        with self.assertRaises(ValueError):
            self.app.native.control(gate["id"], "release", {"review_id": None, "reason": ""})
        released = self.app.native.control(gate["id"], "release", {"review_id": None, "reason": "人工核对"})
        self.assertEqual(released["status"], "released")
        self.assertIsNone(released["review"])
        self.assertEqual(released["override"]["reason"], "人工核对")
        self.request("ack", gate["id"])
        with self.assertRaises(Conflict):
            self.app.native.control(gate["id"], "release", {"review_id": None, "reason": "重复"})
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='native_released'").fetchone()[0], 1)

    def test_quota_blocks_retry_and_release_preserves_result(self):
        cfg = self.store.settings(); cfg["max_per_day"] = 0
        with self.store.connect() as db:
            db.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(cfg),))
        gate = self.finished(self.request())
        self.assertEqual(gate["status"], "blocked")
        old = gate["review"]["result"]
        self.app.native.control(gate["id"], "retry", {"review_id": gate["review_id"], "operation_id": str(uuid.uuid4())})
        retried = self.finished(gate)
        self.assertNotEqual(gate["review_id"], retried["review_id"])
        self.assertEqual(self.store.get(gate["review_id"])["result"], old)
        with self.assertRaises(Conflict):
            self.app.native.control(gate["id"], "release", {"review_id": gate["review_id"], "reason": "过期操作"})
        self.app.native.control(gate["id"], "release", {"review_id": retried["review_id"], "reason": "人工确认"})
        self.assertEqual(self.store.get(retried["review_id"])["result"]["decision"], "blocked")

    def test_cancel_and_lease_loss_prevent_late_approval(self):
        self.fake.with_suffix('.delay').write_text('.4')
        gate = self.request()
        self.request("cancel", gate["id"])
        self.finished(gate)
        with self.assertRaises(Conflict): self.request("ack", gate["id"])
        gate = self.request()
        self.app.native.update(gate["id"], heartbeat=time.time() - 30)
        gate = self.finished(gate)
        self.assertEqual(gate["review"]["result"]["decision"], "blocked")
        self.assertIn("租约", gate["review"]["result"]["summary"])

    def test_background_work_cannot_be_manually_bypassed(self):
        body = dict(action="open", home=str(self.home), owner=self.owner, gate_id=str(uuid.uuid4()), ready=False,
            packet=dict(session_id=self.sid, phase="plan", summary="计划", pause_seq=0))
        gate = self.app.native.connector(body)
        with self.assertRaises(Conflict): self.app.native.control(gate["id"], "release", {"reason": "跳过", "review_id": None})
        self.assertIsNone(gate["review_id"])
        self.request("poll", gate["id"])
        self.assertEqual(self.finished(gate)["review"]["result"]["decision"], "approve")

    def test_native_takeover_and_new_steps_proof(self):
        self.fake.with_suffix('.delay').write_text('.5')
        gate = self.request()
        wait_for(lambda: self.store.get(gate["review_id"])["status"] == 'running')
        row = self.store.get(gate["review_id"])
        self.store.control(row["id"], "takeover", row["version"])
        wait_for(lambda: self.store.get(row["id"])["status"] == 'awaiting_human')
        row = self.store.get(row["id"])
        self.store.control(row["id"], "decision", row["version"], "approve", "人工批准")
        self.assertEqual(self.finished(gate)["review"]["result"]["decision"], "approve")

    def test_native_endpoint_requires_token_and_matching_home(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(self.app, 0, ROOT / 'dashboard/dist'))
        port = server.server_address[1]
        server.RequestHandlerClass = handler_for(self.app, port, ROOT / 'dashboard/dist')
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            body = json.dumps(dict(action="enable", home=str(self.home))).encode()
            url = f'http://127.0.0.1:{port}/api/connector/native'
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(url, data=body, headers={'Content-Type': 'application/json'}))
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
            with urlopen(Request(url, data=body, headers={'Authorization': 'Bearer ' + self.app.connector_key})) as response:
                self.assertTrue(json.load(response)['enabled'])
            with self.assertRaises(Conflict): self.app.native.connector(dict(action='enable', home=str(self.root)))
        finally:
            server.shutdown(); server.server_close(); thread.join()



    def test_lost_owner_is_reclaimed_without_reusing_old_approval(self):
        gate = self.finished(self.request())
        self.app.native.update(gate["id"], heartbeat=time.time() - 30)
        self.owner = str(uuid.uuid4())
        recovered = self.request(gate_id=gate["id"])
        self.assertEqual(recovered["status"], "blocked")
        with self.assertRaises(Conflict): self.request("ack", gate["id"])

    def test_new_activity_after_pause_fails_before_model_call(self):
        with self.log.open('a') as stream:
            stream.write(json.dumps({"seq": 1, "type": "step/start", "data": {"step": 1, "turn": 1}}) + '\n')
        gate = self.finished(self.request())
        self.assertEqual(gate["status"], "blocked")
        self.assertIn("新的执行活动", gate["review"]["result"]["summary"])
        self.assertFalse((self.state / "quota.json").exists())

    def test_concurrent_open_creates_only_one_review(self):
        import concurrent.futures
        gate_id = str(uuid.uuid4())
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda _: self.request(gate_id=gate_id), range(4)))
        self.assertEqual({r["review_id"] for r in replies}, {gate_id})
        self.finished(replies[0])
        self.assertEqual(json.loads((self.state / "quota.json").read_text())["count"], 1)

    def test_cancellation_before_open_is_durable(self):
        gate_id = str(uuid.uuid4())
        result = self.request("cancel", gate_id)
        self.assertEqual(result["status"], "cancelled")
        result = self.request("open", gate_id)
        self.assertEqual(result["status"], "cancelled")
        self.assertIsNone(result["review"])
        self.assertFalse((self.state / "quota.json").exists())
