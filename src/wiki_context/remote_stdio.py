"""Zero-dependency local stdio -> remote Streamable HTTP MCP bridge (`wctx bridge`).

Forwards newline-delimited JSON-RPC messages from a desktop MCP host over HTTPS/HTTP
with Bearer token authentication to a remote `wiki-context` gateway.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


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

    for raw_line in sys.stdin:
        line = raw_line.strip()
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
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                if resp.status == 202 or not has_id:
                    continue
                body = resp.read().decode("utf-8", errors="replace").strip()
                if body:
                    sys.stdout.write(body + "\n")
                    sys.stdout.flush()
        except urllib.error.HTTPError as exc:
            if has_id:
                err_msg = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": f"Gateway HTTP {exc.code}: {exc.reason}"},
                }
                sys.stdout.write(json.dumps(err_msg) + "\n")
                sys.stdout.flush()
        except OSError as exc:
            if has_id:
                err_msg = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": f"Gateway unreachable: {exc}"},
                }
                sys.stdout.write(json.dumps(err_msg) + "\n")
                sys.stdout.flush()
    return 0
