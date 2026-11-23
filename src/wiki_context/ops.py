"""Environment diagnostics (`wctx doctor`) and background daemon service helper (`wctx service`).

Zero-dependency operational utilities for first-time setup and production deployment:
- `run_doctor`: checks Python runtime, `wctx` PATH resolution, `poppler` (`pdftotext`/`pdftoppm`),
  multimodal OCR/ASR env hooks, AI client configs, console vault registry, and service status.
- `render_service_definition` / `install_service` / `uninstall_service` / `service_status`:
  generates and manages macOS `launchd` agents or Linux `systemd --user` units for
  `wctx ui` (loopback admin console) and `wctx gateway` (production Bearer-authenticated gateway).
"""

from __future__ import annotations

import ipaddress
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from . import __version__
from .clients import inspect_all_clients, resolve_wctx_command


def _is_loopback_host(host: str) -> bool:
    h = host.strip().lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _probe_tcp(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _poppler_install_hint() -> str:
    if sys.platform == "darwin":
        return "brew install poppler"
    if sys.platform.startswith("linux"):
        return "sudo apt-get update && sudo apt-get install -y poppler-utils"
    return "Install poppler (provides pdftotext & pdftoppm) and add it to PATH"


def _resolve_config_dir(config_dir: Path | str | None = None) -> Path:
    base = config_dir or os.environ.get("WCTX_HOME") or (Path.home() / ".wiki-context")
    return Path(base).expanduser().resolve()


def _service_metadata(target: str, *, platform: str | None = None, home: Path | None = None) -> dict[str, Any]:
    if target not in {"ui", "gateway"}:
        raise ValueError(f"Unsupported service target '{target}' (expected 'ui' or 'gateway')")
    plat = platform or sys.platform
    user_home = (home or Path.home()).expanduser().resolve()
    if plat == "darwin":
        label = f"io.wikicontext.{target}"
        return {
            "platform": "launchd",
            "label": label,
            "service_path": user_home / "Library" / "LaunchAgents" / f"{label}.plist",
            "log_path": user_home / ".wiki-context" / f"{target}.log",
        }
    if plat.startswith("linux"):
        label = f"wctx-{target}.service"
        return {
            "platform": "systemd",
            "label": label,
            "service_path": user_home / ".config" / "systemd" / "user" / label,
            "log_path": user_home / ".wiki-context" / f"{target}.log",
        }
    return {
        "platform": "unsupported",
        "label": f"wctx-{target}",
        "service_path": user_home / ".wiki-context" / f"{target}.service",
        "log_path": user_home / ".wiki-context" / f"{target}.log",
    }


def service_status(
    target: str,
    *,
    platform: str | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """Inspect user-level daemon installation & runtime state for `ui` or `gateway`."""
    meta = _service_metadata(target, platform=platform, home=home)
    spath: Path = meta["service_path"]
    installed = spath.is_file()
    running = False
    detail = "not installed"

    if installed:
        detail = "installed"
        if meta["platform"] == "launchd":
            res = subprocess.run(
                ["launchctl", "list", meta["label"]],
                capture_output=True,
                text=True,
                check=False,
            )
            running = res.returncode == 0
            detail = "loaded (launchd)" if running else "installed (not loaded)"
        elif meta["platform"] == "systemd":
            res = subprocess.run(
                ["systemctl", "--user", "is-active", meta["label"]],
                capture_output=True,
                text=True,
                check=False,
            )
            running = res.returncode == 0 and res.stdout.strip() == "active"
            detail = res.stdout.strip() or ("active" if running else "inactive")

    return {
        "target": target,
        "platform": meta["platform"],
        "label": meta["label"],
        "service_path": str(spath),
        "log_path": str(meta["log_path"]),
        "installed": installed,
        "running": running,
        "detail": detail,
    }


def render_service_definition(
    target: str,
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    config_dir: Path | str | None = None,
    vault_specs: list[str] | None = None,
    token_file: Path | str | None = None,
    allow_remote_admin: bool = False,
    platform: str | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """Build a macOS `launchd` plist or Linux `systemd --user` service unit for `ui` or `gateway`."""
    meta = _service_metadata(target, platform=platform, home=home)
    if meta["platform"] == "unsupported":
        raise RuntimeError(f"Background service generation is only supported on macOS (launchd) and Linux (systemd), got '{sys.platform}'")

    chosen_port = int(port if port is not None else (8765 if target == "ui" else 8090))
    if target == "ui" and not allow_remote_admin and not _is_loopback_host(host):
        raise ValueError(
            f"Refusing to configure background Control Console (`ui`) on non-loopback host '{host}' "
            "without --allow-remote-admin. Use `--target gateway` for team network serving."
        )

    cmd, base_args = resolve_wctx_command()
    argv: list[str] = [cmd, *base_args, target, "--host", host, "--port", str(chosen_port)]
    cfg_dir = _resolve_config_dir(config_dir)

    if target == "ui":
        argv.append("--no-browser")
        argv.extend(["--config-dir", str(cfg_dir)])
        for v in vault_specs or []:
            argv.extend(["--vault", v])
        if allow_remote_admin:
            argv.append("--allow-remote-admin")
    else:
        for v in vault_specs or []:
            argv.extend(["--vault", v])
        if not vault_specs:
            argv.extend(["--config-dir", str(cfg_dir)])
        tok_path = Path(token_file).expanduser().resolve() if token_file else (cfg_dir / "tokens.json")
        argv.extend(["--token-file", str(tok_path)])

    log_path = str(meta["log_path"])
    path_env = os.environ.get("PATH") or "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

    if meta["platform"] == "launchd":
        prog_args = "\n".join(f"    <string>{xml_escape(a)}</string>" for a in argv)
        content = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n'
            "<dict>\n"
            "  <key>Label</key>\n"
            f"  <string>{xml_escape(meta['label'])}</string>\n"
            "  <key>ProgramArguments</key>\n"
            "  <array>\n"
            f"{prog_args}\n"
            "  </array>\n"
            "  <key>EnvironmentVariables</key>\n"
            "  <dict>\n"
            "    <key>PATH</key>\n"
            f"    <string>{xml_escape(path_env)}</string>\n"
            "  </dict>\n"
            "  <key>RunAtLoad</key>\n"
            "  <true/>\n"
            "  <key>KeepAlive</key>\n"
            "  <true/>\n"
            "  <key>StandardOutPath</key>\n"
            f"  <string>{xml_escape(log_path)}</string>\n"
            "  <key>StandardErrorPath</key>\n"
            f"  <string>{xml_escape(log_path)}</string>\n"
            "</dict>\n"
            "</plist>\n"
        )
    else:
        exec_line = " ".join(shlex.quote(a) for a in argv)
        content = (
            "[Unit]\n"
            f"Description=wiki-context ({target})\n"
            "After=network.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={exec_line}\n"
            "Restart=on-failure\n"
            "RestartSec=3\n"
            f"Environment=PATH={path_env}\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    return {
        "target": target,
        "platform": meta["platform"],
        "label": meta["label"],
        "service_path": str(meta["service_path"]),
        "log_path": log_path,
        "command": " ".join(shlex.quote(a) for a in argv),
        "content": content,
    }


def install_service(
    target: str,
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    config_dir: Path | str | None = None,
    vault_specs: list[str] | None = None,
    token_file: Path | str | None = None,
    allow_remote_admin: bool = False,
    dry_run: bool = False,
    activate: bool = True,
    platform: str | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """Write user-level daemon service file and optionally enable/start it."""
    spec = render_service_definition(
        target,
        host=host,
        port=port,
        config_dir=config_dir,
        vault_specs=vault_specs,
        token_file=token_file,
        allow_remote_admin=allow_remote_admin,
        platform=platform,
        home=home,
    )
    if dry_run:
        return {**spec, "dry_run": True, "installed": False, "activated": False}

    spath = Path(spec["service_path"])
    spath.parent.mkdir(parents=True, exist_ok=True)
    Path(spec["log_path"]).parent.mkdir(parents=True, exist_ok=True)
    spath.write_text(spec["content"], encoding="utf-8")

    activated = False
    activation_msg = "written (not activated)"
    if activate:
        if spec["platform"] == "launchd":
            subprocess.run(["launchctl", "unload", str(spath)], capture_output=True, text=True, check=False)
            res = subprocess.run(["launchctl", "load", "-w", str(spath)], capture_output=True, text=True, check=False)
            activated = res.returncode == 0
            activation_msg = "launchctl load -w ok" if activated else (res.stderr.strip() or res.stdout.strip() or "launchctl failed")
        elif spec["platform"] == "systemd":
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True, check=False)
            res = subprocess.run(
                ["systemctl", "--user", "enable", "--now", spec["label"]],
                capture_output=True,
                text=True,
                check=False,
            )
            activated = res.returncode == 0
            activation_msg = "systemctl enable --now ok" if activated else (res.stderr.strip() or res.stdout.strip() or "systemctl failed")

    return {
        **spec,
        "dry_run": False,
        "installed": True,
        "activated": activated,
        "activation_detail": activation_msg,
    }


def uninstall_service(
    target: str,
    *,
    dry_run: bool = False,
    activate: bool = True,
    platform: str | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """Unload and remove a user-level daemon service file."""
    meta = _service_metadata(target, platform=platform, home=home)
    spath: Path = meta["service_path"]
    existed = spath.is_file()
    if dry_run:
        return {
            "target": target,
            "platform": meta["platform"],
            "service_path": str(spath),
            "existed": existed,
            "removed": False,
            "dry_run": True,
        }

    if activate and existed:
        if meta["platform"] == "launchd":
            subprocess.run(["launchctl", "unload", "-w", str(spath)], capture_output=True, text=True, check=False)
        elif meta["platform"] == "systemd":
            subprocess.run(["systemctl", "--user", "disable", "--now", meta["label"]], capture_output=True, text=True, check=False)
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True, check=False)

    if existed:
        spath.unlink(missing_ok=True)
    return {
        "target": target,
        "platform": meta["platform"],
        "service_path": str(spath),
        "existed": existed,
        "removed": existed,
        "dry_run": False,
    }


def run_doctor(
    *,
    config_dir: Path | str | None = None,
    client_home: Path | None = None,
    ui_port: int = 8765,
    gateway_port: int = 8090,
) -> dict[str, Any]:
    """Run a comprehensive first-time installation & runtime health diagnostic."""
    cfg_dir = _resolve_config_dir(config_dir)
    console_file = cfg_dir / "console.json"
    tokens_file = cfg_dir / "tokens.json"

    vault_count = 0
    if console_file.is_file():
        try:
            cdata = json.loads(console_file.read_text(encoding="utf-8"))
            vault_count = len(
                [
                    v
                    for v in (cdata.get("vaults") or [])
                    if isinstance(v, dict) and (v.get("path") or v.get("gateway_url"))
                ]
            )
        except (OSError, ValueError):
            pass

    active_token_count = 0
    if tokens_file.is_file():
        try:
            tdata = json.loads(tokens_file.read_text(encoding="utf-8"))
            toks = tdata.get("tokens") if isinstance(tdata, dict) else []
            active_token_count = len([t for t in (toks or []) if isinstance(t, dict) and not t.get("disabled")])
        except (OSError, ValueError):
            pass

    cmd, base_args = resolve_wctx_command()
    wctx_in_path = shutil.which("wctx")
    pdftotext_path = shutil.which("pdftotext")
    pdftoppm_path = shutil.which("pdftoppm")
    ocr_cmd = (os.environ.get("WCTX_OCR_CMD") or "").strip()
    asr_cmd = (os.environ.get("WCTX_ASR_CMD") or "").strip()
    vlm_key_set = bool((os.environ.get("WCTX_VLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip())

    clients = inspect_all_clients(home=client_home)
    installed_clients = [c for c in clients if c.get("installed")]
    configured_clients = [c for c in installed_clients if c.get("configured_servers")]

    min_py = (3, 14)
    min_py_str = f"{min_py[0]}.{min_py[1]}"
    py_ok = sys.version_info >= min_py
    poppler_hint = _poppler_install_hint()
    checks: list[dict[str, Any]] = [
        {
            "id": "python",
            "label": f"Python 运行环境 (>= {min_py_str})",
            "ok": py_ok,
            "level": "ok" if py_ok else "error",
            "detail": f"Python {sys.version.split()[0]} ({sys.executable})",
            "fix": "" if py_ok else f"请升级至 Python {min_py_str}+",
        },
        {
            "id": "wctx_path",
            "label": "wctx 全局命令行入口",
            "ok": bool(wctx_in_path),
            "level": "ok" if wctx_in_path else "warn",
            "detail": wctx_in_path or f"未在 PATH 中找到 wctx（当前回退为 {' '.join([cmd, *base_args])}）",
            "fix": "" if wctx_in_path else "pipx install git+https://github.com/Minokun/wiki-context.git",
        },
        {
            "id": "pdftotext",
            "label": "PDF 文本提取器 (pdftotext)",
            "ok": bool(pdftotext_path),
            "level": "ok" if pdftotext_path else "warn",
            "detail": pdftotext_path or "未检测到 pdftotext（PDF 资料将无法提取正文文本）",
            "fix": "" if pdftotext_path else poppler_hint,
        },
        {
            "id": "pdftoppm",
            "label": "PDF 多模态视觉渲染 (pdftoppm)",
            "ok": bool(pdftoppm_path),
            "level": "ok" if pdftoppm_path else "info",
            "detail": pdftoppm_path or "未检测到 pdftoppm（wiki_inspect_visual 将无法渲染 PDF 整页图片）",
            "fix": "" if pdftoppm_path else poppler_hint,
        },
        {
            "id": "vaults",
            "label": "已纳管知识库数量",
            "ok": vault_count > 0,
            "level": "ok" if vault_count > 0 else "info",
            "detail": f"已纳管 {vault_count} 个知识库（配置目录: {cfg_dir}）",
            "fix": "" if vault_count > 0 else "wctx ui  # 在 Web 控制中心选择文件夹完成初始化",
        },
        {
            "id": "clients",
            "label": "本机 AI 客户端 MCP 挂载",
            "ok": bool(configured_clients) or not installed_clients,
            "level": "ok" if configured_clients else ("warn" if installed_clients else "info"),
            "detail": (
                f"检测到 {len(installed_clients)} 个已安装客户端，其中 {len(configured_clients)} 个已配置 wiki-context"
                if installed_clients
                else "未检测到 DSH / Codex / Claude Code / WorkBuddy / Cursor / Antigravity"
            ),
            "fix": "" if configured_clients or not installed_clients else "在 wctx ui ->「MCP 客户端配置」点击一键同步",
        },
    ]

    return {
        "version": __version__,
        "platform": sys.platform,
        "python": {"version": sys.version.split()[0], "executable": sys.executable, "ok": py_ok},
        "wctx_cli": {"command": cmd, "base_args": base_args, "in_path": bool(wctx_in_path), "which": wctx_in_path or ""},
        "tools": {
            "pdftotext": {"available": bool(pdftotext_path), "path": pdftotext_path or "", "install_hint": poppler_hint},
            "pdftoppm": {"available": bool(pdftoppm_path), "path": pdftoppm_path or "", "install_hint": poppler_hint},
            "ocr_cmd_configured": bool(ocr_cmd),
            "asr_cmd_configured": bool(asr_cmd),
            "vlm_key_configured": vlm_key_set,
        },
        "console": {
            "config_dir": str(cfg_dir),
            "console_file_exists": console_file.is_file(),
            "tokens_file_exists": tokens_file.is_file(),
            "vault_count": vault_count,
            "active_token_count": active_token_count,
        },
        "ports": {
            "ui_port": ui_port,
            "ui_listening": _probe_tcp("127.0.0.1", ui_port),
            "gateway_port": gateway_port,
            "gateway_listening": _probe_tcp("127.0.0.1", gateway_port),
        },
        "services": {
            "ui": service_status("ui", home=client_home),
            "gateway": service_status("gateway", home=client_home),
        },
        "clients_summary": [
            {
                "id": c["id"],
                "name": c["name"],
                "installed": c["installed"],
                "configured_count": len(c.get("configured_servers") or {}),
            }
            for c in clients
        ],
        "checks": checks,
        "security_boundary": {
            "ui": "wctx ui 默认仅绑定 127.0.0.1（无密码本机管理面板 + 开发网关，CSRF 保护）",
            "gateway": "wctx gateway 面向团队多人共享部署（强制 SHA-256 Bearer Token 鉴权，无管理接口）",
        },
    }
