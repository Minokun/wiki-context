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
import mimetypes
import os
import re
import secrets
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from .clients import (
    CLIENT_BY_ID,
    disconnect_client_mcp_config,
    discover_vaults_from_clients,
    inspect_all_clients,
    render_client_snippet,
    resolve_wctx_command,
    sync_client_mcp_config,
)
from .config import VaultConfig
from .compiler import (
    LLM_PROVIDER_PRESETS,
    compile_vault,
    plan_compilation,
    test_llm_connection,
)
from .context import AllowAllPolicy, PrefixACLPolicy, Principal, SynonymBook
from .discovery import (
    LanDiscoveryResponder,
    PairingManager,
    compute_host_fingerprint,
    decode_pairing_code,
    discover_tailscale_peers,
    probe_lan_udp,
    probe_verified_pairing_endpoint,
)
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
from .search import route as route_query
from .state_lock import (
    atomic_write_json,
    clear_daemon_info,
    ensure_schema_version,
    file_lock,
    probe_live_daemon,
    write_daemon_info,
)
from .sync import sync_vault
from .telemetry import TelemetryStore
from .ui_assets import render_console_html
from .watcher import VaultWatcherManager


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
    atomic_write_json(path, data, private_mode=private_mode)


def _vault_capabilities(kind: str) -> dict[str, bool | str]:
    if kind == "remote":
        return {
            "kind": "remote",
            "can_xray": False,
            "can_shadow_diff": False,
            "can_edit_synonyms": False,
            "can_edit_memory": False,
            "can_sync": False,
            "can_watch": False,
            "can_rename": True,
        }
    return {
        "kind": "local",
        "can_xray": True,
        "can_shadow_diff": True,
        "can_edit_synonyms": True,
        "can_edit_memory": True,
        "can_sync": True,
        "can_watch": True,
        "can_rename": True,
    }


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

    REMOTE_PROBE_TTL_SEC = 15.0

    def __init__(
        self,
        *,
        config_dir: Path | str | None = None,
        extra_vault_specs: dict[str, Path] | None = None,
        client_home: Path | None = None,
        enable_watcher: bool = True,
        defer_vault_hydration: bool = False,
    ):
        base = config_dir or os.environ.get("WCTX_HOME") or (Path.home() / ".wiki-context")
        self.config_dir = Path(base).expanduser().resolve()
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.console_file = self.config_dir / "console.json"
        self.tokens_file = self.config_dir / "tokens.json"
        self.remotes_file = self.config_dir / "remotes.json"
        self.llm_secret_file = self.config_dir / "llm_secret.json"
        self.state_lock_file = self.config_dir / ".state.lock"
        self.client_home = client_home
        self._lock = threading.RLock()
        self._lan_server: _HighBacklogHTTPServer | None = None
        self._lan_conflict: str = ""
        self._udp_responder: LanDiscoveryResponder | None = None
        self._remote_probe_cache: dict[tuple[str, str], tuple[float, dict[str, Any] | None, str]] = {}
        self._jobs: dict[str, dict[str, Any]] = {}
        self._active_compile_jobs: dict[str, str] = {}
        self._compile_finished_ts: dict[str, float] = {}
        self._compile_plan_cache: dict[str, tuple[tuple[str, int], dict[str, int]]] = {}
        self._job_lock = threading.Lock()
        self._hydrating_vaults: set[str] = set()
        self._vault_access_errors: dict[str, str] = {}

        self._bootstrap_files(extra_vault_specs or {})
        init_data = self._load_console_data()
        tok_data = self._load_tokens_doc()
        self.instance_id = str(init_data.get("instance_id") or "")
        self.fp = compute_host_fingerprint(self.instance_id, str(tok_data.get("host_secret") or ""))
        self.pairing = PairingManager()
        self.telemetry = TelemetryStore(db_path=self.config_dir / "telemetry.sqlite", async_writer=True)
        self.token_registry = TokenRegistry(token_file=self.tokens_file)

        local_specs: list[tuple[str, str]] = []
        for item in init_data.get("vaults", []):
            if not isinstance(item, dict) or item.get("kind") == "remote":
                continue
            vid = str(item.get("id") or "").strip()
            raw_p = str(item.get("path") or "").strip()
            if vid and raw_p:
                local_specs.append((vid, raw_p))

        initial_map: dict[str, Path] = {}
        if defer_vault_hydration and local_specs:
            self._hydrating_vaults = {vid for vid, _ in local_specs}
        else:
            for vid, raw_p in local_specs:
                try:
                    vpath = Path(raw_p).expanduser().resolve()
                    if vpath.is_dir():
                        cfg = VaultConfig.load(vpath)
                        if (cfg.index_root / "current.json").is_file() or (cfg.index_root / "bm25.sqlite").is_file():
                            initial_map[vid] = vpath
                except PermissionError as exc:
                    self._vault_access_errors[vid] = f"无文件夹读取权限: {exc}"
                except Exception:
                    pass

        self.gateway = MultiVaultGateway(
            initial_map,
            tokens=self.token_registry,
            allow_empty_vaults=True,
            telemetry=self.telemetry,
            instance_id=self.instance_id,
            fp=self.fp,
            on_redeem_pairing=self.redeem_pairing_ticket,
        )

        self._watcher = VaultWatcherManager(
            on_change=lambda vid: self.start_sync_job(vid, force=False, trigger="watcher"),
        )
        if defer_vault_hydration and local_specs:
            def _hydrate_worker() -> None:
                for vid, raw_p in local_specs:
                    try:
                        vpath = Path(raw_p).expanduser().resolve()
                        if vpath.is_dir():
                            cfg = VaultConfig.load(vpath)
                            if (cfg.index_root / "current.json").is_file() or (cfg.index_root / "bm25.sqlite").is_file():
                                self.gateway.upsert_vault(vid, vpath)
                        self._vault_access_errors.pop(vid, None)
                    except PermissionError:
                        self._vault_access_errors[vid] = (
                            "无文件夹访问权限（请在 系统设置 -> 隐私与安全性 -> 文件与文件夹 中允许 wiki-context）"
                        )
                    except Exception:
                        pass
                    finally:
                        with self._lock:
                            self._hydrating_vaults.discard(vid)
                try:
                    self._refresh_watcher_targets()
                    if enable_watcher:
                        self._watcher.start()
                except Exception:
                    pass

            threading.Thread(target=_hydrate_worker, name="wctx-vault-hydrator", daemon=True).start()
        else:
            self._refresh_watcher_targets(init_data)
            if enable_watcher:
                self._watcher.start()

        lan_cfg = init_data.get("lan_gateway")
        if isinstance(lan_cfg, dict) and lan_cfg.get("enabled"):
            try:
                self.start_lan_gateway(
                    host=str(lan_cfg.get("host") or "0.0.0.0"),
                    port=int(lan_cfg.get("port") or 8090),
                    persist=False,
                )
            except Exception as exc:
                self._lan_conflict = str(exc)

    def close(self) -> None:
        self._watcher.stop()
        self._shutdown_lan_server()
        self.gateway.close()
        self.telemetry.close()

    def _shutdown_lan_server(self) -> None:
        if self._udp_responder is not None:
            try:
                self._udp_responder.stop()
            except Exception:
                pass
            self._udp_responder = None
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
        with file_lock(self.state_lock_file, blocking=True):
            if not self.tokens_file.is_file():
                _atomic_write_json(
                    self.tokens_file,
                    {"version": 2, "host_secret": secrets.token_hex(24), "tokens": [], "acl": {}},
                    private_mode=True,
                )
            else:
                tok_doc = self._load_tokens_doc()
                if not tok_doc.get("host_secret"):
                    tok_doc["host_secret"] = secrets.token_hex(24)
                    tok_doc["version"] = 2
                    _atomic_write_json(self.tokens_file, tok_doc, private_mode=True)

            if not self.console_file.is_file():
                discovered = discover_vaults_from_clients(home=self.client_home)
                vault_rows: list[dict[str, Any]] = []
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
                _atomic_write_json(
                    self.console_file,
                    {
                        "version": 2,
                        "instance_id": f"inst_{secrets.token_hex(6)}",
                        "watched_vaults": [],
                        "vaults": vault_rows,
                    },
                )
            else:
                data = self._load_console_data()
                migrated, changed = ensure_schema_version(
                    data,
                    target_version=2,
                    migrator=lambda d, _v: {
                        **d,
                        "instance_id": d.get("instance_id") or f"inst_{secrets.token_hex(6)}",
                        "watched_vaults": list(d.get("watched_vaults") or []),
                    },
                    source_path=self.console_file,
                )
                data = migrated
                if not data.get("instance_id"):
                    data["instance_id"] = f"inst_{secrets.token_hex(6)}"
                    changed = True
                if extra_vault_specs:
                    vault_rows = list(data.get("vaults") or [])
                    seen_ids = {str(x.get("id")) for x in vault_rows}
                    seen_paths = {str(Path(str(x.get("path"))).resolve()) for x in vault_rows if x.get("path")}
                    for vid, vpath in extra_vault_specs.items():
                        vp = str(vpath.resolve())
                        if vp in seen_paths:
                            continue
                        use_id = vid if vid not in seen_ids else _slugify_vault_id(vid, seen_ids)
                        seen_ids.add(use_id)
                        seen_paths.add(vp)
                        vault_rows.append({"id": use_id, "path": vp, "added_at": _now_iso()})
                        changed = True
                    data["vaults"] = vault_rows
                if changed:
                    _atomic_write_json(self.console_file, data)

    def _load_console_data(self) -> dict[str, Any]:
        if not self.console_file.is_file():
            return {"version": 2, "instance_id": "", "watched_vaults": [], "vaults": []}
        try:
            d = json.loads(self.console_file.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                if not isinstance(d.get("vaults"), list):
                    d["vaults"] = []
                if not isinstance(d.get("watched_vaults"), list):
                    d["watched_vaults"] = []
                return d
        except (OSError, ValueError):
            pass
        return {"version": 2, "instance_id": "", "watched_vaults": [], "vaults": []}

    def _save_console_data(self, data: dict[str, Any]) -> None:
        with file_lock(self.state_lock_file, blocking=True):
            _atomic_write_json(self.console_file, data)

    def _load_tokens_doc(self) -> dict[str, Any]:
        if not self.tokens_file.is_file():
            return {"version": 2, "host_secret": "", "tokens": [], "acl": {}}
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
        return {"version": 2, "host_secret": "", "tokens": [], "acl": {}}

    def _save_tokens_doc(self, doc: dict[str, Any]) -> None:
        with file_lock(self.state_lock_file, blocking=True):
            _atomic_write_json(self.tokens_file, doc, private_mode=True)
        self.token_registry.force_reload()

    def _load_remotes_doc(self) -> dict[str, Any]:
        if not self.remotes_file.is_file():
            return {"version": 2, "credentials": {}, "nodes": {}}
        try:
            d = json.loads(self.remotes_file.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                if not isinstance(d.get("credentials"), dict):
                    d["credentials"] = {}
                if not isinstance(d.get("nodes"), dict):
                    d["nodes"] = {}
                return d
        except (OSError, ValueError):
            pass
        return {"version": 2, "credentials": {}, "nodes": {}}

    def _save_remotes_doc(self, doc: dict[str, Any]) -> None:
        doc.setdefault("version", 2)
        doc.setdefault("nodes", {})
        with file_lock(self.state_lock_file, blocking=True):
            _atomic_write_json(self.remotes_file, doc, private_mode=True)

    def _refresh_watcher_targets(self, data: dict[str, Any] | None = None) -> None:
        doc = data if data is not None else self._load_console_data()
        watched = {str(x) for x in (doc.get("watched_vaults") or []) if str(x).strip()}
        target_map: dict[str, Path] = {}
        for item in doc.get("vaults") or []:
            if not isinstance(item, dict) or item.get("kind") == "remote":
                continue
            vid = str(item.get("id") or "").strip()
            raw_p = str(item.get("path") or "").strip()
            if vid in watched and raw_p:
                vp = Path(raw_p).expanduser().resolve()
                if vp.is_dir():
                    target_map[vid] = vp
        self._watcher.set_vaults(target_map)

    def get_remote_token(self, vault_id: str) -> str:
        with self._lock:
            creds = self._load_remotes_doc().get("credentials") or {}
            item = creds.get(vault_id)
            if isinstance(item, dict):
                return str(item.get("token") or "").strip()
        return ""

    # -- Built-in AI Compiler Agent & LLM Credentials ----------------------- #

    def _load_llm_secret(self) -> str:
        if not self.llm_secret_file.is_file():
            return ""
        try:
            d = json.loads(self.llm_secret_file.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return str(d.get("api_key") or "").strip()
        except (OSError, ValueError):
            pass
        return ""

    def _save_llm_secret(self, api_key: str) -> None:
        with file_lock(self.state_lock_file, blocking=True):
            _atomic_write_json(
                self.llm_secret_file,
                {"version": 1, "api_key": api_key.strip(), "updated_at": _now_iso()},
                private_mode=True,
            )

    @staticmethod
    def _mask_secret(raw: str) -> str:
        s = (raw or "").strip()
        if not s:
            return ""
        if len(s) <= 8:
            return s[:2] + "****"
        return f"{s[:5]}****{s[-4:]}"

    def _resolve_llm_runtime(self) -> dict[str, Any]:
        with self._lock:
            data = self._load_console_data()
            cfg = data.get("llm_compiler") if isinstance(data.get("llm_compiler"), dict) else {}
            saved_key = self._load_llm_secret()

        preset_map = {p["id"]: p for p in LLM_PROVIDER_PRESETS}
        provider = str(cfg.get("provider") or "deepseek").strip().lower()
        if provider not in preset_map:
            provider = "custom"
        preset = preset_map[provider]
        protocol = str(cfg.get("protocol") or preset["protocol"] or "openai").strip().lower()
        base_url = str(cfg.get("base_url") or preset["base_url"] or "https://api.deepseek.com/v1").strip().rstrip("/")
        model = str(cfg.get("model") or preset["model"] or "deepseek-chat").strip()
        auto_compile = bool(cfg.get("auto_compile_on_sync", True))
        max_items = max(1, min(100, int(cfg.get("max_items_per_run") or 15)))

        env_key = (
            os.environ.get("WCTX_API_KEY")
            or (os.environ.get("ANTHROPIC_API_KEY") if protocol == "anthropic" else "")
            or os.environ.get("DEEPSEEK_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        ).strip()

        eff_key = saved_key or env_key
        key_source = "saved" if saved_key else ("env" if env_key else "none")
        return {
            "provider": provider,
            "protocol": protocol,
            "base_url": base_url,
            "model": model,
            "api_key": eff_key,
            "has_api_key": bool(eff_key),
            "api_key_masked": self._mask_secret(eff_key),
            "key_source": key_source,
            "ready": bool(eff_key and base_url and model),
            "auto_compile_on_sync": auto_compile,
            "max_items_per_run": max_items,
        }

    def get_llm_compiler_status(self) -> dict[str, Any]:
        rt = self._resolve_llm_runtime()
        rt.pop("api_key", None)
        rt["presets"] = LLM_PROVIDER_PRESETS
        return rt

    def save_llm_compiler_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        preset_map = {p["id"]: p for p in LLM_PROVIDER_PRESETS}
        with self._lock:
            data = self._load_console_data()
            prev = data.get("llm_compiler") if isinstance(data.get("llm_compiler"), dict) else {}
            provider = str(payload.get("provider") or prev.get("provider") or "deepseek").strip().lower()
            if provider not in preset_map:
                provider = "custom"
            preset = preset_map[provider]
            protocol = str(payload.get("protocol") or preset["protocol"] or prev.get("protocol") or "openai").strip().lower()
            if protocol not in {"openai", "anthropic"}:
                protocol = "openai"
            base_url = str(
                payload.get("base_url") if "base_url" in payload else (prev.get("base_url") or preset["base_url"])
            ).strip().rstrip("/")
            model = str(
                payload.get("model") if "model" in payload else (prev.get("model") or preset["model"])
            ).strip()
            auto_compile = bool(
                payload["auto_compile_on_sync"]
                if "auto_compile_on_sync" in payload
                else prev.get("auto_compile_on_sync", True)
            )
            max_items = max(1, min(100, int(payload.get("max_items_per_run") or prev.get("max_items_per_run") or 15)))

            data["llm_compiler"] = {
                "provider": provider,
                "protocol": protocol,
                "base_url": base_url,
                "model": model,
                "auto_compile_on_sync": auto_compile,
                "max_items_per_run": max_items,
                "updated_at": _now_iso(),
            }
            self._save_console_data(data)

            if payload.get("clear_api_key"):
                self._save_llm_secret("")
            elif "api_key" in payload and str(payload.get("api_key") or "").strip():
                self._save_llm_secret(str(payload["api_key"]).strip())

        return self.get_llm_compiler_status()

    def test_llm_compiler(self, override: dict[str, Any] | None = None) -> dict[str, Any]:
        rt = self._resolve_llm_runtime()
        ov = override or {}
        eff_key = str(ov.get("api_key") or "").strip() or rt["api_key"]
        eff_url = str(ov.get("base_url") or "").strip().rstrip("/") or rt["base_url"]
        eff_model = str(ov.get("model") or "").strip() or rt["model"]
        eff_proto = str(ov.get("protocol") or "").strip().lower() or rt["protocol"]
        if not eff_key:
            raise ValueError("请先填写大模型访问令牌 (API Key)")
        return test_llm_connection(
            model=eff_model,
            base_url=eff_url,
            api_key=eff_key,
            protocol=eff_proto,
            timeout_sec=20,
        )

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
            conflict_msg = self._lan_conflict

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
            "port_conflict": conflict_msg,
            "instance_id": self.instance_id,
            "fp": self.fp,
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

            try:
                srv = create_http_server(self.gateway, host=host, port=port, allow_zero_tokens=True)  # type: ignore[assignment]
            except OSError as exc:
                self._lan_conflict = f"端口 {port} 已被占用 ({exc})，请停止外部网关或切换端口"
                raise ValueError(self._lan_conflict) from exc

            self._lan_conflict = ""
            actual_port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True, name="wctx-lan-gateway")
            t.start()
            self._lan_server = srv  # type: ignore[assignment]

            if self._udp_responder is None:
                self._udp_responder = LanDiscoveryResponder(
                    instance_id=self.instance_id,
                    fp=self.fp,
                    host_label=socket.gethostname(),
                    get_gateway_port=lambda: self._lan_server.server_address[1] if self._lan_server else None,
                )
                self._udp_responder.start()

            if persist:
                data = self._load_console_data()
                data["lan_gateway"] = {"enabled": True, "host": host, "port": int(actual_port)}
                self._save_console_data(data)

        return self.get_lan_gateway_status()

    def stop_lan_gateway(self) -> dict[str, Any]:
        self._shutdown_lan_server()
        with self._lock:
            self._lan_conflict = ""
            data = self._load_console_data()
            prev = data.get("lan_gateway") if isinstance(data.get("lan_gateway"), dict) else {}
            data["lan_gateway"] = {
                "enabled": False,
                "host": str(prev.get("host") or "0.0.0.0"),
                "port": int(prev.get("port") or 8090),
            }
            self._save_console_data(data)
        return self.get_lan_gateway_status()

    # -- Vault Inspection & Lifecycle (Local + Remote) ---------------------- #

    def list_vaults_detailed(self, *, refresh_remote: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            cdata = self._load_console_data()
            rows = list(cdata.get("vaults") or [])
            watched_set = {str(x) for x in (cdata.get("watched_vaults") or []) if str(x).strip()}
            creds = dict(self._load_remotes_doc().get("credentials") or {})

        # Collect unique remote (gateway_url, token) pairs and probe concurrently OUTSIDE lock with 15s TTL cache
        now_m = time.monotonic()
        pending_probes: dict[tuple[str, str], str] = {}
        probe_cache: dict[tuple[str, str], tuple[dict[str, Any] | None, str]] = {}
        for item in rows:
            if not isinstance(item, dict) or item.get("kind") != "remote":
                continue
            vid = str(item.get("id") or "").strip()
            gw = str(item.get("gateway_url") or "").strip().rstrip("/")
            tok = str((creds.get(vid) or {}).get("token") or "").strip()
            key = (gw, tok)
            if not gw or key in probe_cache or key in pending_probes:
                continue
            if not tok:
                probe_cache[key] = (None, "未配置访问令牌")
                continue
            cached = self._remote_probe_cache.get(key)
            if not refresh_remote and cached is not None and (now_m - cached[0]) < self.REMOTE_PROBE_TTL_SEC:
                probe_cache[key] = (cached[1], cached[2])
            else:
                pending_probes[key] = tok

        if pending_probes:
            def _do_probe(k: tuple[str, str]) -> tuple[tuple[str, str], dict[str, Any] | None, str]:
                gw_u, tok_u = k
                try:
                    return k, probe_remote_gateway(gw_u, tok_u, timeout=1.2), ""
                except Exception as exc:
                    return k, None, str(exc)

            with ThreadPoolExecutor(max_workers=min(4, len(pending_probes))) as pool:
                for k, res_p, err_s in pool.map(_do_probe, list(pending_probes.keys())):
                    probe_cache[k] = (res_p, err_s)
                    self._remote_probe_cache[k] = (time.monotonic(), res_p, err_s)

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
                    "capabilities": _vault_capabilities("remote"),
                    "remote_vault_id": rvid,
                    "gateway_url": gw,
                    "mcp_url": f"{gw}/mcp/{rvid}" if gw else "",
                    "path": f"{gw}/mcp/{rvid}" if gw else "",
                    "token_masked": _mask_token(tok),
                    "has_token": bool(tok),
                    "remote_uid": str((probed or {}).get("uid") or item.get("remote_uid") or ""),
                    "remote_groups": list((probed or {}).get("groups") or item.get("remote_groups") or []),
                    "instance_id": str(item.get("instance_id") or ""),
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
                    "watching": False,
                    "source_prefixes": [],
                })
                continue

            raw_p = str(item.get("path") or "").strip()
            with self._lock:
                is_hydrating = vid in self._hydrating_vaults
                access_err = self._vault_access_errors.get(vid, "")

            if is_hydrating:
                out.append({
                    "id": vid,
                    "kind": "local",
                    "capabilities": _vault_capabilities("local"),
                    "watching": vid in watched_set,
                    "path": raw_p,
                    "added_at": item.get("added_at") or "",
                    "exists": True,
                    "hydrating": True,
                    "ready": False,
                    "name": Path(raw_p).name if raw_p else vid,
                    "layout": "zh",
                    "online_error": "正在加载知识库目录（若系统弹出文件夹访问授权提示，请点击“允许”）...",
                    "generation_id": "",
                    "docs": 0,
                    "sections": 0,
                    "wiki_docs": 0,
                    "active_memory_notes": 0,
                    "source_prefixes": [],
                    "folder_tree": [],
                })
                continue

            if access_err:
                out.append({
                    "id": vid,
                    "kind": "local",
                    "capabilities": _vault_capabilities("local"),
                    "watching": vid in watched_set,
                    "path": raw_p,
                    "added_at": item.get("added_at") or "",
                    "exists": True,
                    "ready": False,
                    "name": Path(raw_p).name if raw_p else vid,
                    "layout": "zh",
                    "online_error": access_err,
                    "generation_id": "",
                    "docs": 0,
                    "sections": 0,
                    "wiki_docs": 0,
                    "active_memory_notes": 0,
                    "source_prefixes": [],
                    "folder_tree": [],
                })
                continue

            try:
                vpath = Path(raw_p).expanduser().resolve() if raw_p else (self.config_dir / "__missing__")
                dir_exists = vpath.is_dir()
            except PermissionError:
                err_txt = "无文件夹访问权限（请在 系统设置 -> 隐私与安全性 -> 文件与文件夹 中允许 wiki-context）"
                self._vault_access_errors[vid] = err_txt
                out.append({
                    "id": vid,
                    "kind": "local",
                    "capabilities": _vault_capabilities("local"),
                    "watching": vid in watched_set,
                    "path": raw_p,
                    "added_at": item.get("added_at") or "",
                    "exists": True,
                    "ready": False,
                    "name": Path(raw_p).name if raw_p else vid,
                    "layout": "zh",
                    "online_error": err_txt,
                    "generation_id": "",
                    "docs": 0,
                    "sections": 0,
                    "wiki_docs": 0,
                    "active_memory_notes": 0,
                    "source_prefixes": [],
                    "folder_tree": [],
                })
                continue

            entry: dict[str, Any] = {
                "id": vid,
                "kind": "local",
                "capabilities": _vault_capabilities("local"),
                "watching": vid in watched_set,
                "path": str(vpath),
                "added_at": item.get("added_at") or "",
                "exists": dir_exists,
            }
            if not dir_exists:
                entry.update({"name": vpath.name, "layout": "en", "ready": False, "docs": 0, "sections": 0, "wiki_docs": 0, "active_memory_notes": 0})
                out.append(entry)
                continue

            try:
                cfg = VaultConfig.load(vpath)
            except PermissionError:
                entry.update({
                    "name": vpath.name,
                    "layout": "zh",
                    "ready": False,
                    "online_error": "无文件夹访问权限（请在 系统设置 -> 隐私与安全性 -> 文件与文件夹 中允许 wiki-context）",
                    "docs": 0,
                    "sections": 0,
                    "wiki_docs": 0,
                    "active_memory_notes": 0,
                })
                out.append(entry)
                continue
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
                    "sample_sources": [d["source"] for d in (lst.get("docs") or lst.get("documents") or [])[:200]],
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

            # Attach cached compilation backlog counts + active compile job status
            try:
                gen_id = str(entry.get("generation_id") or "")
                w_mtime = cfg.wiki_root.stat().st_mtime_ns if cfg.wiki_root.is_dir() else 0
                cache_sig = (gen_id, w_mtime)
                cached_plan = self._compile_plan_cache.get(vid)
                if cached_plan is not None and cached_plan[0] == cache_sig:
                    plan_counts = cached_plan[1]
                else:
                    pinfo = plan_compilation(vpath)
                    plan_counts = {
                        "uncompiled_count": len(pinfo.get("uncompiled_sources") or []),
                        "stale_count": len(pinfo.get("stale_pages") or []),
                    }
                    self._compile_plan_cache[vid] = (cache_sig, plan_counts)
                entry.update(plan_counts)
            except Exception:
                entry.setdefault("uncompiled_count", 0)
                entry.setdefault("stale_count", 0)

            with self._job_lock:
                c_jid = self._active_compile_jobs.get(vid)
                if c_jid and c_jid in self._jobs:
                    entry["compile_job"] = dict(self._jobs[c_jid])

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
        auto_compile: bool | None = None,
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
        self._compile_plan_cache.pop(vid, None)
        rt = self._resolve_llm_runtime()
        should_compile = rt["auto_compile_on_sync"] if auto_compile is None else bool(auto_compile)
        compile_job: dict[str, Any] | None = None
        uncompiled_cnt = 0
        try:
            plan_info = plan_compilation(target)
            uncompiled_cnt = len(plan_info.get("uncompiled_sources") or [])
        except Exception:
            uncompiled_cnt = 0
        if should_compile and rt["ready"] and uncompiled_cnt > 0:
            compile_job = self.start_compile_job(vid, trigger="onboard")
        return {
            "vault_id": vid,
            **rep,
            "uncompiled_count": uncompiled_cnt,
            "llm_ready": rt["ready"],
            "compile_job": compile_job,
        }

    def import_folder(self, *, vault_id: str, external_folder: str, target_subfolder: str = "") -> dict[str, Any]:
        vpath = self.get_vault_path(vault_id)
        rep = import_external_folder(
            vpath,
            external_folder,
            target_subfolder=target_subfolder or None,
            run_sync=True,
        )
        self.gateway.upsert_vault(vault_id, vpath)
        self._compile_plan_cache.pop(vault_id, None)
        rt = self._resolve_llm_runtime()
        compile_job: dict[str, Any] | None = None
        if rt["ready"] and rt["auto_compile_on_sync"]:
            compile_job = self.start_compile_job(vault_id, trigger="import")
        return {"vault_id": vault_id, **rep, "llm_ready": rt["ready"], "compile_job": compile_job}

    def sync_registered_vault(self, vault_id: str, *, force: bool = False) -> dict[str, Any]:
        vpath = self.get_vault_path(vault_id)
        rep = sync_vault(vpath, force=force, rebuild=True)
        self.gateway.upsert_vault(vault_id, vpath)
        self._compile_plan_cache.pop(vault_id, None)
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

    def plan_vault_compile(self, vault_id: str) -> dict[str, Any]:
        vpath = self.get_vault_path(vault_id)
        plan = plan_compilation(vpath)
        return {
            "vault_id": vault_id,
            "vault": plan.get("vault", vault_id),
            "uncompiled_sources": plan.get("uncompiled_sources", []),
            "stale_pages": plan.get("stale_pages", []),
            "active_notes": plan.get("active_notes", []),
            "uncompiled_count": len(plan.get("uncompiled_sources") or []),
            "stale_count": len(plan.get("stale_pages") or []),
        }

    def start_compile_job(
        self,
        vault_id: str,
        *,
        max_items: int | None = None,
        trigger: str = "manual",
    ) -> dict[str, Any]:
        vpath = self.get_vault_path(vault_id)
        rt = self._resolve_llm_runtime()
        if not rt["ready"]:
            if trigger == "manual":
                raise ValueError("尚未配置内置整理智能体的大模型访问令牌 (API Key)，请先在智能体设置中填写")
            return {"job_id": "", "kind": "compile", "vault_id": vault_id, "status": "skipped", "reason": "no_api_key"}

        with self._job_lock:
            existing_jid = self._active_compile_jobs.get(vault_id)
            if existing_jid and existing_jid in self._jobs and self._jobs[existing_jid].get("status") == "running":
                return dict(self._jobs[existing_jid])

        plan = plan_compilation(vpath)
        pending_total = (
            len(plan.get("uncompiled_sources") or [])
            + len(plan.get("stale_pages") or [])
            + (1 if plan.get("active_notes") else 0)
        )
        if pending_total == 0 and trigger != "manual":
            return {
                "job_id": "",
                "kind": "compile",
                "vault_id": vault_id,
                "status": "done",
                "noop": True,
                "result": {"compiled_pages": [], "refreshed_pages": [], "consolidated_notes": [], "noop": True},
            }

        eff_max = max(1, min(100, int(max_items or rt["max_items_per_run"] or 15)))
        job_id = f"job_cmp_{secrets.token_hex(5)}"
        job_rec: dict[str, Any] = {
            "job_id": job_id,
            "kind": "compile",
            "vault_id": vault_id,
            "trigger": trigger,
            "status": "running",
            "started_at": _now_iso(),
            "finished_at": "",
            "progress": {
                "phase": "planning",
                "done": 0,
                "total": min(pending_total, eff_max + 1),
                "pending_total": pending_total,
                "batch_limit": eff_max,
                "current": "",
                "compiled_count": 0,
                "refreshed_count": 0,
            },
            "result": None,
            "error": "",
        }
        with self._job_lock:
            if len(self._jobs) > 100:
                oldest = list(self._jobs.keys())[:20]
                for k in oldest:
                    if self._jobs[k]["status"] != "running":
                        self._jobs.pop(k, None)
            self._jobs[job_id] = job_rec
            self._active_compile_jobs[vault_id] = job_id

        def _on_progress(prog: dict[str, Any]) -> None:
            with self._job_lock:
                job_rec["progress"] = dict(prog)

        def _worker() -> None:
            try:
                res = compile_vault(
                    vpath,
                    model=rt["model"],
                    base_url=rt["base_url"],
                    api_key=rt["api_key"],
                    protocol=rt["protocol"],
                    max_items=eff_max,
                    on_progress=_on_progress,
                )
                self._compile_finished_ts[vault_id] = time.monotonic()
                self.gateway.upsert_vault(vault_id, vpath)
                self._compile_plan_cache.pop(vault_id, None)
                with self._job_lock:
                    job_rec["status"] = "done"
                    job_rec["result"] = res
                    job_rec["finished_at"] = _now_iso()
                    if self._active_compile_jobs.get(vault_id) == job_id:
                        self._active_compile_jobs.pop(vault_id, None)
            except Exception as exc:
                self._compile_finished_ts[vault_id] = time.monotonic()
                with self._job_lock:
                    job_rec["status"] = "failed"
                    job_rec["error"] = str(exc)
                    job_rec["finished_at"] = _now_iso()
                    if self._active_compile_jobs.get(vault_id) == job_id:
                        self._active_compile_jobs.pop(vault_id, None)

        threading.Thread(target=_worker, name=f"wctx-compile-{vault_id}", daemon=True).start()
        return dict(job_rec)

    def start_sync_job(self, vault_id: str, *, force: bool = False, trigger: str = "manual") -> dict[str, Any]:
        self.get_vault_path(vault_id)
        if trigger == "watcher":
            with self._job_lock:
                if vault_id in self._active_compile_jobs:
                    return {
                        "job_id": self._active_compile_jobs[vault_id],
                        "kind": "sync",
                        "vault_id": vault_id,
                        "status": "skipped",
                        "reason": "compile_in_progress",
                    }
            if (time.monotonic() - self._compile_finished_ts.get(vault_id, 0.0)) < 8.0:
                return {
                    "job_id": "",
                    "kind": "sync",
                    "vault_id": vault_id,
                    "status": "skipped",
                    "reason": "compile_cooldown",
                }

        job_id = f"job_{secrets.token_hex(5)}"
        job_rec: dict[str, Any] = {
            "job_id": job_id,
            "kind": "sync",
            "vault_id": vault_id,
            "trigger": trigger,
            "status": "running",
            "started_at": _now_iso(),
            "finished_at": "",
            "result": None,
            "error": "",
        }
        with self._job_lock:
            if len(self._jobs) > 100:
                oldest = list(self._jobs.keys())[:20]
                for k in oldest:
                    if self._jobs[k]["status"] != "running":
                        self._jobs.pop(k, None)
            self._jobs[job_id] = job_rec

        def _worker() -> None:
            try:
                res = self.sync_registered_vault(vault_id, force=force)
                rt = self._resolve_llm_runtime()
                if rt["ready"] and rt["auto_compile_on_sync"]:
                    cjob = self.start_compile_job(vault_id, trigger=f"sync:{trigger}")
                    if cjob.get("job_id"):
                        res["compile_job"] = cjob
                with self._job_lock:
                    job_rec["status"] = "done"
                    job_rec["result"] = res
                    job_rec["finished_at"] = _now_iso()
            except Exception as exc:
                with self._job_lock:
                    job_rec["status"] = "failed"
                    job_rec["error"] = str(exc)
                    job_rec["finished_at"] = _now_iso()

        threading.Thread(target=_worker, name=f"wctx-sync-{vault_id}", daemon=True).start()
        return dict(job_rec)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._job_lock:
            rec = self._jobs.get(job_id)
            if rec is None:
                raise KeyError(f"Unknown job_id '{job_id}'")
            return dict(rec)

    def set_vault_watch(self, vault_id: str, *, enabled: bool) -> dict[str, Any]:
        self.get_vault_path(vault_id)
        with self._lock:
            data = self._load_console_data()
            watched = [str(x) for x in (data.get("watched_vaults") or []) if str(x).strip()]
            if enabled and vault_id not in watched:
                watched.append(vault_id)
            elif not enabled:
                watched = [x for x in watched if x != vault_id]
            data["watched_vaults"] = watched
            self._save_console_data(data)
            self._refresh_watcher_targets(data)
        return {"vault_id": vault_id, "watching": bool(enabled), "watched_vaults": watched}

    def get_vault_synonyms(self, vault_id: str) -> dict[str, Any]:
        vpath = self.get_vault_path(vault_id)
        cfg = VaultConfig.load(vpath)
        spath = cfg.synonyms_path
        groups: list[dict[str, Any]] = []
        if spath.is_file():
            try:
                raw = json.loads(spath.read_text(encoding="utf-8"))
                raw_groups = raw.get("groups") if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
                for g in raw_groups or []:
                    if not isinstance(g, dict):
                        continue
                    can = [str(x).strip() for x in (g.get("canonical") or []) if str(x).strip()]
                    ali = [str(x).strip() for x in (g.get("aliases") or []) if str(x).strip()]
                    if can or ali:
                        groups.append({"canonical": can, "aliases": ali})
            except (OSError, ValueError):
                groups = []
        return {"vault_id": vault_id, "path": str(spath), "groups": groups}

    def save_vault_synonyms(self, vault_id: str, groups: list[dict[str, Any]]) -> dict[str, Any]:
        vpath = self.get_vault_path(vault_id)
        cfg = VaultConfig.load(vpath)
        cleaned: list[dict[str, list[str]]] = []
        for g in groups or []:
            if not isinstance(g, dict):
                continue
            can = [str(x).strip() for x in (g.get("canonical") or []) if str(x).strip()]
            ali = [str(x).strip() for x in (g.get("aliases") or []) if str(x).strip()]
            if can and ali:
                cleaned.append({"canonical": can, "aliases": ali})
        payload = {"version": 1, "groups": cleaned}
        _atomic_write_json(cfg.synonyms_path, payload)
        # Ensure active service reloads synonyms immediately
        svc = self.gateway.services.get(vault_id)
        if svc is not None:
            svc.context_engine.synonym_book = SynonymBook(cfg.synonyms_path)
        return {"vault_id": vault_id, "path": str(cfg.synonyms_path), "groups": cleaned, "count": len(cleaned)}

    def inspect_playground(
        self,
        *,
        vault_id: str,
        question: str,
        mode: str = "wiki_context",
        principal_spec: dict[str, Any] | None = None,
        expanded_terms: list[str] | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        q = (question or "").strip()
        if not q:
            raise ValueError("请输入测试问题或检索关键词")
        rec = self.get_vault_record(vault_id)
        kind = "remote" if rec.get("kind") == "remote" else "local"
        caps = _vault_capabilities(kind)

        if kind == "remote":
            if mode != "wiki_context":
                return {
                    "kind": "remote",
                    "capabilities": caps,
                    "mode": mode,
                    "unsupported": True,
                    "message": "远端挂载库仅开放客户端 /v1/context 实测模式；底层分词与 ACL Shadow Diff 需在提供方主机本地控制台执行。",
                }
            res = self.query_registered_vault(vault_id, q, max_tokens=max_tokens)
            return {
                "kind": "remote",
                "capabilities": caps,
                "mode": "wiki_context",
                "principal": {"uid": str(rec.get("remote_uid") or "remote-token"), "groups": list(rec.get("remote_groups") or [])},
                "result": res,
                "shadow_diff": None,
            }

        vpath = self.get_vault_path(vault_id)
        svc = self.gateway.services.get(vault_id) or self.gateway.upsert_vault(vault_id, vpath)

        # Resolve target principal for comparison (or admin if none specified)
        target_principal = Principal(uid="playground-admin", groups=("admin",), allowed_vaults=("*",))
        spec = principal_spec or {}
        spec_kind = str(spec.get("type") or "admin").strip()
        if spec_kind == "token" and spec.get("token_id"):
            tok = next((t for t in self.list_tokens_public() if t["id"] == spec["token_id"]), None)
            if tok is None:
                raise ValueError(f"未找到令牌 '{spec['token_id']}'")
            target_principal = Principal(
                uid=tok["uid"],
                groups=tuple(tok["groups"]),
                allowed_vaults=tuple(tok["allowed_vaults"]),
            )
        elif spec_kind == "group":
            grps = tuple(str(g).strip() for g in (spec.get("groups") or []) if str(g).strip()) or ("reader",)
            target_principal = Principal(uid="playground-role", groups=grps, allowed_vaults=("*",))

        if mode == "wiki_route":
            r_out = route_query(q)
            return {
                "kind": "local",
                "capabilities": caps,
                "mode": "wiki_route",
                "result": r_out,
            }

        if mode == "wiki_search":
            if not target_principal.can_access_vault(vault_id):
                return {
                    "kind": "local",
                    "capabilities": caps,
                    "mode": "wiki_search",
                    "vault_denied": True,
                    "result": {"hits": [], "wiki_hits": []},
                }
            res = svc.tool_search({"query": q, "k": 10}, principal=target_principal)
            return {
                "kind": "local",
                "capabilities": caps,
                "mode": "wiki_search",
                "principal": {"uid": target_principal.uid, "groups": list(target_principal.groups)},
                "result": res,
            }

        # Default: full 4-layer X-Ray + ACL Shadow Diff (`wiki_context`)
        snap = svc._get_snapshot()
        admin_principal = Principal(uid="playground-admin", groups=("admin",), allowed_vaults=("*",))
        admin_out = svc.context_engine.build(
            snap,
            q,
            expanded_terms=expanded_terms,
            max_tokens=max_tokens,
            principal=admin_principal,
            resolve_mirror_fn=lambda s, r: svc._resolve_mirror_jailed(snap, s, r),
            exclude_patterns=svc.exclude_patterns,
            include_debug=True,
        )

        vault_denied = not target_principal.can_access_vault(vault_id)
        if spec_kind == "admin":
            target_out = admin_out
        elif vault_denied:
            target_out = {
                "vault": svc.cfg.name,
                "generation_id": snap.generation_id,
                "route": admin_out.get("route", "fact"),
                "elapsed_ms": 0.0,
                "wiki_count": 0,
                "primary_count": 0,
                "memory_count": 0,
                "acl_filtered_count": len(admin_out.get("wiki_hits") or []) + len(admin_out.get("primary_hits") or []),
                "wiki_hits": [],
                "primary_hits": [],
                "memory_hits": [],
                "missing_concepts": [],
                "context_markdown": f"403 Forbidden: Principal '{target_principal.uid}' is not authorized to access vault '{vault_id}'.",
                "acl_shadow_blocked": [],
            }
        else:
            target_out = svc.context_engine.build(
                snap,
                q,
                expanded_terms=expanded_terms,
                max_tokens=max_tokens,
                principal=target_principal,
                resolve_mirror_fn=lambda s, r: svc._resolve_mirror_jailed(snap, s, r),
                exclude_patterns=svc.exclude_patterns,
                include_debug=True,
            )

        self.telemetry.record({
            "client": "playground",
            "mode": "playground",
            "principal_uid": target_principal.uid,
            "vault_id": vault_id,
            "tool": "wiki_context",
            "query": q,
            "route": str(target_out.get("route") or ""),
            "elapsed_ms": float(target_out.get("elapsed_ms") or 0.0),
            "wiki_count": int(target_out.get("wiki_count") or 0),
            "primary_count": int(target_out.get("primary_count") or 0),
            "memory_count": int(target_out.get("memory_count") or 0),
            "acl_filtered_count": int(target_out.get("acl_filtered_count") or 0),
            "missing_concepts": target_out.get("missing_concepts") or [],
            "hit_sources": [
                str(h.get("source"))
                for h in (*(target_out.get("wiki_hits") or []), *(target_out.get("primary_hits") or []))
                if isinstance(h, dict) and h.get("source")
            ],
            "is_error": False,
        })

        return {
            "kind": "local",
            "capabilities": caps,
            "mode": "wiki_context",
            "vault_denied": vault_denied,
            "principal": {
                "uid": target_principal.uid,
                "groups": list(target_principal.groups),
                "allowed_vaults": list(target_principal.allowed_vaults),
            },
            "result": target_out,
            "admin_baseline": {
                "wiki_count": admin_out.get("wiki_count", 0),
                "primary_count": admin_out.get("primary_count", 0),
                "memory_count": admin_out.get("memory_count", 0),
                "wiki_sources": [h.get("source") for h in (admin_out.get("wiki_hits") or [])],
                "primary_sources": [h.get("source") for h in (admin_out.get("primary_hits") or [])],
            },
            "shadow_blocked": target_out.get("acl_shadow_blocked") or [],
        }

    # -- OTP Pairing & Network Discovery ------------------------------------ #

    def create_pairing_ticket(
        self,
        *,
        allowed_vaults: list[str] | None = None,
        groups: list[str] | None = None,
        ttl_sec: int = 300,
        auto_start_gateway: bool = True,
        allow_loopback: bool = False,
    ) -> dict[str, Any]:
        status = self.get_lan_gateway_status()
        if not status["running"] and auto_start_gateway:
            status = self.start_lan_gateway(host="0.0.0.0", port=int(status.get("port") or 8090))

        port = int(status.get("port") or 8090)
        urls: list[str] = list(status.get("lan_urls") or [])
        ts = discover_tailscale_peers()
        for tip in ts.get("self_ips") or []:
            u = f"http://{tip}:{port}"
            if u not in urls:
                urls.append(u)
        if allow_loopback and not urls:
            urls.append(f"http://127.0.0.1:{port}")
        if not urls:
            raise ValueError("未检测到可用的局域网或 Tailscale IP 地址，无法生成配对码")

        av = [str(x).strip() for x in (allowed_vaults or ["*"]) if str(x).strip()] or ["*"]
        grps = [str(x).strip() for x in (groups or ["reader"]) if str(x).strip()] or ["reader"]
        return self.pairing.create_pairing_code(
            host_label=socket.gethostname(),
            instance_id=self.instance_id,
            fp=self.fp,
            urls=urls,
            allowed_vaults=av,
            groups=grps,
            ttl_sec=ttl_sec,
            allow_loopback=allow_loopback,
        )

    def redeem_pairing_ticket(self, otp_id: str, otp_secret: str, client_label: str = "Paired Device") -> dict[str, Any]:
        grant = self.pairing.redeem_otp(otp_id, otp_secret)
        clean_lbl = (client_label or "Paired Device").strip()[:48]
        uid_slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", clean_lbl.lower()).strip("-") or "peer"
        tok_res = self.create_token(
            uid=f"peer-{uid_slug}-{secrets.token_hex(2)}",
            label=f"Paired: {clean_lbl}",
            groups=grant["groups"],
            allowed_vaults=grant["allowed_vaults"],
        )
        return {
            "token": tok_res["token"],
            "record": tok_res["record"],
            "instance_id": self.instance_id,
            "fp": self.fp,
        }

    def connect_with_pairing_code(
        self,
        *,
        code: str,
        client_label: str = "",
        selected_remote_ids: list[str] | None = None,
        sync_clients: bool = False,
        allow_loopback: bool = False,
    ) -> dict[str, Any]:
        parsed = decode_pairing_code(code, allow_loopback=allow_loopback)
        best_url, probe_meta = probe_verified_pairing_endpoint(
            parsed["urls"],
            expected_instance_id=parsed["instance_id"],
            expected_fp=parsed["fp"],
        )
        lbl = (client_label or socket.gethostname()).strip()
        exchanged = _http_json_request(
            f"{best_url}/v1/pair/exchange",
            method="POST",
            payload={
                "otp_id": parsed["otp_id"],
                "otp_secret": parsed["otp_secret"],
                "client_label": lbl,
            },
            timeout=5.0,
        )
        new_token = str(exchanged.get("token") or "").strip()
        if not new_token:
            raise ValueError("配对交换未返回有效令牌")

        res = self.connect_remote_gateway(
            gateway_url=best_url,
            token=new_token,
            selected_remote_ids=selected_remote_ids,
            sync_clients=sync_clients,
        )
        # Update remotes.json v2 node metadata for failover & TOFU tracking
        with self._lock:
            rem_doc = self._load_remotes_doc()
            nodes = dict(rem_doc.get("nodes") or {})
            nodes[parsed["instance_id"]] = {
                "instance_id": parsed["instance_id"],
                "host": parsed["host"],
                "fp": parsed["fp"],
                "urls": parsed["urls"],
                "active_url": best_url,
                "latency_ms": probe_meta["latency_ms"],
                "last_seen_at": _now_iso(),
            }
            rem_doc["nodes"] = nodes
            self._save_remotes_doc(rem_doc)

            cdata = self._load_console_data()
            changed = False
            conn_ids = {c["id"] for c in res.get("connected_vaults") or []}
            for v in cdata.get("vaults") or []:
                if isinstance(v, dict) and v.get("id") in conn_ids:
                    v["instance_id"] = parsed["instance_id"]
                    changed = True
            if changed:
                self._save_console_data(cdata)

        return {
            **res,
            "paired_host": parsed["host"],
            "instance_id": parsed["instance_id"],
            "fp": parsed["fp"],
            "latency_ms": probe_meta["latency_ms"],
        }

    def discover_network(self, *, force_refresh: bool = False) -> dict[str, Any]:
        with ThreadPoolExecutor(max_workers=2) as pool:
            ts_fut = pool.submit(discover_tailscale_peers, force_refresh=force_refresh)
            udp_fut = pool.submit(probe_lan_udp, self_instance_id=self.instance_id)
            ts_res = ts_fut.result()
            udp_res = udp_fut.result()
        with self._lock:
            rem_doc = self._load_remotes_doc()
            nodes = list((rem_doc.get("nodes") or {}).values())
        return {
            "self_instance_id": self.instance_id,
            "self_fp": self.fp,
            "lan_ips": detect_lan_ips(),
            "tailscale": ts_res,
            "lan_peers": udp_res,
            "paired_nodes": nodes,
        }

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

    def rotate_token(self, token_id: str) -> dict[str, Any]:
        raw_secret = f"wctx_{secrets.token_urlsafe(24)}"
        preview = f"{raw_secret[:9]}…{raw_secret[-4:]}"
        with self._lock:
            doc = self._load_tokens_doc()
            target: dict[str, Any] | None = None
            for i, r in enumerate(doc["tokens"]):
                if not isinstance(r, dict):
                    continue
                rid = str(r.get("id") or f"tok_{i}")
                if rid == token_id:
                    r["id"] = rid
                    r["token_sha256"] = _sha256_hex(raw_secret)
                    r["token_preview"] = preview
                    r["rotated_at"] = _now_iso()
                    target = dict(r)
                    break
            if target is None:
                raise KeyError(f"Unknown token_id '{token_id}'")
            self._save_tokens_doc(doc)
        return {
            "token": raw_secret,
            "record": {k: v for k, v in target.items() if k != "token_sha256"},
        }

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
            "items": filtered,
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
    admin_token: str | None = None,
    shutdown_event: threading.Event | None = None,
) -> _HighBacklogHTTPServer:
    if not allow_remote_admin and not _is_loopback_host(host):
        raise ValueError(
            f"Refusing to bind admin Control Console to non-loopback host '{host}' without --allow-remote-admin"
        )

    csrf_token = secrets.token_urlsafe(24)
    eff_admin_token = (admin_token if admin_token is not None else os.environ.get("WCTX_ADMIN_TOKEN") or "").strip()
    stop_evt = shutdown_event or threading.Event()
    ui_dist_dir = Path(__file__).resolve().parent / "ui_dist"
    if not ui_dist_dir.is_dir():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            cand_mei = Path(meipass) / "wiki_context" / "ui_dist"
            if cand_mei.is_dir():
                ui_dist_dir = cand_mei

    class ConsoleHTTPHandler(GatewayRoutesMixin, BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        gateway = state.gateway

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

        def _is_allowed_host(self) -> bool:
            if allow_remote_admin and not _is_loopback_host(host):
                return True
            req_host = (self.headers.get("Host") or "").strip().lower()
            if not req_host:
                return True
            actual_port = self.server.server_address[1]  # type: ignore[attr-defined]
            allowed_hosts = {
                f"127.0.0.1:{actual_port}",
                f"localhost:{actual_port}",
                f"[::1]:{actual_port}",
                "127.0.0.1",
                "localhost",
            }
            return req_host in allowed_hosts

        def _cors_origin(self, *, allow_null_for_sidecar: bool = False) -> str:
            origin = (self.headers.get("Origin") or "").strip()
            if not origin:
                return ""
            actual_port = self.server.server_address[1]  # type: ignore[attr-defined]
            allowed_origins = {
                f"http://127.0.0.1:{actual_port}",
                f"http://localhost:{actual_port}",
                "tauri://localhost",
                "http://tauri.localhost",
                "https://tauri.localhost",
            }
            if os.environ.get("WCTX_DEV_ORIGIN") == "1":
                allowed_origins.update({"http://127.0.0.1:5173", "http://localhost:5173"})
            if origin in allowed_origins:
                return origin
            if allow_null_for_sidecar and origin == "null" and eff_admin_token:
                return "null"
            return ""

        def _cors_headers(self) -> dict[str, str]:
            supplied_admin = (self.headers.get("X-WCTX-Admin-Token") or "").strip()
            has_valid_admin = bool(
                eff_admin_token and supplied_admin and hmac.compare_digest(supplied_admin, eff_admin_token)
            )
            req_hdrs = (self.headers.get("Access-Control-Request-Headers") or "").lower()
            is_admin_preflight = self.command == "OPTIONS" and bool(eff_admin_token) and "x-wctx-admin-token" in req_hdrs
            co = self._cors_origin(allow_null_for_sidecar=(has_valid_admin or is_admin_preflight))
            if not co:
                return {}
            return {
                "Access-Control-Allow-Origin": co,
                "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, X-WCTX-CSRF, X-WCTX-Admin-Token, Authorization, Last-Event-ID",
                "Access-Control-Allow-Private-Network": "true",
                "Vary": "Origin",
            }

        def _send_json(self, status: int, payload: dict | list, extra_headers: dict[str, str] | None = None) -> None:
            merged = self._cors_headers()
            if extra_headers:
                merged.update(extra_headers)
            super()._send_json(status, payload, merged or None)

        def _send_empty(self, status: int, extra_headers: dict[str, str] | None = None) -> None:
            merged = self._cors_headers()
            if extra_headers:
                merged.update(extra_headers)
            super()._send_empty(status, merged or None)

        def _send_html(self, status: int, html_text: str) -> None:
            raw = html_text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            for hk, hv in self._cors_headers().items():
                self.send_header(hk, hv)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _send_bytes(self, status: int, data: bytes, content_type: str, cache_control: str = "public, max-age=3600") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", cache_control)
            self.send_header("X-Frame-Options", "DENY")
            for hk, hv in self._cors_headers().items():
                self.send_header(hk, hv)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _require_admin_csrf(self) -> bool:
            if not self._is_allowed_host():
                self._send_json(403, {"error": "Disallowed Host header (DNS rebinding guard)"})
                return False
            supplied_admin = (self.headers.get("X-WCTX-Admin-Token") or "").strip()
            has_valid_admin = bool(
                eff_admin_token and supplied_admin and hmac.compare_digest(supplied_admin, eff_admin_token)
            )
            origin = (self.headers.get("Origin") or "").strip()
            if origin and not self._cors_origin(allow_null_for_sidecar=has_valid_admin):
                self._send_json(403, {"error": "Cross-origin admin request blocked"})
                return False
            if has_valid_admin:
                return True
            supplied_csrf = (self.headers.get("X-WCTX-CSRF") or "").strip()
            if supplied_csrf and hmac.compare_digest(supplied_csrf, csrf_token):
                return True
            self._send_json(403, {"error": "Missing or invalid X-WCTX-CSRF / X-WCTX-Admin-Token session token"})
            return False

        def do_OPTIONS(self) -> None:  # noqa: N802
            if not self._is_allowed_host():
                self._send_empty(403)
                return
            origin = (self.headers.get("Origin") or "").strip()
            req_hdrs = (self.headers.get("Access-Control-Request-Headers") or "").lower()
            is_admin_preflight = bool(eff_admin_token) and "x-wctx-admin-token" in req_hdrs
            if origin and not self._cors_origin(allow_null_for_sidecar=is_admin_preflight):
                self._send_empty(403)
                return
            self._send_empty(204, {"Access-Control-Max-Age": "600"})

        def _gateway_base_url(self) -> str:
            actual_port = self.server.server_address[1]  # type: ignore[attr-defined]
            disp_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
            return f"http://{disp_host}:{actual_port}"

        def _bootstrap_payload(self) -> dict[str, Any]:
            return {
                "csrfToken": csrf_token,
                "version": __version__,
                "mode": "desktop" if eff_admin_token else "web",
                "gatewayBaseUrl": self._gateway_base_url(),
                "instanceId": state.instance_id,
                "fp": state.fp,
                "uiDist": (ui_dist_dir / "index.html").is_file(),
            }

        def _serve_ui_index(self) -> None:
            boot = self._bootstrap_payload()
            dist_index = ui_dist_dir / "index.html"
            if dist_index.is_file():
                html = dist_index.read_text(encoding="utf-8")
                boot_script = f'<script>window.__WCTX_BOOTSTRAP__={json.dumps(boot, ensure_ascii=False)};</script>'
                if "</head>" in html:
                    html = html.replace("</head>", f"{boot_script}</head>", 1)
                else:
                    html = boot_script + html
                self._send_html(200, html)
                return
            self._send_html(200, render_console_html(boot))

        def _try_serve_static(self, path: str) -> bool:
            if not ui_dist_dir.is_dir():
                return False
            rel = path.lstrip("/")
            if not rel or ".." in rel.split("/"):
                return False
            cand = (ui_dist_dir / rel).resolve()
            if ui_dist_dir.resolve() not in cand.parents or not cand.is_file():
                return False
            ctype, _ = mimetypes.guess_type(str(cand))
            if not ctype:
                ctype = "application/octet-stream"
            elif ctype.startswith("text/") or ctype in {"application/javascript", "application/json", "image/svg+xml"}:
                ctype = f"{ctype}; charset=utf-8"
            self._send_bytes(200, cand.read_bytes(), ctype)
            return True

        def _handle_logs_sse(self, qs: dict[str, list[str]]) -> None:
            if not self._require_admin_csrf():
                return
            after_raw = (self.headers.get("Last-Event-ID") or (qs.get("after_id") or ["0"])[0] or "0").strip()
            try:
                cursor_id = max(0, int(after_raw))
            except ValueError:
                cursor_id = 0

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            for hk, hv in self._cors_headers().items():
                self.send_header(hk, hv)
            self.end_headers()

            try:
                self.wfile.write(b"retry: 1500\n\n")
                if cursor_id == 0:
                    init_items = state.telemetry.list_invocations(limit=40)["items"]
                    init_items.reverse()
                    if init_items:
                        cursor_id = max(int(x["id"]) for x in init_items)
                        payload = json.dumps({"items": init_items, "latest_id": cursor_id}, ensure_ascii=False)
                        self.wfile.write(f"id: {cursor_id}\nevent: init\ndata: {payload}\n\n".encode("utf-8"))
                else:
                    cursor_id = max(cursor_id, 0)
                self.wfile.flush()

                last_ping = time.monotonic()
                while not stop_evt.is_set():
                    new_rows = state.telemetry.tail_after(cursor_id, limit=50)
                    if new_rows:
                        for row in new_rows:
                            cursor_id = max(cursor_id, int(row["id"]))
                            data_line = json.dumps(row, ensure_ascii=False)
                            self.wfile.write(f"id: {cursor_id}\nevent: log\ndata: {data_line}\n\n".encode("utf-8"))
                        self.wfile.flush()
                        last_ping = time.monotonic()
                    elif (time.monotonic() - last_ping) >= 1.0:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        last_ping = time.monotonic()
                    stop_evt.wait(0.25)
            except (OSError, BrokenPipeError, ConnectionResetError):
                return

        def _inspect_clients_enriched(
            self,
            vaults: list[dict[str, Any]] | None = None,
        ) -> list[dict[str, Any]]:
            vault_list = vaults if vaults is not None else state.list_vaults_detailed()
            vault_by_id = {v["id"]: v for v in vault_list}
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
                if not vault_list:
                    c["sync_status"] = "idle"
                elif matched == len(vault_list):
                    c["sync_status"] = "synced"
                elif matched > 0:
                    c["sync_status"] = "partial"
                else:
                    c["sync_status"] = "unconfigured"
                c["matched_count"] = matched
                c["total_vaults"] = len(vault_list)
            return clients

        def _build_overview(self) -> dict[str, Any]:
            vaults = state.list_vaults_detailed()
            clients = self._inspect_clients_enriched(vaults)

            cmd, base_args = resolve_wctx_command()
            return {
                "version": __version__,
                "config_dir": str(state.config_dir),
                "tokens_file": str(state.tokens_file),
                "gateway_base_url": self._gateway_base_url(),
                "lan_gateway": state.get_lan_gateway_status(),
                "llm_compiler": state.get_llm_compiler_status(),
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
                if not self._is_allowed_host():
                    self._send_json(403, {"error": "Disallowed Host header"})
                    return
                self._serve_ui_index()
                return

            if path == "/api/bootstrap":
                if not self._is_allowed_host():
                    self._send_json(403, {"error": "Disallowed Host header"})
                    return
                origin = (self.headers.get("Origin") or "").strip()
                if origin and not self._cors_origin():
                    self._send_json(403, {"error": "Cross-origin bootstrap request blocked"})
                    return
                self._send_json(200, self._bootstrap_payload())
                return

            if self.try_gateway_get(path):
                return

            if path == "/api/logs/stream":
                self._handle_logs_sse(qs)
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
                    if path.startswith("/api/jobs/"):
                        jid = path[len("/api/jobs/"):].strip("/")
                        self._send_json(200, state.get_job(jid))
                        return
                    if path == "/api/vaults/synonyms":
                        vid = (qs.get("vault_id") or [""])[0]
                        self._send_json(200, state.get_vault_synonyms(vid))
                        return
                    if path == "/api/vaults/compile-plan":
                        vid = (qs.get("vault_id") or [""])[0]
                        self._send_json(200, state.plan_vault_compile(vid))
                        return
                    if path == "/api/llm/config":
                        self._send_json(200, state.get_llm_compiler_status())
                        return
                    if path == "/api/logs/list":
                        lim = int((qs.get("limit") or ["100"])[0])
                        before_raw = (qs.get("before_id") or [""])[0].strip()
                        before_id = int(before_raw) if before_raw.isdigit() else None
                        min_ms = float((qs.get("min_elapsed_ms") or ["0"])[0] or 0.0)
                        self._send_json(
                            200,
                            state.telemetry.list_invocations(
                                limit=lim,
                                before_id=before_id,
                                client=(qs.get("client") or [""])[0],
                                vault_id=(qs.get("vault_id") or [""])[0],
                                tool=(qs.get("tool") or [""])[0],
                                only_missing=(qs.get("only_missing") or ["0"])[0] in {"1", "true"},
                                only_errors=(qs.get("only_errors") or ["0"])[0] in {"1", "true"},
                                min_elapsed_ms=min_ms,
                                query=(qs.get("q") or [""])[0],
                            ),
                        )
                        return
                    if path == "/api/logs/stats":
                        hrs = int((qs.get("hours") or ["24"])[0])
                        self._send_json(200, state.telemetry.stats(hours=hrs))
                        return
                    if path == "/api/network/discover":
                        force = (qs.get("refresh") or ["0"])[0] in {"1", "true"}
                        self._send_json(200, state.discover_network(force_refresh=force))
                        return
                    if path == "/api/clients/scan":
                        clients = self._inspect_clients_enriched()
                        installed_clients = [c for c in clients if c.get("installed")]
                        synced_count = sum(1 for c in installed_clients if c.get("sync_status") == "synced")
                        self._send_json(
                            200,
                            {
                                "clients": clients,
                                "installed_count": len(installed_clients),
                                "synced_count": synced_count,
                                "total_supported": len(clients),
                            },
                        )
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

            if self._try_serve_static(path):
                return

            # SPA client-side route fallback when ui_dist is built
            if not path.startswith(("/api/", "/mcp", "/v1/", "/healthz")) and "." not in Path(path).name:
                if (ui_dist_dir / "index.html").is_file():
                    self._serve_ui_index()
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
                    auto_cmp = body.get("auto_compile") if "auto_compile" in body else None
                    res = state.onboard_and_register(
                        path=str(body.get("path") or ""),
                        vault_id=str(body.get("vault_id") or ""),
                        name=str(body.get("name") or ""),
                        layout=str(body.get("layout") or ""),
                        adopt_loose_files=bool(body.get("adopt_loose_files", True)),
                        auto_compile=auto_cmp if isinstance(auto_cmp, bool) else None,
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
                    vid = str(body.get("vault_id") or "")
                    force_flag = bool(body.get("force", False))
                    trig = str(body.get("trigger") or "manual")
                    if bool(body.get("async", False)):
                        job = state.start_sync_job(vid, force=force_flag, trigger=trig)
                        self._send_json(202, job)
                    else:
                        res = state.sync_registered_vault(vid, force=force_flag)
                        self._send_json(200, res)
                    return

                if path == "/api/vaults/compile":
                    vid = str(body.get("vault_id") or "")
                    max_items = int(body["max_items"]) if body.get("max_items") is not None else None
                    job = state.start_compile_job(vid, max_items=max_items, trigger="manual")
                    self._send_json(202, job)
                    return

                if path == "/api/llm/config":
                    res = state.save_llm_compiler_config(body)
                    self._send_json(200, res)
                    return

                if path == "/api/llm/test":
                    res = state.test_llm_compiler(body)
                    self._send_json(200, res)
                    return

                if path == "/api/vaults/watch":
                    res = state.set_vault_watch(
                        str(body.get("vault_id") or ""),
                        enabled=bool(body.get("enabled", True)),
                    )
                    self._send_json(200, res)
                    return

                if path == "/api/vaults/synonyms":
                    res = state.save_vault_synonyms(
                        str(body.get("vault_id") or ""),
                        body.get("groups") or [],
                    )
                    self._send_json(200, res)
                    return

                if path == "/api/playground/inspect":
                    res = state.inspect_playground(
                        vault_id=str(body.get("vault_id") or ""),
                        question=str(body.get("question") or ""),
                        mode=str(body.get("mode") or "wiki_context"),
                        principal_spec=body.get("principal_spec"),
                        expanded_terms=body.get("expanded_terms"),
                        max_tokens=body.get("max_tokens"),
                    )
                    self._send_json(200, res)
                    return

                if path == "/api/logs/clear":
                    deleted = state.telemetry.clear()
                    self._send_json(200, {"cleared": deleted})
                    return

                if path == "/api/network/pairing-code":
                    res = state.create_pairing_ticket(
                        allowed_vaults=body.get("allowed_vaults"),
                        groups=body.get("groups"),
                        ttl_sec=int(body.get("ttl_sec") or 300),
                        auto_start_gateway=bool(body.get("auto_start_gateway", True)),
                        allow_loopback=bool(body.get("allow_loopback", False)),
                    )
                    self._send_json(200, res)
                    return

                if path == "/api/network/connect-code":
                    res = state.connect_with_pairing_code(
                        code=str(body.get("code") or ""),
                        client_label=str(body.get("client_label") or ""),
                        selected_remote_ids=body.get("remote_vault_ids"),
                        sync_clients=bool(body.get("sync_clients", False)),
                        allow_loopback=bool(body.get("allow_loopback", False)),
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

                if path == "/api/tokens/rotate":
                    res = state.rotate_token(str(body.get("id") or ""))
                    self._send_json(200, res)
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

                if path == "/api/clients/disconnect":
                    client_ids = [str(x) for x in (body.get("client_ids") or []) if str(x).strip()]
                    invalid_ids = [cid for cid in client_ids if cid not in CLIENT_BY_ID]
                    if invalid_ids:
                        raise ValueError(f"Unsupported client(s): {', '.join(invalid_ids)}")
                    disconnected = [
                        disconnect_client_mcp_config(cid, home=state.client_home)
                        for cid in client_ids
                    ]
                    self._send_json(200, {"results": disconnected})
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
                        if cid not in CLIENT_BY_ID:
                            raise ValueError(f"Unsupported client '{cid}'")
                        snippet = render_client_snippet(cid, desired)
                        cpath = str(CLIENT_BY_ID[cid].config_path(state.client_home))
                        self._send_json(200, {"client_id": cid, "config_path": cpath, "snippet": snippet})
                        return

                    client_ids = [str(x) for x in (body.get("client_ids") or []) if str(x).strip()]
                    invalid_ids = [cid for cid in client_ids if cid not in CLIENT_BY_ID]
                    if invalid_ids:
                        raise ValueError(f"Unsupported client(s): {', '.join(invalid_ids)}")
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
    sidecar: bool = False,
    watch_stdin_eof: bool = False,
) -> int:
    cfg_base = Path(config_dir or os.environ.get("WCTX_HOME") or (Path.home() / ".wiki-context")).expanduser().resolve()
    cfg_base.mkdir(parents=True, exist_ok=True)
    daemon_lock_path = cfg_base / "daemon.lock"

    # Single-instance check before binding ports
    try:
        lock_ctx = file_lock(daemon_lock_path, blocking=False, busy_message="Another console daemon is running")
        lock_ctx.__enter__()
    except RuntimeError:
        live = probe_live_daemon(cfg_base)
        if live and live.get("port"):
            live_port = int(live["port"])
            live_url = str(live.get("url") or f"http://127.0.0.1:{live_port}")
            if sidecar:
                print(f"WCTX_READY {live_port} {live.get('pid', os.getpid())}", flush=True)
            else:
                print(f"wiki-context Control Console already running at {live_url} (pid={live.get('pid')})", flush=True)
                if open_browser:
                    webbrowser.open(live_url)
            return 0
        # Stale lock fallback: wait briefly with blocking lock
        lock_ctx = file_lock(daemon_lock_path, blocking=True, timeout=5.0)
        lock_ctx.__enter__()

    shutdown_event = threading.Event()
    state: ConsoleState | None = None
    srv: _HighBacklogHTTPServer | None = None
    try:
        state = ConsoleState(
            config_dir=cfg_base,
            extra_vault_specs=extra_vault_specs,
            defer_vault_hydration=sidecar,
        )
        srv = create_console_server(
            state,
            host=host,
            port=port,
            allow_remote_admin=allow_remote_admin,
            shutdown_event=shutdown_event,
        )
        actual_port = srv.server_address[1]
        lan_stat = state.get_lan_gateway_status()
        gw_port = int(lan_stat["port"]) if lan_stat.get("running") else None
        write_daemon_info(
            cfg_base,
            port=actual_port,
            mode="sidecar" if sidecar else "console",
            gateway_port=gw_port,
            instance_id=state.instance_id,
        )

        def _trigger_shutdown(*_args) -> None:
            if not shutdown_event.is_set():
                shutdown_event.set()
                if srv is not None:
                    threading.Thread(target=srv.shutdown, daemon=True).start()

        if threading.current_thread() is threading.main_thread():
            for sig_name in ("SIGINT", "SIGTERM"):
                sig = getattr(signal, sig_name, None)
                if sig is not None:
                    try:
                        signal.signal(sig, _trigger_shutdown)
                    except (ValueError, OSError):
                        pass

        if watch_stdin_eof:
            def _stdin_watchdog() -> None:
                try:
                    while not shutdown_event.is_set():
                        chunk = sys.stdin.buffer.read(1)
                        if not chunk:
                            break
                except Exception:
                    pass
                _trigger_shutdown()

            threading.Thread(target=_stdin_watchdog, name="wctx-stdin-watchdog", daemon=True).start()

        url = f"http://{'127.0.0.1' if host in {'0.0.0.0', '::'} else host}:{actual_port}"
        if allow_remote_admin and not _is_loopback_host(host):
            print(
                "[WARNING] Control Console admin routes (/api/*) have no user login password and are "
                f"currently bound to non-loopback host '{host}'. Place it behind an authenticated VPN/reverse proxy, "
                "or run `wctx gateway` for production multi-user serving (Bearer Token only, no admin UI).",
                file=sys.stderr,
                flush=True,
            )
        if sidecar:
            print(f"WCTX_READY {actual_port} {os.getpid()}", flush=True)
        else:
            print(
                f"wiki-context Control Console & Gateway running at {url}\n"
                f"  Config & Tokens : {state.config_dir}\n"
                f"  MCP HTTP Route  : {url}/mcp/<vault_id>",
                flush=True,
            )
        if open_browser and not sidecar:
            threading.Timer(0.35, lambda: webbrowser.open(url)).start()

        try:
            srv.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
        finally:
            shutdown_event.set()
            srv.server_close()
    finally:
        clear_daemon_info(cfg_base)
        if state is not None:
            state.close()
        try:
            lock_ctx.__exit__(None, None, None)
        except Exception:
            pass
    return 0
