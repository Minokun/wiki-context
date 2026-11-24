"""Zero-dependency local stdio -> remote Streamable HTTP MCP bridge (`wctx bridge`).

Forwards newline-delimited JSON-RPC messages from a desktop MCP host over HTTPS/HTTP
with Bearer token authentication to a remote `wiki-context` gateway using UTF-8 binary
stdio streams (preventing `cp936`/`gbk` UnicodeEncodeError on Windows).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from .net import TLSUnavailable, format_network_error, ssl_context


def _write_stdout_line(text: str) -> None:
    data = (text.rstrip("\r\n") + "\n").encode("utf-8", errors="replace")
    buf = getattr(sys.stdout, "buffer", None)
    if buf is not None:
        buf.write(data)
        buf.flush()
    else:
        sys.stdout.write(text.rstrip("\r\n") + "\n")
        sys.stdout.flush()


def run_bridge(*, url: str | None = None, token: str | None = None, timeout_sec: int = 30) -> int:
    endpoint = (url or os.environ.get("WCTX_REMOTE_URL") or os.environ.get("GW_KB_URL") or "").strip()
    secret = (token or os.environ.get("WCTX_TOKEN") or os.environ.get("GW_KB_TOKEN") or "").strip()
    if not endpoint:
        print("Error: missing remote gateway URL (--url or WCTX_REMOTE_URL)", file=sys.stderr)
        return 2
    if not secret:
        print("Error: missing Bearer token (--token or WCTX_TOKEN)", file=sys.stderr)
        return 2

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {secret}",
    }

    while True:
        raw_line = sys.stdin.buffer.readline()
        if not raw_line:
            break
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        req_id = parsed.get("id") if isinstance(parsed, dict) else None
        has_id = isinstance(parsed, dict) and "id" in parsed

        req = urllib.request.Request(endpoint, data=line.encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec, context=ssl_context()) as resp:
                if resp.status == 202 or not has_id:
                    continue
                body = resp.read().decode("utf-8", errors="replace").strip()
                if body:
                    _write_stdout_line(body)
        except urllib.error.HTTPError as exc:
            if has_id:
                err_msg = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": f"Gateway HTTP {exc.code}: {exc.reason}"},
                }
                _write_stdout_line(json.dumps(err_msg, ensure_ascii=False))
        except (OSError, TLSUnavailable) as exc:
            if has_id:
                err_msg = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32000,
                        "message": "Gateway unreachable: "
                        + format_network_error(exc, target=endpoint),
                    },
                }
                _write_stdout_line(json.dumps(err_msg, ensure_ascii=False))
    return 0
