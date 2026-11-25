"""Versioned immutable index builder: table-aware sub-chunking, dedup & FTS5.

Produces ``<mirrors>/.index/gen-<id>/bm25.sqlite`` + JSONL artifacts and
atomically swaps ``current.json`` so active readers experience zero-downtime
hot reloads.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
from pathlib import Path, PurePosixPath

from .config import VaultConfig
from .freshness import clean_source_ref, compute_wiki_freshness, parse_wiki_frontmatter, split_frontmatter
from .policy import POLICY_VERSION, mirror_skip_reason
from .search import (
    INDEX_SCHEMA_VERSION,
    build_fts_sqlite,
    is_excluded_source,
    load_exclude_patterns,
    refresh_wiki_fts_sqlite,
)
from .state_lock import atomic_write_bytes, atomic_write_text, file_lock, replace_with_retry

SCHEMA_VERSION = INDEX_SCHEMA_VERSION
MAX_SECTION_LINES = 400
TARGET_CHUNK_LINES = 130
KEEP_GENERATIONS = 2
MAX_SIDECAR_BYTES = 16 * 1024 * 1024

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_COPY_SUFFIX_RE = re.compile(r"(?:\s*[-_]?\s*(?:\(\d+\)|copy|副本))+$", re.I)
_HTML_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")


def _atomic_write_text(path: Path, text: str) -> None:
    atomic_write_text(path, text)


def _stream_write_jsonl(path: Path, rows: list[dict]) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    h = hashlib.sha256()
    total_bytes = 0
    with tmp.open("wb") as fh:
        for r in rows:
            raw = (json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            fh.write(raw)
            h.update(raw)
            total_bytes += len(raw)
    replace_with_retry(tmp, path)
    return {"lines": len(rows), "bytes": total_bytes, "sha256": h.hexdigest()}


def _is_table_line(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.endswith("|") and len(s) >= 3


def _is_table_sep(line: str) -> bool:
    s = line.strip()
    if not (s.startswith("|") and s.endswith("|")):
        return False
    cells = [c.strip() for c in s.strip("|").split("|")]
    return bool(cells) and all(bool(re.match(r"^:?-{2,}:?$", c)) for c in cells)


def _extract_first_table_header(lines: list[str]) -> str:
    for i in range(len(lines) - 1):
        if _is_table_line(lines[i]) and _is_table_sep(lines[i + 1]):
            return f"{lines[i].strip()}\n{lines[i + 1].strip()}"
    return ""


def _trim_section_lines(lines: list[str], base_start: int) -> tuple[int, int, str]:
    """Strip leading/trailing blank lines while adjusting 1-based line offsets accurately."""
    s_idx = 0
    e_idx = len(lines)
    while s_idx < e_idx and not lines[s_idx].strip():
        s_idx += 1
    while e_idx > s_idx and not lines[e_idx - 1].strip():
        e_idx -= 1
    trimmed = lines[s_idx:e_idx]
    if not trimmed:
        return base_start, base_start, ""
    start_line = base_start + s_idx
    end_line = start_line + len(trimmed) - 1
    return start_line, end_line, "\n".join(trimmed).strip()


def split_markdown_sections(raw_text: str) -> list[dict]:
    """Split Markdown into table-aware sections (`<= MAX_SECTION_LINES` lines each).

    Line numbers (`line_start`, `line_end`) are 1-based over ``raw_text`` (including frontmatter)
    so ``wiki_read(start=line_start, end=line_end)`` aligns to the exact file lines.
    """
    all_lines = raw_text.splitlines()
    fm_lines = 0
    if raw_text.startswith("---\n") or raw_text.startswith("---\r\n"):
        for idx in range(1, len(all_lines)):
            if all_lines[idx].strip() == "---":
                fm_lines = idx + 1
                break

    raw_sections: list[tuple[str, int, int, list[str]]] = []
    heading_stack: list[tuple[int, str]] = []
    cur_heading = ""
    cur_start = fm_lines + 1
    cur_buf: list[str] = []

    for idx in range(fm_lines, len(all_lines)):
        lineno = idx + 1
        line = all_lines[idx]
        m = _HEADING_RE.match(line)
        if m:
            if any(x.strip() for x in cur_buf):
                raw_sections.append((cur_heading, cur_start, lineno - 1, cur_buf))
            lvl = len(m.group(1))
            htxt = m.group(2).strip()
            while heading_stack and heading_stack[-1][0] >= lvl:
                heading_stack.pop()
            heading_stack.append((lvl, htxt))
            cur_heading = " > ".join(h for _, h in heading_stack)
            cur_start = lineno
            cur_buf = [line]
        else:
            cur_buf.append(line)

    if any(x.strip() for x in cur_buf):
        raw_sections.append((cur_heading, cur_start, len(all_lines), cur_buf))

    chunks: list[dict] = []
    for hpath, s_line, e_line, lines in raw_sections:
        sec_table_hdr = _extract_first_table_header(lines)
        if len(lines) <= TARGET_CHUNK_LINES:
            t_start, t_end, t_text = _trim_section_lines(lines, s_line)
            if t_text:
                chunks.append({
                    "heading_path": hpath,
                    "table_header": sec_table_hdr,
                    "line_start": t_start,
                    "line_end": t_end,
                    "text": t_text,
                })
            continue

        # Sub-chunk long sections at blank lines or complete table row boundaries
        sub_start_offset = 0
        active_tbl_hdr = ""
        i = 0
        while i < len(lines):
            if i + 1 < len(lines) and _is_table_line(lines[i]) and _is_table_sep(lines[i + 1]):
                active_tbl_hdr = f"{lines[i].strip()}\n{lines[i + 1].strip()}"
            elif lines[i].strip() and not _is_table_line(lines[i]):
                if i > 0 and not lines[i - 1].strip():
                    active_tbl_hdr = ""

            span = i - sub_start_offset + 1
            should_cut = False
            if span >= MAX_SECTION_LINES:
                should_cut = True
            elif span >= TARGET_CHUNK_LINES:
                if not lines[i].strip():
                    should_cut = True
                elif _is_table_line(lines[i]) and not _is_table_sep(lines[i]):
                    should_cut = True

            if should_cut and i < len(lines) - 1:
                slice_lines = lines[sub_start_offset: i + 1]
                if any(x.strip() for x in slice_lines):
                    t_start, t_end, t_text = _trim_section_lines(slice_lines, s_line + sub_start_offset)
                    if t_text:
                        hdr_for_chunk = _extract_first_table_header(slice_lines) or active_tbl_hdr or sec_table_hdr
                        chunks.append({
                            "heading_path": hpath,
                            "table_header": hdr_for_chunk if any(_is_table_line(x) for x in slice_lines) else "",
                            "line_start": t_start,
                            "line_end": t_end,
                            "text": t_text,
                        })
                sub_start_offset = i + 1
            i += 1

        if sub_start_offset < len(lines):
            tail = lines[sub_start_offset:]
            if any(x.strip() for x in tail):
                t_start, t_end, t_text = _trim_section_lines(tail, s_line + sub_start_offset)
                if t_text:
                    hdr_for_tail = _extract_first_table_header(tail) or active_tbl_hdr or sec_table_hdr
                    chunks.append({
                        "heading_path": hpath,
                        "table_header": hdr_for_tail if any(_is_table_line(x) for x in tail) else "",
                        "line_start": t_start,
                        "line_end": t_end,
                        "text": t_text,
                    })

    return [c for c in chunks if c["text"]]


def _canonical_doc_priority(doc: dict) -> tuple[int, int, str]:
    src = doc.get("source") or ""
    stem = PurePosixPath(src).stem
    is_copy_named = 1 if _COPY_SUFFIX_RE.search(stem) else 0
    return (is_copy_named, len(src), src)


def assign_duplicates(docs: list[dict]) -> tuple[int, int]:
    """Fold exact (`source_hash`) and near-duplicate (`copy / (1)` stem + <=1% char delta) docs."""
    by_hash: dict[str, list[dict]] = {}
    for d in docs:
        sh = d.get("source_hash")
        if isinstance(sh, str) and len(sh) >= 16:
            by_hash.setdefault(sh, []).append(d)

    dup_docs = 0
    dup_groups = 0
    for group in by_hash.values():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=_canonical_doc_priority)
        primary = ordered[0]
        primary.setdefault("duplicates", [])
        dup_groups += 1
        for replica in ordered[1:]:
            replica["duplicate_of"] = primary["source"]
            primary["duplicates"].append(replica["source"])
            dup_docs += 1

    # Near-duplicate pass for same directory + copy-stripped stem + <=1% char count
    by_norm_stem: dict[tuple[str, str, str], list[dict]] = {}
    for d in docs:
        if d.get("duplicate_of"):
            continue
        p = PurePosixPath(d["source"])
        norm_stem = _COPY_SUFFIX_RE.sub("", p.stem).strip().lower()
        key = (str(p.parent), norm_stem, p.suffix.lower())
        by_norm_stem.setdefault(key, []).append(d)

    for group in by_norm_stem.values():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=_canonical_doc_priority)
        primary = ordered[0]
        p_chars = int(primary.get("chars") or 0)
        if p_chars <= 0:
            continue
        matched_replicas = []
        for cand in ordered[1:]:
            c_chars = int(cand.get("chars") or 0)
            if abs(c_chars - p_chars) / max(p_chars, 1) <= 0.01:
                cand["duplicate_of"] = primary["source"]
                matched_replicas.append(cand["source"])
        if matched_replicas:
            primary.setdefault("duplicates", []).extend(matched_replicas)
            dup_docs += len(matched_replicas)
            dup_groups += 1

    return dup_docs, dup_groups


def _compute_primary_signature(cfg: VaultConfig) -> str:
    """Hash schema, config, manifests, and all Layer-2 mirrors (independent of Layer-3 `wiki/*.md`)."""
    h = hashlib.sha256()
    h.update(f"schema={SCHEMA_VERSION};policy={POLICY_VERSION};".encode("utf-8"))
    h.update(json.dumps(cfg.config_signature_dict(), sort_keys=True).encode("utf-8"))

    for meta_file in (
        cfg.mirrors_root / ".manifest.json",
        cfg.mirrors_root / ".failed.json",
        cfg.mirrors_root / ".skipped_media.json",
    ):
        if meta_file.is_file():
            h.update(meta_file.read_bytes())

    if cfg.mirrors_root.is_dir():
        for mp in sorted(cfg.mirrors_root.rglob("*.md")):
            rel = mp.relative_to(cfg.mirrors_root).as_posix()
            if rel.startswith(".") or rel == "INDEX.md" or rel == "索引.md":
                continue
            try:
                st = mp.stat()
                h.update(f"M:{rel}:{st.st_size}:{st.st_mtime_ns}\n".encode("utf-8"))
            except OSError:
                pass

    return h.hexdigest()


def _compute_input_signature(cfg: VaultConfig, *, primary_sig: str | None = None) -> str:
    h = hashlib.sha256()
    p_sig = primary_sig if primary_sig is not None else _compute_primary_signature(cfg)
    h.update(f"primary={p_sig};".encode("utf-8"))

    if cfg.wiki_root.is_dir():
        for wp in sorted(cfg.wiki_root.glob("*.md")):
            try:
                st = wp.stat()
                h.update(f"W:{wp.name}:{st.st_size}:{st.st_mtime_ns}\n".encode("utf-8"))
            except OSError:
                pass

    return h.hexdigest()


def _collect_wiki_sections(cfg: VaultConfig, indexed_hashes: dict[str, str]) -> tuple[list[dict], int]:
    """Scan Layer-3 `wiki/*.md` pages and return `(wiki_sections, wiki_doc_count)`."""
    wiki_sections: list[dict] = []
    wiki_doc_count = 0
    if cfg.wiki_root.is_dir():
        for wp in sorted(cfg.wiki_root.glob("*.md")):
            if wp.is_symlink():
                continue
            wiki_doc_count += 1
            raw_w = wp.read_text(encoding="utf-8", errors="replace")
            fm_w, _ = split_frontmatter(raw_w)
            w_meta = parse_wiki_frontmatter(fm_w)
            title = w_meta["title"] or wp.stem
            norm_refs = [
                cfg.normalize_source_ref(cleaned)
                for r in w_meta["sources"]
                if isinstance(r, str)
                for cleaned in (clean_source_ref(r),)
                if cfg.is_source_ref(cleaned)
            ]
            fresh_state = compute_wiki_freshness(
                norm_refs, w_meta["source_hashes"], indexed_hashes, cfg=cfg
            )
            cleaned_raw = _HTML_COMMENT_RE.sub("", raw_w)
            w_chunks = split_markdown_sections(cleaned_raw)
            for wc in w_chunks:
                wiki_sections.append({
                    "source": f"{cfg.wiki_prefix}{wp.name}",
                    "title": title,
                    "heading": wc["heading_path"] or title,
                    "table_header": wc["table_header"],
                    "line_start": wc["line_start"],
                    "line_end": wc["line_end"],
                    "freshness": fresh_state,
                    "sources": norm_refs,
                    "text": wc["text"],
                })
    return wiki_sections, wiki_doc_count


def _link_or_copy(src: Path, dst: Path) -> None:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _prune_old_generations(index_root: Path, gen_id: str) -> None:
    deferred = set(_sweep_trashed_generations(index_root, gen_id))
    gens = sorted(
        (p for p in index_root.iterdir() if p.is_dir() and p.name.startswith("gen-")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    kept = 0
    for g in gens:
        if g.name == gen_id or (g.name not in deferred and kept < KEEP_GENERATIONS):
            if g.name not in deferred:
                kept += 1
            continue
        try:
            shutil.rmtree(g)
            deferred.discard(g.name)
        except OSError:
            # Windows [WinError 32]: open bm25.sqlite read handle still draining; defer to .trash.json
            deferred.add(g.name)

    trash_file = index_root / ".trash.json"
    if deferred:
        _atomic_write_text(trash_file, json.dumps(sorted(deferred), indent=2) + "\n")
    elif trash_file.is_file():
        try:
            trash_file.unlink(missing_ok=True)
        except OSError:
            pass


MANAGED_MARKER = ".wctx-managed"


def _write_managed_marker(index_root: Path) -> None:
    """Mark `.index/` as owned by wiki-context.

    Vault-local legacy tooling (``scripts/wiki_index.py`` / ``oxr_sync.py``) checks this marker
    and delegates to ``wctx index`` instead of publishing an incompatible schema-3 generation.
    """
    marker = index_root / MANAGED_MARKER
    if marker.is_file():
        return
    try:
        _atomic_write_text(
            marker,
            json.dumps(
                {
                    "generator": "wiki-context",
                    "schema_version": SCHEMA_VERSION,
                    "note": "Index is owned by wiki-context; rebuild with `wctx index`, never with legacy scripts.",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
    except OSError:
        pass


def _sweep_trashed_generations(index_root: Path, active_gen: str) -> list[str]:
    """Retry removing deferred `.trash.json` generations once old SQLite handles release."""
    trash_file = index_root / ".trash.json"
    if not trash_file.is_file():
        return []
    try:
        items = json.loads(trash_file.read_text(encoding="utf-8"))
        if not isinstance(items, list):
            items = []
    except (OSError, ValueError):
        items = []

    remaining: list[str] = []
    for name in items:
        if not isinstance(name, str) or not name or "/" in name or ".." in name:
            continue
        if name == active_gen:
            continue
        gpath = index_root / name
        if not gpath.exists():
            continue
        try:
            shutil.rmtree(gpath)
        except OSError:
            remaining.append(name)

    if remaining:
        _atomic_write_text(trash_file, json.dumps(sorted(set(remaining)), indent=2) + "\n")
    else:
        try:
            trash_file.unlink(missing_ok=True)
        except OSError:
            pass
    return remaining


def rebuild_index(vault: Path | str, *, force: bool = False) -> dict:
    """Build immutable `gen-<id>/bm25.sqlite` + JSONL snapshot and atomically swap `current.json`."""
    cfg = VaultConfig.load(vault)
    cfg.index_root.mkdir(parents=True, exist_ok=True)

    lock_path = cfg.index_root / ".build.lock"
    with file_lock(lock_path, blocking=True, busy_message="Another index build is running on this vault"):

        primary_sig = _compute_primary_signature(cfg)
        sig = _compute_input_signature(cfg, primary_sig=primary_sig)
        cur_path = cfg.index_root / "current.json"
        cur_meta: dict | None = None
        if not force and cur_path.is_file():
            try:
                loaded_cur = json.loads(cur_path.read_text(encoding="utf-8"))
                if isinstance(loaded_cur, dict):
                    cur_meta = loaded_cur
                gen_id = (cur_meta or {}).get("generation_id", "")
                if (
                    cur_meta is not None
                    and cur_meta.get("input_signature") == sig
                    and gen_id
                    and (cfg.index_root / gen_id / "bm25.sqlite").is_file()
                    and (cfg.index_root / "summary.json").is_file()
                ):
                    summary = json.loads((cfg.index_root / "summary.json").read_text(encoding="utf-8"))
                    return {**summary, "noop": True}
            except (OSError, ValueError):
                cur_meta = None

        # Fast incremental path: when only Layer-3 `wiki/*.md` changed and all Layer-2 mirrors are untouched,
        # reuse `docs/pages/sections.jsonl` and clone `bm25.sqlite` while rebuilding ONLY the 4 Wiki tables.
        if (
            not force
            and cur_meta is not None
            and cur_meta.get("schema_version") == SCHEMA_VERSION
            and cur_meta.get("primary_signature") == primary_sig
        ):
            prev_gen_id = str(cur_meta.get("generation_id") or "")
            prev_gen_dir = cfg.index_root / prev_gen_id if prev_gen_id else None
            prev_fstats = cur_meta.get("file_stats") if isinstance(cur_meta.get("file_stats"), dict) else {}
            if (
                prev_gen_dir is not None
                and (prev_gen_dir / "bm25.sqlite").is_file()
                and (prev_gen_dir / "summary.json").is_file()
                and all((prev_gen_dir / f).is_file() and f in prev_fstats for f in ("docs.jsonl", "pages.jsonl", "sections.jsonl"))
            ):
                try:
                    indexed_hashes: dict[str, str] = {}
                    with (prev_gen_dir / "docs.jsonl").open("r", encoding="utf-8") as dfh:
                        for raw_ln in dfh:
                            if not raw_ln.strip():
                                continue
                            d_row = json.loads(raw_ln)
                            if d_row.get("source") and d_row.get("source_hash"):
                                indexed_hashes[d_row["source"]] = d_row["source_hash"]
                    prev_summary = json.loads((prev_gen_dir / "summary.json").read_text(encoding="utf-8"))
                    wiki_sections, wiki_doc_count = _collect_wiki_sections(cfg, indexed_hashes)
                    exclude_pats = load_exclude_patterns(cfg.root)
                    wiki_fts_rows = [
                        w for w in wiki_sections
                        if not is_excluded_source(w["source"], exclude_pats)
                        and not any(is_excluded_source(r, exclude_pats) for r in w["sources"])
                    ]

                    ts_str = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    gen_id = f"gen-{ts_str}-{sig[:8]}"
                    gen_dir = cfg.index_root / gen_id
                    while gen_dir.exists():
                        gen_id = f"gen-{ts_str}-{sig[:8]}-{secrets.token_hex(3)}"
                        gen_dir = cfg.index_root / gen_id
                    gen_dir.mkdir(parents=True, exist_ok=True)

                    for fname in ("docs.jsonl", "pages.jsonl", "sections.jsonl"):
                        _link_or_copy(prev_gen_dir / fname, gen_dir / fname)
                    wiki_stat = _stream_write_jsonl(gen_dir / "wiki_sections.jsonl", wiki_sections)

                    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
                    summary = {
                        **prev_summary,
                        "schema_version": SCHEMA_VERSION,
                        "generator": "wiki-context",
                        "generation_id": gen_id,
                        "generated_at": now_iso,
                        "wiki_docs": wiki_doc_count,
                        "wiki_sections": len(wiki_sections),
                    }
                    _atomic_write_text(gen_dir / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
                    refresh_wiki_fts_sqlite(prev_gen_dir / "bm25.sqlite", gen_dir / "bm25.sqlite", wiki_fts_rows)

                    for fname in ("wiki_sections.jsonl", "summary.json"):
                        dst = cfg.index_root / fname
                        tmp_dst = cfg.index_root / f".{fname}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
                        shutil.copy2(gen_dir / fname, tmp_dst)
                        replace_with_retry(tmp_dst, dst)
                    for fname in ("docs.jsonl", "pages.jsonl", "sections.jsonl"):
                        dst = cfg.index_root / fname
                        if not dst.is_file():
                            tmp_dst = cfg.index_root / f".{fname}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
                            shutil.copy2(gen_dir / fname, tmp_dst)
                            replace_with_retry(tmp_dst, dst)

                    current_payload = {
                        "schema_version": SCHEMA_VERSION,
                        "generator": "wiki-context",
                        "generation_id": gen_id,
                        "generated_at": now_iso,
                        "input_signature": sig,
                        "primary_signature": primary_sig,
                        "file_stats": {
                            "docs.jsonl": prev_fstats["docs.jsonl"],
                            "pages.jsonl": prev_fstats["pages.jsonl"],
                            "sections.jsonl": prev_fstats["sections.jsonl"],
                            "wiki_sections.jsonl": wiki_stat,
                        },
                    }
                    _atomic_write_text(cur_path, json.dumps(current_payload, ensure_ascii=False, indent=2) + "\n")
                    _write_managed_marker(cfg.index_root)
                    _prune_old_generations(cfg.index_root, gen_id)
                    return {**summary, "wiki_only_refresh": True}
                except (OSError, ValueError, KeyError, sqlite3.Error):
                    # Clean up any partial fast-path generation dir and fall back cleanly to full rebuild
                    shutil.rmtree(gen_dir, ignore_errors=True)

        manifest: dict = {}
        manifest_path = cfg.mirrors_root / ".manifest.json"
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        failed_map: dict = {}
        failed_path = cfg.mirrors_root / ".failed.json"
        if failed_path.is_file():
            try:
                failed_map = json.loads(failed_path.read_text(encoding="utf-8"))
            except ValueError:
                failed_map = {}

        docs: list[dict] = []
        pages_by_src: dict[str, list[dict]] = {}
        secs_by_src: dict[str, list[dict]] = {}
        policy_skipped: list[str] = []
        oversized_skipped = 0
        orphan_docs = 0

        seen_mirrors: set[str] = set()

        def ingest_one_mirror(mirror_rel: str, m_entry: dict | None, is_orphan: bool) -> None:
            nonlocal oversized_skipped, orphan_docs
            m_path = (cfg.mirrors_root / mirror_rel).resolve()
            if m_path.is_symlink() or cfg.mirrors_root.resolve() not in m_path.parents or not m_path.is_file():
                return
            skip = mirror_skip_reason(m_path, mirror_rel, cfg=cfg, manifest_entry=m_entry)
            orig_rel = mirror_rel[:-3] if mirror_rel.endswith(".md") else mirror_rel
            src_ref = f"{cfg.source_prefix}{orig_rel}"
            if skip is not None:
                if skip.startswith("oversized"):
                    oversized_skipped += 1
                policy_skipped.append(src_ref)
                return

            raw = m_path.read_text(encoding="utf-8", errors="replace")
            fm, body = split_frontmatter(raw)
            fm_meta: dict[str, str] = {}
            for line in fm.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm_meta[k.strip()] = v.strip().strip('"').strip("'")

            source_str = cfg.normalize_source_ref(fm_meta.get("source") or src_ref)
            source_hash = (
                (m_entry.get("hash") if isinstance(m_entry, dict) else None)
                or fm_meta.get("source_hash")
                or ""
            )
            converter = (
                (m_entry.get("converter") if isinstance(m_entry, dict) else None)
                or fm_meta.get("converter")
                or "unknown"
            )

            visual_pages: list[int] = []
            warnings: list[str] = []
            page_rows: list[dict] = []
            sidecar_path = cfg.mirrors_root / f"{orig_rel}.json"
            if sidecar_path.is_file() and not sidecar_path.is_symlink():
                try:
                    if sidecar_path.stat().st_size <= MAX_SIDECAR_BYTES:
                        sc = json.loads(sidecar_path.read_text(encoding="utf-8"))
                        visual_pages = list(sc.get("visual_pages") or [])
                        warnings = list(sc.get("warnings") or [])
                        for p in sc.get("pages") or []:
                            if not isinstance(p, dict):
                                continue
                            b_texts = [
                                b.get("content", "")
                                for b in (p.get("blocks") or [])
                                if isinstance(b, dict) and b.get("format") != "placeholder"
                            ]
                            ptxt = "\n".join(x for x in b_texts if x).strip()
                            page_rows.append({
                                "source": source_str,
                                "mirror": f"{cfg.mirror_prefix}{mirror_rel}",
                                "page_num": p.get("page_num", 0),
                                "page_label": p.get("page_label") or f"Page {int(p.get('page_num', 0)) + 1}",
                                "text": ptxt,
                            })
                except (OSError, ValueError):
                    pass

            sec_chunks = split_markdown_sections(raw)
            sec_rows = [
                {
                    "source": source_str,
                    "mirror": f"{cfg.mirror_prefix}{mirror_rel}",
                    "heading_path": c["heading_path"],
                    "table_header": c["table_header"],
                    "line_start": c["line_start"],
                    "line_end": c["line_end"],
                    "text": c["text"],
                }
                for c in sec_chunks
            ]

            if is_orphan:
                orphan_docs += 1

            docs.append({
                "source": source_str,
                "mirror": f"{cfg.mirror_prefix}{mirror_rel}",
                "source_hash": source_hash,
                "converter": converter,
                "chars": len(body),
                "lines": len(raw.splitlines()),
                "pages": len(page_rows),
                "sections": len(sec_rows),
                "visual_pages": visual_pages,
                "warnings": warnings,
                "manifest": not is_orphan,
            })
            pages_by_src[source_str] = page_rows
            secs_by_src[source_str] = sec_rows

        for src_rel, entry in sorted(manifest.items()):
            if not isinstance(entry, dict):
                continue
            m_rel = str(entry.get("mirror") or f"{src_rel}.md").lstrip("/")
            seen_mirrors.add(m_rel)
            ingest_one_mirror(m_rel, entry, is_orphan=False)

        if cfg.mirrors_root.is_dir():
            for mp in sorted(cfg.mirrors_root.rglob("*.md")):
                rel = mp.relative_to(cfg.mirrors_root).as_posix()
                if rel in seen_mirrors or rel in {"INDEX.md", "索引.md"}:
                    continue
                ingest_one_mirror(rel, None, is_orphan=True)

        dup_docs, dup_groups = assign_duplicates(docs)
        duplicate_sources = {d["source"] for d in docs if d.get("duplicate_of")}

        all_pages = [p for src, plist in pages_by_src.items() if src not in duplicate_sources for p in plist]
        all_sections = [s for src, slist in secs_by_src.items() if src not in duplicate_sources for s in slist]

        indexed_hashes = {
            d["source"]: d["source_hash"]
            for d in docs
            if d.get("source_hash")
        }

        # Pre-index Layer-3 `wiki/*.md`
        wiki_sections, wiki_doc_count = _collect_wiki_sections(cfg, indexed_hashes)

        exclude_pats = load_exclude_patterns(cfg.root)
        l2_fts_rows: list[dict] = []
        srcs_with_sec: set[str] = set()
        for s in all_sections:
            if is_excluded_source(s["source"], exclude_pats):
                continue
            srcs_with_sec.add(s["source"])
            l2_fts_rows.append({
                "kind": "section",
                "source": s["source"],
                "heading": s["heading_path"],
                "table_header": s["table_header"],
                "line_start": s["line_start"],
                "line_end": s["line_end"],
                "mirror": s["mirror"],
                "text": s["text"],
            })
        for p in all_pages:
            if is_excluded_source(p["source"], exclude_pats) or p["source"] in srcs_with_sec or not p["text"].strip():
                continue
            l2_fts_rows.append({
                "kind": "page",
                "source": p["source"],
                "heading": p["page_label"],
                "page_num": p["page_num"],
                "mirror": p["mirror"],
                "text": p["text"],
            })

        wiki_fts_rows = [
            w for w in wiki_sections
            if not is_excluded_source(w["source"], exclude_pats)
            and not any(is_excluded_source(r, exclude_pats) for r in w["sources"])
        ]

        ts_str = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        gen_id = f"gen-{ts_str}-{sig[:8]}"
        gen_dir = cfg.index_root / gen_id
        while gen_dir.exists():
            gen_id = f"gen-{ts_str}-{sig[:8]}-{secrets.token_hex(3)}"
            gen_dir = cfg.index_root / gen_id
        gen_dir.mkdir(parents=True, exist_ok=True)

        by_conv: dict[str, int] = {}
        for d in docs:
            by_conv[d["converter"]] = by_conv.get(d["converter"], 0) + 1

        skipped_media: dict = {}
        skipped_media_path = cfg.mirrors_root / ".skipped_media.json"
        if skipped_media_path.is_file():
            try:
                loaded_sm = json.loads(skipped_media_path.read_text(encoding="utf-8"))
                if isinstance(loaded_sm, dict):
                    skipped_media = loaded_sm
            except (OSError, ValueError):
                skipped_media = {}

        summary = {
            "schema_version": SCHEMA_VERSION,
            "generator": "wiki-context",
            "generation_id": gen_id,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "vault": cfg.name,
            "layout": cfg.layout,
            "docs": len(docs),
            "orphan_docs": orphan_docs,
            "duplicate_docs": dup_docs,
            "duplicate_groups": dup_groups,
            "pages": len(all_pages),
            "sections": len(all_sections),
            "wiki_docs": wiki_doc_count,
            "wiki_sections": len(wiki_sections),
            "policy_skipped": len(policy_skipped),
            "policy_skipped_sources": policy_skipped[:200],
            "skipped_image_count": int(skipped_media.get("skipped_image_count") or 0),
            "skipped_image_dirs": dict(list((skipped_media.get("skipped_image_dirs") or {}).items())[:80]),
            "oversized_skipped": oversized_skipped,
            "docs_with_visual": sum(1 for d in docs if d.get("visual_pages")),
            "failed_count": len(failed_map),
            "failed_sources": sorted(failed_map.keys())[:50],
            "by_converter": by_conv,
        }

        fstats = {
            "docs.jsonl": _stream_write_jsonl(gen_dir / "docs.jsonl", docs),
            "pages.jsonl": _stream_write_jsonl(gen_dir / "pages.jsonl", all_pages),
            "sections.jsonl": _stream_write_jsonl(gen_dir / "sections.jsonl", all_sections),
            "wiki_sections.jsonl": _stream_write_jsonl(gen_dir / "wiki_sections.jsonl", wiki_sections),
        }
        _atomic_write_text(gen_dir / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        build_fts_sqlite(gen_dir / "bm25.sqlite", l2_fts_rows, wiki_fts_rows)

        # Dual-publish root JSONL for tooling compatibility, then atomically swap current.json
        for fname in ("docs.jsonl", "pages.jsonl", "sections.jsonl", "wiki_sections.jsonl", "summary.json"):
            dst = cfg.index_root / fname
            tmp_dst = cfg.index_root / f".{fname}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
            shutil.copy2(gen_dir / fname, tmp_dst)
            replace_with_retry(tmp_dst, dst)

        current_payload = {
            "schema_version": SCHEMA_VERSION,
            "generator": "wiki-context",
            "generation_id": gen_id,
            "generated_at": summary["generated_at"],
            "input_signature": sig,
            "primary_signature": primary_sig,
            "file_stats": fstats,
        }
        _atomic_write_text(cur_path, json.dumps(current_payload, ensure_ascii=False, indent=2) + "\n")
        _write_managed_marker(cfg.index_root)
        _prune_old_generations(cfg.index_root, gen_id)

        return summary


def check_index(vault: Path | str, *, deep: bool = False) -> tuple[bool, list[str]]:
    """Verify index freshness, framing, and optional SHA-256 integrity."""
    cfg = VaultConfig.load(vault)
    msgs: list[str] = []
    cur_path = cfg.index_root / "current.json"
    sum_path = cfg.index_root / "summary.json"
    if not cur_path.is_file() or not sum_path.is_file():
        return False, ["ERROR: Index missing current.json or summary.json"]

    try:
        cur = json.loads(cur_path.read_text(encoding="utf-8"))
        summary = json.loads(sum_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return False, [f"ERROR: Corrupt index metadata JSON: {exc}"]

    ok = True
    expected_sig = _compute_input_signature(cfg)
    if cur.get("input_signature") != expected_sig:
        ok = False
        msgs.append("ERROR: Index signature is stale relative to current mirrors/wiki")

    gen_id = cur.get("generation_id", "")
    db_file = cfg.index_root / gen_id / "bm25.sqlite"
    if not gen_id or not db_file.is_file():
        ok = False
        msgs.append(f"ERROR: Missing FTS5 database {gen_id}/bm25.sqlite")

    fstats = cur.get("file_stats") or {}
    for fname in ("docs.jsonl", "pages.jsonl", "sections.jsonl", "wiki_sections.jsonl"):
        fpath = cfg.index_root / fname
        if not fpath.is_file():
            ok = False
            msgs.append(f"ERROR: Missing {fname}")
            continue
        if deep:
            h = hashlib.sha256()
            lines = 0
            with fpath.open("rb") as fh:
                for raw_line in fh:
                    h.update(raw_line)
                    if raw_line.strip():
                        json.loads(raw_line.decode("utf-8"))
                        lines += 1
            st_meta = fstats.get(fname) or {}
            if st_meta.get("lines") != lines or st_meta.get("sha256") != h.hexdigest():
                ok = False
                msgs.append(f"ERROR: Deep checksum/line mismatch on {fname}")

    msgs.append(
        f"INFO: generation={gen_id} docs={summary.get('docs', 0)} "
        f"sections={summary.get('sections', 0)} wiki_docs={summary.get('wiki_docs', 0)}"
    )
    return ok, msgs
