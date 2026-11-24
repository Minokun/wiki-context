"""AI Client MCP Configuration Manager for 19+ mainstream AI Agents, IDEs, CLIs, and Desktop Apps.

Supports:
- Auto-detecting installed AI clients (IDE editors, coding CLIs, and desktop assistants) and
  parsing currently configured `wiki-context` MCP servers plus preserving non-wctx MCP count;
- Lossless, atomic configuration syncing and disconnecting (preserving unrelated MCP servers,
  comments, and settings across JSON/JSONC, TOML, and YAML formats);
- Generating ready-to-copy snippets for both Local Stdio (`wctx serve`) and Token-Authenticated
  Gateway (`streamable-http` / `wctx bridge`) modes.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .state_lock import atomic_write_text


def _platform_app_support_dir(h: Path, *, allow_env: bool) -> Path:
    """Return platform Application Support / Roaming / XDG config root under `h`."""
    if os.name == "nt":
        if allow_env:
            appdata = (os.environ.get("APPDATA") or "").strip()
            if appdata:
                return Path(appdata).expanduser()
        return h / "AppData" / "Roaming"
    if sys.platform == "darwin":
        return h / "Library" / "Application Support"
    if allow_env:
        xdg = (os.environ.get("XDG_CONFIG_HOME") or "").strip()
        if xdg:
            return Path(xdg).expanduser()
    return h / ".config"


def _dir_has_real_footprint(path: Path) -> bool:
    """Return True if `path` exists and has non-skills user data/config entries."""
    try:
        if not path.is_dir():
            return False
        for child in path.iterdir():
            if child.name not in {"skills", ".DS_Store"}:
                return True
        return False
    except OSError:
        return False


def _macos_app_exists(app_names: tuple[str, ...]) -> bool:
    """Check `/Applications` and `~/Applications` for installed macOS `.app` bundles."""
    if sys.platform != "darwin" or not app_names:
        return False
    roots = (Path("/Applications"), Path.home() / "Applications")
    for app_name in app_names:
        for root in roots:
            if (root / app_name).exists():
                return True
    return False


def _strip_json_comments(raw: str) -> str:
    """Strip `//` line comments and `/* ... */` block comments outside double-quoted strings."""
    out: list[str] = []
    i = 0
    n = len(raw)
    in_string = False
    escaped = False
    while i < n:
        ch = raw[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n:
            nxt = raw[i + 1]
            if nxt == "/":
                i += 2
                while i < n and raw[i] != "\n":
                    i += 1
                continue
            if nxt == "*":
                i += 2
                while i + 1 < n and not (raw[i] == "*" and raw[i + 1] == "/"):
                    i += 1
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_trailing_json_commas(raw: str) -> str:
    """Remove trailing commas before `}` or `]` outside double-quoted strings."""
    out: list[str] = []
    i = 0
    n = len(raw)
    in_string = False
    escaped = False
    while i < n:
        ch = raw[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < n and raw[j] in " \t\r\n":
                j += 1
            if j < n and raw[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _load_json_lenient(raw: str) -> Any:
    """Parse JSON cleanly, falling back to JSONC (comments + trailing commas) when needed."""
    try:
        return json.loads(raw)
    except ValueError:
        cleaned = _strip_trailing_json_commas(_strip_json_comments(raw))
        return json.loads(cleaned)


@dataclass(frozen=True)
class ClientSpec:
    id: str
    name: str
    vendor: str
    category: str  # "ide" | "cli" | "desktop"
    format: str
    description: str
    app_bundles: tuple[str, ...] = ()
    cli_binaries: tuple[str, ...] = ()

    def config_path(self, home: Path | None = None) -> Path:
        h = (home or Path.home()).expanduser().resolve()
        app_sup = _platform_app_support_dir(h, allow_env=(home is None))
        if self.id == "dsh":
            desktop = h / ".dsh" / "profiles" / "desktop" / "cordis.patch.yml"
            web = h / ".dsh" / "profiles" / "web" / "cordis.patch.yml"
            if not desktop.is_file() and web.is_file():
                return web
            return desktop
        if self.id == "codex":
            return h / ".codex" / "config.toml"
        if self.id == "claude":
            return h / ".claude.json"
        if self.id == "workbuddy":
            return h / ".workbuddy" / "mcp.json"
        if self.id == "cursor":
            return h / ".cursor" / "mcp.json"
        if self.id == "agy":
            return h / ".gemini" / "config" / "mcp_config.json"
        if self.id == "claude-desktop":
            return app_sup / "Claude" / "claude_desktop_config.json"
        if self.id == "vscode":
            return app_sup / "Code" / "User" / "mcp.json"
        if self.id == "windsurf":
            return h / ".codeium" / "windsurf" / "mcp_config.json"
        if self.id == "trae":
            cn_cfg = h / ".trae-cn" / "mcp.json"
            intl_cfg = h / ".trae" / "mcp.json"
            if cn_cfg.is_file():
                return cn_cfg
            if intl_cfg.is_file():
                return intl_cfg
            if (app_sup / "Trae CN").is_dir() or (home is None and _macos_app_exists(("Trae CN.app",))):
                return cn_cfg
            return intl_cfg
        if self.id == "kiro":
            return h / ".kiro" / "settings" / "mcp.json"
        if self.id == "cline":
            vsc_dir = app_sup / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev"
            if vsc_dir.is_dir():
                return vsc_dir / "settings" / "cline_mcp_settings.json"
            if _dir_has_real_footprint(h / ".cline"):
                return h / ".cline" / "data" / "settings" / "cline_mcp_settings.json"
            return vsc_dir / "settings" / "cline_mcp_settings.json"
        if self.id == "roo-code":
            roo_settings = app_sup / "Code" / "User" / "globalStorage" / "rooveterinaryinc.roo-cline" / "settings"
            legacy_file = roo_settings / "cline_mcp_settings.json"
            modern_file = roo_settings / "mcp_settings.json"
            if not modern_file.is_file() and legacy_file.is_file():
                return legacy_file
            return modern_file
        if self.id == "gemini-cli":
            return h / ".gemini" / "settings.json"
        if self.id == "qwen-code":
            return h / ".qwen" / "settings.json"
        if self.id == "kimi":
            kimi_code = h / ".kimi-code" / "mcp.json"
            kimi_legacy = h / ".kimi" / "mcp.json"
            if kimi_code.is_file() or (not kimi_legacy.is_file() and _dir_has_real_footprint(h / ".kimi-code")):
                return kimi_code
            if kimi_legacy.is_file() or _dir_has_real_footprint(h / ".kimi"):
                return kimi_legacy
            return kimi_code
        if self.id == "opencode":
            return h / ".config" / "opencode" / "opencode.json"
        if self.id == "zed":
            if os.name == "nt":
                return app_sup / "Zed" / "settings.json"
            return h / ".config" / "zed" / "settings.json"
        if self.id == "lmstudio":
            return h / ".lmstudio" / "mcp.json"
        raise ValueError(f"Unknown client id: {self.id}")

    def _home_dir_detected(self, home: Path | None = None) -> bool:
        h = (home or Path.home()).expanduser().resolve()
        app_sup = _platform_app_support_dir(h, allow_env=(home is None))
        if self.id == "dsh":
            return (h / ".dsh").is_dir()
        if self.id == "codex":
            return (h / ".codex").is_dir()
        if self.id == "claude":
            return (h / ".claude").is_dir() or (h / ".claude.json").is_file()
        if self.id == "workbuddy":
            return (h / ".workbuddy").is_dir()
        if self.id == "cursor":
            return (h / ".cursor").is_dir()
        if self.id == "agy":
            return (h / ".gemini" / "config").is_dir() or (h / ".gemini" / "antigravity").is_dir()
        if self.id == "claude-desktop":
            return _dir_has_real_footprint(app_sup / "Claude")
        if self.id == "vscode":
            return _dir_has_real_footprint(app_sup / "Code" / "User")
        if self.id == "windsurf":
            return _dir_has_real_footprint(h / ".codeium" / "windsurf") or _dir_has_real_footprint(app_sup / "Windsurf" / "User")
        if self.id == "trae":
            return (
                _dir_has_real_footprint(h / ".trae")
                or _dir_has_real_footprint(h / ".trae-cn")
                or _dir_has_real_footprint(app_sup / "Trae" / "User")
                or _dir_has_real_footprint(app_sup / "Trae CN" / "User")
            )
        if self.id == "kiro":
            return (h / ".kiro" / "settings").is_dir() or _dir_has_real_footprint(h / ".kiro")
        if self.id == "cline":
            return (
                (app_sup / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev").is_dir()
                or _dir_has_real_footprint(h / ".cline")
            )
        if self.id == "roo-code":
            # Avoid false positives from skills-only `~/.roo/skills`.
            return (
                (app_sup / "Code" / "User" / "globalStorage" / "rooveterinaryinc.roo-cline").is_dir()
                or (h / ".roo" / "mcp.json").is_file()
            )
        if self.id == "gemini-cli":
            # Only rely on `~/.gemini/settings.json` so Antigravity-only setups are never conflated.
            return (h / ".gemini" / "settings.json").is_file()
        if self.id == "qwen-code":
            return _dir_has_real_footprint(h / ".qwen")
        if self.id == "kimi":
            return _dir_has_real_footprint(h / ".kimi-code") or _dir_has_real_footprint(h / ".kimi")
        if self.id == "opencode":
            return _dir_has_real_footprint(h / ".config" / "opencode")
        if self.id == "zed":
            return _dir_has_real_footprint(self.config_path(home).parent)
        if self.id == "lmstudio":
            return _dir_has_real_footprint(h / ".lmstudio")
        return False

    def detect_installation(self, home: Path | None = None) -> tuple[bool, list[str]]:
        """Return `(installed, detected_via)` without peeking host system apps when `home` is sandboxed."""
        signals: list[str] = []
        cpath = self.config_path(home)
        if cpath.is_file():
            signals.append("config_file")
        if self._home_dir_detected(home):
            signals.append("client_dir")
        if home is None:
            if self.app_bundles and _macos_app_exists(self.app_bundles):
                signals.append("app_bundle")
            if self.cli_binaries and any(shutil.which(b) for b in self.cli_binaries):
                signals.append("cli_binary")
        return (len(signals) > 0), signals

    def client_dir_exists(self, home: Path | None = None) -> bool:
        return self._home_dir_detected(home)


CLIENT_SPECS: tuple[ClientSpec, ...] = (
    ClientSpec(
        id="claude",
        name="Claude Code",
        vendor="Anthropic",
        category="cli",
        format="claude-json",
        description="无损合并写入 ~/.claude.json 的顶层 mcpServers 字段",
        cli_binaries=("claude",),
    ),
    ClientSpec(
        id="cursor",
        name="Cursor",
        vendor="Anysphere",
        category="ide",
        format="cursor-json",
        description="无损合并写入 ~/.cursor/mcp.json 的 mcpServers 字段",
        app_bundles=("Cursor.app",),
        cli_binaries=("cursor",),
    ),
    ClientSpec(
        id="codex",
        name="Codex CLI",
        vendor="OpenAI",
        category="cli",
        format="codex-toml",
        description="写入 ~/.codex/config.toml 的 [mcp_servers.<id>] 段落",
        cli_binaries=("codex",),
    ),
    ClientSpec(
        id="agy",
        name="Antigravity (AGY)",
        vendor="Google",
        category="ide",
        format="agy-json",
        description="无损合并写入 ~/.gemini/config/mcp_config.json 的 mcpServers 字段",
        app_bundles=("Antigravity.app", "Antigravity IDE.app"),
    ),
    ClientSpec(
        id="workbuddy",
        name="WorkBuddy",
        vendor="Tencent",
        category="desktop",
        format="workbuddy-json",
        description="写入 ~/.workbuddy/mcp.json（兼容 stdio 与 streamable-http）",
        app_bundles=("WorkBuddy.app",),
    ),
    ClientSpec(
        id="dsh",
        name="DeepSeek Harness (DSH)",
        vendor="DeepSeek",
        category="desktop",
        format="dsh-yaml",
        description="写入 ~/.dsh/profiles/desktop/cordis.patch.yml 并递增版本号触发热重载",
    ),
    ClientSpec(
        id="claude-desktop",
        name="Claude Desktop",
        vendor="Anthropic",
        category="desktop",
        format="standard-json",
        description="无损合并写入 Claude Desktop claude_desktop_config.json 的 mcpServers 字段",
        app_bundles=("Claude.app",),
    ),
    ClientSpec(
        id="vscode",
        name="VS Code (Copilot)",
        vendor="Microsoft",
        category="ide",
        format="vscode-json",
        description="无损合并写入 VS Code 用户 mcp.json 的 servers 字段",
        app_bundles=("Visual Studio Code.app",),
        cli_binaries=("code",),
    ),
    ClientSpec(
        id="windsurf",
        name="Windsurf",
        vendor="Codeium",
        category="ide",
        format="windsurf-json",
        description="无损合并写入 ~/.codeium/windsurf/mcp_config.json 的 mcpServers 字段",
        app_bundles=("Windsurf.app",),
        cli_binaries=("windsurf",),
    ),
    ClientSpec(
        id="trae",
        name="Trae",
        vendor="ByteDance",
        category="ide",
        format="http-json",
        description="无损合并写入 Trae 全局 mcp.json 的 mcpServers 字段",
        app_bundles=("Trae.app", "Trae CN.app"),
    ),
    ClientSpec(
        id="kiro",
        name="Kiro",
        vendor="AWS",
        category="ide",
        format="http-json",
        description="无损合并写入 ~/.kiro/settings/mcp.json 的 mcpServers 字段",
        app_bundles=("Kiro.app",),
        cli_binaries=("kiro-cli", "kiro"),
    ),
    ClientSpec(
        id="cline",
        name="Cline",
        vendor="Cline",
        category="ide",
        format="http-json",
        description="无损合并写入 Cline 插件的 cline_mcp_settings.json 配置文件",
        cli_binaries=("cline",),
    ),
    ClientSpec(
        id="roo-code",
        name="Roo Code",
        vendor="Roo",
        category="ide",
        format="http-json",
        description="无损合并写入 Roo Code 插件的 mcp_settings.json 配置文件",
    ),
    ClientSpec(
        id="gemini-cli",
        name="Gemini CLI",
        vendor="Google",
        category="cli",
        format="gemini-json",
        description="无损合并写入 ~/.gemini/settings.json 的 mcpServers 字段",
        cli_binaries=("gemini",),
    ),
    ClientSpec(
        id="qwen-code",
        name="Qwen Code",
        vendor="Alibaba",
        category="cli",
        format="gemini-json",
        description="无损合并写入 ~/.qwen/settings.json 的 mcpServers 字段",
        cli_binaries=("qwen",),
    ),
    ClientSpec(
        id="kimi",
        name="Kimi CLI",
        vendor="Moonshot",
        category="cli",
        format="http-json",
        description="无损合并写入 ~/.kimi-code/mcp.json 的 mcpServers 字段",
        cli_binaries=("kimi",),
    ),
    ClientSpec(
        id="opencode",
        name="OpenCode",
        vendor="SST",
        category="cli",
        format="opencode-json",
        description="无损合并写入 ~/.config/opencode/opencode.json 的 mcp 字段",
        cli_binaries=("opencode",),
    ),
    ClientSpec(
        id="zed",
        name="Zed",
        vendor="Zed Industries",
        category="ide",
        format="zed-json",
        description="无损合并写入 Zed settings.json 的 context_servers 字段",
        app_bundles=("Zed.app",),
        cli_binaries=("zed",),
    ),
    ClientSpec(
        id="lmstudio",
        name="LM Studio",
        vendor="LM Studio",
        category="desktop",
        format="http-json",
        description="无损合并写入 ~/.lmstudio/mcp.json 的 mcpServers 字段",
        app_bundles=("LM Studio.app",),
    ),
)

CLIENT_BY_ID: dict[str, ClientSpec] = {c.id: c for c in CLIENT_SPECS}


def default_shim_path() -> Path:
    if os.name == "nt":
        local_app = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return local_app / "Programs" / "WikiContext" / "bin" / "wctx.cmd"
    return Path.home() / ".local" / "bin" / "wctx"


def _is_relocatable_app_bundle_path(p: str) -> bool:
    norm = p.replace("\\", "/")
    return ".app/Contents/" in norm or "/AppData/Local/Temp/_MEI" in norm


def resolve_wctx_command() -> tuple[str, list[str]]:
    """Return `(command, base_args)` to invoke `wctx` reliably across environments."""
    shim_env = (os.environ.get("WCTX_SHIM_PATH") or "").strip()
    shim_default = str(default_shim_path())
    candidates: list[str | None] = [
        shim_env or None,
        shim_default,
        shutil.which("wctx"),
        "/opt/homebrew/bin/wctx",
        "/usr/local/bin/wctx",
        str(Path.home() / ".local" / "bin" / "wctx"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        p = Path(candidate)
        if p.is_file() and (os.name == "nt" or os.access(p, os.X_OK)):
            resolved = str(p.resolve())
            if _is_relocatable_app_bundle_path(resolved) and Path(shim_default).is_file():
                return shim_default, []
            return resolved, []
    return sys.executable, ["-m", "wiki_context"]


def default_kb_user() -> str:
    return (os.environ.get("WCTX_USER") or os.environ.get("KB_USER") or os.environ.get("USER") or getpass.getuser() or "local").strip()


def _is_wctx_entry(
    command: str,
    args: list[str],
    url: str = "",
    headers: dict[str, Any] | None = None,
) -> bool:
    cmd_low = (command or "").lower()
    joined_args = " ".join(str(a) for a in (args or ())).lower()
    if any(k in cmd_low for k in ("wctx", "wiki-context", "wiki_context")):
        return True
    if any(k in joined_args for k in ("kb_mcp.py", "wiki_context", "wctx", "remote_stdio.py")):
        return True
    if isinstance(headers, dict):
        auth = str(headers.get("Authorization") or headers.get("authorization") or "")
        if "wctx_" in auth or headers.get("X-WCTX-Client"):
            return True
    if url and "/mcp" in url and ("8765" in url or "8090" in url or "8421" in url or "wctx" in url):
        return True
    return False


def _extract_vault_from_args(args: list[str]) -> str:
    for i, a in enumerate(args):
        if a == "--vault" and i + 1 < len(args):
            return str(args[i + 1])
        if a.startswith("--vault="):
            return a.split("=", 1)[1]
    return ""


def _extract_url_from_args(args: list[str]) -> str:
    for i, a in enumerate(args):
        if a == "--url" and i + 1 < len(args):
            return str(args[i + 1])
        if a.startswith("--url="):
            return a.split("=", 1)[1]
    return ""


def _atomic_write_with_backup(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        bak = path.with_name(f"{path.name}.wctx.bak")
        try:
            shutil.copy2(path, bak)
        except OSError:
            pass
    atomic_write_text(path, content, private_mode=True)


# -- JSON Clients (Claude, Cursor, VS Code, Windsurf, Trae, OpenCode, Zed...) #


def _json_root_key_for_format(fmt: str) -> str:
    if fmt == "vscode-json":
        return "servers"
    if fmt == "opencode-json":
        return "mcp"
    if fmt == "zed-json":
        return "context_servers"
    return "mcpServers"


def _parse_json_mcp_entry(cfg: dict[str, Any], *, fmt: str = "standard-json") -> tuple[str, list[str], str, dict[str, Any] | None, str]:
    """Extract normalized `(cmd, args, url, headers, mode)` from any supported JSON MCP schema."""
    raw_cmd = cfg.get("command")
    if fmt == "opencode-json" and isinstance(raw_cmd, list):
        cmd = str(raw_cmd[0]) if raw_cmd else ""
        args = [str(x) for x in raw_cmd[1:] if isinstance(x, (str, int, float))]
    elif fmt == "zed-json" and isinstance(raw_cmd, dict):
        cmd = str(raw_cmd.get("path") or raw_cmd.get("command") or "")
        raw_args = raw_cmd.get("args") if isinstance(raw_cmd.get("args"), list) else cfg.get("args")
        args = [str(x) for x in (raw_args or []) if isinstance(x, (str, int, float))]
    else:
        cmd = str(raw_cmd or "")
        args = [str(x) for x in (cfg.get("args") or []) if isinstance(x, (str, int, float))]

    explicit_url = str(cfg.get("url") or cfg.get("httpUrl") or cfg.get("serverUrl") or "")
    url = explicit_url or _extract_url_from_args(args)
    hdrs = cfg.get("headers") if isinstance(cfg.get("headers"), dict) else None
    mode = "http" if explicit_url else ("bridge" if "bridge" in args else "stdio")
    return cmd, args, url, hdrs, mode


def _inspect_json_mcp_file(path: Path, *, fmt: str = "standard-json") -> tuple[dict[str, dict[str, Any]], int]:
    if not path.is_file():
        return {}, 0
    try:
        data = _load_json_lenient(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, 0
    if not isinstance(data, dict):
        return {}, 0
    root_key = _json_root_key_for_format(fmt)
    srvs = data.get(root_key)
    if not isinstance(srvs, dict):
        return {}, 0
    out: dict[str, dict[str, Any]] = {}
    other_count = 0
    for name, cfg in srvs.items():
        if not isinstance(cfg, dict):
            continue
        cmd, args, url, hdrs, mode = _parse_json_mcp_entry(cfg, fmt=fmt)
        if _is_wctx_entry(cmd, args, url, hdrs):
            out[str(name)] = {
                "server_name": str(name),
                "mode": mode,
                "command": cmd,
                "args": args,
                "url": url,
                "vault_path": _extract_vault_from_args(args),
            }
        else:
            other_count += 1
    return out, other_count


def _read_json_mcp_servers(path: Path, *, fmt: str = "standard-json") -> dict[str, dict[str, Any]]:
    return _inspect_json_mcp_file(path, fmt=fmt)[0]


def _build_json_server_entry(
    client_id: str,
    server_spec: dict[str, Any],
    *,
    cmd: str,
    base_args: list[str],
    kb_user: str,
    fmt: str | None = None,
) -> dict[str, Any]:
    eff_fmt = fmt or (CLIENT_BY_ID[client_id].format if client_id in CLIENT_BY_ID else "standard-json")
    mode = str(server_spec.get("mode") or "stdio")
    if mode == "http":
        url = str(server_spec["url"])
        token = str(server_spec.get("token") or "")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if eff_fmt == "workbuddy-json" or client_id == "workbuddy":
            entry: dict[str, Any] = {"type": "streamable-http", "url": url}
            if headers:
                entry["headers"] = headers
            return entry
        if eff_fmt in {"claude-json", "vscode-json"} or client_id in {"claude", "vscode"}:
            entry = {"type": "http", "url": url}
            if headers:
                entry["headers"] = headers
            return entry
        if eff_fmt == "windsurf-json" or client_id == "windsurf":
            entry = {"serverUrl": url}
            if headers:
                entry["headers"] = headers
            return entry
        if eff_fmt == "gemini-json" or client_id in {"gemini-cli", "qwen-code"}:
            entry = {"httpUrl": url}
            if headers:
                entry["headers"] = headers
            return entry
        if eff_fmt == "opencode-json" or client_id == "opencode":
            entry = {"type": "remote", "url": url, "enabled": True}
            if headers:
                entry["headers"] = headers
            return entry
        if client_id == "cline":
            entry = {"type": "streamableHttp", "url": url}
            if headers:
                entry["headers"] = headers
            return entry
        if eff_fmt == "http-json":
            entry = {"url": url}
            if headers:
                entry["headers"] = headers
            return entry
        # Cursor / Antigravity / Claude Desktop / Zed fallback via wctx bridge stdio
        b_args = [*base_args, "bridge", "--url", url]
        env_map: dict[str, str] = {"WCTX_CLIENT": client_id, "PYTHONUTF8": "1"}
        if token:
            env_map["WCTX_TOKEN"] = token
        return {"command": cmd, "args": b_args, "env": env_map}

    if mode == "bridge":
        url = str(server_spec["url"])
        token = str(server_spec.get("token") or "")
        b_args = [*base_args, "bridge", "--url", url]
        env_map = {"WCTX_CLIENT": client_id, "PYTHONUTF8": "1"}
        if token:
            env_map["WCTX_TOKEN"] = token
        if eff_fmt == "opencode-json":
            return {"type": "local", "command": [cmd, *b_args], "environment": env_map, "enabled": True}
        entry = {"command": cmd, "args": b_args, "env": env_map}
        if eff_fmt in {"claude-json", "workbuddy-json", "vscode-json"}:
            entry = {"type": "stdio", **entry}
        return entry

    # Default: local stdio `wctx serve --vault <path>`
    vault_path = str(Path(server_spec["vault_path"]).expanduser().resolve())
    s_args = [*base_args, "serve", "--vault", vault_path]
    env_map = {"WCTX_USER": kb_user, "WCTX_CLIENT": client_id, "PYTHONUTF8": "1"}
    if eff_fmt == "opencode-json":
        return {"type": "local", "command": [cmd, *s_args], "environment": env_map, "enabled": True}
    entry = {"command": cmd, "args": s_args, "env": env_map}
    if eff_fmt in {"claude-json", "workbuddy-json", "vscode-json"}:
        entry = {"type": "stdio", **entry}
    return entry


def _patch_json_mcp_file(
    client_id: str,
    path: Path,
    desired_servers: list[dict[str, Any]],
    *,
    remove_unlisted_wctx: bool = True,
    fmt: str | None = None,
) -> dict[str, Any]:
    eff_fmt = fmt or (CLIENT_BY_ID[client_id].format if client_id in CLIENT_BY_ID else "standard-json")
    root_key = _json_root_key_for_format(eff_fmt)

    root: dict[str, Any] = {}
    if path.is_file():
        raw = path.read_text(encoding="utf-8").strip()
        if raw:
            loaded = _load_json_lenient(raw)
            if not isinstance(loaded, dict):
                raise ValueError(f"Expected JSON object root in {path}")
            root = loaded

    mcp_map = root.get(root_key)
    if not isinstance(mcp_map, dict):
        mcp_map = {}
        root[root_key] = mcp_map

    desired_names = {str(s["server_name"]).strip() for s in desired_servers if str(s.get("server_name") or "").strip()}
    removed: list[str] = []
    if remove_unlisted_wctx:
        for k in list(mcp_map.keys()):
            v = mcp_map.get(k)
            if isinstance(v, dict) and k not in desired_names:
                c_str, a_lst, u_str, hdrs, _ = _parse_json_mcp_entry(v, fmt=eff_fmt)
                if _is_wctx_entry(c_str, a_lst, u_str, hdrs):
                    mcp_map.pop(k, None)
                    removed.append(k)

    cmd, base_args = resolve_wctx_command()
    kb_user = default_kb_user()
    written: list[str] = []
    for s in desired_servers:
        sname = str(s["server_name"]).strip()
        if not sname:
            continue
        mcp_map[sname] = _build_json_server_entry(
            client_id,
            s,
            cmd=cmd,
            base_args=base_args,
            kb_user=kb_user,
            fmt=eff_fmt,
        )
        written.append(sname)

    serialized = json.dumps(root, ensure_ascii=False, indent=2) + "\n"
    _atomic_write_with_backup(path, serialized)
    return {"path": str(path), "written": written, "removed": removed}


# -- Codex CLI (`~/.codex/config.toml`) ------------------------------------- #

_TOML_HEADER_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")


def _parse_toml_string_list(raw: str) -> list[str]:
    return [m.group(1) or m.group(2) for m in re.finditer(r'"([^"\\]*(?:\\.[^"\\]*)*)"|\'([^\']*)\'', raw)]


def _inspect_codex_toml(path: Path) -> tuple[dict[str, dict[str, Any]], int]:
    if not path.is_file():
        return {}, 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}, 0

    sections: dict[str, list[str]] = {}
    cur: str | None = None
    for ln in lines:
        m = _TOML_HEADER_RE.match(ln)
        if m:
            cur = m.group(1).strip()
            sections.setdefault(cur, [])
            continue
        if cur is not None:
            sections[cur].append(ln)

    out: dict[str, dict[str, Any]] = {}
    other_count = 0
    for sec_name, body_lines in sections.items():
        if not sec_name.startswith("mcp_servers."):
            continue
        srv_part = sec_name[len("mcp_servers."):].strip().strip('"').strip("'")
        if "." in srv_part:
            continue
        cmd = ""
        args: list[str] = []
        url = ""
        for bln in body_lines:
            s = bln.strip()
            if s.startswith("command"):
                vals = _parse_toml_string_list(s.split("=", 1)[1] if "=" in s else "")
                cmd = vals[0] if vals else ""
            elif s.startswith("args") and "=" in s:
                args = _parse_toml_string_list(s.split("=", 1)[1])
            elif s.startswith("url") and "=" in s:
                vals = _parse_toml_string_list(s.split("=", 1)[1])
                url = vals[0] if vals else ""
        eff_url = url or _extract_url_from_args(args)
        if _is_wctx_entry(cmd, args, eff_url):
            out[srv_part] = {
                "server_name": srv_part,
                "mode": "http" if url else ("bridge" if "bridge" in args else "stdio"),
                "command": cmd,
                "args": args,
                "url": eff_url,
                "vault_path": _extract_vault_from_args(args),
            }
        else:
            other_count += 1
    return out, other_count


def _read_codex_wctx_servers(path: Path) -> dict[str, dict[str, Any]]:
    return _inspect_codex_toml(path)[0]


def _format_codex_section(
    server_spec: dict[str, Any],
    *,
    cmd: str,
    base_args: list[str],
    kb_user: str,
) -> str:
    sname = str(server_spec["server_name"]).strip()
    hdr = f'[mcp_servers."{sname}"]' if not re.match(r"^[A-Za-z0-9_-]+$", sname) else f"[mcp_servers.{sname}]"
    mode = str(server_spec.get("mode") or "stdio")
    if mode in {"http", "bridge"}:
        url = str(server_spec["url"])
        token = str(server_spec.get("token") or "")
        b_args = [*base_args, "bridge", "--url", url]
        args_json = ", ".join(json.dumps(x, ensure_ascii=False) for x in b_args)
        env_items = [
            f'WCTX_CLIENT = "codex"',
            f'PYTHONUTF8 = "1"',
        ]
        if token:
            env_items.append(f"WCTX_TOKEN = {json.dumps(token, ensure_ascii=False)}")
        return f"{hdr}\ncommand = {json.dumps(cmd, ensure_ascii=False)}\nargs = [{args_json}]\nenv = {{ {', '.join(env_items)} }}\n"

    vault_path = str(Path(server_spec["vault_path"]).expanduser().resolve())
    s_args = [*base_args, "serve", "--vault", vault_path]
    args_json = ", ".join(json.dumps(x, ensure_ascii=False) for x in s_args)
    return (
        f"{hdr}\n"
        f"command = {json.dumps(cmd, ensure_ascii=False)}\n"
        f"args = [{args_json}]\n"
        f"env = {{ WCTX_USER = {json.dumps(kb_user, ensure_ascii=False)}, WCTX_CLIENT = \"codex\", PYTHONUTF8 = \"1\" }}\n"
    )


def _patch_codex_toml(
    path: Path,
    desired_servers: list[dict[str, Any]],
    *,
    remove_unlisted_wctx: bool = True,
) -> dict[str, Any]:
    existing_wctx = _read_codex_wctx_servers(path)
    desired_names = {str(s["server_name"]).strip() for s in desired_servers if str(s.get("server_name") or "").strip()}
    drop_names = set(desired_names)
    removed: list[str] = []
    if remove_unlisted_wctx:
        for old_name in existing_wctx:
            if old_name not in desired_names:
                drop_names.add(old_name)
                removed.append(old_name)

    kept_lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
        skipping = False
        for ln in lines:
            m = _TOML_HEADER_RE.match(ln)
            if m:
                sec = m.group(1).strip()
                if sec.startswith("mcp_servers."):
                    rest = sec[len("mcp_servers."):].strip()
                    head_key = rest.split(".", 1)[0].strip('"').strip("'")
                    skipping = head_key in drop_names
                else:
                    skipping = False
            if not skipping:
                kept_lines.append(ln)

    while kept_lines and not kept_lines[-1].strip():
        kept_lines.pop()

    cmd, base_args = resolve_wctx_command()
    kb_user = default_kb_user()
    new_blocks: list[str] = []
    written: list[str] = []
    for s in desired_servers:
        sname = str(s["server_name"]).strip()
        if not sname:
            continue
        new_blocks.append(_format_codex_section(s, cmd=cmd, base_args=base_args, kb_user=kb_user).rstrip())
        written.append(sname)

    parts: list[str] = []
    if kept_lines:
        parts.append("\n".join(kept_lines))
    if new_blocks:
        parts.append("\n\n".join(new_blocks))
    final_text = ("\n\n".join(parts) + "\n") if parts else ""
    _atomic_write_with_backup(path, final_text)
    return {"path": str(path), "written": written, "removed": removed}


# -- DeepSeek Harness (`~/.dsh/profiles/desktop/cordis.patch.yml`) ---------- #

_DSH_BEGIN_MARKER = "# >>> wiki-context managed mcp >>>"
_DSH_END_MARKER = "# <<< wiki-context managed mcp <<<"


def _unquote_yaml_scalar(val: str) -> str:
    s = val.split(" #", 1)[0].strip()
    if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
        return s[1:-1]
    return s


def _yaml_single_quote(val: str) -> str:
    return "'" + str(val).replace("'", "''") + "'"


def _inspect_dsh_yaml(path: Path) -> tuple[dict[str, dict[str, Any]], int]:
    if not path.is_file():
        return {}, 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}, 0

    out: dict[str, dict[str, Any]] = {}
    other_count = 0
    i = 0
    n = len(lines)
    while i < n:
        m_id = re.match(r"^(\s*)-\s+id:\s*(.+)$", lines[i])
        if not m_id:
            i += 1
            continue
        indent = len(m_id.group(1))
        plugin_id = _unquote_yaml_scalar(m_id.group(2))
        block: list[str] = [lines[i]]
        j = i + 1
        while j < n:
            ln = lines[j]
            if ln.strip() and not ln.lstrip().startswith("#"):
                cur_ind = len(ln) - len(ln.lstrip())
                if cur_ind < indent or (cur_ind == indent and ln.lstrip().startswith("- ")):
                    break
            block.append(ln)
            j += 1

        block_text = "\n".join(block)
        if "dsh-mcp-client" in block_text:
            srv_name = ""
            cmd = ""
            url = ""
            version = 1
            args: list[str] = []
            in_args = False
            args_indent = 0
            for bln in block:
                st = bln.strip()
                if not st or st.startswith("#"):
                    continue
                cind = len(bln) - len(bln.lstrip())
                if in_args:
                    if cind > args_indent and st.startswith("- "):
                        args.append(_unquote_yaml_scalar(st[2:]))
                        continue
                    in_args = False
                if st.startswith("serverName:"):
                    srv_name = _unquote_yaml_scalar(st.split(":", 1)[1])
                elif st.startswith("command:"):
                    cmd = _unquote_yaml_scalar(st.split(":", 1)[1])
                elif st.startswith("url:"):
                    url = _unquote_yaml_scalar(st.split(":", 1)[1])
                elif st.startswith(("WCTX_MCP_VERSION:", "KB_MCP_VERSION:")):
                    raw_v = _unquote_yaml_scalar(st.split(":", 1)[1])
                    if raw_v.isdigit():
                        version = int(raw_v)
                elif st.startswith("args:"):
                    rest = st.split(":", 1)[1].strip()
                    if rest.startswith("[") and rest.endswith("]"):
                        args = _parse_toml_string_list(rest)
                    else:
                        in_args = True
                        args_indent = cind

            eff_url = url or _extract_url_from_args(args)
            if srv_name and _is_wctx_entry(cmd, args, eff_url):
                out[srv_name] = {
                    "plugin_id": plugin_id,
                    "server_name": srv_name,
                    "mode": "http" if url else ("bridge" if "bridge" in args else "stdio"),
                    "command": cmd,
                    "args": args,
                    "url": eff_url,
                    "vault_path": _extract_vault_from_args(args),
                    "version": version,
                }
            else:
                other_count += 1
        i = j
    return out, other_count


def _read_dsh_wctx_servers(path: Path) -> dict[str, dict[str, Any]]:
    return _inspect_dsh_yaml(path)[0]


def _render_dsh_managed_block(
    desired_servers: list[dict[str, Any]],
    *,
    existing: dict[str, dict[str, Any]],
    cmd: str,
    base_args: list[str],
    kb_user: str,
) -> str:
    if not desired_servers:
        return ""
    lines = [_DSH_BEGIN_MARKER, "- insert:"]
    for s in desired_servers:
        sname = str(s["server_name"]).strip()
        if not sname:
            continue
        prev = existing.get(sname) or {}
        plugin_id = str(prev.get("plugin_id") or f"wctx-mcp-{sname}")
        next_ver = int(prev.get("version") or 0) + 1
        mode = str(s.get("mode") or "stdio")
        token = ""
        if mode in {"http", "bridge"}:
            url = str(s["url"])
            token = str(s.get("token") or "")
            s_args = [*base_args, "bridge", "--url", url]
        else:
            vault_path = str(Path(s["vault_path"]).expanduser().resolve())
            s_args = [*base_args, "serve", "--vault", vault_path]

        lines.extend([
            f"    - id: {_yaml_single_quote(plugin_id)}",
            "      name: '@deepseek-ai/dsh-mcp-client'",
            "      config:",
            f"        serverName: {_yaml_single_quote(sname)}",
            "        transport: stdio",
            f"        command: {_yaml_single_quote(cmd)}",
            "        args:",
        ])
        for arg in s_args:
            lines.append(f"          - {_yaml_single_quote(arg)}")
        lines.extend([
            "        env:",
            f"          WCTX_USER: {_yaml_single_quote(kb_user)}",
            "          WCTX_CLIENT: 'dsh'",
            "          PYTHONUTF8: '1'",
        ])
        if token:
            lines.append(f"          WCTX_TOKEN: {_yaml_single_quote(token)}")
        lines.extend([
            f"          WCTX_MCP_VERSION: '{next_ver}' # Auto-bumped by wiki-context console for hot reload",
            "        failOnStartupError: false",
        ])
    lines.append(_DSH_END_MARKER)
    return "\n".join(lines)


def _patch_dsh_yaml(
    path: Path,
    desired_servers: list[dict[str, Any]],
    *,
    remove_unlisted_wctx: bool = True,
) -> dict[str, Any]:
    existing = _read_dsh_wctx_servers(path)
    desired_names = {str(s["server_name"]).strip() for s in desired_servers if str(s.get("server_name") or "").strip()}

    merged_list = list(desired_servers)
    removed: list[str] = []
    if not remove_unlisted_wctx:
        for old_name, old_info in existing.items():
            if old_name not in desired_names and old_info.get("vault_path"):
                merged_list.append({"server_name": old_name, "mode": "stdio", "vault_path": old_info["vault_path"]})
    else:
        removed = [k for k in existing if k not in desired_names]

    raw_text = path.read_text(encoding="utf-8") if path.is_file() else ""
    cmd, base_args = resolve_wctx_command()
    kb_user = default_kb_user()
    managed_block = _render_dsh_managed_block(
        merged_list, existing=existing, cmd=cmd, base_args=base_args, kb_user=kb_user
    )

    if _DSH_BEGIN_MARKER in raw_text and _DSH_END_MARKER in raw_text:
        b_idx = raw_text.index(_DSH_BEGIN_MARKER)
        e_idx = raw_text.index(_DSH_END_MARKER) + len(_DSH_END_MARKER)
        prefix = raw_text[:b_idx].rstrip("\n")
        suffix = raw_text[e_idx:].lstrip("\n")
        parts = [p for p in (prefix, managed_block, suffix) if p]
        new_text = "\n\n".join(parts) + "\n"
    else:
        # Strip any legacy `- insert:` block whose entries are exclusively wctx/kb_mcp
        lines = raw_text.splitlines()
        cleaned_lines: list[str] = []
        insert_pos: int | None = None
        i = 0
        n = len(lines)
        while i < n:
            ln = lines[i]
            if re.match(r"^- insert:\s*$", ln):
                j = i + 1
                while j < n:
                    nxt = lines[j]
                    if nxt.strip() and not nxt.lstrip().startswith("#") and (len(nxt) - len(nxt.lstrip())) == 0:
                        break
                    j += 1
                chunk = "\n".join(lines[i:j])
                has_mcp = "dsh-mcp-client" in chunk
                # Check every `- id:` inside this `- insert:` chunk
                sub_ids = re.findall(r"^\s+-\s+id:\s*(.+)$", chunk, flags=re.M)
                wctx_like = ("wctx" in chunk or "kb_mcp.py" in chunk or "wiki_context" in chunk)
                other_mcp = any(
                    not any(tag in _unquote_yaml_scalar(sid) for tag in ("kb", "wctx", "wiki"))
                    for sid in sub_ids
                )
                if has_mcp and wctx_like and not other_mcp:
                    # Also drop immediate preceding comment banner if it describes LLM Wiki
                    if cleaned_lines and cleaned_lines[-1].lstrip().startswith("#") and (
                        "Wiki" in cleaned_lines[-1] or "知识库" in cleaned_lines[-1]
                    ):
                        cleaned_lines.pop()
                    if insert_pos is None:
                        insert_pos = len(cleaned_lines)
                    i = j
                    continue
            cleaned_lines.append(ln)
            i += 1

        if insert_pos is None:
            # Prefer inserting right before `- id: preset-ptc` if present, else append
            for idx, ln in enumerate(cleaned_lines):
                if ln.strip().startswith("# >>> dsh preset-ptc") or re.match(r"^- id:\s*preset-ptc\b", ln.strip()):
                    insert_pos = idx
                    break
            if insert_pos is None:
                insert_pos = len(cleaned_lines)

        before = "\n".join(cleaned_lines[:insert_pos]).rstrip("\n")
        after = "\n".join(cleaned_lines[insert_pos:]).lstrip("\n")
        parts = [p for p in (before, managed_block, after) if p]
        new_text = ("\n\n".join(parts) + "\n") if parts else ""

    _atomic_write_with_backup(path, new_text)
    written = [str(s["server_name"]).strip() for s in merged_list if str(s.get("server_name") or "").strip()]
    return {"path": str(path), "written": written, "removed": removed}


# -- Public Inspection, Sync & Snippet Generation API ----------------------- #


def inspect_all_clients(*, home: Path | None = None) -> list[dict[str, Any]]:
    """Inspect all supported AI clients and return their installation & configured MCP status."""
    results: list[dict[str, Any]] = []
    for spec in CLIENT_SPECS:
        cpath = spec.config_path(home)
        installed, detected_via = spec.detect_installation(home)
        if spec.format == "dsh-yaml":
            servers, other_count = _inspect_dsh_yaml(cpath)
        elif spec.format == "codex-toml":
            servers, other_count = _inspect_codex_toml(cpath)
        else:
            servers, other_count = _inspect_json_mcp_file(cpath, fmt=spec.format)

        command_issues: list[str] = []
        for sname, sinfo in servers.items():
            scmd = str(sinfo.get("command") or "").strip()
            if sinfo.get("mode") == "http":
                continue
            if not scmd:
                command_issues.append(f"{sname}: missing command")
            elif _is_relocatable_app_bundle_path(scmd):
                command_issues.append(f"{sname}: points inside .app bundle ({scmd})")
            elif not (Path(scmd).is_file() or shutil.which(scmd)):
                command_issues.append(f"{sname}: command not found ({scmd})")

        results.append({
            "id": spec.id,
            "name": spec.name,
            "vendor": spec.vendor,
            "category": spec.category,
            "format": spec.format,
            "description": spec.description,
            "installed": installed,
            "detected_via": detected_via,
            "config_path": str(cpath),
            "config_exists": cpath.is_file(),
            "configured_servers": servers,
            "wctx_server_count": len(servers),
            "other_mcp_count": other_count,
            "command_ok": len(command_issues) == 0,
            "command_issues": command_issues,
        })
    return results


def discover_vaults_from_clients(*, home: Path | None = None) -> list[dict[str, str]]:
    """Discover existing local vaults already mounted in any local AI client config."""
    found: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for client in inspect_all_clients(home=home):
        for sname, info in client["configured_servers"].items():
            vp = str(info.get("vault_path") or "").strip()
            if not vp:
                continue
            resolved = str(Path(vp).expanduser().resolve())
            if resolved in seen_paths or not Path(resolved).is_dir():
                continue
            seen_paths.add(resolved)
            found.append({"id": sname, "path": resolved, "discovered_from": client["id"]})
    return found


def _sync_magpie_library_if_present(
    home: Path | None,
    desired_servers: list[dict[str, Any]],
    *,
    remove_unlisted_wctx: bool = True,
) -> bool:
    """Keep `~/.config/magpie/library.json` in sync if Magpie MCP manager is present."""
    h = (home or Path.home()).expanduser().resolve()
    magpie_path = h / ".config" / "magpie" / "library.json"
    if not magpie_path.is_file():
        return False
    try:
        old_raw = magpie_path.read_text(encoding="utf-8")
        lib = json.loads(old_raw)
    except (OSError, ValueError):
        return False
    if not isinstance(lib, dict) or not isinstance(lib.get("mcp"), list):
        return False

    mcp_list = lib["mcp"]
    desired_names = [
        str(s["server_name"]).strip()
        for s in desired_servers
        if str(s.get("server_name") or "").strip()
    ]
    desired_set = set(desired_names)

    old_wctx_names: set[str] = set()
    inherited_agents: list[str] = []
    for item in mcp_list:
        if not isinstance(item, dict):
            continue
        c_str = str(item.get("command") or "")
        a_lst = [str(x) for x in (item.get("args") or []) if isinstance(x, (str, int, float))]
        u_str = str(item.get("url") or "") or _extract_url_from_args(a_lst)
        hdrs = item.get("headers") if isinstance(item.get("headers"), dict) else None
        if _is_wctx_entry(c_str, a_lst, u_str, hdrs):
            nm = str(item.get("name") or "").strip()
            if nm:
                old_wctx_names.add(nm)
            for ag in item.get("agents") or []:
                ag_s = str(ag).strip()
                if ag_s and ag_s not in inherited_agents:
                    inherited_agents.append(ag_s)

    if not inherited_agents:
        inherited_agents = ["agy", "claude", "codex", "cursor"]

    cmd, base_args = resolve_wctx_command()
    kb_user = default_kb_user()

    new_wctx_entries: list[dict[str, Any]] = []
    for s in desired_servers:
        sname = str(s.get("server_name") or "").strip()
        if not sname:
            continue
        base_entry = _build_json_server_entry("cursor", s, cmd=cmd, base_args=base_args, kb_user=kb_user)
        new_wctx_entries.append({
            "name": sname,
            "transport": "stdio",
            **base_entry,
            "agents": list(inherited_agents),
            "local": True,
        })

    rebuilt_mcp: list[Any] = []
    inserted = False
    for item in mcp_list:
        if not isinstance(item, dict):
            rebuilt_mcp.append(item)
            continue
        nm = str(item.get("name") or "").strip()
        is_old_wctx = nm in old_wctx_names or nm in desired_set
        if is_old_wctx:
            if not inserted:
                rebuilt_mcp.extend(new_wctx_entries)
                inserted = True
            if not remove_unlisted_wctx and nm not in desired_set:
                rebuilt_mcp.append(item)
            continue
        rebuilt_mcp.append(item)
    if not inserted:
        rebuilt_mcp.extend(new_wctx_entries)
    lib["mcp"] = rebuilt_mcp

    applied = lib.get("applied")
    if isinstance(applied, dict) and (old_wctx_names or desired_set):
        target_old = old_wctx_names | desired_set
        for ag_cfg in applied.values():
            if not isinstance(ag_cfg, dict) or not isinstance(ag_cfg.get("mcp"), list):
                continue
            cur_list = [str(x) for x in ag_cfg["mcp"]]
            if not any(x in target_old for x in cur_list):
                continue
            updated_list: list[str] = []
            put_desired = False
            for x in cur_list:
                if x in target_old:
                    if not put_desired:
                        for dn in desired_names:
                            if dn not in updated_list:
                                updated_list.append(dn)
                        put_desired = True
                    if not remove_unlisted_wctx and x not in desired_set and x not in updated_list:
                        updated_list.append(x)
                elif x not in updated_list:
                    updated_list.append(x)
            ag_cfg["mcp"] = updated_list

    new_raw = json.dumps(lib, ensure_ascii=False, indent=2) + "\n"
    if new_raw != old_raw:
        _atomic_write_with_backup(magpie_path, new_raw)
    return True


def sync_client_mcp_config(
    client_id: str,
    desired_servers: list[dict[str, Any]],
    *,
    remove_unlisted_wctx: bool = True,
    home: Path | None = None,
    sync_magpie: bool = True,
) -> dict[str, Any]:
    """Atomically write `desired_servers` into the specified client's MCP configuration file."""
    spec = CLIENT_BY_ID.get(client_id)
    if spec is None:
        raise ValueError(f"Unsupported client '{client_id}'; choose from {list(CLIENT_BY_ID)}")
    cpath = spec.config_path(home)
    if spec.format == "dsh-yaml":
        res = _patch_dsh_yaml(cpath, desired_servers, remove_unlisted_wctx=remove_unlisted_wctx)
        h = (home or Path.home()).expanduser().resolve()
        web_cpath = h / ".dsh" / "profiles" / "web" / "cordis.patch.yml"
        if web_cpath != cpath and web_cpath.is_file():
            _patch_dsh_yaml(web_cpath, desired_servers, remove_unlisted_wctx=remove_unlisted_wctx)
            res["mirrored_web_path"] = str(web_cpath)
    elif spec.format == "codex-toml":
        res = _patch_codex_toml(cpath, desired_servers, remove_unlisted_wctx=remove_unlisted_wctx)
    else:
        res = _patch_json_mcp_file(
            spec.id,
            cpath,
            desired_servers,
            remove_unlisted_wctx=remove_unlisted_wctx,
            fmt=spec.format,
        )
    if sync_magpie and desired_servers and _sync_magpie_library_if_present(
        home, desired_servers, remove_unlisted_wctx=remove_unlisted_wctx
    ):
        res["magpie_library_synced"] = True
    return {"client_id": spec.id, "client_name": spec.name, **res}


def disconnect_client_mcp_config(
    client_id: str,
    *,
    home: Path | None = None,
) -> dict[str, Any]:
    """Remove all `wiki-context` entries from one client without disturbing other MCPs or Magpie."""
    spec = CLIENT_BY_ID.get(client_id)
    if spec is None:
        raise ValueError(f"Unsupported client '{client_id}'; choose from {list(CLIENT_BY_ID)}")
    cpath = spec.config_path(home)
    if not cpath.is_file():
        return {
            "client_id": spec.id,
            "client_name": spec.name,
            "path": str(cpath),
            "written": [],
            "removed": [],
        }
    return sync_client_mcp_config(
        client_id,
        [],
        remove_unlisted_wctx=True,
        home=home,
        sync_magpie=False,
    )


def render_client_snippet(
    client_id: str,
    desired_servers: list[dict[str, Any]],
) -> str:
    """Render a copy-pasteable configuration snippet for previewing in the Control Console UI."""
    spec = CLIENT_BY_ID.get(client_id)
    if spec is None:
        raise ValueError(f"Unsupported client '{client_id}'")
    cmd, base_args = resolve_wctx_command()
    kb_user = default_kb_user()

    if spec.format == "dsh-yaml":
        return _render_dsh_managed_block(
            desired_servers, existing={}, cmd=cmd, base_args=base_args, kb_user=kb_user
        )
    if spec.format == "codex-toml":
        return "\n\n".join(
            _format_codex_section(s, cmd=cmd, base_args=base_args, kb_user=kb_user).rstrip()
            for s in desired_servers
        )
    root_key = _json_root_key_for_format(spec.format)
    obj = {
        root_key: {
            str(s["server_name"]): _build_json_server_entry(
                spec.id,
                s,
                cmd=cmd,
                base_args=base_args,
                kb_user=kb_user,
                fmt=spec.format,
            )
            for s in desired_servers
            if str(s.get("server_name") or "").strip()
        }
    }
    return json.dumps(obj, ensure_ascii=False, indent=2)
