"""运行身份必须声明版本化的仓库扫描能力，供前端在调用 /api/scans 前核对。"""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import dashboard_server as dashboard


class RuntimeIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"

    def tearDown(self):
        self.tmp.cleanup()

    def test_identity_declares_versioned_scan_capability(self):
        identity = dashboard.runtime_identity(self.root)
        self.assertEqual(identity["capabilities"]["repository_scans"], 1)
        self.assertEqual(identity["capabilities"]["repository_scans"], dashboard.REPOSITORY_SCAN_API_VERSION)
        self.assertIsNone(identity["product_id"])
        self.assertIsNone(identity["commit"])
        self.assertIsNone(identity["release_id"])

    def test_identity_preserves_marker_fields_and_overrides_stale_capability(self):
        (self.root / "autopilot-release.json").write_text(
            json.dumps(
                {
                    "product_id": "p1",
                    "commit": "abc123",
                    "release_id": "r1",
                    "capabilities": {"repository_scans": 0, "other": True},
                }
            )
        )
        identity = dashboard.runtime_identity(self.root)
        self.assertEqual(identity["product_id"], "p1")
        self.assertEqual(identity["commit"], "abc123")
        self.assertEqual(identity["release_id"], "r1")
        self.assertEqual(identity["capabilities"]["repository_scans"], 1)
        self.assertTrue(identity["capabilities"]["other"])

    def test_http_runtime_identity_publishes_probe_value(self):
        self.assertTrue(dashboard.Control.handles("scans"))
        self.assertEqual(dashboard.REPOSITORY_SCAN_API_VERSION, 1)
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
        try:
            with urlopen(
                f"http://127.0.0.1:{port}/api/runtime-identity"
            ) as response:
                self.assertEqual(response.status, 200)
                payload = json.load(response)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual(payload["capabilities"]["repository_scans"], 1)
        self.assertIsInstance(payload["capabilities"]["repository_scans"], int)


if __name__ == "__main__":
    unittest.main()
