#!/usr/bin/env python3
"""Watch local DeepSeek Harness sessions and wake Codex only for reviewable events."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from urllib import error as urlerror
from urllib import request as urlrequest


LOG_NAME = re.compile(r"session(?:\.v(?P<version>\d+))?\.jsonl(?:\.zstd)?$")
MAX_TEXT = 9000


def now_ms() -> int:
    return int(time.time() * 1000)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def atomic_json(path: Path, value: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)


def record(state_dir: Path, kind: str, **fields: object) -> None:
    entry = {"at": dt.datetime.now(dt.timezone.utc).isoformat(), "kind": kind, **fields}
    with (state_dir / "events.jsonl").open("a") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(json.dumps(entry, ensure_ascii=False), flush=True)


def selected_logs(home: Path) -> dict[str, Path]:
    selected: dict[str, tuple[int, Path]] = {}
    root = home / "sessions"
    if not root.is_dir():
        return {}
    for path in root.glob("*/*/session*.jsonl*"):
        match = LOG_NAME.fullmatch(path.name)
        if not match or not path.is_file():
            continue
        key = str(path.parent)
        version = int(match.group("version") or 0)
        if key not in selected or version > selected[key][0]:
            selected[key] = (version, path)
    return {key: item[1] for key, item in selected.items()}


def read_log(path: Path) -> list[dict]:
    if path.suffix == ".zstd":
        result = subprocess.run(
            ["zstd", "-qdc", str(path)], capture_output=True, check=True, timeout=30
        )
        raw = result.stdout
    else:
        raw = path.read_bytes()
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def harness_reachable(origin: str) -> bool:
    try:
        with urlrequest.urlopen(origin, timeout=2):
            return True
    except urlerror.HTTPError:
        # The Harness root normally returns 401 without its browser cookie.
        return True
    except (urlerror.URLError, TimeoutError):
        return False


def text_from_message(event: dict) -> str:
    message = event.get("data", {}).get("message", {})
    parts = message.get("content", []) if isinstance(message, dict) else []
    texts = [part.get("text", "") for part in parts
             if isinstance(part, dict) and part.get("type") == "text"]
    return "\n".join(text for text in texts if isinstance(text, str))[:MAX_TEXT]


def packet_from(rows: list[dict], event: dict, reason: str, path: Path) -> dict:
    header = rows[0]
    session_id = str(header.get("id") or path.parent.name)
    title = next((row.get("data", {}).get("title") for row in reversed(rows)
                  if row.get("type") == "session/title"), None)
    users = [text_from_message(row) for row in rows if row.get("type") == "user/message"]
    assistants = [text_from_message(row) for row in rows
                  if row.get("type") == "assistant/message"]
    errors = []
    for row in rows[-100:]:
        if row.get("type") != "tool/result":
            continue
        data = row.get("data", {})
        if not data.get("message", {}).get("isError"):
            continue
        error = data.get("error") or {}
        errors.append({"seq": row.get("seq"), "code": error.get("code"),
                       "name": error.get("name")})
    return {
        "session_id": session_id,
        "title": title,
        "workspace": header.get("cwd"),
        "log_path": str(path),
        "event_seq": event.get("seq"),
        "reason": reason,
        "turn_reason": event.get("data", {}).get("reason"),
        "first_user_request": users[0] if users else "",
        "latest_user_request": users[-1] if users else "",
        "latest_assistant_answer": assistants[-1] if assistants else "",
        "recent_tool_errors": errors[-5:],
    }


def trigger_for(event: dict, rows: list[dict], checkpoint_steps: int) -> str | None:
    if event.get("type") == "step/end":
        step = event.get("data", {}).get("step")
        if isinstance(step, int) and step > 0 and step % checkpoint_steps == 0:
            return "progress_checkpoint"
    if event.get("type") == "tool/result" and event.get("data", {}).get("message", {}).get("isError"):
        seq = event.get("seq", -1)
        turn = event.get("data", {}).get("turn")
        recent = [row for row in rows if row.get("type") == "tool/result"
                  and row.get("seq", -1) <= seq and row.get("data", {}).get("turn") == turn][-5:]
        if sum(bool(row.get("data", {}).get("message", {}).get("isError")) for row in recent) >= 3:
            return "repeated_tool_failures"
    if event.get("type") != "turn/end":
        return None
    reason = event.get("data", {}).get("reason", {})
    kind = reason.get("kind") if isinstance(reason, dict) else None
    if kind == "completed":
        return "turn_completed"
    if kind == "aborted" and reason.get("reason", {}).get("kind") == "user":
        return None
    return "turn_failed_or_blocked"


def prompt_for(packet: dict) -> str:
    return (
        "你是 DeepSeek Harness 的按需监工。请审查以下刚结束的会话轮次，核对用户要求、成果与可验证证据。"
        "优先使用只读检查和确定性测试。只有发现具体错误或遗漏时，才使用 computer use 在"
        "已打开的 http://127.0.0.1:3080/ 中找到这一个会话，向 DeepSeek 发送简洁、可执行的纠偏指令。"
        "不要向其他会话发消息。不要读取、复制或输出凭证。不要替用户推送代码、发布、部署或执行其他"
        "对外高影响操作。若无法使用界面，清楚说明原因和待执行的纠偏文字。"
        "报告结论、证据、是否干预、剩余问题。\n\n"
        + json.dumps(packet, ensure_ascii=False, indent=2)
    )


def supervise(packet: dict, state_dir: Path, codex_bin: str, dry_run: bool) -> None:
    stem = f"{int(time.time())}-{packet['session_id']}-{packet['event_seq']}"
    packet_path = state_dir / "packets" / f"{stem}.json"
    packet_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n")
    if dry_run:
        record(state_dir, "dry_run", session=packet["session_id"], packet=str(packet_path))
        return
    review_path = state_dir / "reviews" / f"{stem}.md"
    trace_path = state_dir / "reviews" / f"{stem}.trace"
    command = [codex_bin, "exec", "--ephemeral", "--skip-git-repo-check",
               "--sandbox", "read-only", "-C", str(state_dir),
               "-o", str(review_path), "-"]
    record(state_dir, "codex_started", session=packet["session_id"],
           event_seq=packet["event_seq"])
    with trace_path.open("w") as trace:
        result = subprocess.run(command, input=prompt_for(packet), text=True,
                                stdout=trace, stderr=subprocess.STDOUT, timeout=1800)
    record(state_dir, "codex_finished", session=packet["session_id"],
           event_seq=packet["event_seq"], exit_code=result.returncode,
           review=str(review_path), trace=str(trace_path))


def watch(args: argparse.Namespace) -> None:
    os.umask(0o077)
    home = Path(args.home).expanduser().resolve()
    state_dir = Path(args.state_dir).expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    state_dir.chmod(0o700)
    if shutil.which("zstd") is None:
        raise SystemExit("zstd executable is required to read Harness session logs")
    if not args.dry_run and shutil.which(args.codex_bin) is None:
        raise SystemExit(f"Codex executable not found: {args.codex_bin}")
    if args.interval <= 0 or args.cooldown_minutes < 0 or args.max_per_day < 1 or args.checkpoint_steps < 1:
        raise SystemExit("interval and limits must be positive")
    (state_dir / "packets").mkdir(exist_ok=True)
    (state_dir / "reviews").mkdir(exist_ok=True)
    pid_path = state_dir / "monitor.pid"
    if pid_path.exists():
        old_pid = int(pid_path.read_text().strip())
        if old_pid != os.getpid() and alive(old_pid):
            raise SystemExit(f"monitor already running (pid {old_pid})")
    pid_path.write_text(str(os.getpid()))
    started = now_ms()
    seen: dict[str, tuple[int, int]] = {}
    processed_seq: dict[str, int] = {}
    for key, path in selected_logs(home).items():
        stat = path.stat()
        seen[key] = (stat.st_mtime_ns, stat.st_size)
    last_review: dict[str, float] = {}
    daily_count: dict[str, int] = {}
    last_reachable: bool | None = None
    history_path = state_dir / "events.jsonl"
    if history_path.exists():
        for line in history_path.read_text().splitlines():
            try:
                old = json.loads(line)
                if old.get("kind") != "codex_started":
                    continue
                when = dt.datetime.fromisoformat(old["at"])
                day = when.astimezone().date().isoformat()
                daily_count[day] = daily_count.get(day, 0) + 1
                session = old.get("session")
                if isinstance(session, str):
                    last_review[session] = max(last_review.get(session, 0), when.timestamp())
            except (KeyError, ValueError, TypeError):
                continue
    stop = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    record(state_dir, "monitor_started", pid=os.getpid(), home=str(home),
           existing_sessions=len(seen), dry_run=args.dry_run)
    try:
        while not stop:
            reachable = harness_reachable(args.origin)
            if reachable != last_reachable:
                record(state_dir, "harness_reachable" if reachable else "harness_unreachable",
                       origin=args.origin)
                last_reachable = reachable
            logs = selected_logs(home)
            for key, path in logs.items():
                try:
                    stat = path.stat()
                    signature = (stat.st_mtime_ns, stat.st_size)
                    if seen.get(key) == signature:
                        continue
                    rows = read_log(path)
                    seen[key] = signature
                    if not rows:
                        continue
                    new_events = [event for event in rows[1:]
                                  if isinstance(event.get("seq"), int)
                                  and event["seq"] > processed_seq.get(key, -1)]
                    if new_events:
                        processed_seq[key] = max(event["seq"] for event in new_events)
                    triggers = [(event, trigger_for(event, rows, args.checkpoint_steps))
                                for event in new_events if event.get("time", 0) >= started]
                    triggers = [(event, reason) for event, reason in triggers if reason is not None]
                    if any(event.get("type") == "turn/end" for event, _ in triggers):
                        triggers = [(event, reason) for event, reason in triggers
                                    if event.get("type") == "turn/end"]
                    for event, reason in triggers:
                        packet = packet_from(rows, event, reason, path)
                        session = packet["session_id"]
                        day = dt.datetime.now().date().isoformat()
                        if daily_count.get(day, 0) >= args.max_per_day:
                            record(state_dir, "budget_skipped", session=session,
                                   event_seq=event.get("seq"), limit=args.max_per_day)
                            continue
                        if time.time() - last_review.get(session, 0) < args.cooldown_minutes * 60:
                            record(state_dir, "cooldown_skipped", session=session,
                                   event_seq=event.get("seq"))
                            continue
                        daily_count[day] = daily_count.get(day, 0) + 1
                        last_review[session] = time.time()
                        try:
                            supervise(packet, state_dir, args.codex_bin, args.dry_run)
                        except Exception as exc:
                            record(state_dir, "codex_error", session=session,
                                   error=f"{type(exc).__name__}: {exc}")
                except Exception as exc:
                    record(state_dir, "read_error", file=str(path),
                           error=f"{type(exc).__name__}: {exc}")
            atomic_json(state_dir / "status.json", {
                "pid": os.getpid(), "started_at_ms": started,
                "last_scan_at_ms": now_ms(), "tracked_sessions": len(logs),
                "harness_reachable": reachable,
                "reviews_today": daily_count.get(dt.datetime.now().date().isoformat(), 0),
                "max_reviews_per_day": args.max_per_day,
            })
            time.sleep(args.interval)
    finally:
        record(state_dir, "monitor_stopped", pid=os.getpid())
        if pid_path.exists() and pid_path.read_text().strip() == str(os.getpid()):
            pid_path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "start", "stop", "status"))
    parser.add_argument("--home", default=str(Path.home() / ".dsh"))
    parser.add_argument("--origin", default="http://127.0.0.1:3080/")
    parser.add_argument("--state-dir", default="./dsh-supervisor-state")
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--cooldown-minutes", type=float, default=10.0)
    parser.add_argument("--checkpoint-steps", type=int, default=25)
    parser.add_argument("--max-per-day", type=int, default=12)
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    state_dir = Path(args.state_dir).expanduser().resolve()
    pid_path = state_dir / "monitor.pid"
    if args.command == "run":
        watch(args)
    elif args.command == "start":
        state_dir.mkdir(parents=True, exist_ok=True)
        if pid_path.exists() and alive(int(pid_path.read_text().strip())):
            raise SystemExit(f"monitor already running (pid {pid_path.read_text().strip()})")
        command = [sys.executable, str(Path(__file__).resolve()), "run",
                   "--home", args.home, "--origin", args.origin,
                   "--state-dir", str(state_dir),
                   "--interval", str(args.interval),
                   "--cooldown-minutes", str(args.cooldown_minutes),
                   "--checkpoint-steps", str(args.checkpoint_steps),
                   "--max-per-day", str(args.max_per_day),
                   "--codex-bin", args.codex_bin]
        if args.dry_run:
            command.append("--dry-run")
        with (state_dir / "monitor.log").open("a") as stream:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                     stdout=stream, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        for _ in range(30):
            if pid_path.exists() and pid_path.read_text().strip() == str(child.pid):
                break
            time.sleep(0.1)
        if not alive(child.pid):
            raise SystemExit("monitor failed to start; inspect monitor.log")
        print(f"monitor running (pid {child.pid}); state: {state_dir}")
    elif args.command == "stop":
        if not pid_path.exists():
            print("monitor is not running")
        else:
            pid = int(pid_path.read_text().strip())
            if alive(pid):
                os.kill(pid, signal.SIGTERM)
                print(f"stop requested for pid {pid}")
            else:
                pid_path.unlink()
                print("removed stale pid file")
    else:
        status_path = state_dir / "status.json"
        status = json.loads(status_path.read_text()) if status_path.exists() else {}
        pid = int(pid_path.read_text().strip()) if pid_path.exists() else None
        status["running"] = pid is not None and alive(pid)
        print(json.dumps(status, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
