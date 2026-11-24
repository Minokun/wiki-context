"""Incremental source-to-mirror synchronization with multimodal visual assets, VLM, OCR & ASR hooks.

Scans ``<vault>/<sources_dir>/`` read-only, computes SHA-256 digests, extracts:
- Structured documents via ``extract_document`` + embedded diagrams into ``mirrors/.assets/``
- Sparse PDF ``visual_pages`` rendered selectively via ``pdftoppm`` (when installed)
- Optional zero-SDK ``HttpVLMProvider`` (`WCTX_VLM_MODEL`) or ``CommandOCRProvider`` (`WCTX_OCR_CMD`)
  captioning only visual assets / scanned pages
- Optional ``WCTX_ASR_CMD`` hook transcribing audio/video recordings with timestamp locators
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Protocol

from .config import VaultConfig
from .net import ssl_context
from .state_lock import atomic_write_text, file_lock, resolve_external_tool
from .extract import (
    ExtractedDocument,
    _extract_transcript,
    _mime_for_ext,
    extract_archive_assets,
    extract_document,
    supports_native,
)
from .policy import AUDIO_VIDEO_EXTS, IMAGE_EXTS, LEGACY_OFFICE_EXTS, source_skip_reason


class OCRProvider(Protocol):
    def extract(self, source_path: Path) -> ExtractedDocument: ...


def _split_command_template(template: str, *, is_windows: bool | None = None) -> list[str]:
    """Split a CLI command template without stripping Windows path backslashes."""
    win = (os.name == "nt") if is_windows is None else bool(is_windows)
    if not win:
        return shlex.split(template, posix=True)
    tokens: list[str] = []
    for tok in shlex.split(template, posix=False):
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in {'"', "'"}:
            quote = tok[0]
            inner = tok[1:-1]
            inner = inner.replace('\\"', '"') if quote == '"' else inner.replace("\\'", "'")
            tokens.append(inner)
        else:
            tokens.append(tok)
    return tokens


class CommandOCRProvider:
    """Runs an external CLI OCR or ASR command template (`{input}`, optional `{output}`)."""

    def __init__(self, command_template: str, *, timeout_sec: int = 600, method_name: str = "cmd-ocr"):
        self.command_template = command_template.strip()
        self.timeout_sec = timeout_sec
        self.method_name = method_name

    def extract(self, source_path: Path) -> ExtractedDocument:
        with tempfile.TemporaryDirectory(prefix="wctx-ext-") as td:
            out_md = Path(td) / "out.md"
            uses_out_file = "{output}" in self.command_template
            argv = [
                part.replace("{input}", str(source_path)).replace("{output}", str(out_md))
                for part in _split_command_template(self.command_template)
            ]
            try:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_sec,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return ExtractedDocument(self.method_name, "", [], 0, [f"External command failed: {exc}"], usable=False)

            if proc.returncode != 0:
                return ExtractedDocument(
                    self.method_name,
                    "",
                    [],
                    0,
                    [f"Command exited {proc.returncode}: {proc.stderr.strip()[:240]}"],
                    usable=False,
                )
            text = out_md.read_text(encoding="utf-8", errors="replace") if (uses_out_file and out_md.is_file()) else proc.stdout
            text = (text or "").strip()
            if not text:
                return ExtractedDocument(self.method_name, "", [], 0, ["Command produced empty output"], usable=False)

            # If ASR returned SRT/VTT cues, structure into timestamped sections automatically
            if "-->" in text and self.method_name == "cmd-asr":
                tmp_vtt = Path(td) / f"{source_path.stem}.vtt"
                tmp_vtt.write_text(text, encoding="utf-8")
                parsed = _extract_transcript(tmp_vtt)
                parsed.method = "cmd-asr"
                return parsed

            return ExtractedDocument(
                method=self.method_name,
                markdown=text + "\n",
                pages=[{"page_num": 0, "page_label": "Extracted", "blocks": [{"type": "Text", "content": text, "format": "text"}]}],
                page_count=1,
                text_char_count=len(text),
                usable=True,
            )


class HttpVLMProvider:
    """Zero-SDK OpenAI-compatible Vision captioner (`WCTX_VLM_MODEL`)."""

    def __init__(self, *, model: str, api_key: str, base_url: str = "https://api.openai.com/v1", timeout_sec: int = 90):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = timeout_sec

    @classmethod
    def from_env(cls) -> HttpVLMProvider | None:
        model = os.environ.get("WCTX_VLM_MODEL", "").strip()
        if not model:
            return None
        key = (
            os.environ.get("WCTX_VLM_API_KEY")
            or os.environ.get("WCTX_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        ).strip()
        if not key:
            return None
        base_url = (
            os.environ.get("WCTX_VLM_BASE_URL")
            or os.environ.get("WCTX_BASE_URL")
            or "https://api.openai.com/v1"
        ).strip()
        return cls(model=model, api_key=key, base_url=base_url)

    def caption_image(self, image_path: Path) -> str:
        raw = image_path.read_bytes()
        if len(raw) > 8 * 1024 * 1024:
            return ""
        mime = _mime_for_ext(image_path.suffix)
        b64 = base64.b64encode(raw).decode("ascii")
        prompt = (
            "Transcribe all visible text, numbers, table cells, component names, and flowchart arrows "
            "from this technical figure or scanned page accurately into concise Markdown."
        )
        payload = json.dumps({
            "model": self.model,
            "temperature": 0.0,
            "max_tokens": 1500,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ],
                }
            ],
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec, context=ssl_context()) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            choices = body.get("choices") or []
            if choices:
                return str((choices[0].get("message") or {}).get("content") or "").strip()
        except Exception:
            return ""
        return ""


def _render_pdf_visual_pages(pdf_path: Path, asset_dir: Path, visual_pages: list[int], *, max_pages: int = 12) -> list[dict]:
    pdftoppm = resolve_external_tool("pdftoppm")
    if not pdftoppm or not visual_pages:
        return []
    rendered: list[dict] = []
    asset_dir.mkdir(parents=True, exist_ok=True)
    for pno in visual_pages[:max_pages]:
        out_stem = asset_dir / f"page-{pno:03d}"
        try:
            proc = subprocess.run(
                [pdftoppm, "-png", "-r", "144", "-singlefile", "-f", str(pno), "-l", str(pno), str(pdf_path), str(out_stem)],
                capture_output=True,
                timeout=30,
                check=False,
            )
            png_file = asset_dir / f"page-{pno:03d}.png"
            if proc.returncode == 0 and png_file.is_file():
                rendered.append({
                    "asset_id": png_file.name,
                    "name": png_file.name,
                    "page_num": pno - 1,
                    "bytes": png_file.stat().st_size,
                    "mime": "image/png",
                })
        except (OSError, subprocess.TimeoutExpired):
            continue
    return rendered


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write(path: Path, content: str) -> None:
    atomic_write_text(path, content)


def sync_vault(
    vault: Path | str,
    *,
    source: str | None = None,
    list_only: bool = False,
    force: bool = False,
    include_all_images: bool = False,
    ocr_provider: OCRProvider | None = None,
    ocr_cmd: str | None = None,
    asr_cmd: str | None = None,
    vlm_provider: HttpVLMProvider | None = None,
    rebuild: bool = True,
) -> dict:
    """Incrementally convert new/modified sources into ``mirrors/`` + ``mirrors/.assets/`` and rebuild index."""
    cfg = VaultConfig.load(vault)
    cfg.mirrors_root.mkdir(parents=True, exist_ok=True)
    if cfg.sources_path is None:
        cfg.sources_root.mkdir(parents=True, exist_ok=True)
    elif not cfg.sources_root.is_dir():
        raise FileNotFoundError(
            f"外部源目录当前不可访问或已移动：{cfg.sources_root}（历史索引已保留；请检查挂载点或使用重定位功能更新路径）"
        )

    cmd_str = (ocr_cmd or os.environ.get("WCTX_OCR_CMD") or "").strip()
    ocr = ocr_provider or (CommandOCRProvider(cmd_str, method_name="cmd-ocr") if cmd_str else None)

    asr_str = (asr_cmd or os.environ.get("WCTX_ASR_CMD") or "").strip()
    asr = CommandOCRProvider(asr_str, method_name="cmd-asr") if asr_str else None

    vlm = vlm_provider or HttpVLMProvider.from_env()

    lock_path = cfg.mirrors_root / ".sync.lock"
    with file_lock(
        lock_path,
        blocking=False,
        busy_message="Another sync process is already running on this vault",
    ):

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
        offline_mounts: list[dict[str, str]] = []
        skipped_image_dirs: dict[str, int] = {}
        if source:
            rel = cfg.strip_source_prefix(source)
            resolved = cfg.resolve_source_rel(rel)
            if resolved is None:
                raise ValueError(f"Source file not found inside {cfg.sources_dir}/: {source}")
            target, _jail = resolved
            if not target.is_file():
                raise ValueError(f"Source file not found inside {cfg.sources_dir}/: {source}")
            reason = source_skip_reason(
                target, rel, cfg=cfg, explicit=True, include_all_images=include_all_images, has_asr=(asr is not None)
            )
            if reason is not None:
                raise ValueError(f"Source file rejected by policy ({reason}): {source}")
            candidates.append((target, rel, True))
        else:
            for mount_name, sroot in cfg.iter_source_roots():
                if not sroot.is_dir():
                    if mount_name:
                        offline_mounts.append({"name": mount_name, "path": str(sroot)})
                    continue
                for p in sorted(sroot.rglob("*")):
                    if p.is_symlink() or not p.is_file():
                        continue
                    sub_rel = p.relative_to(sroot).as_posix()
                    rel = f"{mount_name}/{sub_rel}" if mount_name else sub_rel
                    reason = source_skip_reason(
                        p, rel, cfg=cfg, explicit=False, include_all_images=include_all_images, has_asr=(asr is not None)
                    )
                    if reason is not None:
                        if reason == "image-skipped-by-default":
                            parent_rel = str(PurePosixPath(rel).parent)
                            src_dir = (
                                f"{cfg.source_prefix}{parent_rel}"
                                if parent_rel and parent_rel != "."
                                else cfg.source_prefix.rstrip("/")
                            )
                            skipped_image_dirs[src_dir] = skipped_image_dirs.get(src_dir, 0) + 1
                        continue
                    candidates.append((p, rel, False))

        pending: list[tuple[Path, str, str, bool]] = []
        up_to_date = 0
        for p, rel, is_explicit in candidates:
            digest = sha256_file(p)
            prev = manifest.get(rel)
            mirror_rel = f"{rel}.md"
            mirror_file = cfg.mirrors_root / mirror_rel
            legacy_stem_mirror = (
                cfg.mirrors_root / f"{Path(rel).with_suffix('').as_posix()}.md"
                if p.suffix.lower() in {".txt", ".md", ".markdown", ".rst"}
                else None
            )
            has_mirror = mirror_file.is_file() or (legacy_stem_mirror is not None and legacy_stem_mirror.is_file())
            if (
                not force
                and isinstance(prev, dict)
                and prev.get("hash") == digest
                and has_mirror
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
            asset_dir = cfg.mirrors_root / ".assets" / rel
            saved_assets: list[dict] = []

            if ext in AUDIO_VIDEO_EXTS:
                if asr is not None:
                    doc = asr.extract(p)
                else:
                    doc = ExtractedDocument(
                        method="asr-required",
                        markdown="",
                        pages=[],
                        page_count=0,
                        warnings=[f"Media {ext} requires WCTX_ASR_CMD or --asr-cmd"],
                        usable=False,
                    )
            elif ext in IMAGE_EXTS:
                direct_asset = {
                    "asset_id": p.name,
                    "name": p.name,
                    "source_direct": True,
                    "bytes": p.stat().st_size,
                    "mime": _mime_for_ext(ext),
                }
                saved_assets = [direct_asset]
                if vlm is not None:
                    cap = vlm.caption_image(p)
                    if cap:
                        md_cap = f"# {p.stem}\n\n{cap}\n"
                        doc = ExtractedDocument(
                            method="vlm-caption",
                            markdown=md_cap,
                            pages=[{"page_num": 0, "page_label": p.stem, "blocks": [{"type": "Text", "content": md_cap, "format": "markdown"}]}],
                            page_count=1,
                            embedded_media=saved_assets,
                            text_char_count=len(cap),
                            usable=True,
                        )
                if (doc is None or not doc.usable) and ocr is not None:
                    ocr_doc = ocr.extract(p)
                    if ocr_doc.usable:
                        ocr_doc.embedded_media = saved_assets
                        doc = ocr_doc
                if doc is None or not doc.usable:
                    # Register visual asset stub so path-based queries and MCP wiki_inspect_visual still work zero-dep
                    stub_md = (
                        f"# {p.stem}\n\n"
                        f"Visual figure `{cfg.source_prefix}{rel}` ({direct_asset['bytes']} bytes). "
                        f"Inspect full image via MCP `wiki_inspect_visual(source=\"{cfg.source_prefix}{rel}\")`.\n"
                    )
                    doc = ExtractedDocument(
                        method="visual-asset",
                        markdown=stub_md,
                        pages=[{"page_num": 0, "page_label": p.stem, "blocks": [{"type": "Text", "content": stub_md, "format": "markdown"}]}],
                        page_count=1,
                        visual_pages=[1],
                        embedded_media=saved_assets,
                        text_char_count=len(stub_md),
                        usable=True,
                    )
            elif supports_native(p):
                doc = extract_document(p)
                if doc.embedded_media:
                    saved_assets.extend(extract_archive_assets(p, asset_dir, doc.embedded_media))
                if ext == ".pdf" and doc.visual_pages:
                    saved_assets.extend(_render_pdf_visual_pages(p, asset_dir, doc.visual_pages))

                # Selective VLM captioning on extracted diagrams / sparse visual pages
                if saved_assets and vlm is not None:
                    fig_captions: list[str] = []
                    for a_item in saved_assets[:8]:
                        aid = a_item.get("asset_id") or ""
                        a_file = asset_dir / aid
                        if a_file.is_file():
                            cap = vlm.caption_image(a_file)
                            if cap:
                                fig_captions.append(f"### [Visual Figure: {aid}]\n\n{cap}")
                    if fig_captions:
                        extra_md = "\n\n## Extracted Visual Figures\n\n" + "\n\n".join(fig_captions) + "\n"
                        doc.markdown = (doc.markdown.rstrip() + extra_md) if doc.markdown else extra_md.lstrip()
                        doc.text_char_count += len(extra_md)
                        doc.usable = True

                # Fallback to CLI OCR if document text was unusable or visual pages need OCR
                if (not doc.usable or (doc.visual_pages and vlm is None)) and ocr is not None:
                    if not doc.usable:
                        ocr_doc = ocr.extract(p)
                        if ocr_doc.usable:
                            ocr_doc.embedded_media = saved_assets
                            doc = ocr_doc
                    elif saved_assets:
                        # Selective OCR ONLY on rendered visual pages rather than re-OCRing 50 text pages!
                        ocr_parts: list[str] = []
                        for a_item in saved_assets[:8]:
                            a_file = asset_dir / str(a_item.get("asset_id") or "")
                            if a_file.is_file():
                                sub_ocr = ocr.extract(a_file)
                                if sub_ocr.usable and sub_ocr.markdown.strip():
                                    ocr_parts.append(f"### [OCR Visual {a_file.name}]\n\n{sub_ocr.markdown.strip()}")
                        if ocr_parts:
                            doc.markdown = doc.markdown.rstrip() + "\n\n## OCR Visual Pages\n\n" + "\n\n".join(ocr_parts) + "\n"
                        else:
                            ocr_doc = ocr.extract(p)
                            if ocr_doc.usable:
                                ocr_doc.embedded_media = saved_assets
                                doc = ocr_doc
                    else:
                        ocr_doc = ocr.extract(p)
                        if ocr_doc.usable:
                            ocr_doc.embedded_media = saved_assets
                            doc = ocr_doc
            elif ext in LEGACY_OFFICE_EXTS and ocr is not None:
                doc = ocr.extract(p)

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

            if saved_assets:
                doc.embedded_media = saved_assets

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
            if saved_assets:
                entry["assets_count"] = len(saved_assets)
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

        if source is None and (candidates or not manifest):
            skipped_media_path = cfg.mirrors_root / ".skipped_media.json"
            if skipped_image_dirs:
                _atomic_write(
                    skipped_media_path,
                    json.dumps(
                        {
                            "skipped_image_count": sum(skipped_image_dirs.values()),
                            "skipped_image_dirs": dict(sorted(skipped_image_dirs.items())),
                        },
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                )
            elif skipped_media_path.is_file():
                skipped_media_path.unlink(missing_ok=True)

        index_summary = None
        if rebuild:
            from .indexer import rebuild_index
            index_summary = rebuild_index(cfg.root)

        out = {
            "vault": cfg.name,
            "scanned": len(candidates),
            "up_to_date": up_to_date,
            "converted": converted,
            "preserved": preserved,
            "failed": failed_now,
            "total_failed_recorded": len(failed_map),
            "offline_mounts": offline_mounts,
            "index": index_summary,
        }
        if cfg.sources_path is not None and not candidates and len(manifest) >= 5:
            out["warning"] = (
                f"External source directory '{cfg.sources_root}' currently returned 0 readable files "
                "(possible unmounted volume); existing mirrors and index were preserved."
            )
        return out
