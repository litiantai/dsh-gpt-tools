"""Isolated integration server. Never uses real Harness sessions or model calls."""

import json
import os
import socket
import sys
import signal
import threading
import subprocess
from pathlib import Path
from test_dashboard import Fixture, ROOT

sys.path.insert(0, str(ROOT))
from dashboard_server import Dashboard, ThreadingHTTPServer, handler_for

fixture = Fixture()
port = int(os.environ.get('DSH_E2E_PORT', '13085'))
origin = f'http://127.0.0.1:{port}'
fixture.setUp()
app = Dashboard(fixture.state)
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    bridge_port = sock.getsockname()[1]
cfg = app.store.settings() | {"bridge_port": bridge_port, "handoff_timeout": 20}
with app.store.connect() as db:
    db.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(cfg),))
with fixture.log.open("a") as stream:
    stream.write(
        json.dumps(
            {"seq": 1, "type": "session/title", "data": {"title": "看板集成测试会话"}}
        )
        + "\n"
    )
meta = ROOT / ".dashboard/e2e.json"
meta.parent.mkdir(exist_ok=True)
meta.write_text(
    json.dumps(
        dict(
            state=str(fixture.state),
            bridge_port=bridge_port,
            home=str(fixture.home),
            project=str(fixture.project),
            session=fixture.sid,
            origin=origin,
        )
    )
)
connector = subprocess.Popen(
    ["node", str(ROOT / "tests/connector-fixture.mjs"), str(meta)]
)
server = ThreadingHTTPServer(
    ("127.0.0.1", port), handler_for(app, port, ROOT / "dashboard/dist")
)


def shutdown(*_):
    threading.Thread(target=server.shutdown, daemon=True).start()


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)
print(f"Integration fixture: {origin}", flush=True)
try:
    server.serve_forever()
finally:
    connector.terminate()
    connector.wait(timeout=5)
    if app.bridge:
        app.service(False)
    server.server_close()
    fixture.tearDown()
