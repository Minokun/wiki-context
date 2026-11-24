"""PyInstaller --onedir entrypoint for `wiki-context` (`wctx` / `wctx.exe`)."""

from __future__ import annotations

import os
import sys

# Ensure UTF-8 stdio on Windows regardless of system codepage (cp936/gbk)
os.environ.setdefault("PYTHONUTF8", "1")
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from wiki_context.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
