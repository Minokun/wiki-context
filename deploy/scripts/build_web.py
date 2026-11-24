#!/usr/bin/env python3
"""Build `deploy/web` React SPA and verify output in `src/wiki_context/ui_dist/`."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT / "deploy" / "web"
UI_DIST = ROOT / "src" / "wiki_context" / "ui_dist"


def main() -> int:
    npm = shutil.which("npm")
    if not npm:
        print("Error: npm not found in PATH", file=sys.stderr)
        return 2

    if not (WEB_DIR / "node_modules").is_dir():
        print("[build_web] Installing npm dependencies...", flush=True)
        subprocess.run([npm, "install", "--no-audit", "--no-fund"], cwd=WEB_DIR, check=True)

    print("[build_web] Type-checking & building Vite bundle -> src/wiki_context/ui_dist ...", flush=True)
    subprocess.run([npm, "run", "build"], cwd=WEB_DIR, check=True)

    index_html = UI_DIST / "index.html"
    assets_dir = UI_DIST / "assets"
    if not index_html.is_file() or not assets_dir.is_dir():
        print(f"Error: Expected ui_dist artifacts missing under {UI_DIST}", file=sys.stderr)
        return 1

    files = [p for p in UI_DIST.rglob("*") if p.is_file()]
    total_kb = sum(p.stat().st_size for p in files) / 1024.0
    print(f"[build_web] OK: {len(files)} files ({total_kb:.1f} KiB) written to {UI_DIST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
