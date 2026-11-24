"""Document admission policy shared across sync, indexer, and MCP read gates.

Only human-readable or extractable documents enter mirrors and the FTS5 index:
- Text & transcript documents (`.md`, `.txt`, `.csv`, `.tsv`, `.rst`, `.srt`, `.vtt`, `.lrc`)
- Rich & structured documents (`.docx`, `.pptx`, `.xlsx`, `.pdf`, `.html`, `.htm`, `.eml`, `.ipynb`, `.rtf`, legacy Office)
- Audio/video recordings (`.mp3`, `.m4a`, `.wav`, `.mp4`, etc.) when an ASR hook is configured or explicitly synced
- Machine logs/dumps (`.log`, `.json`, `.xml`, `.db`, `.sql`, etc.), archives,
  secret files (`.env`, `.pem`, `.key`), oversized exports (>2 MiB by default),
  and pure image placeholders -> excluded with explicit skip reasons.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

from .config import DEFAULT_MAX_DOC_BYTES, DEFAULT_MAX_SOURCE_BYTES, VaultConfig

POLICY_VERSION = 2

TEXT_EXTS = frozenset({".md", ".markdown", ".txt", ".csv", ".tsv", ".rst"})
TRANSCRIPT_EXTS = frozenset({".srt", ".vtt", ".lrc"})
RICH_STDLIB_EXTS = frozenset({".html", ".htm", ".eml", ".ipynb", ".rtf", ".svg"})
NATIVE_OOXML_EXTS = frozenset({".docx", ".pptx", ".xlsx"})
LEGACY_OFFICE_EXTS = frozenset({".doc", ".ppt", ".xls"})
PDF_EXTS = frozenset({".pdf"})
RICH_BINARY_EXTS = NATIVE_OOXML_EXTS | LEGACY_OFFICE_EXTS | PDF_EXTS
IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"})
OCR_CONVERTERS = frozenset({"vlm-caption", "cmd-ocr", "visual-asset"})
AUDIO_VIDEO_EXTS = frozenset({".mp3", ".wav", ".m4a", ".flac", ".ogg", ".mp4", ".mov", ".avi", ".mkv", ".webm"})

READABLE_EXTS = (
    TEXT_EXTS
    | TRANSCRIPT_EXTS
    | RICH_STDLIB_EXTS
    | NATIVE_OOXML_EXTS
    | LEGACY_OFFICE_EXTS
    | PDF_EXTS
)

SECRET_EXTS = frozenset({".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".kdbx"})
SECRET_NAMES = frozenset({
    ".env", ".netrc", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", "id_ecdsa", "credentials.json"
})

MACHINE_DATA_EXTS = frozenset({
    ".log", ".json", ".jsonl", ".xml", ".yaml", ".yml", ".toml", ".ini",
    ".db", ".sqlite", ".sqlite3", ".sql", ".parquet", ".feather", ".npy", ".npz",
    ".css", ".js", ".mjs", ".ts", ".pyc", ".lock", ".drawnix",
})

BINARY_MEDIA_ARCHIVE_EXTS = frozenset({
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".dmg", ".iso", ".apk", ".ipa",
    ".dwg", ".dxf", ".step", ".stp", ".iges", ".stl", ".blend", ".psd", ".ai",
})

SKIP_DIR_NAMES = frozenset({
    ".git", ".svn", ".hg", ".obsidian", "__pycache__", "node_modules",
    ".tmp", ".previous", ".index", ".assets", "complete",
})

_PLACEHOLDER_IMG_ONLY_RE = re.compile(
    r"^\s*(?:#[^\n]*\n+|\s*)*(?:!\[[^\]]*\]\([^)]+\)\s*)+$"
)


def _has_skipped_dir(rel_posix: str) -> bool:
    parts = PurePosixPath(rel_posix).parts[:-1]
    for p in parts:
        if p in SKIP_DIR_NAMES or p.startswith(".") or p.endswith(".assets"):
            return True
    return False


def _is_secret_filename(name: str) -> bool:
    low = name.lower()
    if low in SECRET_NAMES or low.startswith(".env."):
        return True
    ext = Path(low).suffix
    return ext in SECRET_EXTS


def _in_ocr_image_dir(rel_posix: str, ocr_dirs: frozenset[str]) -> bool:
    if not ocr_dirs:
        return False
    for d in ocr_dirs:
        clean = d.strip("/")
        if rel_posix == clean or rel_posix.startswith(clean + "/"):
            return True
    return False


def source_skip_reason(
    path: Path,
    rel_from_sources: str,
    *,
    cfg: VaultConfig | None = None,
    explicit: bool = False,
    include_all_images: bool = False,
    has_asr: bool | None = None,
) -> str | None:
    """Return human-readable skip reason for a raw file under ``sources/``, or ``None`` to admit."""
    rel = rel_from_sources.replace("\\", "/").lstrip("/")
    name = Path(rel).name
    if not name or name.endswith(".mineru_complete"):
        return "hidden-or-marker"
    if _is_secret_filename(name):
        return "secret-credential-file"
    if name.startswith("."):
        return "hidden-or-marker"
    if not explicit and _has_skipped_dir(rel):
        return "process-or-assets-dir"

    ext = Path(name).suffix.lower()
    max_bytes = cfg.max_doc_bytes if cfg is not None else DEFAULT_MAX_DOC_BYTES
    max_src_bytes = cfg.max_source_bytes if cfg is not None else DEFAULT_MAX_SOURCE_BYTES
    try:
        st = path.stat()
    except OSError:
        return "unreadable"

    # Audio/video files can legitimately exceed 2 MiB when ASR is configured
    is_media = ext in AUDIO_VIDEO_EXTS
    asr_ready = bool(has_asr) if has_asr is not None else bool(os.environ.get("WCTX_ASR_CMD", "").strip())
    if is_media:
        if explicit or asr_ready:
            return None
        return "audio-video-requires-asr"

    size_limit = max_src_bytes if ext in RICH_BINARY_EXTS else max_bytes
    if st.st_size > size_limit and not explicit:
        return f"oversized(>{size_limit}B)"

    if ext in IMAGE_EXTS:
        ocr_dirs = cfg.ocr_image_dirs if cfg is not None else frozenset()
        vlm_ready = bool(os.environ.get("WCTX_VLM_MODEL", "").strip() and os.environ.get("WCTX_VLM_API_KEY", os.environ.get("WCTX_API_KEY", "")).strip())
        if explicit or include_all_images or _in_ocr_image_dir(rel, ocr_dirs) or vlm_ready:
            return None
        return "image-skipped-by-default"

    if ext in READABLE_EXTS:
        return None
    if explicit and ext in MACHINE_DATA_EXTS:
        return None
    if ext in MACHINE_DATA_EXTS:
        return "machine-data"
    if ext in BINARY_MEDIA_ARCHIVE_EXTS:
        return "binary-or-media"
    return f"unsupported-ext({ext or 'none'})"


def strip_frontmatter_quick(text: str) -> str:
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return text
    end = text.find("\n---\n", 4)
    if end != -1:
        return text[end + 5:]
    end_crlf = text.find("\r\n---\r\n", 4)
    if end_crlf != -1:
        return text[end_crlf + 7:]
    return text


def mirror_skip_reason(
    mirror_path: Path,
    mirror_rel: str,
    *,
    cfg: VaultConfig | None = None,
    manifest_entry: dict | None = None,
) -> str | None:
    """Return skip reason for a derived `.md` file under ``mirrors/``, or ``None`` to index."""
    rel = mirror_rel.replace("\\", "/").lstrip("/")
    name = Path(rel).name
    if not name or name.startswith(".") or not name.lower().endswith(".md"):
        return "non-markdown-mirror"
    if _has_skipped_dir(rel):
        return "process-or-assets-dir"

    orig_name = name[:-3]  # strip trailing `.md`
    if _is_secret_filename(orig_name):
        return "secret-credential-file"

    orig_ext = Path(orig_name).suffix.lower()
    explicit = bool(isinstance(manifest_entry, dict) and manifest_entry.get("explicit"))
    max_bytes = cfg.max_doc_bytes if cfg is not None else DEFAULT_MAX_DOC_BYTES

    try:
        st = mirror_path.stat()
    except OSError:
        return "missing-mirror"

    if st.st_size > max_bytes:
        return f"oversized-mirror(>{max_bytes}B)"

    if orig_ext in MACHINE_DATA_EXTS and not explicit:
        return f"machine-data-mirror({orig_ext})"
    if orig_ext in BINARY_MEDIA_ARCHIVE_EXTS:
        return f"binary-mirror({orig_ext})"

    if orig_ext in IMAGE_EXTS:
        ocr_dirs = cfg.ocr_image_dirs if cfg is not None else frozenset()
        conv = str((manifest_entry or {}).get("converter") or "").strip()
        has_ocr_converter = conv in OCR_CONVERTERS
        if not (explicit or has_ocr_converter or _in_ocr_image_dir(rel[:-3], ocr_dirs)):
            if manifest_entry is None:
                pass
            else:
                return "unwhitelisted-image-mirror"
        try:
            body = strip_frontmatter_quick(
                mirror_path.read_text(encoding="utf-8", errors="replace")
            ).strip()
        except OSError:
            return "unreadable-mirror"
        if not body or _PLACEHOLDER_IMG_ONLY_RE.match(body):
            return "image-placeholder-only"

    return None
