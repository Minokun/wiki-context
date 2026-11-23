"""wiki-context Control Console & Embedded Multi-Vault Gateway (`wctx ui` / `wctx console`).

Provides a zero-dependency local Web Control Plane for:
1. Onboarding folders into four-layer `wiki-context` vaults + importing external folders;
2. Issuing SHA-256 Bearer tokens and configuring vault-level + directory-prefix RBAC (`acl`);
3. One-click MCP plugin configuration for DSH, Codex CLI, Claude Code, WorkBuddy, Cursor, and Antigravity;
4. Viewing, creating, editing, archiving, and importing Layer-4 Operational Memory & Errata notes;
5. Simultaneously serving live `/mcp`, `/mcp/<vault_id>`, `/v1/*`, and `/healthz` Gateway routes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from .clients import (
    CLIENT_BY_ID,
    discover_vaults_from_clients,
    inspect_all_clients,
    render_client_snippet,
    resolve_wctx_command,
    sync_client_mcp_config,
)
from .config import VaultConfig
from .context import AllowAllPolicy, PrefixACLPolicy, Principal
from .gateway import (
    GatewayRoutesMixin,
    MultiVaultGateway,
    TokenRegistry,
    _HighBacklogHTTPServer,
    create_http_server,
)
from .lint import lint_vault
from .mcp_server import scrub_secrets
from .memory import MemoryNote, MemoryStore
from .ops import run_doctor
from .scaffold import import_external_folder, inspect_folder, onboard_folder
from .sync import sync_vault
from .ui_assets import render_console_html


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_hex(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _slugify_vault_id(name: str, existing: set[str]) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip().lower()).strip("-")
    if not s:
        s = "wiki"
    elif not s.startswith("wiki"):
        s = f"wiki-{s}"
    cand = s
    idx = 2
    while cand in existing:
        cand = f"{s}-{idx}"
        idx += 1
    return cand


def _is_loopback_host(host: str) -> bool:
    h = host.strip().lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _mask_token(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    if len(s) > 12:
        return f"{s[:7]}…{s[-4:]}"
    return "••••••"


def normalize_gateway_url(raw_url: str) -> str:
    """Normalize user-entered gateway address (e.g. `192.168.1.25:8090` or `http://host:8090/mcp/wiki`) to base URL."""
    s = (raw_url or "").strip()
    if not s:
        raise ValueError("请填写远程知识库网关地址（例如 http://192.168.1.25:8090）")
    if not s.startswith(("http://", "https://")):
        s = f"http://{s}"
    parsed = urlparse(s)
    if not parsed.netloc:
        raise ValueError(f"远程网关地址格式不正确: {raw_url}")
    clean_path = (parsed.path or "").rstrip("/")
    if clean_path == "/mcp":
        clean_path = ""
    elif "/mcp/" in clean_path:
        clean_path = clean_path.split("/mcp/", 1)[0]
    elif clean_path.endswith("/healthz"):
        clean_path = clean_path[: -len("/healthz")]
    return f"{parsed.scheme}://{parsed.netloc}{clean_path}".rstrip("/")


def detect_lan_ips() -> list[str]:
    """Best-effort zero-dependency detection of local non-loopback IPv4 LAN addresses."""
    found: list[str] = []

    def _add(ip: str) -> None:
        ip = (ip or "").strip()
        if not ip or ip in found:
            return
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return
        if addr.version == 4 and not addr.is_loopback and not addr.is_link_local and not addr.is_unspecified:
            found.append(ip)

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            _add(s.getsockname()[0])
    except OSError:
        pass

    try:
        _, _, ips = socket.gethostbyname_ex(socket.gethostname())
        for ip in ips:
            _add(ip)
    except OSError:
        pass

    return found


def _http_json_request(
    url: str,
    *,
    method: str = "GET",
    token: str = "",
    payload: dict[str, Any] | None = None,
    timeout: float = 4.0,
) -> Any:
    headers: dict[str, str] = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data_bytes: bytes | None = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace").strip()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace").strip()
        except Exception:
            pass
        finally:
            exc.close()
        detail = ""
        if err_body:
            try:
                detail = str(json.loads(err_body).get("error") or "")
            except ValueError:
                detail = err_body[:120]
        if exc.code == 401:
            raise ValueError("远程网关鉴权失败（HTTP 401）：令牌无效、为空或已被吊销") from None
        if exc.code == 403:
            raise ValueError(f"远程网关拒绝访问（HTTP 403）：{detail or '该令牌无目标知识库权限'}") from None
        if exc.code == 404:
            raise ValueError(f"远程网关接口不存在（HTTP 404）：请确认地址指向 wiki-context 网关 ({url})") from None
        raise ValueError(f"远程网关返回 HTTP {exc.code}：{detail or exc.reason}") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ValueError(
            f"无法连接到远程地址 {url}（{reason}）。请确认提供方电脑已开启局域网共享网关且防火墙放行。"
        ) from None


def probe_remote_gateway(gateway_url: str, token: str, *, timeout: float = 4.0) -> dict[str, Any]:
    """Authenticate against a remote `wiki-context` gateway and discover authorized vaults."""
    base = normalize_gateway_url(gateway_url)
    clean_tok = (token or "").strip()
    if not clean_tok:
        raise ValueError("请填写远程网关签发的访问令牌 (Bearer Token)")

    who = _http_json_request(f"{base}/v1/whoami", token=clean_tok, timeout=timeout)
    v_resp = _http_json_request(f"{base}/v1/vaults", token=clean_tok, timeout=timeout)
    health_vaults: dict[str, Any] = {}
    try:
        hz = _http_json_request(f"{base}/healthz", token=clean_tok, timeout=min(timeout, 2.5))
        if isinstance(hz, dict) and isinstance(hz.get("vaults"), dict):
            health_vaults = hz["vaults"]
    except Exception:
        pass

    discovered: list[dict[str, Any]] = []
    for v in v_resp.get("vaults") or []:
        if not isinstance(v, dict):
            continue
        rvid = str(v.get("id") or "").strip()
        if not rvid:
            continue
        hz_info = health_vaults.get(rvid) if isinstance(health_vaults.get(rvid), dict) else {}
        discovered.append({
            "remote_vault_id": rvid,
            "name": str(v.get("name") or rvid),
            "layout": str(v.get("layout") or "en"),
            "docs": int(v.get("docs") if v.get("docs") is not None else (hz_info.get("docs") or 0)),
            "sections": int(v.get("sections") if v.get("sections") is not None else (hz_info.get("sections") or 0)),
            "generation_id": str(v.get("generation_id") or hz_info.get("generation_id") or ""),
            "mcp_url": f"{base}/mcp/{rvid}",
        })

    return {
        "gateway_url": base,
        "uid": str(who.get("uid") or ""),
        "groups": [str(g) for g in (who.get("groups") or [])],
        "allowed_vaults": [str(x) for x in (who.get("allowed_vaults") or [])],
        "default_vault": str(v_resp.get("default") or ""),
        "vaults": discovered,
    }


def _atomic_write_json(path: Path, data: Any, *, private_mode: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if private_mode:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    tmp.replace(path)


def load_console_vault_map(config_dir: Path | str | None = None) -> tuple[dict[str, Path], Path]:
    """Read registered LOCAL vaults & default tokens file path from `~/.wiki-context` (or `config_dir`)."""
    base = config_dir or os.environ.get("WCTX_HOME") or (Path.home() / ".wiki-context")
    cfg_dir = Path(base).expanduser().resolve()
    console_file = cfg_dir / "console.json"
    tokens_file = cfg_dir / "tokens.json"
    vmap: dict[str, Path] = {}
    if console_file.is_file():
        try:
            data = json.loads(console_file.read_text(encoding="utf-8"))
            for item in data.get("vaults") or []:
                if not isinstance(item, dict) or item.get("kind") == "remote":
                    continue
                vid = str(item.get("id") or "").strip()
                raw_p = str(item.get("path") or "").strip()
                if not vid or not raw_p:
                    continue
                vpath = Path(raw_p).expanduser().resolve()
                if vpath.is_dir():
                    vmap[vid] = vpath
        except (OSError, ValueError):
            pass
    return vmap, tokens_file


class ConsoleState:
    """Thread-safe persistence & orchestration layer for the `wiki-context` Control Console."""

    def __init__(
        self,
        *,
        config_dir: Path | str | None = None,
        extra_vault_specs: dict[str, Path] | None = None,
        client_home: Path | None = None,
    ):
        base = config_dir or os.environ.get("WCTX_HOME") or (Path.home() / ".wiki-context")
        self.config_dir = Path(base).expanduser().resolve()
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.console_file = self.config_dir / "console.json"
        self.tokens_file = self.config_dir / "tokens.json"
        self.remotes_file = self.config_dir / "remotes.json"
        self.client_home = client_home
        self._lock = threading.RLock()
        self._lan_server: _HighBacklogHTTPServer | None = None

        self._bootstrap_files(extra_vault_specs or {})
        self.token_registry = TokenRegistry(token_file=self.tokens_file)
        initial_map: dict[str, Path] = {}
        init_data = self._load_console_data()
        for item in init_data.get("vaults", []):
            if not isinstance(item, dict) or item.get("kind") == "remote":
                continue
            vid = str(item.get("id") or "").strip()
            raw_p = str(item.get("path") or "").strip()
            if not vid or not raw_p:
                continue
            vpath = Path(raw_p).expanduser().resolve()
            if vpath.is_dir():
                try:
                    cfg = VaultConfig.load(vpath)
                    if (cfg.index_root / "current.json").is_file() or (cfg.index_root / "bm25.sqlite").is_file():
                        initial_map[vid] = vpath
                except Exception:
                    pass
        self.gateway = MultiVaultGateway(initial_map, tokens=self.token_registry, allow_empty_vaults=True)

        lan_cfg = init_data.get("lan_gateway")
        if isinstance(lan_cfg, dict) and lan_cfg.get("enabled"):
            try:
                self.start_lan_gateway(
                    host=str(lan_cfg.get("host") or "0.0.0.0"),
                    port=int(lan_cfg.get("port") or 8090),
                    persist=False,
                )
            except Exception:
                pass

    def close(self) -> None:
        self._shutdown_lan_server()
        self.gateway.close()

    def _shutdown_lan_server(self) -> None:
        with self._lock:
            srv = self._lan_server
            self._lan_server = None
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass

    def _bootstrap_files(self, extra_vault_specs: dict[str, Path]) -> None:
        if not self.tokens_file.is_file():
            _atomic_write_json(self.tokens_file, {"tokens": [], "acl": {}}, private_mode=True)

        if not self.console_file.is_file():
            discovered = discover_vaults_from_clients(home=self.client_home)
            vault_rows: list[dict[str, str]] = []
            seen_ids: set[str] = set()
            seen_paths: set[str] = set()
            for row in discovered:
                vp = str(Path(row["path"]).resolve())
                if vp in seen_paths:
                    continue
                vid = row["id"] if row["id"] not in seen_ids else _slugify_vault_id(row["id"], seen_ids)
                seen_ids.add(vid)
                seen_paths.add(vp)
                vault_rows.append({"id": vid, "path": vp, "added_at": _now_iso()})
            for vid, vpath in extra_vault_specs.items():
                vp = str(vpath.resolve())
                if vp in seen_paths:
                    continue
                use_id = vid if vid not in seen_ids else _slugify_vault_id(vid, seen_ids)
                seen_ids.add(use_id)
                seen_paths.add(vp)
                vault_rows.append({"id": use_id, "path": vp, "added_at": _now_iso()})
            _atomic_write_json(self.console_file, {"version": 1, "vaults": vault_rows})
        elif extra_vault_specs:
            data = self._load_console_data()
            vault_rows = list(data.get("vaults") or [])
            seen_ids = {str(x.get("id")) for x in vault_rows}
            seen_paths = {str(Path(str(x.get("path"))).resolve()) for x in vault_rows if x.get("path")}
            changed = False
            for vid, vpath in extra_vault_specs.items():
                vp = str(vpath.resolve())
                if vp in seen_paths:
                    continue
                use_id = vid if vid not in seen_ids else _slugify_vault_id(vid, seen_ids)
                seen_ids.add(use_id)
                seen_paths.add(vp)
                vault_rows.append({"id": use_id, "path": vp, "added_at": _now_iso()})
                changed = True
            if changed:
                data["vaults"] = vault_rows
                _atomic_write_json(self.console_file, data)

    def _load_console_data(self) -> dict[str, Any]:
        if not self.console_file.is_file():
            return {"version": 1, "vaults": []}
        try:
            d = json.loads(self.console_file.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {"version": 1, "vaults": []}
        except (OSError, ValueError):
            return {"version": 1, "vaults": []}

    def _load_tokens_doc(self) -> dict[str, Any]:
        if not self.tokens_file.is_file():
            return {"tokens": [], "acl": {}}
        try:
            d = json.loads(self.tokens_file.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                if not isinstance(d.get("tokens"), list):
                    d["tokens"] = []
                if not isinstance(d.get("acl"), dict):
                    d["acl"] = {}
                return d
        except (OSError, ValueError):
            pass
        return {"tokens": [], "acl": {}}

    def _save_tokens_doc(self, doc: dict[str, Any]) -> None:
        _atomic_write_json(self.tokens_file, doc, private_mode=True)
        self.token_registry.force_reload()

    def _load_remotes_doc(self) -> dict[str, Any]:
        if not self.remotes_file.is_file():
            return {"credentials": {}}
        try:
            d = json.loads(self.remotes_file.read_text(encoding="utf-8"))
            if isinstance(d, dict) and isinstance(d.get("credentials"), dict):
                return d
        except (OSError, ValueError):
            pass
        return {"credentials": {}}

    def _save_remotes_doc(self, doc: dict[str, Any]) -> None:
        _atomic_write_json(self.remotes_file, doc, private_mode=True)

    def get_remote_token(self, vault_id: str) -> str:
        with self._lock:
            creds = self._load_remotes_doc().get("credentials") or {}
            item = creds.get(vault_id)
            if isinstance(item, dict):
                return str(item.get("token") or "").strip()
        return ""

    # -- Embedded LAN Sharing Gateway (Computer A) -------------------------- #

    def get_lan_gateway_status(self) -> dict[str, Any]:
        with self._lock:
            data = self._load_console_data()
            lan_cfg = data.get("lan_gateway") if isinstance(data.get("lan_gateway"), dict) else {}
            srv = self._lan_server
            local_count = len(self.gateway.services)
            has_tokens = self.token_registry.has_any_token
            tok_doc = self._load_tokens_doc()
            active_tok_count = len([t for t in (tok_doc.get("tokens") or []) if isinstance(t, dict) and not t.get("disabled")])

        cfg_host = str(lan_cfg.get("host") or "0.0.0.0")
        cfg_port = int(lan_cfg.get("port") or 8090)

        if srv is not None:
            bind_host, actual_port = srv.server_address[0], srv.server_address[1]
            running = True
            mode = "embedded"
        else:
            bind_host, actual_port = cfg_host, cfg_port
            ext_up = False
            try:
                with socket.create_connection(("127.0.0.1", actual_port), timeout=0.2):
                    ext_up = True
            except OSError:
                ext_up = False
            running = ext_up
            mode = "external" if ext_up else "stopped"

        lan_ips = detect_lan_ips()
        lan_urls = [f"http://{ip}:{actual_port}" for ip in lan_ips]
        recommended_url = lan_urls[0] if lan_urls else f"http://127.0.0.1:{actual_port}"
        return {
            "running": running,
            "mode": mode,
            "bind_host": str(bind_host),
            "port": int(actual_port),
            "lan_ips": lan_ips,
            "lan_urls": lan_urls,
            "recommended_url": recommended_url,
            "has_tokens": has_tokens,
            "active_token_count": active_tok_count,
            "local_vault_count": local_count,
        }

    def start_lan_gateway(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = 8090,
        persist: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            if self._lan_server is not None:
                cur_h, cur_p = self._lan_server.server_address[0], self._lan_server.server_address[1]
                if port == 0 or (cur_h == host and cur_p == port):
                    return self.get_lan_gateway_status()
                self._shutdown_lan_server()

            srv = create_http_server(self.gateway, host=host, port=port, allow_zero_tokens=True)  # type: ignore[assignment]
            actual_port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True, name="wctx-lan-gateway")
            t.start()
            self._lan_server = srv  # type: ignore[assignment]

            if persist:
                data = self._load_console_data()
                data["lan_gateway"] = {"enabled": True, "host": host, "port": int(actual_port)}
                _atomic_write_json(self.console_file, data)

        return self.get_lan_gateway_status()

    def stop_lan_gateway(self) -> dict[str, Any]:
        self._shutdown_lan_server()
        with self._lock:
            data = self._load_console_data()
            prev = data.get("lan_gateway") if isinstance(data.get("lan_gateway"), dict) else {}
            data["lan_gateway"] = {
                "enabled": False,
                "host": str(prev.get("host") or "0.0.0.0"),
                "port": int(prev.get("port") or 8090),
            }
            _atomic_write_json(self.console_file, data)
        return self.get_lan_gateway_status()

    # -- Vault Inspection & Lifecycle (Local + Remote) ---------------------- #

    def list_vaults_detailed(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self._load_console_data().get("vaults") or [])
            creds = dict(self._load_remotes_doc().get("credentials") or {})

        # Group remote probes by (gateway_url, token) so multiple remote vaults on one server only probe once
        probe_cache: dict[tuple[str, str], tuple[dict[str, Any] | None, str]] = {}
        for item in rows:
            if not isinstance(item, dict) or item.get("kind") != "remote":
                continue
            vid = str(item.get("id") or "").strip()
            gw = str(item.get("gateway_url") or "").strip().rstrip("/")
            tok = str((creds.get(vid) or {}).get("token") or "").strip()
            key = (gw, tok)
            if gw and key not in probe_cache:
                if not tok:
                    probe_cache[key] = (None, "未配置访问令牌")
                else:
                    try:
                        probe_cache[key] = (probe_remote_gateway(gw, tok, timeout=1.2), "")
                    except Exception as exc:
                        probe_cache[key] = (None, str(exc))

        out: list[dict[str, Any]] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            vid = str(item.get("id") or "").strip()
            if not vid:
                continue

            if item.get("kind") == "remote":
                gw = str(item.get("gateway_url") or "").strip().rstrip("/")
                rvid = str(item.get("remote_vault_id") or vid).strip()
                tok = str((creds.get(vid) or {}).get("token") or "").strip()
                probed, err_msg = probe_cache.get((gw, tok), (None, "未指定网关地址"))
                matched_remote: dict[str, Any] | None = None
                if probed is not None:
                    matched_remote = next(
                        (rv for rv in probed.get("vaults", []) if rv.get("remote_vault_id") == rvid),
                        None,
                    )
                    if matched_remote is None and not err_msg:
                        err_msg = f"远端网关未授权或不存在知识库 '{rvid}'"

                ready = matched_remote is not None
                out.append({
                    "id": vid,
                    "kind": "remote",
                    "remote_vault_id": rvid,
                    "gateway_url": gw,
                    "mcp_url": f"{gw}/mcp/{rvid}" if gw else "",
                    "path": f"{gw}/mcp/{rvid}" if gw else "",
                    "token_masked": _mask_token(tok),
                    "has_token": bool(tok),
                    "remote_uid": str((probed or {}).get("uid") or item.get("remote_uid") or ""),
                    "remote_groups": list((probed or {}).get("groups") or item.get("remote_groups") or []),
                    "name": str((matched_remote or {}).get("name") or item.get("name") or rvid),
                    "layout": str((matched_remote or {}).get("layout") or item.get("layout") or "zh"),
                    "added_at": str(item.get("added_at") or ""),
                    "exists": True,
                    "ready": ready,
                    "online_error": "" if ready else err_msg,
                    "generation_id": str((matched_remote or {}).get("generation_id") or ""),
                    "docs": int((matched_remote or {}).get("docs") if matched_remote else (item.get("cached_docs") or 0)),
                    "sections": int((matched_remote or {}).get("sections") if matched_remote else (item.get("cached_sections") or 0)),
                    "wiki_docs": 0,
                    "active_memory_notes": 0,
                    "source_prefixes": [],
                })
                continue

            raw_p = str(item.get("path") or "").strip()
            vpath = Path(raw_p).expanduser().resolve() if raw_p else (self.config_dir / "__missing__")
            entry: dict[str, Any] = {
                "id": vid,
                "kind": "local",
                "path": str(vpath),
                "added_at": item.get("added_at") or "",
                "exists": vpath.is_dir(),
            }
            if not vpath.is_dir():
                entry.update({"name": vpath.name, "layout": "en", "ready": False, "docs": 0, "sections": 0, "wiki_docs": 0, "active_memory_notes": 0})
                out.append(entry)
                continue

            cfg = VaultConfig.load(vpath)
            entry["name"] = cfg.name
            entry["layout"] = cfg.layout
            entry["sources_dir"] = cfg.sources_dir
            entry["wiki_dir"] = cfg.wiki_dir
            entry["notes_dir"] = cfg.notes_dir

            # Source subdirectories & 2-level visual folder tree for graphical ACL editor
            subdirs: list[str] = [f"{cfg.sources_dir}/**"]
            folder_tree: list[dict[str, Any]] = []
            if cfg.sources_root.is_dir():
                for child in sorted(cfg.sources_root.iterdir(), key=lambda p: p.name.lower()):
                    if not child.is_dir() or child.name.startswith("."):
                        continue
                    c_prefix = f"{cfg.sources_dir}/{child.name}/**"
                    subdirs.append(c_prefix)
                    sub_nodes: list[dict[str, Any]] = []
                    try:
                        for sub in sorted(child.iterdir(), key=lambda p: p.name.lower()):
                            if sub.is_dir() and not sub.name.startswith("."):
                                sub_p = f"{cfg.sources_dir}/{child.name}/{sub.name}/**"
                                subdirs.append(sub_p)
                                sub_nodes.append({
                                    "name": sub.name,
                                    "prefix": sub_p,
                                })
                    except PermissionError:
                        pass
                    folder_tree.append({
                        "name": child.name,
                        "prefix": c_prefix,
                        "children": sub_nodes,
                    })
            entry["source_prefixes"] = subdirs
            entry["folder_tree"] = folder_tree

            svc = self.gateway.services.get(vid)
            if svc is None and ((cfg.index_root / "current.json").is_file() or (cfg.index_root / "bm25.sqlite").is_file()):
                try:
                    svc = self.gateway.upsert_vault(vid, vpath)
                except Exception:
                    svc = None

            if svc is not None:
                st = svc.tool_status()
                lst = svc.tool_list({"limit": 300})
                entry.update({
                    "ready": True,
                    "generation_id": st.get("generation_id", ""),
                    "generated_at": st.get("generated_at", ""),
                    "docs": st.get("docs", 0),
                    "pages": st.get("pages", 0),
                    "sections": st.get("sections", 0),
                    "wiki_docs": st.get("wiki_docs", 0),
                    "wiki_sections": st.get("wiki_sections", 0),
                    "orphan_docs": st.get("orphan_docs", 0),
                    "docs_with_visual": st.get("docs_with_visual", 0),
                    "active_memory_notes": st.get("active_memory_notes", 0),
                    "sample_sources": [d["source"] for d in (lst.get("documents") or [])[:200]],
                })
            else:
                mem_cnt = len(MemoryStore(vpath, scrub_fn=scrub_secrets).list_notes(status="active"))
                entry.update({
                    "ready": False,
                    "generation_id": "",
                    "docs": 0,
                    "sections": 0,
                    "wiki_docs": 0,
                    "active_memory_notes": mem_cnt,
                    "sample_sources": [],
                })
            out.append(entry)
        return out

    def get_vault_record(self, vault_id: str) -> dict[str, Any]:
        with self._lock:
            for item in self._load_console_data().get("vaults") or []:
                if isinstance(item, dict) and str(item.get("id")) == vault_id:
                    return dict(item)
        raise KeyError(f"Unknown vault_id '{vault_id}'")

    def get_vault_path(self, vault_id: str) -> Path:
        rec = self.get_vault_record(vault_id)
        if rec.get("kind") == "remote":
            raise ValueError(f"知识库 '{vault_id}' 为远程网关知识库，原件与记忆存储在提供端电脑上")
        raw_p = str(rec.get("path") or "").strip()
        if not raw_p:
            raise KeyError(f"Missing local path for vault_id '{vault_id}'")
        return Path(raw_p).expanduser().resolve()

    def onboard_and_register(
        self,
        *,
        path: str,
        vault_id: str = "",
        name: str = "",
        layout: str = "",
        adopt_loose_files: bool = True,
    ) -> dict[str, Any]:
        target = Path(path).expanduser().resolve()
        rep = onboard_folder(
            target,
            name=name or None,
            layout=layout or None,
            adopt_loose_files=adopt_loose_files,
            run_sync=True,
        )
        with self._lock:
            data = self._load_console_data()
            vaults = list(data.get("vaults") or [])
            existing_by_path = next(
                (
                    v
                    for v in vaults
                    if v.get("kind") != "remote"
                    and v.get("path")
                    and Path(str(v.get("path"))).resolve() == target
                ),
                None,
            )
            if existing_by_path is not None:
                vid = str(existing_by_path["id"])
            else:
                existing_ids = {str(v.get("id")) for v in vaults}
                clean_req_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", vault_id.strip()).strip("-")
                if clean_req_id and clean_req_id in existing_ids:
                    raise ValueError(f"知识库 ID '{clean_req_id}' 已存在，请更换一个标识")
                vid = clean_req_id if clean_req_id else _slugify_vault_id(rep["name"], existing_ids)
                vaults.append({"id": vid, "path": str(target), "added_at": _now_iso()})
                data["vaults"] = vaults
                _atomic_write_json(self.console_file, data)

        self.gateway.upsert_vault(vid, target)
        return {"vault_id": vid, **rep}

    def import_folder(self, *, vault_id: str, external_folder: str, target_subfolder: str = "") -> dict[str, Any]:
        vpath = self.get_vault_path(vault_id)
        rep = import_external_folder(
            vpath,
            external_folder,
            target_subfolder=target_subfolder or None,
            run_sync=True,
        )
        self.gateway.upsert_vault(vault_id, vpath)
        return {"vault_id": vault_id, **rep}

    def sync_registered_vault(self, vault_id: str, *, force: bool = False) -> dict[str, Any]:
        vpath = self.get_vault_path(vault_id)
        rep = sync_vault(vpath, force=force, rebuild=True)
        self.gateway.upsert_vault(vault_id, vpath)
        return {"vault_id": vault_id, **rep}

    def lint_registered_vault(self, vault_id: str) -> dict[str, Any]:
        vpath = self.get_vault_path(vault_id)
        return lint_vault(vpath)

    def connect_remote_gateway(
        self,
        *,
        gateway_url: str,
        token: str,
        selected_remote_ids: list[str] | None = None,
        sync_clients: bool = False,
    ) -> dict[str, Any]:
        """Connect to Computer A's gateway, register authorized remote vaults, and optionally auto-sync AI clients."""
        probed = probe_remote_gateway(gateway_url, token)
        base = probed["gateway_url"]
        clean_tok = token.strip()
        avail = probed["vaults"]
        if not avail:
            raise ValueError("连接网关成功，但该令牌下暂无任何授权可见的知识库")

        wanted_set = {str(x).strip() for x in (selected_remote_ids or []) if str(x).strip()}
        chosen = [v for v in avail if not wanted_set or v["remote_vault_id"] in wanted_set]
        if not chosen:
            raise ValueError("请至少勾选一个要接入的远程知识库")

        connected: list[dict[str, Any]] = []
        with self._lock:
            data = self._load_console_data()
            vaults = list(data.get("vaults") or [])
            rem_doc = self._load_remotes_doc()
            creds = dict(rem_doc.get("credentials") or {})
            existing_ids = {str(v.get("id")) for v in vaults if isinstance(v, dict)}

            for rv in chosen:
                rvid = rv["remote_vault_id"]
                existing_entry = next(
                    (
                        v
                        for v in vaults
                        if isinstance(v, dict)
                        and v.get("kind") == "remote"
                        and str(v.get("gateway_url") or "").rstrip("/") == base
                        and str(v.get("remote_vault_id") or v.get("id")) == rvid
                    ),
                    None,
                )
                if existing_entry is not None:
                    local_id = str(existing_entry["id"])
                    existing_entry.update({
                        "name": rv["name"],
                        "layout": rv["layout"],
                        "gateway_url": base,
                        "remote_uid": probed["uid"],
                        "remote_groups": probed["groups"],
                        "cached_docs": rv["docs"],
                        "cached_sections": rv["sections"],
                    })
                else:
                    if rvid not in existing_ids:
                        local_id = rvid
                    else:
                        local_id = _slugify_vault_id(f"{rvid}-remote", existing_ids)
                    existing_ids.add(local_id)
                    vaults.append({
                        "id": local_id,
                        "kind": "remote",
                        "remote_vault_id": rvid,
                        "name": rv["name"],
                        "layout": rv["layout"],
                        "gateway_url": base,
                        "remote_uid": probed["uid"],
                        "remote_groups": probed["groups"],
                        "cached_docs": rv["docs"],
                        "cached_sections": rv["sections"],
                        "added_at": _now_iso(),
                    })
                creds[local_id] = {"token": clean_tok, "updated_at": _now_iso()}
                connected.append({
                    "id": local_id,
                    "remote_vault_id": rvid,
                    "name": rv["name"],
                    "mcp_url": f"{base}/mcp/{rvid}",
                })

            data["vaults"] = vaults
            rem_doc["credentials"] = creds
            _atomic_write_json(self.console_file, data)
            self._save_remotes_doc(rem_doc)

        client_sync_results: list[dict[str, Any]] = []
        if sync_clients:
            installed_ids = [c["id"] for c in inspect_all_clients(home=self.client_home) if c["installed"]]
            if installed_ids:
                desired = self.build_desired_client_servers(mode="auto")
                client_sync_results = [
                    sync_client_mcp_config(cid, desired, remove_unlisted_wctx=True, home=self.client_home)
                    for cid in installed_ids
                ]

        return {
            "gateway_url": base,
            "uid": probed["uid"],
            "groups": probed["groups"],
            "connected_vaults": connected,
            "client_sync": client_sync_results,
        }

    def update_remote_vault(
        self,
        vault_id: str,
        *,
        gateway_url: str | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        rec = self.get_vault_record(vault_id)
        if rec.get("kind") != "remote":
            raise ValueError(f"'{vault_id}' 不是远程知识库")
        new_base = normalize_gateway_url(gateway_url or str(rec.get("gateway_url") or ""))
        new_tok = (token or "").strip() or self.get_remote_token(vault_id)
        probed = probe_remote_gateway(new_base, new_tok)
        rvid = str(rec.get("remote_vault_id") or vault_id)
        matched = next((v for v in probed["vaults"] if v["remote_vault_id"] == rvid), None)
        if matched is None:
            raise ValueError(f"新网关或令牌下无权访问知识库 '{rvid}'")

        with self._lock:
            data = self._load_console_data()
            for v in data.get("vaults") or []:
                if isinstance(v, dict) and str(v.get("id")) == vault_id:
                    v.update({
                        "gateway_url": new_base,
                        "name": matched["name"],
                        "layout": matched["layout"],
                        "remote_uid": probed["uid"],
                        "remote_groups": probed["groups"],
                        "cached_docs": matched["docs"],
                        "cached_sections": matched["sections"],
                    })
            _atomic_write_json(self.console_file, data)
            rem_doc = self._load_remotes_doc()
            rem_doc.setdefault("credentials", {})[vault_id] = {"token": new_tok, "updated_at": _now_iso()}
            self._save_remotes_doc(rem_doc)

        return {"vault_id": vault_id, "gateway_url": new_base, "uid": probed["uid"]}

    def build_desired_client_servers(
        self,
        vault_ids: list[str] | None = None,
        *,
        mode: str = "auto",
        gateway_url: str = "",
        token: str = "",
    ) -> list[dict[str, Any]]:
        """Build client MCP server specs supporting local, remote, and hybrid setups cleanly."""
        all_vaults = {v["id"]: v for v in self.list_vaults_detailed()}
        target_ids = vault_ids if vault_ids is not None else list(all_vaults.keys())
        gw_override = gateway_url.strip().rstrip("/")
        tok_override = token.strip()

        desired: list[dict[str, Any]] = []
        for vid in target_ids:
            vinfo = all_vaults.get(vid)
            if not vinfo:
                continue
            if vinfo.get("kind") == "remote":
                eff_tok = tok_override or self.get_remote_token(vid)
                rvid = str(vinfo.get("remote_vault_id") or vid)
                eff_url = f"{gw_override}/mcp/{rvid}" if (mode in {"http", "bridge"} and gw_override) else str(vinfo["mcp_url"])
                desired.append({
                    "server_name": vid,
                    "mode": "bridge" if mode == "bridge" else "http",
                    "url": eff_url,
                    "token": eff_tok,
                })
            elif mode in {"http", "bridge"}:
                desired.append({
                    "server_name": vid,
                    "mode": mode,
                    "url": f"{gw_override}/mcp/{vid}",
                    "token": tok_override,
                })
            else:
                desired.append({
                    "server_name": vid,
                    "mode": "stdio",
                    "vault_path": vinfo["path"],
                })
        return desired

    def query_registered_vault(self, vault_id: str, question: str, *, max_tokens: int | None = None) -> dict[str, Any]:
        rec = self.get_vault_record(vault_id)
        if rec.get("kind") == "remote":
            gw = str(rec.get("gateway_url") or "").rstrip("/")
            rvid = str(rec.get("remote_vault_id") or vault_id)
            tok = self.get_remote_token(vault_id)
            if not tok:
                raise ValueError(f"远程知识库 '{vault_id}' 缺少访问令牌")
            payload: dict[str, Any] = {"vault": rvid, "question": question}
            if max_tokens:
                payload["max_tokens"] = int(max_tokens)
            return _http_json_request(f"{gw}/v1/context", method="POST", token=tok, payload=payload, timeout=15.0)

        vpath = self.get_vault_path(vault_id)
        svc = self.gateway.services.get(vault_id) or self.gateway.upsert_vault(vault_id, vpath)
        args: dict[str, Any] = {"question": question}
        if max_tokens:
            args["max_tokens"] = int(max_tokens)
        return svc.tool_context(args, principal=Principal(uid="console-admin", groups=("admin",)))

    def unregister_vault(self, vault_id: str) -> bool:
        with self._lock:
            data = self._load_console_data()
            before = list(data.get("vaults") or [])
            after = [v for v in before if str(v.get("id")) != vault_id]
            if len(after) == len(before):
                return False
            data["vaults"] = after
            _atomic_write_json(self.console_file, data)
            rem_doc = self._load_remotes_doc()
            if vault_id in (rem_doc.get("credentials") or {}):
                rem_doc["credentials"].pop(vault_id, None)
                self._save_remotes_doc(rem_doc)
        self.gateway.remove_vault(vault_id)
        return True

    def rename_vault_id(self, old_id: str, new_id: str, *, sync_clients: bool = True) -> dict[str, Any]:
        clean_new = re.sub(r"[^a-zA-Z0-9_-]+", "-", (new_id or "").strip()).strip("-")
        if not clean_new:
            raise ValueError("新的知识库 ID 不能为空（仅限字母、数字、下划线或连字符）")
        if clean_new in {"all", "admin", "*"}:
            raise ValueError(f"'{clean_new}' 为保留字，请使用其他标识（如 wiki、wiki-team）")
        if clean_new == old_id:
            return {"old_id": old_id, "new_id": clean_new, "client_sync": []}

        local_path: Path | None = None
        with self._lock:
            data = self._load_console_data()
            vaults = list(data.get("vaults") or [])
            target = next((v for v in vaults if isinstance(v, dict) and str(v.get("id")) == old_id), None)
            if target is None:
                raise KeyError(f"未找到知识库 '{old_id}'")
            if any(isinstance(v, dict) and str(v.get("id")) == clean_new for v in vaults):
                raise ValueError(f"知识库 ID '{clean_new}' 已存在，请更换一个标识")
            target["id"] = clean_new
            if target.get("kind") != "remote" and target.get("path"):
                local_path = Path(str(target["path"])).expanduser().resolve()
            data["vaults"] = vaults
            _atomic_write_json(self.console_file, data)

            rem_doc = self._load_remotes_doc()
            creds = rem_doc.get("credentials") or {}
            if old_id in creds:
                creds[clean_new] = creds.pop(old_id)
                self._save_remotes_doc(rem_doc)

            tok_doc = self._load_tokens_doc()
            tok_changed = False
            for t in tok_doc.get("tokens") or []:
                if not isinstance(t, dict):
                    continue
                av = t.get("allowed_vaults")
                if isinstance(av, list) and old_id in av:
                    t["allowed_vaults"] = [clean_new if x == old_id else x for x in av]
                    tok_changed = True
            if tok_changed:
                self._save_tokens_doc(tok_doc)

        self.gateway.remove_vault(old_id)
        if local_path is not None and local_path.is_dir():
            self.gateway.upsert_vault(clean_new, local_path)

        client_sync_results: list[dict[str, Any]] = []
        if sync_clients:
            installed_ids = [c["id"] for c in inspect_all_clients(home=self.client_home) if c["installed"]]
            if installed_ids:
                desired = self.build_desired_client_servers(mode="auto")
                client_sync_results = [
                    sync_client_mcp_config(cid, desired, remove_unlisted_wctx=True, home=self.client_home)
                    for cid in installed_ids
                ]
        return {"old_id": old_id, "new_id": clean_new, "client_sync": client_sync_results}

    # -- Tokens & Role Prefix ACL ------------------------------------------- #

    def list_tokens_public(self) -> list[dict[str, Any]]:
        with self._lock:
            doc = self._load_tokens_doc()
        out: list[dict[str, Any]] = []
        for idx, row in enumerate(doc.get("tokens") or []):
            if not isinstance(row, dict):
                continue
            tid = str(row.get("id") or f"tok_{idx}")
            preview = str(row.get("token_preview") or "")
            if not preview and row.get("token_sha256"):
                preview = f"sha256:{str(row['token_sha256'])[:10]}…"
            elif not preview and row.get("token"):
                t_raw = str(row["token"])
                preview = f"{t_raw[:7]}…{t_raw[-4:]}" if len(t_raw) > 12 else "••••••"
            out.append({
                "id": tid,
                "label": str(row.get("label") or row.get("uid") or ""),
                "uid": str(row.get("uid") or ""),
                "token_preview": preview,
                "groups": [str(g) for g in (row.get("groups") or ["all"])],
                "allowed_vaults": [str(v) for v in (row.get("allowed_vaults") or ["*"])],
                "disabled": bool(row.get("disabled") is True),
                "created_at": str(row.get("created_at") or ""),
            })
        return out

    def create_token(
        self,
        *,
        uid: str,
        label: str = "",
        groups: list[str] | None = None,
        allowed_vaults: list[str] | None = None,
    ) -> dict[str, Any]:
        uid_clean = re.sub(r"[^\w\u4e00-\u9fff.@:-]+", "-", uid.strip()).strip("-")
        if not uid_clean:
            raise ValueError("令牌身份标识 (uid) 不能为空")
        grp_clean = [str(g).strip() for g in (groups or ["all"]) if str(g).strip()] or ["all"]
        av_clean = [str(v).strip() for v in (allowed_vaults or ["*"]) if str(v).strip()] or ["*"]

        raw_secret = f"wctx_{secrets.token_urlsafe(24)}"
        tok_id = f"tok_{secrets.token_hex(5)}"
        preview = f"{raw_secret[:9]}…{raw_secret[-4:]}"
        record = {
            "id": tok_id,
            "label": (label or uid_clean).strip()[:80],
            "uid": uid_clean,
            "token_sha256": _sha256_hex(raw_secret),
            "token_preview": preview,
            "groups": grp_clean,
            "allowed_vaults": av_clean,
            "disabled": False,
            "created_at": _now_iso(),
        }
        with self._lock:
            doc = self._load_tokens_doc()
            # Ensure older entries also get stable `id`s
            for i, r in enumerate(doc["tokens"]):
                if isinstance(r, dict) and not r.get("id"):
                    r["id"] = f"tok_legacy_{i}"
            doc["tokens"].append(record)
            self._save_tokens_doc(doc)

        return {
            "token": raw_secret,
            "record": {k: v for k, v in record.items() if k != "token_sha256"},
        }

    def update_token(
        self,
        token_id: str,
        *,
        label: str | None = None,
        groups: list[str] | None = None,
        allowed_vaults: list[str] | None = None,
        disabled: bool | None = None,
    ) -> bool:
        with self._lock:
            doc = self._load_tokens_doc()
            found = False
            for i, r in enumerate(doc["tokens"]):
                if not isinstance(r, dict):
                    continue
                rid = str(r.get("id") or f"tok_{i}")
                if rid == token_id:
                    r["id"] = rid
                    if label is not None:
                        r["label"] = label.strip()[:80]
                    if groups is not None:
                        r["groups"] = [str(g).strip() for g in groups if str(g).strip()] or ["all"]
                    if allowed_vaults is not None:
                        r["allowed_vaults"] = [str(v).strip() for v in allowed_vaults if str(v).strip()] or ["*"]
                    if disabled is not None:
                        r["disabled"] = bool(disabled)
                    found = True
                    break
            if found:
                self._save_tokens_doc(doc)
            return found

    def delete_token(self, token_id: str) -> bool:
        with self._lock:
            doc = self._load_tokens_doc()
            before = len(doc["tokens"])
            kept = [
                r for i, r in enumerate(doc["tokens"])
                if isinstance(r, dict) and str(r.get("id") or f"tok_{i}") != token_id
            ]
            if len(kept) == before:
                return False
            doc["tokens"] = kept
            self._save_tokens_doc(doc)
            return True

    def get_acl_rules(self) -> dict[str, dict[str, list[str]]]:
        with self._lock:
            return dict(self._load_tokens_doc().get("acl") or {})

    def save_acl_rules(self, acl: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
        cleaned: dict[str, dict[str, list[str]]] = {}
        for role, spec in (acl or {}).items():
            rname = str(role).strip()
            if not rname or rname in {"admin", "all"} or not isinstance(spec, dict):
                continue
            allow = [str(x).strip() for x in (spec.get("allow") or []) if str(x).strip()]
            deny = [str(x).strip() for x in (spec.get("deny") or []) if str(x).strip()]
            cleaned[rname] = {"allow": allow, "deny": deny}
        with self._lock:
            doc = self._load_tokens_doc()
            doc["acl"] = cleaned
            self._save_tokens_doc(doc)
        return cleaned

    def simulate_acl(self, *, token_id: str, vault_id: str) -> dict[str, Any]:
        tokens = self.list_tokens_public()
        tok = next((t for t in tokens if t["id"] == token_id), None)
        if tok is None:
            raise ValueError(f"Unknown token_id '{token_id}'")
        principal = Principal(
            uid=tok["uid"],
            groups=tuple(tok["groups"]),
            allowed_vaults=tuple(tok["allowed_vaults"]),
        )
        if tok["disabled"]:
            return {
                "vault_allowed": False,
                "reason": "该令牌当前处于「已禁用」状态，网关会直接拒绝 (401)",
                "allowed_sources": [],
                "denied_sources": [],
                "allowed_wiki": [],
                "denied_wiki": [],
            }
        if not principal.can_access_vault(vault_id):
            return {
                "vault_allowed": False,
                "reason": f"该令牌未授权访问知识库 '{vault_id}'（网关将拦截并返回 403 Forbidden）",
                "allowed_sources": [],
                "denied_sources": [],
                "allowed_wiki": [],
                "denied_wiki": [],
            }

        vpath = self.get_vault_path(vault_id)
        svc = self.gateway.services.get(vault_id) or self.gateway.upsert_vault(vault_id, vpath)
        vcfg = VaultConfig.load(vpath)
        merged_rules = dict(vcfg.acl_rules)
        merged_rules.update(self.get_acl_rules())
        pol = PrefixACLPolicy(merged_rules) if merged_rules else AllowAllPolicy()

        snap = svc._get_snapshot()
        allowed_srcs: list[str] = []
        denied_srcs: list[str] = []
        for d in snap.docs:
            src = str(d.get("source") or "")
            if not src:
                continue
            if pol.can_access_source(principal, src):
                allowed_srcs.append(src)
            else:
                denied_srcs.append(src)

        seen_wiki: dict[str, list[str]] = {}
        gen_dir = snap.index._resolve_generation_dir()
        wiki_jsonl = gen_dir / "wiki_sections.jsonl"
        if wiki_jsonl.is_file():
            for line in wiki_jsonl.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                w = json.loads(line)
                ws = str(w.get("source") or "")
                if ws and ws not in seen_wiki:
                    seen_wiki[ws] = list(w.get("sources") or [])

        allowed_wiki: list[str] = []
        denied_wiki: list[str] = []
        for ws, refs in sorted(seen_wiki.items()):
            if pol.can_access_wiki(principal, ws, refs):
                allowed_wiki.append(ws)
            else:
                denied_wiki.append(ws)

        return {
            "vault_allowed": True,
            "reason": "知识库级访问通过；以下为目录前缀 ACL 及 Wiki 溯源继承模拟结果",
            "principal": {"uid": principal.uid, "groups": list(principal.groups), "allowed_vaults": list(principal.allowed_vaults)},
            "allowed_sources_count": len(allowed_srcs),
            "denied_sources_count": len(denied_srcs),
            "allowed_sources": allowed_srcs[:80],
            "denied_sources": denied_srcs[:80],
            "allowed_wiki_count": len(allowed_wiki),
            "denied_wiki_count": len(denied_wiki),
            "allowed_wiki": allowed_wiki[:50],
            "denied_wiki": denied_wiki[:50],
        }

    # -- Layer-4 Memory & Errata -------------------------------------------- #

    @staticmethod
    def _serialize_note_item(n: MemoryNote) -> dict[str, Any]:
        return {
            "note_id": n.note_id,
            "kind": n.kind,
            "status": n.status,
            "author": n.author,
            "created_at": n.created_at,
            "related_sources": list(n.related_sources),
            "tags": list(n.tags),
            "text": n.text,
            "file": str(n.file_path),
        }

    def list_memories(
        self,
        vault_id: str,
        *,
        status: str = "all",
        kind: str = "all",
        query: str = "",
    ) -> dict[str, Any]:
        vpath = self.get_vault_path(vault_id)
        store = MemoryStore(vpath, scrub_fn=scrub_secrets)
        st_arg = None if status in {"", "all"} else status
        notes = store.list_notes(status=st_arg)
        q_low = query.strip().lower()
        filtered: list[dict[str, Any]] = []
        for n in notes:
            if kind not in {"", "all"} and n.kind != kind:
                continue
            if q_low:
                hay = f"{n.note_id} {n.text} {' '.join(n.tags)} {' '.join(n.related_sources)}".lower()
                if q_low not in hay:
                    continue
            filtered.append(self._serialize_note_item(n))

        counts = {"active": 0, "consolidated": 0, "archived": 0, "errata": 0}
        for n in store.list_notes(status=None):
            if n.status in counts:
                counts[n.status] += 1
            if n.kind == "errata" and n.status == "active":
                counts["errata"] += 1

        return {
            "vault_id": vault_id,
            "vault_name": store.cfg.name,
            "notes_dir": str(store.cfg.notes_root),
            "counts": counts,
            "notes": filtered,
        }

    def create_memory(
        self,
        vault_id: str,
        *,
        text: str,
        kind: str = "insight",
        related_sources: list[str] | None = None,
        tags: list[str] | None = None,
        author: str = "console",
    ) -> dict[str, Any]:
        vpath = self.get_vault_path(vault_id)
        store = MemoryStore(vpath, scrub_fn=scrub_secrets)
        note = store.remember(
            text,
            kind=kind,
            related_sources=related_sources or [],
            tags=tags or [],
            author=author or "console",
        )
        return self._serialize_note_item(note)

    def update_memory(
        self,
        vault_id: str,
        note_id: str,
        *,
        text: str | None = None,
        kind: str | None = None,
        status: str | None = None,
        related_sources: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        vpath = self.get_vault_path(vault_id)
        store = MemoryStore(vpath, scrub_fn=scrub_secrets)
        updated = store.update_note(
            note_id,
            text=text,
            kind=kind,
            status=status,
            related_sources=related_sources,
            tags=tags,
        )
        if updated is None:
            raise KeyError(f"Memory note '{note_id}' not found")
        return self._serialize_note_item(updated)

    def delete_memory(self, vault_id: str, note_id: str) -> bool:
        vpath = self.get_vault_path(vault_id)
        store = MemoryStore(vpath, scrub_fn=scrub_secrets)
        return store.delete_note(note_id)

    def import_memories(self, vault_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        vpath = self.get_vault_path(vault_id)
        store = MemoryStore(vpath, scrub_fn=scrub_secrets)
        created = store.import_json_items(items, default_author="console-import")
        return {"imported_count": len(created), "note_ids": created}

    # -- Local Filesystem Browser Helper ------------------------------------ #

    @staticmethod
    def browse_directory(raw_path: str = "") -> dict[str, Any]:
        home = Path.home().resolve()
        target = Path(raw_path).expanduser().resolve() if raw_path.strip() else home
        if not target.exists() or not target.is_dir():
            target = home

        dirs: list[dict[str, Any]] = []
        doc_count = 0
        try:
            for item in sorted(target.iterdir(), key=lambda p: p.name.lower()):
                if item.name.startswith("."):
                    continue
                if item.is_dir():
                    is_vault = (
                        (item / "vault.json").is_file()
                        or (item / "史料").is_dir()
                        or (item / "sources").is_dir()
                    )
                    dirs.append({
                        "name": item.name,
                        "path": str(item),
                        "is_vault": is_vault,
                    })
                elif item.is_file():
                    doc_count += 1
        except PermissionError:
            pass

        shortcuts = []
        for label, cand in (
            ("主目录 (~)", home),
            ("文稿 (Documents)", home / "Documents"),
            ("桌面 (Desktop)", home / "Desktop"),
            ("Obsidian 知识库", home / "Documents" / "obsidian知识库"),
            ("工作区 (Workspace)", Path.cwd().resolve()),
        ):
            if cand.is_dir():
                shortcuts.append({"label": label, "path": str(cand)})

        parent = str(target.parent) if target.parent != target else ""
        return {
            "current": str(target),
            "parent": parent,
            "file_count_here": doc_count,
            "directories": dirs[:250],
            "shortcuts": shortcuts,
        }


def create_console_server(
    state: ConsoleState,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    allow_remote_admin: bool = False,
) -> _HighBacklogHTTPServer:
    if not allow_remote_admin and not _is_loopback_host(host):
        raise ValueError(
            f"Refusing to bind admin Control Console to non-loopback host '{host}' without --allow-remote-admin"
        )

    csrf_token = secrets.token_urlsafe(24)

    class ConsoleHTTPHandler(GatewayRoutesMixin, BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        gateway = state.gateway

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

        def _send_html(self, status: int, html_text: str) -> None:
            raw = html_text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _require_admin_csrf(self) -> bool:
            origin = (self.headers.get("Origin") or "").strip()
            if origin:
                parsed_origin = urlparse(origin)
                req_host = (self.headers.get("Host") or "").strip()
                if parsed_origin.netloc and req_host and parsed_origin.netloc != req_host:
                    self._send_json(403, {"error": "Cross-origin admin request blocked"})
                    return False
            supplied = (self.headers.get("X-WCTX-CSRF") or "").strip()
            if not supplied or not hmac.compare_digest(supplied, csrf_token):
                self._send_json(403, {"error": "Missing or invalid X-WCTX-CSRF session token"})
                return False
            return True

        def _gateway_base_url(self) -> str:
            actual_port = self.server.server_address[1]  # type: ignore[attr-defined]
            disp_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
            return f"http://{disp_host}:{actual_port}"

        def _build_overview(self) -> dict[str, Any]:
            vaults = state.list_vaults_detailed()
            vault_by_id = {v["id"]: v for v in vaults}
            clients = inspect_all_clients(home=state.client_home)
            for c in clients:
                cfg_srvs = c["configured_servers"]
                matched = 0
                for vid, vinfo in vault_by_id.items():
                    srv_cfg = cfg_srvs.get(vid)
                    if not srv_cfg:
                        continue
                    if vinfo.get("kind") == "remote":
                        if str(srv_cfg.get("url") or "").rstrip("/") == str(vinfo.get("mcp_url") or "").rstrip("/"):
                            matched += 1
                    else:
                        vp = str(srv_cfg.get("vault_path") or "")
                        if not vp or Path(vp).resolve() == Path(vinfo["path"]).resolve():
                            matched += 1
                if not vaults:
                    c["sync_status"] = "idle"
                elif matched == len(vaults):
                    c["sync_status"] = "synced"
                elif matched > 0:
                    c["sync_status"] = "partial"
                else:
                    c["sync_status"] = "unconfigured"
                c["matched_count"] = matched
                c["total_vaults"] = len(vaults)

            cmd, base_args = resolve_wctx_command()
            return {
                "version": __version__,
                "config_dir": str(state.config_dir),
                "tokens_file": str(state.tokens_file),
                "gateway_base_url": self._gateway_base_url(),
                "lan_gateway": state.get_lan_gateway_status(),
                "wctx_command": " ".join([cmd, *base_args]),
                "vaults": vaults,
                "tokens": state.list_tokens_public(),
                "acl": state.get_acl_rules(),
                "clients": clients,
            }

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            qs = parse_qs(parsed.query)

            if path in {"/", "/index.html"}:
                boot = {
                    "csrfToken": csrf_token,
                    "version": __version__,
                    "gatewayBaseUrl": self._gateway_base_url(),
                }
                self._send_html(200, render_console_html(boot))
                return

            if self.try_gateway_get(path):
                return

            if path.startswith("/api/"):
                if not self._require_admin_csrf():
                    return
                try:
                    if path == "/api/overview":
                        self._send_json(200, self._build_overview())
                        return
                    if path == "/api/doctor":
                        actual_port = self.server.server_address[1]  # type: ignore[attr-defined]
                        self._send_json(
                            200,
                            run_doctor(
                                config_dir=state.config_dir,
                                client_home=state.client_home,
                                ui_port=actual_port,
                            ),
                        )
                        return
                    if path == "/api/fs/list":
                        raw_p = (qs.get("path") or [""])[0]
                        self._send_json(200, state.browse_directory(raw_p))
                        return
                    if path == "/api/memory/list":
                        vid = (qs.get("vault_id") or [""])[0]
                        st_f = (qs.get("status") or ["all"])[0]
                        kd_f = (qs.get("kind") or ["all"])[0]
                        q_f = (qs.get("q") or [""])[0]
                        self._send_json(200, state.list_memories(vid, status=st_f, kind=kd_f, query=q_f))
                        return
                except Exception as exc:
                    self._send_json(400, {"error": str(exc)})
                    return

            self._send_json(404, {"error": f"Not found: {path}"})

        def do_DELETE(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            if self.try_gateway_delete(path):
                return
            self._send_json(404, {"error": f"Not found: {path}"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            if self.try_gateway_post(path):
                return

            if not path.startswith("/api/"):
                self._send_json(404, {"error": f"Not found: {path}"})
                return

            if not self._require_admin_csrf():
                return

            body, err = self._read_json_body(max_bytes=4 * 1024 * 1024)
            if err is not None or not isinstance(body, dict):
                self._send_json(400, {"error": err or "JSON object body required"})
                return

            try:
                if path == "/api/fs/inspect":
                    info = inspect_folder(str(body.get("path") or ""))
                    existing_ids = {v["id"] for v in state.list_vaults_detailed()}
                    info["suggested_id"] = _slugify_vault_id(info["suggested_name"], existing_ids)
                    self._send_json(200, info)
                    return

                if path == "/api/fs/mkdir":
                    parent = Path(str(body.get("parent") or "")).expanduser().resolve()
                    name = str(body.get("name") or "").strip().strip("/")
                    if not name or "/" in name or ".." in name:
                        raise ValueError("请输入合法的新文件夹名称")
                    target = parent / name
                    target.mkdir(parents=True, exist_ok=True)
                    self._send_json(200, {"created": str(target)})
                    return

                if path == "/api/vaults/onboard":
                    res = state.onboard_and_register(
                        path=str(body.get("path") or ""),
                        vault_id=str(body.get("vault_id") or ""),
                        name=str(body.get("name") or ""),
                        layout=str(body.get("layout") or ""),
                        adopt_loose_files=bool(body.get("adopt_loose_files", True)),
                    )
                    self._send_json(200, res)
                    return

                if path == "/api/vaults/import-folder":
                    res = state.import_folder(
                        vault_id=str(body.get("vault_id") or ""),
                        external_folder=str(body.get("external_folder") or ""),
                        target_subfolder=str(body.get("target_subfolder") or ""),
                    )
                    self._send_json(200, res)
                    return

                if path == "/api/vaults/sync":
                    res = state.sync_registered_vault(
                        str(body.get("vault_id") or ""),
                        force=bool(body.get("force", False)),
                    )
                    self._send_json(200, res)
                    return

                if path == "/api/vaults/lint":
                    res = state.lint_registered_vault(str(body.get("vault_id") or ""))
                    self._send_json(200, res)
                    return

                if path == "/api/vaults/query":
                    res = state.query_registered_vault(
                        str(body.get("vault_id") or ""),
                        str(body.get("question") or ""),
                        max_tokens=body.get("max_tokens"),
                    )
                    self._send_json(200, res)
                    return

                if path == "/api/vaults/remove":
                    ok = state.unregister_vault(str(body.get("vault_id") or ""))
                    self._send_json(200, {"removed": ok})
                    return

                if path == "/api/vaults/rename":
                    res = state.rename_vault_id(
                        str(body.get("old_id") or ""),
                        str(body.get("new_id") or ""),
                        sync_clients=bool(body.get("sync_clients", True)),
                    )
                    self._send_json(200, res)
                    return

                if path == "/api/tokens/create":
                    res = state.create_token(
                        uid=str(body.get("uid") or ""),
                        label=str(body.get("label") or ""),
                        groups=body.get("groups"),
                        allowed_vaults=body.get("allowed_vaults"),
                    )
                    self._send_json(200, res)
                    return

                if path == "/api/tokens/update":
                    ok = state.update_token(
                        str(body.get("id") or ""),
                        label=body.get("label"),
                        groups=body.get("groups"),
                        allowed_vaults=body.get("allowed_vaults"),
                        disabled=body.get("disabled"),
                    )
                    self._send_json(200, {"updated": ok})
                    return

                if path == "/api/tokens/delete":
                    ok = state.delete_token(str(body.get("id") or ""))
                    self._send_json(200, {"deleted": ok})
                    return

                if path == "/api/acl/save":
                    saved = state.save_acl_rules(body.get("acl") or {})
                    self._send_json(200, {"acl": saved})
                    return

                if path == "/api/acl/simulate":
                    sim = state.simulate_acl(
                        token_id=str(body.get("token_id") or ""),
                        vault_id=str(body.get("vault_id") or ""),
                    )
                    self._send_json(200, sim)
                    return

                if path == "/api/remotes/probe":
                    res = probe_remote_gateway(
                        str(body.get("gateway_url") or ""),
                        str(body.get("token") or ""),
                    )
                    self._send_json(200, res)
                    return

                if path == "/api/remotes/connect":
                    res = state.connect_remote_gateway(
                        gateway_url=str(body.get("gateway_url") or ""),
                        token=str(body.get("token") or ""),
                        selected_remote_ids=body.get("remote_vault_ids"),
                        sync_clients=bool(body.get("sync_clients", True)),
                    )
                    self._send_json(200, res)
                    return

                if path == "/api/remotes/update":
                    res = state.update_remote_vault(
                        str(body.get("vault_id") or ""),
                        gateway_url=body.get("gateway_url"),
                        token=body.get("token"),
                    )
                    self._send_json(200, res)
                    return

                if path == "/api/lan-gateway/start":
                    req_port = int(body["port"]) if body.get("port") is not None else 8090
                    res = state.start_lan_gateway(
                        host=str(body.get("host") or "0.0.0.0"),
                        port=req_port,
                    )
                    self._send_json(200, res)
                    return

                if path == "/api/lan-gateway/stop":
                    res = state.stop_lan_gateway()
                    self._send_json(200, res)
                    return

                if path in {"/api/clients/preview", "/api/clients/sync"}:
                    mode = str(body.get("mode") or "auto")
                    vault_ids = [str(x) for x in (body.get("vault_ids") or []) if str(x).strip()]
                    gw_base = str(body.get("gateway_url") or "").strip().rstrip("/")
                    if mode in {"http", "bridge"} and not gw_base:
                        gw_base = self._gateway_base_url().rstrip("/")
                    raw_tok = str(body.get("token") or "").strip()

                    desired = state.build_desired_client_servers(
                        vault_ids,
                        mode=mode,
                        gateway_url=gw_base,
                        token=raw_tok,
                    )

                    if path == "/api/clients/preview":
                        cid = str(body.get("client_id") or "claude")
                        snippet = render_client_snippet(cid, desired)
                        cpath = str(CLIENT_BY_ID[cid].config_path(state.client_home))
                        self._send_json(200, {"client_id": cid, "config_path": cpath, "snippet": snippet})
                        return

                    client_ids = [str(x) for x in (body.get("client_ids") or []) if str(x).strip()]
                    remove_unlisted = bool(body.get("remove_unlisted_wctx", True))
                    synced_results = [
                        sync_client_mcp_config(
                            cid,
                            desired,
                            remove_unlisted_wctx=remove_unlisted,
                            home=state.client_home,
                        )
                        for cid in client_ids
                    ]
                    self._send_json(200, {"results": synced_results})
                    return

                if path == "/api/memory/create":
                    res = state.create_memory(
                        str(body.get("vault_id") or ""),
                        text=str(body.get("text") or ""),
                        kind=str(body.get("kind") or "insight"),
                        related_sources=body.get("related_sources") or [],
                        tags=body.get("tags") or [],
                        author=str(body.get("author") or "console"),
                    )
                    self._send_json(200, res)
                    return

                if path == "/api/memory/update":
                    res = state.update_memory(
                        str(body.get("vault_id") or ""),
                        str(body.get("note_id") or ""),
                        text=body.get("text"),
                        kind=body.get("kind"),
                        status=body.get("status"),
                        related_sources=body.get("related_sources"),
                        tags=body.get("tags"),
                    )
                    self._send_json(200, res)
                    return

                if path == "/api/memory/delete":
                    ok = state.delete_memory(
                        str(body.get("vault_id") or ""),
                        str(body.get("note_id") or ""),
                    )
                    self._send_json(200, {"deleted": ok})
                    return

                if path == "/api/memory/import":
                    raw_items = body.get("items") or []
                    if isinstance(raw_items, dict):
                        raw_items = raw_items.get("memories") or []
                    res = state.import_memories(
                        str(body.get("vault_id") or ""),
                        raw_items if isinstance(raw_items, list) else [],
                    )
                    self._send_json(200, res)
                    return

                self._send_json(404, {"error": f"Unknown API endpoint: {path}"})
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})

    return _HighBacklogHTTPServer((host, port), ConsoleHTTPHandler)


def run_console(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    config_dir: Path | str | None = None,
    extra_vault_specs: dict[str, Path] | None = None,
    open_browser: bool = True,
    allow_remote_admin: bool = False,
) -> int:
    state = ConsoleState(config_dir=config_dir, extra_vault_specs=extra_vault_specs)
    srv = create_console_server(state, host=host, port=port, allow_remote_admin=allow_remote_admin)
    actual_port = srv.server_address[1]
    url = f"http://{'127.0.0.1' if host in {'0.0.0.0', '::'} else host}:{actual_port}"
    if allow_remote_admin and not _is_loopback_host(host):
        print(
            "[WARNING] Control Console admin routes (/api/*) have no user login password and are "
            f"currently bound to non-loopback host '{host}'. Place it behind an authenticated VPN/reverse proxy, "
            "or run `wctx gateway` for production multi-user serving (Bearer Token only, no admin UI).",
            file=sys.stderr,
            flush=True,
        )
    print(
        f"wiki-context Control Console & Gateway running at {url}\n"
        f"  Config & Tokens : {state.config_dir}\n"
        f"  MCP HTTP Route  : {url}/mcp/<vault_id>",
        flush=True,
    )
    if open_browser:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        state.close()
    return 0
