#!/usr/bin/env python3
"""Local blocking DeepSeek ↔ GPT bridge, sharing durable dashboard controls."""

from __future__ import annotations
import argparse, hmac, json, os, shutil, signal, subprocess, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from review_core import (
    Store,
    Engine,
    Conflict,
    alive,
    atomic_json,
    private_key,
    selected_logs,
    stamp,
    validate_scope,
)


def run_server(a):
    store = Store(
        a.state_dir,
        dict(
            home=a.home,
            model=a.model,
            review_timeout=a.review_timeout,
            max_per_day=a.max_per_day,
            codex_bin=a.codex_bin,
            bridge_port=a.port,
        ),
    )
    overrides = {}
    for flag, key in (
        ("--home", "home"),
        ("--model", "model"),
        ("--review-timeout", "review_timeout"),
        ("--max-per-day", "max_per_day"),
        ("--codex-bin", "codex_bin"),
        ("--port", "bridge_port"),
    ):
        if flag in sys.argv:
            overrides[key] = getattr(a, flag[2:].replace("-", "_"))
    if overrides:
        with store.connect() as db:
            db.execute(
                "UPDATE settings SET value=? WHERE id=1",
                (json.dumps(store.settings() | overrides),),
            )
    store.recover()
    state = store.state
    token = private_key(state / "bridge.key")
    engine = Engine(store)
    stopping = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, obj):
            data = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
                return True
            except (BrokenPipeError, ConnectionResetError):
                return False

        def do_GET(self):
            self.reply(
                200,
                dict(
                    ok=True,
                    protocol="dashboard-v1",
                    pid=os.getpid(),
                    mode="blocking-handoff",
                    observed_sessions=len(
                        selected_logs(Path(store.settings()["home"]).expanduser())
                    ),
                ),
            )

        def do_POST(self):
            if self.path != "/handoff" or not hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + token
            ):
                self.reply(403, {"error": "unauthorized"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 100000:
                    raise ValueError("invalid body size")
                packet = json.loads(self.rfile.read(size))
                rid = engine.submit(packet)
                while True:
                    engine.live(rid)
                    row = store.get(rid)
                    if row["result"] is not None:
                        if self.reply(200, row["result"]):
                            with store.connect() as db:
                                db.execute(
                                    "UPDATE reviews SET delivered=1 WHERE id=?", (rid,)
                                )
                            store.event("response_delivered", row["session_id"], rid)
                        return
                    time.sleep(0.1)
            except Conflict as exc:
                self.reply(409, {"error": str(exc)})
            except (ValueError, KeyError, TypeError) as exc:
                self.reply(400, {"error": str(exc)})
            except Exception as exc:
                self.reply(500, {"error": str(exc)})

    server = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    (state / "pid").write_text(str(os.getpid()))

    def inventory_loop():
        while not stopping.is_set():
            atomic_json(
                state / "status.json",
                dict(
                    pid=os.getpid(),
                    port=a.port,
                    at=stamp(),
                    protocol="dashboard-v1",
                    observed_sessions=len(
                        selected_logs(Path(store.settings()["home"]).expanduser())
                    ),
                    mode="blocking-handoff",
                ),
            )
            stopping.wait(3)

    def shutdown(*_):
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    threading.Thread(target=inventory_loop, daemon=True).start()
    print(f"Bridge listening on 127.0.0.1:{a.port}", flush=True)
    try:
        server.serve_forever()
    finally:
        stopping.set()
        engine.stop()
        server.server_close()
        (state / "pid").unlink(missing_ok=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["run", "start", "stop", "status", "handoff"])
    p.add_argument(
        "--state-dir",
        default=os.environ.get(
            "DSH_SUPERVISOR_STATE",
            str(
                Path(os.environ.get("DSH_HOME", str(Path.home() / ".dsh")))
                / "supervisor"
            ),
        ),
    )
    p.add_argument(
        "--home", default=os.environ.get("DSH_HOME", str(Path.home() / ".dsh"))
    )
    p.add_argument("--port", type=int, default=13083)
    p.add_argument("--codex-bin", default="codex")
    p.add_argument("--model", default=os.environ.get("DSH_SUPERVISOR_MODEL", "gpt-5.5"))
    p.add_argument("--review-timeout", type=int, default=240)
    p.add_argument("--max-per-day", type=int, default=12)
    p.add_argument("--phase", choices=["plan", "checkpoint", "acceptance"])
    p.add_argument(
        "--scope",
        action="append",
        help="workspace-relative review path; repeat as needed",
    )
    p.add_argument("--summary-file")
    p.add_argument("--request-id")
    p.add_argument("--session-id")
    a = p.parse_args()
    state = Path(a.state_dir).expanduser().resolve()
    if not 1 <= a.review_timeout <= 450:
        p.error("review-timeout must be 1..450 seconds")
    if a.command in ("start", "run"):
        if not shutil.which(a.codex_bin):
            p.error("codex CLI not found; install and log in first")
        if not shutil.which("zstd"):
            p.error(
                "zstd not found; install it before monitoring compressed session logs"
            )
        if not shutil.which("git"):
            p.error("git not found; install it before supervising a workspace")
    if a.command == "run":
        run_server(a)
    elif a.command == "start":
        state.mkdir(parents=True, exist_ok=True)
        old = int((state / "pid").read_text()) if (state / "pid").exists() else 0
        if old and alive(old):
            print(f"already running pid={old}")
            return
        args = [
            sys.executable,
            str(Path(__file__).resolve()),
            "run",
            "--state-dir",
            str(state),
            "--home",
            a.home,
            "--port",
            str(a.port),
            "--codex-bin",
            a.codex_bin,
            "--model",
            a.model,
            "--review-timeout",
            str(a.review_timeout),
            "--max-per-day",
            str(a.max_per_day),
        ]
        with (state / "server.log").open("a") as log:
            proc = subprocess.Popen(
                args, stdout=log, stderr=log, start_new_session=True
            )
        for _ in range(50):
            if proc.poll() is not None:
                raise SystemExit("bridge failed to start; see server.log")
            if (state / "pid").exists() and (state / "pid").read_text() == str(
                proc.pid
            ):
                print(f"started pid={proc.pid}, port={a.port}")
                break
            time.sleep(0.1)
        else:
            raise SystemExit("bridge startup timed out; see server.log")
    elif a.command == "stop":
        pid = int((state / "pid").read_text()) if (state / "pid").exists() else 0
        if pid and alive(pid):
            os.kill(pid, signal.SIGTERM)
        print("stop requested")
    elif a.command == "status":
        result = (
            json.loads((state / "status.json").read_text())
            if (state / "status.json").exists()
            else {}
        )
        result["running"] = alive(result.get("pid", 0)) if result.get("pid") else False
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        sid = a.session_id or os.environ.get("DSH_SESSION_ID")
        if not sid or not a.phase or not a.summary_file:
            p.error("handoff needs DSH_SESSION_ID, --phase and --summary-file")
        summary = Path(a.summary_file).read_text()
        if len(summary) > 20000:
            p.error("summary is too long (max 20000 characters)")
        packet = {
            "session_id": sid,
            "cwd": str(Path.cwd()),
            "phase": a.phase,
            "scope": validate_scope(Path.cwd(), a.scope or ["."]),
            "summary": summary,
            "request_id": a.request_id or str(uuid.uuid4()),
        }
        if not (state / "bridge.key").exists():
            print(
                json.dumps(
                    {
                        "decision": "blocked",
                        "instruction": "先运行此脚本 start，再重试交接。",
                    },
                    ensure_ascii=False,
                )
            )
            sys.exit(2)
        key = (state / "bridge.key").read_text().strip()
        req = Request(
            f"http://127.0.0.1:{a.port}/handoff",
            data=json.dumps(packet, ensure_ascii=False).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + key,
            },
        )
        try:
            with urlopen(req, timeout=600) as response:
                result = json.loads(response.read())
        except Exception as e:
            print(
                json.dumps(
                    {
                        "decision": "blocked",
                        "instruction": "停止工作，报告交接失败。",
                        "error": str(e),
                    },
                    ensure_ascii=False,
                )
            )
            sys.exit(1)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        if result["decision"] == "blocked":
            sys.exit(2)


if __name__ == "__main__":
    main()
