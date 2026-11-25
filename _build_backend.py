"""Zero-dependency PEP 517 / PEP 660 build backend for `wiki-context`.

Builds standard `.whl` and editable `.whl` archives using only Python's
standard library (`tomllib`, `zipfile`, `hashlib`, `base64`), enabling instant
offline `pip install .`, `pip install -e .`, and `uv tool install .` including
compiled `ui_dist/` static assets without external build toolchains.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import re
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST_NAME = "wiki_context"
DEFAULT_REQUIRES_PYTHON = ">=3.11"


def _read_pyproject_meta() -> tuple[str, str]:
    """Read package version and requires-python from pyproject.toml with safe fallbacks."""
    pyproject = ROOT / "pyproject.toml"
    if pyproject.is_file():
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        proj = data.get("project", {}) if isinstance(data, dict) else {}
        ver = str(proj.get("version") or "").strip()
        req_py = str(proj.get("requires-python") or "").strip() or DEFAULT_REQUIRES_PYTHON
        if ver:
            return ver, req_py
    init_py = ROOT / "src" / "wiki_context" / "__init__.py"
    if init_py.is_file():
        text = init_py.read_text(encoding="utf-8")
        m = re.search(r'(?m)^__version__\s*=\s*"([^"]+)"\s*$', text)
        if m:
            return m.group(1).strip(), DEFAULT_REQUIRES_PYTHON
    return "0.5.7", DEFAULT_REQUIRES_PYTHON


VERSION, REQUIRES_PYTHON = _read_pyproject_meta()
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
        f"Requires-Python: {REQUIRES_PYTHON}\n"
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


def _iter_package_files(pkg_root: Path) -> list[Path]:
    """Collect all Python source files and static `ui_dist/` assets inside `pkg_root`."""
    files: list[Path] = []
    for p in sorted(pkg_root.rglob("*")):
        if not p.is_file():
            continue
        if "__pycache__" in p.parts or p.suffix in {".pyc", ".pyo", ".DS_Store"}:
            continue
        if p.name.startswith("."):
            continue
        files.append(p)
    return files


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
            for fpath in _iter_package_files(pkg_root):
                rel = fpath.relative_to(src_dir).as_posix()
                put(rel, fpath.read_bytes())

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


def get_requires_for_build_wheel(config_settings=None) -> list[str]:
    return []


def get_requires_for_build_editable(config_settings=None) -> list[str]:
    return []


def get_requires_for_build_sdist(config_settings=None) -> list[str]:
    return []


def prepare_metadata_for_build_wheel(metadata_directory: str, config_settings=None) -> str:
    dest = Path(metadata_directory) / DIST_INFO
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "METADATA").write_bytes(_metadata_bytes())
    (dest / "WHEEL").write_bytes(_wheel_bytes())
    (dest / "entry_points.txt").write_bytes(_entry_points_bytes())
    if (ROOT / "LICENSE").is_file():
        (dest / "LICENSE").write_bytes((ROOT / "LICENSE").read_bytes())
    return DIST_INFO


def prepare_metadata_for_build_editable(metadata_directory: str, config_settings=None) -> str:
    return prepare_metadata_for_build_wheel(metadata_directory, config_settings)


def build_wheel(wheel_directory: str, config_settings=None, metadata_directory=None) -> str:
    return _write_wheel(wheel_directory, editable=False)


def build_editable(wheel_directory: str, config_settings=None, metadata_directory=None) -> str:
    return _write_wheel(wheel_directory, editable=True)
