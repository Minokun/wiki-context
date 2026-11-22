"""Multi-Vault Streamable HTTP MCP + REST Context Gateway (`wctx gateway`).

Supports hosting one or multiple vaults in a single threaded process:
- `POST /mcp` (default vault) & `POST /mcp/<vault_id>` (named vault) — Streamable HTTP MCP
  (`202 Accepted` on JSON-RPC notifications, `405` on GET SSE probe, `200` on DELETE)
- `POST /v1/context` & `POST /v1/read` — Lightweight REST APIs
- `GET /v1/vaults` & `GET /v1/whoami` — Authenticated discovery
- `GET /healthz` — Unauthenticated liveness & generation probe
- Constant-time SHA-256 Bearer Token authentication + hot-reloaded JSON token/ACL file.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .context import PrefixACLPolicy, Principal
from .mcp_server import KBService, handle_jsonrpc_message

MAX_HTTP_BODY_BYTES = 1 * 1024 * 1024


def _sha256_hex(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class TokenRegistry:
    """Bearer token store supporting env (`WCTX_TOKENS` / `GW_KB_TOKENS`) and mtime-reloaded JSON file."""

    def __init__(self, *, env_tokens: str = "", token_file: Path | str | None = None):
        self._env_entries: list[tuple[str, Principal]] = []
        self._file_path = Path(token_file).expanduser().resolve() if token_file else None
        self._file_mtime_ns: int | None = None
        self._file_entries: list[tuple[str, Principal]] = []
        self._file_acl: dict[str, dict[str, list[str]]] = {}
        self._lock = threading.Lock()

        raw_env = (
            env_tokens
            or os.environ.get("WCTX_TOKENS")
            or os.environ.get("GW_KB_TOKENS")
            or ""
        ).strip()
        for item in raw_env.split(","):
            item = item.strip()
            if not item or ":" not in item:
                continue
            uid_part, secret = item.split(":", 1)
            uid_clean = uid_part.strip()
            sec_clean = secret.strip()
            if uid_clean and sec_clean:
                self._env_entries.append((_sha256_hex(sec_clean), Principal(uid=uid_clean, groups=("all",))))

        self._reload_file_if_needed()

    def _reload_file_if_needed(self) -> None:
        if self._file_path is None or not self._file_path.is_file():
            return
        try:
            st = self._file_path.stat()
        except OSError:
            return
        with self._lock:
            if st.st_mtime_ns == self._file_mtime_ns:
                return
            try:
                data = json.loads(self._file_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return

            entries: list[tuple[str, Principal]] = []
            tokens_list = data.get("tokens") if isinstance(data, dict) else data
            if isinstance(tokens_list, list):
                for row in tokens_list:
                    if not isinstance(row, dict) or row.get("disabled") is True:
                        continue
                    uid = str(row.get("uid") or "").strip()
                    if not uid:
                        continue
                    groups = tuple(str(g) for g in (row.get("groups") or ["all"]))
                    tok_sha = str(row.get("token_sha256") or "").strip().lower()
                    if not tok_sha and row.get("token"):
                        tok_sha = _sha256_hex(str(row["token"]))
                    if len(tok_sha) == 64:
                        entries.append((tok_sha, Principal(uid=uid, groups=groups)))

            acl_cfg = data.get("acl") if isinstance(data, dict) and isinstance(data.get("acl"), dict) else {}
            self._file_entries = entries
            self._file_acl = acl_cfg
            self._file_mtime_ns = st.st_mtime_ns

    @property
    def acl_rules(self) -> dict[str, dict[str, list[str]]]:
        self._reload_file_if_needed()
        return self._file_acl

    @property
    def has_any_token(self) -> bool:
        self._reload_file_if_needed()
        return bool(self._env_entries or self._file_entries)

    def authenticate(self, auth_header: str) -> Principal | None:
        if not auth_header or not auth_header.lower().startswith("bearer "):
            return None
        raw_token = auth_header[7:].strip()
        if not raw_token:
            return None
        self._reload_file_if_needed()
        cand_hash = _sha256_hex(raw_token)
        matched: Principal | None = None
        for digest, principal in (*self._env_entries, *self._file_entries):
            if hmac.compare_digest(cand_hash, digest):
                matched = principal
        return matched


class MultiVaultGateway:
    """Manages one or more `KBService` instances behind a single HTTP server."""

    def __init__(
        self,
        vault_map: dict[str, Path | str],
        *,
        tokens: TokenRegistry,
    ):
        if not vault_map:
            raise ValueError("At least one vault must be configured for MultiVaultGateway")
        self.tokens = tokens
        shared_policy = PrefixACLPolicy(tokens.acl_rules) if tokens.acl_rules else None
        self.services: dict[str, KBService] = {}
        self.default_vault_id = next(iter(vault_map))
        for vid, vpath in vault_map.items():
            clean_id = vid.strip("/")
            self.services[clean_id] = KBService(vpath, policy=shared_policy)

    def close(self) -> None:
        for svc in self.services.values():
            svc.close()

    def resolve_service(self, vault_id: str | None) -> KBService | None:
        if not vault_id:
            return self.services[self.default_vault_id]
        return self.services.get(vault_id)


class _HighBacklogHTTPServer(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True


def create_http_server(
    gateway: MultiVaultGateway,
    *,
    host: str = "127.0.0.1",
    port: int = 8090,
) -> ThreadingHTTPServer:
    if not gateway.tokens.has_any_token:
        raise ValueError("Refusing to start gateway with 0 configured tokens (set WCTX_TOKENS or --token-file)")

    class GatewayHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return  # Structured JSONL audit handles logging cleanly

        def _send_json(self, status: int, payload: dict | list, extra_headers: dict[str, str] | None = None) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            if extra_headers:
                for hk, hv in extra_headers.items():
                    self.send_header(hk, hv)
            self.end_headers()
            self.wfile.write(raw)

        def _send_empty(self, status: int, extra_headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            if extra_headers:
                for hk, hv in extra_headers.items():
                    self.send_header(hk, hv)
            self.end_headers()

        def _require_auth(self) -> Principal | None:
            p = gateway.tokens.authenticate(self.headers.get("Authorization") or "")
            if p is None:
                self._send_json(
                    401,
                    {"error": "Unauthorized: valid Bearer token required"},
                    {"WWW-Authenticate": 'Bearer realm="wiki-context"'},
                )
            return p

        def _read_json_body(self) -> tuple[dict | list | None, str | None]:
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                return None, "Invalid Content-Length"
            if length <= 0:
                return {}, None
            if length > MAX_HTTP_BODY_BYTES:
                return None, f"Payload exceeds {MAX_HTTP_BODY_BYTES} bytes"
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8")), None
            except ValueError:
                return None, "Invalid JSON payload"

        def _match_mcp_route(self, path: str) -> tuple[bool, KBService | None]:
            if path == "/mcp":
                return True, gateway.resolve_service(None)
            if path.startswith("/mcp/"):
                vid = path[len("/mcp/"):].strip("/")
                return True, gateway.resolve_service(vid)
            return False, None

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/healthz":
                vaults_stat = {}
                for vid, svc in gateway.services.items():
                    st = svc.tool_status()
                    vaults_stat[vid] = {
                        "name": svc.cfg.name,
                        "generation_id": st["generation_id"],
                        "docs": st["docs"],
                        "sections": st["sections"],
                    }
                self._send_json(200, {"status": "ok", "default_vault": gateway.default_vault_id, "vaults": vaults_stat})
                return

            is_mcp, _ = self._match_mcp_route(path)
            if is_mcp:
                if self._require_auth() is None:
                    return
                self._send_empty(405, {"Allow": "POST, DELETE"})
                return

            if path == "/v1/whoami":
                user = self._require_auth()
                if user is None:
                    return
                self._send_json(200, {"uid": user.uid, "groups": list(user.groups)})
                return

            if path == "/v1/vaults":
                user = self._require_auth()
                if user is None:
                    return
                self._send_json(
                    200,
                    {
                        "default": gateway.default_vault_id,
                        "vaults": [
                            {"id": vid, "name": svc.cfg.name, "layout": svc.cfg.layout}
                            for vid, svc in gateway.services.items()
                        ],
                    },
                )
                return

            self._send_json(404, {"error": f"Not found: {path}"})

        def do_DELETE(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            is_mcp, _ = self._match_mcp_route(path)
            if is_mcp:
                if self._require_auth() is None:
                    return
                self._send_empty(200)
                return
            self._send_json(404, {"error": f"Not found: {path}"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            user = self._require_auth()
            if user is None:
                return

            is_mcp, mcp_svc = self._match_mcp_route(path)
            if is_mcp:
                if mcp_svc is None:
                    self._send_json(404, {"error": f"Unknown vault in MCP route: {path}"})
                    return
                body, err = self._read_json_body()
                if err is not None:
                    status = 413 if "exceeds" in err else 400
                    self._send_json(status, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": err}})
                    return
                if isinstance(body, list):
                    responses = []
                    for item in body:
                        r = handle_jsonrpc_message(mcp_svc, item, principal=user, slim_remote=True)
                        if r is not None:
                            responses.append(r)
                    if not responses:
                        self._send_empty(202)
                    else:
                        self._send_json(200, responses)
                    return
                if isinstance(body, dict):
                    resp = handle_jsonrpc_message(mcp_svc, body, principal=user, slim_remote=True)
                    if resp is None:
                        self._send_empty(202)
                    else:
                        self._send_json(200, resp)
                    return
                self._send_json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}})
                return

            if path in {"/v1/context", "/v1/read"}:
                body, err = self._read_json_body()
                if err is not None or not isinstance(body, dict):
                    self._send_json(400, {"error": err or "JSON object body required"})
                    return
                body = dict(body)
                body.pop("user", None)
                vid = str(body.pop("vault", "") or "").strip() or None
                svc = gateway.resolve_service(vid)
                if svc is None:
                    self._send_json(404, {"error": f"Unknown vault '{vid}'"})
                    return
                out = svc.tool_context(body, principal=user) if path == "/v1/context" else svc.tool_read(body, principal=user)
                status = 400 if "error" in out else 200
                if isinstance(out.get("error"), str) and "Access denied" in out["error"]:
                    status = 403
                self._send_json(status, out)
                return

            self._send_json(404, {"error": f"Not found: {path}"})

    return _HighBacklogHTTPServer((host, port), GatewayHandler)


def parse_vault_specs(specs: list[str]) -> dict[str, Path]:
    """Parse `--vault path` or `--vault id=path` arguments into `{vault_id: Path}`."""
    out: dict[str, Path] = {}
    for raw in specs:
        if "=" in raw:
            vid, p_str = raw.split("=", 1)
            vid_clean = vid.strip()
            p = Path(p_str.strip()).expanduser().resolve()
        else:
            p = Path(raw.strip()).expanduser().resolve()
            vid_clean = p.name or "default"
        if vid_clean in out:
            raise ValueError(f"Duplicate vault id '{vid_clean}'")
        out[vid_clean] = p
    return out


def selftest_gateway(vault_map: dict[str, Path | str]) -> int:
    import urllib.error
    import urllib.request

    reg = TokenRegistry(env_tokens="selftest:wctx_test_tok_123")
    gw = MultiVaultGateway(vault_map, tokens=reg)
    srv = create_http_server(gw, host="127.0.0.1", port=0)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    hdrs = {"Authorization": "Bearer wctx_test_tok_123", "Content-Type": "application/json"}

    try:
        # 1. /healthz unauthenticated
        with urllib.request.urlopen(f"{base}/healthz", timeout=5) as r:
            assert r.status == 200
            assert json.loads(r.read())["status"] == "ok"

        # 2. Unauthenticated /mcp -> 401
        try:
            urllib.request.urlopen(f"{base}/mcp", timeout=5)
            raise AssertionError("Expected 401")
        except urllib.error.HTTPError as e:
            e.close()
            assert e.code == 401

        # 3. GET /mcp -> 405 Method Not Allowed
        try:
            req_get = urllib.request.Request(f"{base}/mcp", headers=hdrs, method="GET")
            urllib.request.urlopen(req_get, timeout=5)
            raise AssertionError("Expected 405")
        except urllib.error.HTTPError as e:
            e.close()
            assert e.code == 405

        # 4. Notification -> 202 Accepted
        req_notif = urllib.request.Request(
            f"{base}/mcp",
            data=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode(),
            headers=hdrs,
            method="POST",
        )
        with urllib.request.urlopen(req_notif, timeout=5) as r:
            assert r.status == 202

        # 5. tools/list -> 3 slim remote tools
        req_tools = urllib.request.Request(
            f"{base}/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
            headers=hdrs,
            method="POST",
        )
        with urllib.request.urlopen(req_tools, timeout=5) as r:
            tools = [x["name"] for x in json.loads(r.read())["result"]["tools"]]
            assert tools == ["kb_context", "kb_read", "kb_list"]

        print(f"gateway selftest: OK (port={port}, vaults={list(gw.services)}, tools={tools})")
        return 0
    finally:
        srv.shutdown()
        srv.server_close()
        gw.close()
