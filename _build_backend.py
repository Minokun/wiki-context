"""Zero-dependency PEP 517 / PEP 660 build backend for `wiki-context`.

Builds standard `.whl` and editable `.whl` archives using only Python's
standard library (`tomllib` / minimal fallback, `zipfile`, `hashlib`, `base64`),
enabling instant offline `pip install .`, `pip install -e .`, and `uv tool install .`
without downloading external build toolchains.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST_NAME = "wiki_context"
VERSION = "0.1.0"
DIST_INFO = f"{DIST_NAME}-{VERSION}.dist-info"


def _hash_bytes(data: bytes) -> tuple[str, int]:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
    return f"sha256={digest}", len(data)


def _metadata_bytes() -> bytes:
    readme = (ROOT / "README.md").read_text(encoding="utf-8") if (ROOT / "README.md").is_file() else ""
    header = (
        "Metadata-Version: 2.1\n"
        "Name: wiki-context\n"
        f"Version: {VERSION}\n"
        "Summary: Deterministic Context Engine for AI Agents — Compiled Wiki + Table-Aware SQLite FTS5 + MCP Gateway\n"
        "License: MIT\n"
        "Requires-Python: >=3.10\n"
        "Project-URL: Homepage, https://github.com/Minokun/wiki-context\n"
        "Project-URL: Repository, https://github.com/Minokun/wiki-context.git\n"
        "Description-Content-Type: text/markdown\n\n"
    )
    return (header + readme).encode("utf-8")


def _wheel_bytes() -> bytes:
    return (
        "Wheel-Version: 1.0\n"
        "Generator: wiki-context-pep517\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    ).encode("utf-8")


def _entry_points_bytes() -> bytes:
    return (
        "[console_scripts]\n"
        "wctx = wiki_context.cli:main\n"
        "wiki-context = wiki_context.cli:main\n"
    ).encode("utf-8")


def _write_wheel(wheel_dir: str, *, editable: bool) -> str:
    whl_name = f"{DIST_NAME}-{VERSION}-py3-none-any.whl"
    out_path = Path(wheel_dir) / whl_name
    records: list[tuple[str, str, str]] = []

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        def put(arcname: str, data: bytes) -> None:
            zf.writestr(arcname, data)
            h, sz = _hash_bytes(data)
            records.append((arcname, h, str(sz)))

        src_dir = ROOT / "src"
        if editable:
            put(f"{DIST_NAME}.pth", (str(src_dir.resolve()) + "\n").encode("utf-8"))
        else:
            pkg_root = src_dir / DIST_NAME
            for py in sorted(pkg_root.rglob("*.py")):
                rel = py.relative_to(src_dir).as_posix()
                put(rel, py.read_bytes())

        if (ROOT / "LICENSE").is_file():
            put(f"{DIST_INFO}/LICENSE", (ROOT / "LICENSE").read_bytes())
        put(f"{DIST_INFO}/METADATA", _metadata_bytes())
        put(f"{DIST_INFO}/WHEEL", _wheel_bytes())
        put(f"{DIST_INFO}/entry_points.txt", _entry_points_bytes())

        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        for row in records:
            writer.writerow(row)
        writer.writerow([f"{DIST_INFO}/RECORD", "", ""])
        zf.writestr(f"{DIST_INFO}/RECORD", buf.getvalue().encode("utf-8"))

    return whl_name


def build_wheel(wheel_directory: str, config_settings=None, metadata_directory=None) -> str:
    return _write_wheel(wheel_directory, editable=False)


def build_editable(wheel_directory: str, config_settings=None, metadata_directory=None) -> str:
    return _write_wheel(wheel_directory, editable=True)
