"""Cross-platform fail-closed file locking, 0600 atomic writes, daemon singleton & tool discovery.

Provides:
- `file_lock`: POSIX `fcntl.flock` + Windows `msvcrt.locking` fail-closed advisory lock;
- `replace_with_retry`: backoff retries around `os.replace` for Windows AV/Explorer transient locks;
- `atomic_write_bytes` / `atomic_write_text` / `atomic_write_json`: O_CREAT|O_EXCL temp file with
  exact `0o600` permission at creation time (zero `0644 -> 0600` TOCTOU window);
- `ensure_schema_version`: explicit JSON state migration with `.bak` backup;
- `DaemonSingleton`: `daemon.lock` + `daemon.json` single-instance arbitration preventing CLI vs
  Desktop split-brain on `:8090` and `~/.wiki-context/*.json`;
- `resolve_external_tool`: cross-platform locator for `pdftotext`, `pdftoppm`, and `tailscale` even
  when launched from Finder / Explorer without interactive shell `PATH`.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import secrets
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterator

if os.name == "nt":
    import msvcrt  # type: ignore[import-not-found]
    fcntl = None  # type: ignore[assignment]
else:
    import fcntl
    msvcrt = None  # type: ignore[assignment]


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def replace_with_retry(src: Path, dst: Path, *, attempts: int = 5, delay: float = 0.04) -> None:
    """Atomically move `src` over `dst`, retrying transient Windows sharing/AV locks."""
    last_exc: OSError | None = None
    for idx in range(max(1, attempts)):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            last_exc = exc
            if idx + 1 < attempts:
                time.sleep(delay * (idx + 1))
    if last_exc is not None:
        raise last_exc


@contextlib.contextmanager
def file_lock(
    lock_path: Path | str,
    *,
    blocking: bool = True,
    timeout: float = 15.0,
    busy_message: str = "Another process holds the lock",
) -> Iterator[None]:
    """Cross-platform fail-closed file lock (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows)."""
    lp = Path(lock_path)
    lp.parent.mkdir(parents=True, exist_ok=True)

    if fcntl is None and msvcrt is None:
        raise RuntimeError("No supported OS file-locking primitive available (fail-closed)")

    fh = lp.open("a+b")
    locked = False
    try:
        if fcntl is not None:
            deadline = time.monotonic() + max(0.0, timeout)
            flags = fcntl.LOCK_EX | fcntl.LOCK_NB
            while True:
                try:
                    fcntl.flock(fh.fileno(), flags)
                    locked = True
                    break
                except BlockingIOError as exc:
                    if not blocking or time.monotonic() >= deadline:
                        raise RuntimeError(busy_message) from exc
                    time.sleep(0.025)
        else:
            assert msvcrt is not None
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(b"0")
                fh.flush()
            deadline = time.monotonic() + max(0.0, timeout)
            while True:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError as exc:
                    if not blocking or time.monotonic() >= deadline:
                        raise RuntimeError(busy_message) from exc
                    time.sleep(0.025)
        yield
    finally:
        if locked:
            try:
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        try:
            fh.close()
        except OSError:
            pass


def atomic_write_bytes(path: Path, data: bytes, *, private_mode: bool = False) -> None:
    """Create temp file atomically with exact mode (0600 if private) and replace target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o600 if private_mode else 0o644
    tmp_path: Path | None = None
    for _ in range(8):
        cand = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(6)}")
        try:
            fd = os.open(str(cand), os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
            tmp_path = cand
            break
        except FileExistsError:
            continue
    if tmp_path is None:
        raise RuntimeError(f"Failed to allocate unique temp file for {path}")

    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
        if private_mode and os.name != "nt":
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass
        replace_with_retry(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str, *, private_mode: bool = False) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), private_mode=private_mode)


def atomic_write_json(path: Path, obj: Any, *, private_mode: bool = False) -> None:
    raw = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(path, raw, private_mode=private_mode)


def ensure_schema_version(
    doc: dict[str, Any],
    *,
    target_version: int,
    migrator: Callable[[dict[str, Any], int], dict[str, Any]] | None = None,
    source_path: Path | None = None,
) -> tuple[dict[str, Any], bool]:
    """Ensure `doc['version']` matches `target_version`, optionally backing up and migrating."""
    cur_raw = doc.get("version", 1)
    try:
        cur_ver = int(cur_raw)
    except (TypeError, ValueError):
        cur_ver = 1
    if cur_ver == target_version and doc.get("version") == target_version:
        return doc, False

    if source_path is not None and source_path.is_file():
        bak = source_path.with_name(f"{source_path.name}.v{cur_ver}.bak")
        if not bak.exists():
            try:
                shutil.copy2(source_path, bak)
            except OSError:
                pass

    updated = dict(doc)
    if migrator is not None and cur_ver != target_version:
        updated = migrator(updated, cur_ver)
    updated["version"] = target_version
    return updated, True


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            SYNCHRONIZE = 0x00100000
            handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def probe_live_daemon(config_dir: Path, *, timeout: float = 0.5) -> dict[str, Any] | None:
    """Return active daemon metadata if `daemon.json` points to a live, responsive console."""
    dpath = Path(config_dir) / "daemon.json"
    if not dpath.is_file():
        return None
    try:
        info = json.loads(dpath.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict):
        return None
    pid = int(info.get("pid") or 0)
    port = int(info.get("port") or 0)
    if port <= 0 or (pid > 0 and not is_pid_alive(pid)):
        return None
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            pass
    except OSError:
        return None

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=timeout) as resp:
            if resp.status == 200:
                hz = json.loads(resp.read().decode("utf-8"))
                return {**info, "healthz": hz}
    except (OSError, ValueError, urllib.error.URLError):
        pass
    return info


def write_daemon_info(
    config_dir: Path,
    *,
    port: int,
    mode: str = "console",
    gateway_port: int | None = None,
    instance_id: str = "",
) -> Path:
    dpath = Path(config_dir) / "daemon.json"
    payload = {
        "version": 1,
        "pid": os.getpid(),
        "port": int(port),
        "gateway_port": int(gateway_port) if gateway_port else None,
        "mode": mode,
        "instance_id": instance_id,
        "started_at": _now_iso(),
        "url": f"http://127.0.0.1:{int(port)}",
    }
    atomic_write_json(dpath, payload, private_mode=True)
    return dpath


def clear_daemon_info(config_dir: Path) -> None:
    dpath = Path(config_dir) / "daemon.json"
    if not dpath.is_file():
        return
    try:
        info = json.loads(dpath.read_text(encoding="utf-8"))
        if isinstance(info, dict) and int(info.get("pid") or 0) in {0, os.getpid()}:
            dpath.unlink(missing_ok=True)
    except (OSError, ValueError):
        try:
            dpath.unlink(missing_ok=True)
        except OSError:
            pass


def resolve_external_tool(name: str) -> str | None:
    """Locate CLI binaries (`pdftotext`, `pdftoppm`, `tailscale`) across GUI & shell environments."""
    clean = (name or "").strip()
    if not clean:
        return None

    direct = shutil.which(clean)
    if direct:
        return direct

    candidates: list[Path] = []
    if sys.platform == "darwin":
        candidates.extend([
            Path("/opt/homebrew/bin") / clean,
            Path("/usr/local/bin") / clean,
            Path.home() / ".local" / "bin" / clean,
        ])
        if clean == "tailscale":
            candidates.extend([
                Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale"),
                Path.home() / "Applications" / "Tailscale.app" / "Contents" / "MacOS" / "Tailscale",
            ])
    elif os.name == "nt":
        exe = clean if clean.lower().endswith(".exe") else f"{clean}.exe"
        prog = Path(os.environ.get("ProgramFiles") or r"C:\Program Files")
        prog86 = Path(os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)")
        local_app = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        if clean == "tailscale":
            candidates.extend([
                prog / "Tailscale" / "tailscale.exe",
                prog86 / "Tailscale" / "tailscale.exe",
            ])
        elif clean in {"pdftotext", "pdftoppm"}:
            candidates.extend([
                prog / "poppler" / "Library" / "bin" / exe,
                prog / "poppler" / "bin" / exe,
                local_app / "Programs" / "poppler" / "Library" / "bin" / exe,
                Path(r"C:\poppler\Library\bin") / exe,
                Path(r"C:\ProgramData\chocolatey\bin") / exe,
                Path.home() / "scoop" / "shims" / exe,
            ])
        candidates.append(local_app / "Programs" / "WikiContext" / "bin" / exe)
    else:
        candidates.extend([
            Path("/usr/local/bin") / clean,
            Path("/usr/bin") / clean,
            Path.home() / ".local" / "bin" / clean,
        ])

    for cand in candidates:
        try:
            if cand.is_file() and (os.name == "nt" or os.access(cand, os.X_OK)):
                return str(cand)
        except OSError:
            continue
    return None
