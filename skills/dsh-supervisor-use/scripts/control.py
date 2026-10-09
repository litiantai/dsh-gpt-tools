#!/usr/bin/env python3
"""Local dashboard API client: private session handling, explicit writes, no retries."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import re
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    Request,
    build_opener,
)

READ = re.compile(
    r"/(?:overview|settings|events|reviewers/(?:codex|claude|harness)/models|sessions(?:/[^/?#]+)?|reviews(?:/[^/?#]+)?|native-gates/[^/?#]+)$"
)
WRITE = re.compile(
    r"/(?:service/(?:start|stop)|settings|reviewers/(?:codex|claude|harness)/models/refresh|reviews(?:/[^/?#]+/(?:retry|cancel|takeover|decision))?|sessions/[^/?#]+/(?:approval-mode|messages|stop)|native-gates/[^/?#]+/(?:retry|release))$"
)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "管理 API 不允许重定向", headers, fp)


def local_origin(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in ("127.0.0.1", "localhost")
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or not parsed.port
    ):
        raise argparse.ArgumentTypeError(
            "origin 必须为含端口的本机 HTTP 地址，例如 http://127.0.0.1:13084"
        )
    return value.rstrip("/")


def call(origin, path, body=None):
    opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()), NoRedirect())
    with opener.open(origin + "/api/bootstrap", timeout=15) as response:
        csrf = json.load(response)["csrf"]
    headers = {"Accept": "application/json", "Origin": origin}
    if body is None:
        request = Request(origin + "/api" + path, headers=headers)
    else:
        headers.update({"Content-Type": "application/json", "X-CSRF-Token": csrf})
        method = (
            "PUT" if path == "/settings" or path.endswith("/approval-mode") else "POST"
        )
        request = Request(
            origin + "/api" + path,
            data=json.dumps(body, ensure_ascii=False).encode(),
            headers=headers,
            method=method,
        )
    with opener.open(request, timeout=30) as response:
        return json.load(response)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", type=local_origin, default="http://127.0.0.1:13084")
    actions = parser.add_subparsers(dest="action", required=True)
    read = actions.add_parser("read", help="读取管理状态")
    read.add_argument("path")
    act = actions.add_parser("act", help="执行一次显式管理操作；不会自动重试")
    act.add_argument("path")
    act.add_argument("--operation-id", required=True, type=uuid.UUID)
    act.add_argument("--body-file", type=Path)
    args = parser.parse_args(argv)
    pattern = READ if args.action == "read" else WRITE
    if not pattern.fullmatch(args.path):
        parser.error("不支持的管理路径；不要添加 /api 前缀")
    body = None
    attempted = False
    try:
        if args.action == "act":
            body = (
                json.loads(args.body_file.read_text(encoding="utf-8"))
                if args.body_file
                else {}
            )
            if not isinstance(body, dict) or "operation_id" in body:
                raise ValueError("正文须为 JSON 对象；operation_id 仅通过命令参数传入")
            body["operation_id"] = str(args.operation_id)
        attempted = True
        result = call(args.origin, args.path, body)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except HTTPError as exc:
        try:
            data = json.load(exc)
        except (ValueError, OSError):
            data = {"error": f"HTTP {exc.code}"}
        finally:
            exc.close()
        print(
            json.dumps(
                {
                    "http_status": exc.code,
                    "response": data,
                    "operation_id": body.get("operation_id") if body else None,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    except (URLError, TimeoutError, OSError, ValueError, KeyError) as exc:
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "operation_id": body.get("operation_id") if body else None,
                    "outcome_unknown": args.action == "act" and attempted,
                    "hint": "不会自动重试；写操作请先核对目标记录。",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
