"""Verify packaged skill authentication and conservative write behavior."""

import argparse
import contextlib
import importlib.util
import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

SPEC = importlib.util.spec_from_file_location(
    "skill_control",
    Path(__file__).resolve().parents[1]
    / "skills/dsh-supervisor-use/scripts/control.py",
)
control = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(control)
OPERATION = "cdf8505c-e5a8-4a93-8f5d-ccbd1a0d3a69"


class ControlTests(unittest.TestCase):
    def test_reject_nonlocal_origins(self):
        for origin in [
            "https://example.com:443",
            "http://remote:13084",
            "http://user@localhost:13084",
            "http://localhost:13084/path",
        ]:
            with self.subTest(origin=origin), self.assertRaises(
                argparse.ArgumentTypeError
            ):
                control.local_origin(origin)

    def test_cookie_csrf_and_put(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Set-Cookie", "dsh_dashboard=test; Path=/; HttpOnly")
                self.end_headers()
                self.wfile.write(b'{"csrf":"private-test-token"}')

            def do_PUT(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append((self.path, dict(self.headers), body))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"saved":true}')

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            origin = f"http://127.0.0.1:{server.server_port}"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = control.main(
                    [
                        "--origin",
                        origin,
                        "act",
                        "/settings",
                        "--operation-id",
                        OPERATION,
                    ]
                )
            self.assertEqual(result, 0)
            self.assertNotIn("private-test-token", stdout.getvalue())
            self.assertEqual(len(received), 1)
            path, headers, body = received[0]
            self.assertEqual(path, "/api/settings")
            self.assertEqual(headers["Cookie"], "dsh_dashboard=test")
            self.assertEqual(headers["X-Csrf-Token"], "private-test-token")
            self.assertEqual(headers["Origin"], origin)
            self.assertEqual(body["operation_id"], OPERATION)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_uncertain_write_is_not_retried(self):
        stderr = io.StringIO()
        with patch.object(
            control, "call", side_effect=URLError("lost receipt")
        ) as call, contextlib.redirect_stderr(stderr):
            result = control.main(
                ["act", "/service/start", "--operation-id", OPERATION]
            )
        self.assertEqual(result, 1)
        self.assertEqual(call.call_count, 1)
        output = json.loads(stderr.getvalue())
        self.assertTrue(output["outcome_unknown"])
        self.assertEqual(output["operation_id"], OPERATION)

    def test_operation_id_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            control.main(["act", "/service/start"])


if __name__ == "__main__":
    unittest.main()
