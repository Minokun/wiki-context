#!/usr/bin/env python3
"""End-to-end Desktop packager: Web SPA -> PyInstaller --onedir Engine -> Tauri Bundle."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESKTOP_DIR = ROOT / "deploy" / "desktop"
SCRIPTS_DIR = ROOT / "deploy" / "scripts"
OUT_DIST = ROOT / "deploy" / "dist"


def main() -> int:
    ap = argparse.ArgumentParser(description="Build wiki-context Desktop installers (.dmg / .exe / .msi)")
    ap.add_argument("--skip-engine", action="store_true", help="Skip PyInstaller --onedir engine rebuild")
    ap.add_argument("--skip-web", action="store_true", help="Skip deploy/web Vite build")
    ap.add_argument("--bundles", default="", help="Optional Tauri --bundles override (e.g. app,dmg or nsis,msi)")
    args = ap.parse_args()

    if not args.skip_web:
        subprocess.run([sys.executable, str(SCRIPTS_DIR / "build_web.py")], cwd=ROOT, check=True)

    if not args.skip_engine:
        subprocess.run([sys.executable, str(SCRIPTS_DIR / "build_engine.py")], cwd=ROOT, check=True)

    npm = shutil.which("npm")
    if not npm:
        print("Error: npm not found in PATH", file=sys.stderr)
        return 2

    if not (DESKTOP_DIR / "node_modules").is_dir():
        print("[build_desktop] Installing @tauri-apps/cli ...", flush=True)
        subprocess.run([npm, "install", "--no-audit", "--no-fund"], cwd=DESKTOP_DIR, check=True)

    env = dict(os.environ)
    if sys.platform == "darwin" and not env.get("SDKROOT"):
        # Avoid broken Xcode/CLT 27.x tapi arm64e.x1 linker mismatch when older SDK is present
        cand_sdk = Path("/Library/Developer/CommandLineTools/SDKs/MacOSX14.4.sdk")
        if cand_sdk.is_dir():
            env["SDKROOT"] = str(cand_sdk)

    npx = shutil.which("npx") or ("npx.cmd" if os.name == "nt" else "npx")
    tauri_cmd = [npx, "tauri", "build"]
    if args.bundles:
        tauri_cmd.extend(["--bundles", args.bundles])

    # Auto-upgrade legacy git:// ustc mirror in ~/.cargo/config* to sparse+https without mutating user files
    cargo_extra_args: list[str] = []
    for cand_cfg in (Path.home() / ".cargo" / "config.toml", Path.home() / ".cargo" / "config"):
        if cand_cfg.is_file():
            raw_cfg = cand_cfg.read_text(encoding="utf-8", errors="ignore")
            if "mirrors.ustc.edu.cn/crates.io-index" in raw_cfg and "sparse+https://mirrors.ustc.edu.cn" not in raw_cfg:
                cargo_extra_args.extend(
                    ["--config", 'source.ustc.registry="sparse+https://mirrors.ustc.edu.cn/crates.io-index/"']
                )
                break
    if cargo_extra_args:
        tauri_cmd.extend(["--", *cargo_extra_args])

    print(f"[build_desktop] Running {' '.join(tauri_cmd)} ...", flush=True)
    subprocess.run(tauri_cmd, cwd=DESKTOP_DIR, env=env, check=True)

    # Guard against accidental devUrl-only builds (must embed frontendDist assets via custom-protocol)
    rel_bin = DESKTOP_DIR / "src-tauri" / "target" / "release" / (
        "wiki-context-desktop.exe" if os.name == "nt" else "wiki-context-desktop"
    )
    if rel_bin.is_file():
        bin_bytes = rel_bin.read_bytes()
        if b"/assets/index-" not in bin_bytes:
            print(
                f"Error: {rel_bin} does not contain embedded /assets/index-* from frontendDist (was custom-protocol disabled?)",
                file=sys.stderr,
            )
            return 3

    bundle_root = DESKTOP_DIR / "src-tauri" / "target" / "release" / "bundle"
    OUT_DIST.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    if bundle_root.is_dir():
        for ext in ("*.dmg", "*.exe", "*.msi", "*.AppImage", "*.deb"):
            for artifact in bundle_root.rglob(ext):
                dest = OUT_DIST / artifact.name
                shutil.copy2(artifact, dest)
                copied.append(str(dest))

    print(f"[build_desktop] Complete. Artifacts in {OUT_DIST}:")
    for c in copied:
        print(f"  - {c}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
