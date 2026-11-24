#!/usr/bin/env python3
"""Build minimal zero-console `PyInstaller --onedir` engine into Tauri `resources/engine/`."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / "deploy" / "scripts" / "_pyinstaller_entry.py"
SRC_DIR = ROOT / "src"
ENGINE_OUT = ROOT / "deploy" / "desktop" / "src-tauri" / "resources" / "engine"

EXCLUDE_MODULES = [
    "tkinter",
    "unittest",
    "pydoc",
    "test",
    "numpy",
    "pandas",
    "scipy",
    "matplotlib",
    "PIL",
    "Pillow",
    "openpyxl",
    "lxml",
    "onnxruntime",
    "openvino",
    "requests",
]


def _ensure_certifi(py: str) -> bool:
    """Bundle certifi so a frozen engine never depends on the host's CA files.

    PyInstaller ships OpenSSL with a compiled-in CA path pointing at the
    python.org framework install (e.g.
    /Library/Frameworks/Python.framework/Versions/3.14/etc/openssl/cert.pem).
    On machines without that exact framework every HTTPS request fails with
    CERTIFICATE_VERIFY_FAILED ("unable to get local issuer certificate").
    """
    chk = subprocess.run([py, "-c", "import certifi"], capture_output=True, check=False)
    if chk.returncode == 0:
        return True
    print("[build_engine] Installing certifi into the build environment ...", flush=True)
    res = subprocess.run([py, "-m", "pip", "install", "certifi"], check=False)
    if res.returncode != 0:
        res = subprocess.run(
            [py, "-m", "pip", "install", "-i", "https://pypi.org/simple", "certifi"],
            check=False,
        )
    return res.returncode == 0


def _resolve_python_with_pyinstaller() -> str:
    # 1. Current interpreter if PyInstaller is available
    chk = subprocess.run(
        [sys.executable, "-c", "import PyInstaller"],
        capture_output=True,
        check=False,
    )
    if chk.returncode == 0:
        return sys.executable

    # 2. Dedicated build venv under deploy/.build_venv
    venv_dir = ROOT / "deploy" / ".build_venv"
    venv_py = venv_dir / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
    if not venv_py.is_file():
        print(f"[build_engine] Creating isolated build venv at {venv_dir} ...", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
    chk_v = subprocess.run([str(venv_py), "-c", "import PyInstaller"], capture_output=True, check=False)
    if chk_v.returncode != 0:
        print("[build_engine] Installing pyinstaller into deploy/.build_venv ...", flush=True)
        res = subprocess.run([str(venv_py), "-m", "pip", "install", "pyinstaller"], check=False)
        if res.returncode != 0:
            subprocess.run(
                [str(venv_py), "-m", "pip", "install", "-i", "https://pypi.org/simple", "pyinstaller"],
                check=True,
            )
    return str(venv_py)


def main() -> int:
    py = _resolve_python_with_pyinstaller()
    with tempfile.TemporaryDirectory(prefix="wctx-pyinstaller-") as td:
        tmp = Path(td)
        dist_dir = tmp / "dist"
        work_dir = tmp / "build"
        spec_dir = tmp / "spec"
        dist_dir.mkdir()
        work_dir.mkdir()
        spec_dir.mkdir()

        cmd = [
            py,
            "-m",
            "PyInstaller",
            "--onedir",
            "--console" if os.name == "nt" else "--noconsole",
            "--clean",
            "--noconfirm",
            "--name",
            "wctx",
            "--paths",
            str(SRC_DIR),
            "--distpath",
            str(dist_dir),
            "--workpath",
            str(work_dir),
            "--specpath",
            str(spec_dir),
        ]
        for mod in EXCLUDE_MODULES:
            cmd.extend(["--exclude-module", mod])
        if _ensure_certifi(py):
            cmd.extend(["--collect-data", "certifi", "--hidden-import", "certifi"])
        else:
            print(
                "[build_engine] WARNING: certifi unavailable; the engine will rely on "
                "system CA bundles at runtime (WCTX_CA_BUNDLE / platform bundles)",
                flush=True,
            )
        ui_dist = SRC_DIR / "wiki_context" / "ui_dist"
        if (ui_dist / "index.html").is_file():
            cmd.extend(["--add-data", f"{ui_dist}{os.pathsep}wiki_context/ui_dist"])
        cmd.append(str(ENTRY))

        print("[build_engine] Running PyInstaller --onedir ...", flush=True)
        subprocess.run(cmd, cwd=ROOT, check=True)

        built_dir = dist_dir / "wctx"
        if not built_dir.is_dir():
            print(f"Error: PyInstaller output missing at {built_dir}", file=sys.stderr)
            return 1

        # Stage into resources/engine/
        if ENGINE_OUT.exists():
            for child in ENGINE_OUT.iterdir():
                if child.name == ".keep":
                    continue
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        ENGINE_OUT.mkdir(parents=True, exist_ok=True)
        for item in built_dir.iterdir():
            dest = ENGINE_OUT / item.name
            if item.is_dir():
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)

    exe = ENGINE_OUT / ("wctx.exe" if os.name == "nt" else "wctx")
    if not exe.is_file():
        print(f"Error: Engine binary not found at {exe}", file=sys.stderr)
        return 1

    # Smoke-test frozen engine `--version`
    ver_out = subprocess.run(
        [str(exe), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    if "wiki-context" not in ver_out.stdout:
        print(f"Error: Unexpected --version output from {exe}: {ver_out.stdout!r}", file=sys.stderr)
        return 1
    total_mb = sum(p.stat().st_size for p in ENGINE_OUT.rglob("*") if p.is_file()) / (1024 * 1024)
    print(f"[build_engine] OK: {ver_out.stdout.strip()} ({total_mb:.1f} MiB) -> {ENGINE_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
