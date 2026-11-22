"""Incremental source-to-mirror synchronization with pluggable OCR hooks.

Scans ``<vault>/<sources_dir>/`` read-only, computes SHA-256 digests, extracts
modified or newly added documents via ``extract_document`` (falling back to an
optional user-configured OCR command for scanned PDFs/images), writes Markdown
mirrors + JSON sidecars into ``<vault>/<mirrors_dir>/``, and triggers FTS5 index
refresh.
"""

from __future__ import annotations

import datetime as dt
try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

from .config import VaultConfig
from .extract import ExtractedDocument, extract_document, supports_native
from .policy import IMAGE_EXTS, LEGACY_OFFICE_EXTS, source_skip_reason


class OCRProvider(Protocol):
    def extract(self, source_path: Path) -> ExtractedDocument: ...


class CommandOCRProvider:
    """Runs an external CLI OCR command template.

    Template placeholders:
      - ``{input}``: path to raw source file
      - ``{output}``: path to temporary output `.md` file (if omitted in template,
        stdout of the command is captured as Markdown).
    """

    def __init__(self, command_template: str, *, timeout_sec: int = 600):
        self.command_template = command_template.strip()
        self.timeout_sec = timeout_sec

    def extract(self, source_path: Path) -> ExtractedDocument:
        with tempfile.TemporaryDirectory(prefix="wctx-ocr-") as td:
            out_md = Path(td) / "ocr_out.md"
            uses_out_file = "{output}" in self.command_template
            argv = [
                part.replace("{input}", str(source_path)).replace("{output}", str(out_md))
                for part in shlex.split(self.command_template)
            ]
            try:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_sec,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return ExtractedDocument("cmd-ocr", "", [], 0, [f"OCR command failed: {exc}"], usable=False)

            if proc.returncode != 0:
                return ExtractedDocument(
                    "cmd-ocr",
                    "",
                    [],
                    0,
                    [f"OCR command exited {proc.returncode}: {proc.stderr.strip()[:240]}"],
                    usable=False,
                )
            text = out_md.read_text(encoding="utf-8", errors="replace") if (uses_out_file and out_md.is_file()) else proc.stdout
            text = (text or "").strip()
            if not text:
                return ExtractedDocument("cmd-ocr", "", [], 0, ["OCR command produced empty output"], usable=False)
            return ExtractedDocument(
                method="cmd-ocr",
                markdown=text + "\n",
                pages=[{"page_num": 0, "page_label": "OCR", "blocks": [{"type": "Text", "content": text, "format": "text"}]}],
                page_count=1,
                text_char_count=len(text),
                usable=True,
            )


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def sync_vault(
    vault: Path | str,
    *,
    source: str | None = None,
    list_only: bool = False,
    force: bool = False,
    include_all_images: bool = False,
    ocr_provider: OCRProvider | None = None,
    ocr_cmd: str | None = None,
    rebuild: bool = True,
) -> dict:
    """Incrementally convert new/modified sources into ``mirrors/`` and rebuild index."""
    cfg = VaultConfig.load(vault)
    cfg.mirrors_root.mkdir(parents=True, exist_ok=True)
    cfg.sources_root.mkdir(parents=True, exist_ok=True)

    cmd_str = (ocr_cmd or os.environ.get("WCTX_OCR_CMD") or "").strip()
    ocr = ocr_provider or (CommandOCRProvider(cmd_str) if cmd_str else None)

    lock_path = cfg.mirrors_root / ".sync.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_fh:
        if fcntl is not None:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Another sync process is already running on this vault") from exc

        manifest_path = cfg.mirrors_root / ".manifest.json"
        failed_path = cfg.mirrors_root / ".failed.json"
        manifest: dict = {}
        if manifest_path.is_file():
            try:
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    manifest = loaded
            except (OSError, ValueError):
                manifest = {}

        failed_map: dict = {}
        if failed_path.is_file():
            try:
                loaded_f = json.loads(failed_path.read_text(encoding="utf-8"))
                if isinstance(loaded_f, dict):
                    failed_map = loaded_f
            except (OSError, ValueError):
                failed_map = {}

        candidates: list[tuple[Path, str, bool]] = []
        if source:
            rel = cfg.strip_source_prefix(source)
            target = (cfg.sources_root / rel).resolve()
            if not target.is_file() or cfg.sources_root.resolve() not in target.parents:
                raise ValueError(f"Source file not found inside {cfg.sources_dir}/: {source}")
            reason = source_skip_reason(target, rel, cfg=cfg, explicit=True, include_all_images=include_all_images)
            if reason is not None:
                raise ValueError(f"Source file rejected by policy ({reason}): {source}")
            candidates.append((target, rel, True))
        else:
            for p in sorted(cfg.sources_root.rglob("*")):
                if p.is_symlink() or not p.is_file():
                    continue
                rel = p.relative_to(cfg.sources_root).as_posix()
                reason = source_skip_reason(p, rel, cfg=cfg, explicit=False, include_all_images=include_all_images)
                if reason is not None:
                    continue
                candidates.append((p, rel, False))

        pending: list[tuple[Path, str, str, bool]] = []
        up_to_date = 0
        for p, rel, is_explicit in candidates:
            digest = sha256_file(p)
            prev = manifest.get(rel)
            mirror_rel = f"{rel}.md"
            mirror_file = cfg.mirrors_root / mirror_rel
            if (
                not force
                and isinstance(prev, dict)
                and prev.get("hash") == digest
                and mirror_file.is_file()
            ):
                up_to_date += 1
                continue
            pending.append((p, rel, digest, is_explicit))

        if list_only:
            return {
                "vault": cfg.name,
                "scanned": len(candidates),
                "up_to_date": up_to_date,
                "pending": [rel for _, rel, _, _ in pending],
            }

        converted = 0
        preserved = 0
        failed_now = 0

        for p, rel, pre_hash, is_explicit in pending:
            ext = p.suffix.lower()
            doc: ExtractedDocument | None = None

            if ext in IMAGE_EXTS or ext in LEGACY_OFFICE_EXTS:
                if ocr is not None:
                    doc = ocr.extract(p)
                else:
                    doc = ExtractedDocument(
                        method="ocr-required",
                        markdown="",
                        pages=[],
                        page_count=0,
                        warnings=[f"Format {ext} requires an OCR provider (set WCTX_OCR_CMD or --ocr-cmd)"],
                        usable=False,
                    )
            elif supports_native(p):
                doc = extract_document(p)
                if (not doc.usable or doc.visual_pages) and ocr is not None:
                    ocr_doc = ocr.extract(p)
                    if ocr_doc.usable:
                        doc = ocr_doc

            post_hash = sha256_file(p)
            if post_hash != pre_hash:
                failed_now += 1
                failed_map[rel] = {
                    "error": "Source file changed during extraction; skipped write",
                    "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                }
                continue

            mirror_rel = f"{rel}.md"
            mirror_file = cfg.mirrors_root / mirror_rel
            sidecar_file = cfg.mirrors_root / f"{rel}.json"

            if doc is None or not doc.usable:
                # Preserve existing mirror if present rather than wiping last good state
                if mirror_file.is_file():
                    preserved += 1
                    continue
                failed_now += 1
                err_msg = "; ".join(doc.warnings) if (doc and doc.warnings) else "Extraction returned empty/unusable output"
                failed_map[rel] = {
                    "error": err_msg,
                    "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                }
                continue

            # Backup old mirror if overwriting
            if mirror_file.is_file():
                backup = cfg.mirrors_root / ".previous" / mirror_rel
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(mirror_file, backup)

            now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            source_ref = f"{cfg.source_prefix}{rel}"
            frontmatter = (
                "---\n"
                f'source: "{source_ref}"\n'
                f'source_hash: "{post_hash}"\n'
                f'converted_at: "{now_iso}"\n'
                f'converter: "{doc.method}"\n'
                "---\n\n"
            )
            _atomic_write(mirror_file, frontmatter + doc.markdown)

            sidecar = doc.as_sidecar()
            sidecar.update({
                "source": source_ref,
                "source_hash": post_hash,
                "converted_at": now_iso,
            })
            _atomic_write(sidecar_file, json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n")

            entry: dict = {
                "hash": post_hash,
                "mirror": mirror_rel,
                "converter": doc.method,
                "converted_at": now_iso,
            }
            if is_explicit:
                entry["explicit"] = True
            manifest[rel] = entry
            failed_map.pop(rel, None)
            converted += 1

        _atomic_write(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        if failed_map:
            _atomic_write(failed_path, json.dumps(failed_map, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        elif failed_path.is_file():
            failed_path.unlink(missing_ok=True)

        index_summary = None
        if rebuild:
            from .indexer import rebuild_index
            index_summary = rebuild_index(cfg.root)

        return {
            "vault": cfg.name,
            "scanned": len(candidates),
            "up_to_date": up_to_date,
            "converted": converted,
            "preserved": preserved,
            "failed": failed_now,
            "total_failed_recorded": len(failed_map),
            "index": index_summary,
        }
