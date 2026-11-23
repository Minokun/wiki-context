"""KBService & stdio MCP server (`wctx serve`).

Features:
- Atomic immutable snapshot hot-reload (`KBSnapshot`) + 30s grace drain queue.
- High-precision credential scrubbing (`PRIVATE KEY`, `sk-...`, `AKIA...`, `Bearer`, `Cookie`).
- `O_NOFOLLOW` + `fstat` sandboxed `wiki_read` over mirrors, wiki, and catalog.
- Dynamic multi-vault tool description disambiguation (`[vault_name]` + domain cheatsheet).
"""

from __future__ import annotations

import base64
import datetime as dt
import errno
import itertools
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .config import VaultConfig
from .context import AccessPolicy, ContextPackEngine, Principal, SynonymBook, policy_for_vault
from .policy import mirror_skip_reason
from .search import (
    BM25Index,
    IndexUnavailableError,
    is_excluded_source,
    load_exclude_patterns,
    route,
    tokenize,
    tokenize_query,
)

MAX_READ_LINES = 400
MAX_STDIN_BYTES = 4 * 1024 * 1024
SNAPSHOT_DRAIN_SECONDS = 30.0
SUPPORTED_PROTOCOL_VERSIONS = ("2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

_SECRET_PATTERNS = (
    (
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)"),
        "[REDACTED_PRIVATE_KEY]",
    ),
    (
        re.compile(r"\bsk-(?:proj-|live-|test-)?[A-Za-z0-9_-]{16,}\b"),
        "[REDACTED_API_KEY]",
    ),
    (
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "[REDACTED_AWS_KEY]",
    ),
    (
        re.compile(r"(?i)\b(authorization\s*:\s*bearer\s+|bearer\s+)[A-Za-z0-9._\-]{16,}\b"),
        r"\1[REDACTED_TOKEN]",
    ),
    (
        re.compile(r"(?i)\b(cookie\s*:\s*)[^\r\n]{8,}"),
        r"\1[REDACTED_COOKIE]",
    ),
)


def scrub_secrets(text: str) -> str:
    if not text:
        return ""
    if os.environ.get("WCTX_SCRUB_SECRETS", "1").strip() in {"0", "false", "no"}:
        return text
    out = text
    for pat, repl in _SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out


def make_snippet(
    text: str,
    query: str,
    *,
    table_header: str = "",
    q_idf: dict[str, float] | None = None,
    max_chars: int = 600,
) -> str:
    if not text:
        return ""
    if len(text) <= max_chars:
        return scrub_secrets(text)

    q_toks, _ = tokenize_query(query)
    if not q_toks:
        return scrub_secrets(text[:max_chars]) + "…"

    hdr = (table_header or "").strip()
    hdr_cost = (len(hdr) + 1) if hdr else 0
    win_budget = max(200, max_chars - hdr_cost)
    idf_map = q_idf or {t: 1.0 for t in q_toks}

    low = text.lower()
    best_pos = 0
    best_sc = -1.0
    step = max(40, win_budget // 4)
    for start in range(0, max(1, len(text) - win_budget + 1), step):
        seg = low[start: start + win_budget]
        sc = sum(idf_map.get(t, 1.0) for t in set(q_toks) if t in seg)
        if sc > best_sc:
            best_sc = sc
            best_pos = start

    # Scrub over margin window before slicing so split tokens at boundaries are still caught
    margin = 128
    m_start = max(0, best_pos - margin)
    m_end = min(len(text), best_pos + win_budget + margin)
    scrubbed_chunk = scrub_secrets(text[m_start:m_end])
    rel_start = best_pos - m_start
    core = scrubbed_chunk[rel_start: rel_start + win_budget].strip()

    prefix = f"{hdr}\n" if (hdr and best_pos > 0 and not core.startswith(hdr[:30])) else ""
    suffix = "…" if (best_pos + win_budget) < len(text) else ""
    return prefix + core + suffix


TOOLS_SPEC: list[dict] = [
    {
        "name": "wiki_context",
        "description": (
            "One-shot deterministic knowledge context assembler (Preferred default: answers 95% of questions in 1 call). "
            "Combines synonym alignment, compiled Wiki synthesis + primary source FTS5 retrieval, table-header pinning, "
            "and missing-concept hallucination guardrails."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Full natural-language question from the user."},
                "expanded_terms": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: 2-5 canonical technical terms or acronyms corresponding to colloquial phrasing.",
                },
                "max_tokens": {
                    "type": "integer",
                    "description": "Optional token budget (auto-allocated 2600 for fact / 4800 for complex, range 1000-8000).",
                },
            },
            "required": ["question"],
        },
    },
    {
        "name": "wiki_search",
        "description": "Fine-grained BM25/FTS5 search returning primary hits (`hits`) and compiled Wiki hits (`wiki_hits`).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keywords or question."},
                "k": {"type": "integer", "description": "Max results (default 5, max 20)."},
                "kind": {
                    "type": "string",
                    "enum": ["section", "page", "wiki"],
                    "description": "Optional unit filter.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "wiki_read",
        "description": (
            "Read exact line ranges (`start`..`end`, <=400 lines) or `page_num` (0-based) from derived primary mirrors, "
            "compiled Wiki pages, or the vault catalog."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Source path (e.g. `sources/...`, `wiki/...`, `index.md`, or `史料/...`, `知识库/...`)."},
                "start": {"type": "integer", "description": "1-based start line."},
                "end": {"type": "integer", "description": "1-based inclusive end line."},
                "page_num": {"type": "integer", "description": "0-based page/slide/sheet index."},
            },
            "required": ["source"],
        },
    },
    {
        "name": "wiki_route",
        "description": "Classify whether a question is a single-lookup `fact` or multi-hop `complex` query.",
        "inputSchema": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    },
    {
        "name": "wiki_list",
        "description": "List indexed documents in the vault (with optional path substring filter).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filter": {"type": "string", "description": "Optional case-insensitive path substring."},
                "limit": {"type": "integer", "description": "Max entries (default 50, max 200)."},
            },
        },
    },
    {
        "name": "wiki_inspect_visual",
        "description": (
            "Return crisp visual diagrams, embedded Office figures, standalone images, or rendered PDF visual pages "
            "as native MCP multimodal `image` content blocks so VLM agents can visually inspect charts and schematics."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "Primary source path (e.g. `sources/specs/arch.docx` or `史料/图纸/架构图.png`).",
                },
                "asset": {
                    "type": "string",
                    "description": "Optional `asset_id` (e.g. `fig-01-diagram.png` or `page-002.png`); defaults to first visual asset.",
                },
            },
            "required": ["source"],
        },
    },
    {
        "name": "wiki_remember",
        "description": (
            "Save a domain operational finding, field troubleshooting insight, or source-anchored errata pin into Layer-4 "
            "`notes/` (`记忆/`). Never mutates read-only `sources/`; automatically inherits `PrefixACLPolicy` from `related_sources`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Self-contained operational fact, decision, or errata note."},
                "kind": {
                    "type": "string",
                    "enum": ["insight", "errata", "decision", "glossary"],
                    "description": "Use `errata` to attach a high-visibility override pin onto `related_sources`.",
                },
                "related_sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional primary source paths (e.g. `sources/specs/rfc-042.md`) for 1-hop pinning & ACL inheritance.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional topic tags.",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "wiki_forget",
        "description": "Archive/retract an obsolete or mistaken Layer-4 memory note by `note_id`.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "note_id": {"type": "string", "description": "Memory note ID (e.g. `mem-20261122-...`)."},
            },
            "required": ["note_id"],
        },
    },
    {
        "name": "wiki_status",
        "description": "Return index generation id, document/section counts, skip statistics, and coverage boundaries.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

_LEGACY_TOOL_ALIASES: dict[str, str] = {
    "kb_context": "wiki_context",
    "kb_search": "wiki_search",
    "kb_read": "wiki_read",
    "kb_route": "wiki_route",
    "kb_list": "wiki_list",
    "kb_inspect_visual": "wiki_inspect_visual",
    "kb_remember": "wiki_remember",
    "kb_forget": "wiki_forget",
    "kb_status": "wiki_status",
}


MAX_VISUAL_IMAGE_BYTES = 4 * 1024 * 1024


@dataclass
class KBSnapshot:
    generation_id: str
    mtime_key: tuple[int, int]
    index: BM25Index
    docs: list[dict]
    summary: dict
    mirror_by_source: dict[str, Path]
    pages_by_source: dict[str, list[dict]]
    duplicates_by_source: dict[str, list[str]]
    assets_by_source: dict[str, list[dict]]


class KBService:
    """Thread-safe knowledge service backing both stdio MCP and multi-user HTTP gateway."""

    def __init__(
        self,
        vault: Path | str,
        *,
        policy: AccessPolicy | None = None,
        synonym_book: SynonymBook | None = None,
    ):
        self.cfg = VaultConfig.load(vault)
        self.vault = self.cfg.root
        self.identity = (
            os.environ.get("WIKI_USER")
            or os.environ.get("WCTX_USER")
            or os.environ.get("KB_USER")
            or "local"
        ).strip()

        if self.cfg.sensitive:
            if self.identity in {"", "anonymous", "local"} or os.environ.get("WCTX_INTERNAL_MODEL") != "1":
                raise PermissionError(
                    f"Vault '{self.cfg.name}' is marked sensitive=true; explicit WCTX_USER and WCTX_INTERNAL_MODEL=1 are required"
                )

        self.policy = policy or policy_for_vault(self.cfg)
        self.exclude_patterns = load_exclude_patterns(self.vault)
        self.context_engine = ContextPackEngine(
            self.vault,
            synonym_book=synonym_book,
            policy=self.policy,
            scrub_fn=scrub_secrets,
        )

        self._snap_lock = threading.Lock()
        self._retired: list[tuple[float, KBSnapshot]] = []
        self._audit_log = (os.environ.get("WCTX_AUDIT_LOG") or os.environ.get("KB_AUDIT_LOG") or "").strip()
        self._audit_lock = threading.Lock()
        self._snapshot: KBSnapshot = self._load_snapshot()

    def close(self) -> None:
        with self._snap_lock:
            snaps = [self._snapshot, *(s for _, s in self._retired)]
            self._retired.clear()
        for s in snaps:
            s.index.close()

    def _current_mtime_key(self) -> tuple[int, int]:
        cur = self.cfg.index_root / "current.json"
        summ = self.cfg.index_root / "summary.json"
        m1 = cur.stat().st_mtime_ns if cur.is_file() else 0
        m2 = summ.stat().st_mtime_ns if summ.is_file() else 0
        return (m1, m2)

    def _load_snapshot(self) -> KBSnapshot:
        mkey = self._current_mtime_key()
        index = BM25Index(self.vault, exclude_patterns=self.exclude_patterns)
        gen_dir = index._resolve_generation_dir()

        summary: dict = {}
        sum_p = gen_dir / "summary.json"
        if sum_p.is_file():
            summary = json.loads(sum_p.read_text(encoding="utf-8"))

        docs: list[dict] = []
        mirror_by_source: dict[str, Path] = {}
        duplicates_by_source: dict[str, list[str]] = {}
        docs_p = gen_dir / "docs.jsonl"
        if docs_p.is_file():
            for line in docs_p.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                src = row.get("source") or ""
                if is_excluded_source(src, self.exclude_patterns):
                    continue
                docs.append(row)
                m_rel = self.cfg.strip_mirror_prefix(row.get("mirror") or "")
                if m_rel:
                    cand = (self.cfg.mirrors_root / m_rel).resolve()
                    if row.get("manifest") is not False or src not in mirror_by_source:
                        mirror_by_source[src] = cand
                if row.get("duplicates"):
                    duplicates_by_source[src] = list(row["duplicates"])

        pages_by_source: dict[str, list[dict]] = {}
        pages_p = gen_dir / "pages.jsonl"
        if pages_p.is_file():
            for line in pages_p.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                src = row.get("source") or ""
                if not is_excluded_source(src, self.exclude_patterns):
                    pages_by_source.setdefault(src, []).append(row)

        assets_by_source: dict[str, list[dict]] = {}
        for src, m_cand in mirror_by_source.items():
            sc_path = m_cand.with_suffix("")  # `<rel>.md` -> `<rel>`
            sc_json = Path(str(sc_path) + ".json")
            if sc_json.is_file():
                try:
                    sc_data = json.loads(sc_json.read_text(encoding="utf-8"))
                    if isinstance(sc_data, dict) and sc_data.get("assets"):
                        assets_by_source[src] = list(sc_data["assets"])
                except (OSError, ValueError):
                    pass

        gen_id = str(summary.get("generation_id") or gen_dir.name)
        return KBSnapshot(
            generation_id=gen_id,
            mtime_key=mkey,
            index=index,
            docs=docs,
            summary=summary,
            mirror_by_source=mirror_by_source,
            pages_by_source=pages_by_source,
            duplicates_by_source=duplicates_by_source,
            assets_by_source=assets_by_source,
        )

    def _get_snapshot(self) -> KBSnapshot:
        now_key = self._current_mtime_key()
        now_ts = time.monotonic()
        to_close: list[KBSnapshot] = []

        with self._snap_lock:
            still_waiting: list[tuple[float, KBSnapshot]] = []
            for exp_ts, old_s in self._retired:
                if now_ts >= exp_ts:
                    to_close.append(old_s)
                else:
                    still_waiting.append((exp_ts, old_s))
            self._retired = still_waiting

            if now_key != self._snapshot.mtime_key and now_key != (0, 0):
                try:
                    fresh = self._load_snapshot()
                    self._retired.append((now_ts + SNAPSHOT_DRAIN_SECONDS, self._snapshot))
                    self._snapshot = fresh
                except Exception:
                    # Retain last-known-good snapshot if mid-swap error occurs
                    pass
            active = self._snapshot

        for s in to_close:
            s.index.close()
        return active

    def _resolve_mirror_jailed(self, snap: KBSnapshot, source: str, rel_from_sources: str) -> Path | None:
        m_root = self.cfg.mirrors_root.resolve()
        cand = snap.mirror_by_source.get(source)
        if cand is None:
            cand = (m_root / f"{rel_from_sources}.md").resolve()
        if m_root not in cand.parents or cand.is_symlink() or not cand.is_file():
            return None
        rel_m = cand.relative_to(m_root).as_posix()
        if mirror_skip_reason(cand, rel_m, cfg=self.cfg) is not None:
            return None
        return cand

    @staticmethod
    def _read_lines_no_symlink(path: Path, start: int, end: int, max_bytes: int) -> tuple[list[str], int]:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(path), flags)
        try:
            st = os.fstat(fd)
            if st.st_size > max_bytes:
                raise ValueError(f"File exceeds max_doc_bytes ({max_bytes}B)")
            with os.fdopen(fd, "r", encoding="utf-8", errors="replace", closefd=False) as fh:
                all_lines = fh.read().splitlines()
        finally:
            os.close(fd)
        total = len(all_lines)
        if total == 0:
            return [], 0
        if start > total:
            raise ValueError(f"start line {start} exceeds file total lines ({total})")
        s_idx = max(0, start - 1)
        e_idx = min(total, end)
        return all_lines[s_idx:e_idx], total

    def build_tools_spec(self, *, slim_remote: bool = False) -> list[dict]:
        cheatsheet = self.context_engine.domain_cheatsheet()
        is_zh = self.cfg.layout == "zh"
        vault_tag = f"【{self.cfg.name}】" if is_zh else f"[{self.cfg.name}] "
        allowed = {"wiki_context", "wiki_read", "wiki_inspect_visual", "wiki_list"} if slim_remote else None
        out: list[dict] = []

        for t in TOOLS_SPEC:
            name = t["name"]
            if allowed is not None and name not in allowed:
                continue
            item = dict(t)
            desc = t["description"]
            if name == "wiki_context" and cheatsheet:
                if is_zh:
                    desc = f"{desc} 【本库领域参考】{cheatsheet}。若提问为口语，可在 expanded_terms 补充对应标准术语。"
                else:
                    desc = f"{desc} [Vault Reference] {cheatsheet}. Supply 2-5 canonical terms in `expanded_terms` when queries use slang."
            elif name == "wiki_list" and slim_remote:
                desc = (
                    "Call ONLY when the user explicitly asks to list files/documents in the vault; "
                    "for all domain questions, call `wiki_context` directly."
                )
            item["description"] = f"{vault_tag}{desc}"
            out.append(item)
        return out

    # -- Tool Implementations ----------------------------------------------- #

    def tool_context(self, args: dict, *, principal: Principal | None = None) -> dict:
        snap = self._get_snapshot()
        user = principal or Principal(uid=self.identity)
        q = str(args.get("question") or "").strip()
        if not q:
            return {"error": "question must not be empty"}
        exp = args.get("expanded_terms")
        exp_list = [str(x) for x in exp if isinstance(x, (str, int, float))] if isinstance(exp, list) else None
        max_toks = args.get("max_tokens")
        max_toks_int = int(max_toks) if isinstance(max_toks, int) else None

        return self.context_engine.build(
            snap,
            q,
            expanded_terms=exp_list,
            max_tokens=max_toks_int,
            principal=user,
            resolve_mirror_fn=lambda s, r: self._resolve_mirror_jailed(snap, s, r),
            exclude_patterns=self.exclude_patterns,
        )

    def tool_search(self, args: dict, *, principal: Principal | None = None) -> dict:
        snap = self._get_snapshot()
        user = principal or Principal(uid=self.identity)
        query = str(args.get("query") or "").strip()
        if not query:
            return {"error": "query must not be empty"}
        k = max(1, min(int(args.get("k") or 5), 20))
        kind = args.get("kind")
        if kind not in {None, "section", "page", "wiki"}:
            return {"error": f"Unsupported kind: {kind}"}

        decision = route(query)
        if kind == "wiki":
            w_raw = snap.index.search_wiki(query, k=k)
            l2_raw = []
        else:
            l2_raw = snap.index.search(query, k=k * 2, kind=kind)
            w_raw = snap.index.search_wiki(query, k=2) if kind is None else []

        hits: list[dict] = []
        for h in l2_raw:
            src = h.get("source") or ""
            if not self.policy.can_access_source(user, src):
                continue
            item = {
                "source": src,
                "kind": h.get("kind"),
                "heading": h.get("heading") or "",
                "line_start": h.get("line_start"),
                "line_end": h.get("line_end"),
                "page_num": h.get("page_num"),
                "score": h.get("score"),
                "matched": h.get("matched") or [],
                "snippet": make_snippet(
                    h.get("text") or "",
                    query,
                    table_header=h.get("table_header") or "",
                    q_idf=h.get("_q_idf"),
                ),
            }
            dups = snap.duplicates_by_source.get(src)
            if dups:
                item["also_at"] = dups[:3]
                item["duplicate_count"] = len(dups)
            hits.append(item)
            if len(hits) >= k:
                break

        wiki_hits: list[dict] = []
        for w in w_raw:
            wsrc = w.get("source") or ""
            refs = w.get("sources") or []
            if not self.policy.can_access_wiki(user, wsrc, refs):
                continue
            wiki_hits.append({
                "source": wsrc,
                "title": w.get("title") or "",
                "heading": w.get("heading") or "",
                "line_start": w.get("line_start"),
                "line_end": w.get("line_end"),
                "freshness": w.get("freshness") or "unverified",
                "score": w.get("score"),
                "snippet": make_snippet(
                    w.get("text") or "",
                    query,
                    table_header=w.get("table_header") or "",
                    q_idf=w.get("_q_idf"),
                ),
            })

        return {
            "identity": user.uid,
            "vault": self.cfg.name,
            "generation_id": snap.generation_id,
            "route": decision["mode"],
            "hits": hits,
            "wiki_hits": wiki_hits,
            "grounding_note": (
                "BM25 scores do not prove answerability; verify every claim directly against cited excerpts."
            ),
        }

    def tool_read(self, args: dict, *, principal: Principal | None = None) -> dict:
        snap = self._get_snapshot()
        user = principal or Principal(uid=self.identity)
        raw_src = str(args.get("source") or "").strip().replace("\\", "/")
        if not raw_src or ".." in raw_src.split("/"):
            return {"error": "Invalid or disallowed source path"}

        start = int(args.get("start") or 1)
        end = int(args.get("end") or (start + 119))
        if start < 1 or end < start:
            return {"error": "Invalid line range (`start` >= 1 and `end` >= `start`)"}
        if (end - start + 1) > MAX_READ_LINES:
            end = start + MAX_READ_LINES - 1

        max_b = self.cfg.max_doc_bytes
        try:
            # Branch 1: Vault catalog (`index.md` / `目录.md`)
            if raw_src in {self.cfg.catalog_file, "index.md", "目录.md"}:
                cpath = self.cfg.catalog_path.resolve()
                if self.vault.resolve() not in cpath.parents and cpath.parent != self.vault.resolve():
                    return {"error": "Catalog path outside vault"}
                if not cpath.is_file():
                    return {"error": f"Catalog {self.cfg.catalog_file} not found"}
                lines, total = self._read_lines_no_symlink(cpath, start, end, max_b)
                return {
                    "source": self.cfg.catalog_file,
                    "start": start,
                    "end": min(end, total),
                    "total_lines": total,
                    "text": scrub_secrets("\n".join(lines)),
                }

            # Branch 2: Compiled Wiki page (`wiki/*.md` or `知识库/*.md`)
            if raw_src.startswith((self.cfg.wiki_prefix, "wiki/", "知识库/")):
                for pfx in (self.cfg.wiki_prefix, "wiki/", "知识库/"):
                    if raw_src.startswith(pfx):
                        w_rel = raw_src[len(pfx):]
                        break
                else:
                    w_rel = raw_src
                if "/" in w_rel or not w_rel.endswith(".md"):
                    return {"error": "Invalid Wiki page path"}
                w_root = self.cfg.wiki_root.resolve()
                wpath = (w_root / w_rel).resolve()
                if w_root not in wpath.parents or not wpath.is_file():
                    return {"error": f"Wiki page not found: {raw_src}"}
                # Enforce Wiki ACL via indexed provenance
                w_norm = f"{self.cfg.wiki_prefix}{w_rel}"
                w_refs: list[str] = []
                for wu in snap.index.wiki_units:
                    if wu.get("source") == w_norm:
                        w_refs = wu.get("sources") or []
                        break
                if snap.index.backend_name == "sqlite" and not w_refs:
                    con = snap.index._con
                    if con is not None:
                        row = con.execute("SELECT sources_json FROM wiki_units WHERE source = ? LIMIT 1", (w_norm,)).fetchone()
                        if row and row[0]:
                            w_refs = json.loads(row[0])
                if not self.policy.can_access_wiki(user, w_norm, w_refs):
                    return {"error": f"Access denied (无权访问): {raw_src}"}
                lines, total = self._read_lines_no_symlink(wpath, start, end, max_b)
                return {
                    "source": w_norm,
                    "start": start,
                    "end": min(end, total),
                    "total_lines": total,
                    "text": scrub_secrets("\n".join(lines)),
                }

            # Branch 3: Primary source mirror (`sources/...` or `史料/...`)
            if not self.cfg.is_source_ref(raw_src):
                return {"error": f"Source must start with '{self.cfg.source_prefix}', '{self.cfg.wiki_prefix}', or '{self.cfg.catalog_file}'"}

            norm_src = self.cfg.normalize_source_ref(raw_src)
            if is_excluded_source(norm_src, self.exclude_patterns):
                return {"error": f"Source excluded by vault policy: {norm_src}"}
            if not self.policy.can_access_source(user, norm_src):
                return {"error": f"Access denied (无权访问): {norm_src}"}

            page_num = args.get("page_num")
            if page_num is not None:
                p_int = int(page_num)
                plist = snap.pages_by_source.get(norm_src) or []
                for p in plist:
                    if p.get("page_num") == p_int:
                        return {
                            "source": norm_src,
                            "page_num": p_int,
                            "page_label": p.get("page_label"),
                            "text": scrub_secrets(p.get("text") or ""),
                        }
                return {"error": f"Page {p_int} not found for {norm_src}"}

            rel_s = self.cfg.strip_source_prefix(norm_src)
            mpath = self._resolve_mirror_jailed(snap, norm_src, rel_s)
            if mpath is None:
                return {"error": f"Mirror not found for {norm_src}"}
            lines, total = self._read_lines_no_symlink(mpath, start, end, max_b)
            return {
                "source": norm_src,
                "start": start,
                "end": min(end, total),
                "total_lines": total,
                "text": scrub_secrets("\n".join(lines)),
            }
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                return {"error": "Symlinks are forbidden"}
            return {"error": f"Read error: {exc}"}
        except ValueError as exc:
            return {"error": str(exc)}

    def tool_route(self, args: dict) -> dict:
        return route(str(args.get("question") or ""))

    def tool_list(self, args: dict, *, principal: Principal | None = None) -> dict:
        snap = self._get_snapshot()
        user = principal or Principal(uid=self.identity)
        flt = str(args.get("filter") or "").strip().lower()
        limit = max(1, min(int(args.get("limit") or 50), 200))
        items: list[dict] = []
        for d in snap.docs:
            if d.get("duplicate_of"):
                continue
            src = d.get("source") or ""
            if not self.policy.can_access_source(user, src):
                continue
            if flt and flt not in src.lower():
                continue
            items.append({
                "source": src,
                "converter": d.get("converter"),
                "sections": d.get("sections", 0),
                "pages": d.get("pages", 0),
            })
            if len(items) >= limit:
                break
        return {"vault": self.cfg.name, "count": len(items), "docs": items}

    def tool_status(self, *, principal: Principal | None = None) -> dict:
        snap = self._get_snapshot()
        user = principal or Principal(uid=self.identity)
        s = snap.summary
        return {
            "identity": user.uid,
            "vault": self.cfg.name,
            "layout": self.cfg.layout,
            "generation_id": snap.generation_id,
            "backend": snap.index.backend_name,
            "docs": s.get("docs", len(snap.docs)),
            "pages": s.get("pages", 0),
            "sections": s.get("sections", 0),
            "wiki_docs": s.get("wiki_docs", 0),
            "wiki_sections": s.get("wiki_sections", 0),
            "policy_skipped": s.get("policy_skipped", 0),
            "oversized_skipped": s.get("oversized_skipped", 0),
            "orphan_docs": s.get("orphan_docs", 0),
            "docs_with_visual": s.get("docs_with_visual", 0),
            "active_memory_notes": len(self.context_engine.memory_store.list_notes(status="active")),
            "failed_count": s.get("failed_count", 0),
            "failed_sources": (s.get("failed_sources") or [])[:20],
            "generated_at": s.get("generated_at"),
        }

    def tool_inspect_visual(self, args: dict, *, principal: Principal | None = None) -> tuple[list[dict] | dict, bool]:
        snap = self._get_snapshot()
        user = principal or Principal(uid=self.identity)
        raw_src = str(args.get("source") or "").strip().replace("\\", "/")
        if not raw_src or ".." in raw_src.split("/"):
            return {"error": "Invalid or disallowed source path"}, True
        if not self.cfg.is_source_ref(raw_src):
            return {"error": f"Source must start with '{self.cfg.source_prefix}'"}, True

        norm_src = self.cfg.normalize_source_ref(raw_src)
        if is_excluded_source(norm_src, self.exclude_patterns):
            return {"error": f"Source excluded by vault policy: {norm_src}"}, True
        if not self.policy.can_access_source(user, norm_src):
            return {"error": f"Access denied (无权访问): {norm_src}"}, True

        rel_s = self.cfg.strip_source_prefix(norm_src)
        assets = list(snap.assets_by_source.get(norm_src) or ())

        # Also support direct standalone images inside `sources/` even if not yet mirrored
        s_root = self.cfg.sources_root.resolve()
        raw_file = (s_root / rel_s).resolve()
        if not assets and s_root in raw_file.parents and raw_file.is_file() and not raw_file.is_symlink():
            if raw_file.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
                assets = [{
                    "asset_id": raw_file.name,
                    "name": raw_file.name,
                    "source_direct": True,
                    "mime": mime_map.get(raw_file.suffix.lower(), "image/png"),
                }]

        if not assets:
            return {"error": f"No extractable visual assets found for {norm_src}"}, True

        req_asset = str(args.get("asset") or "").strip()
        chosen = assets[0]
        if req_asset:
            matched = next((a for a in assets if a.get("asset_id") == req_asset or a.get("name") == req_asset), None)
            if matched is None:
                return {
                    "error": f"Visual asset '{req_asset}' not found for {norm_src}",
                    "available_assets": [a.get("asset_id") for a in assets],
                }, True
            chosen = matched

        if chosen.get("source_direct"):
            target_img = raw_file
            jail_root = s_root
        else:
            aid = str(chosen.get("asset_id") or "")
            if not aid or "/" in aid or ".." in aid:
                return {"error": "Invalid visual asset id"}, True
            jail_root = (self.cfg.mirrors_root / ".assets").resolve()
            target_img = (jail_root / rel_s / aid).resolve()

        if jail_root not in target_img.parents or not target_img.is_file():
            return {"error": f"Visual asset file missing on disk for {norm_src}"}, True

        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(str(target_img), flags)
            try:
                st = os.fstat(fd)
                if st.st_size > MAX_VISUAL_IMAGE_BYTES:
                    return {"error": f"Visual asset exceeds {MAX_VISUAL_IMAGE_BYTES} bytes"}, True
                img_bytes = os.read(fd, st.st_size)
            finally:
                os.close(fd)
        except OSError as exc:
            return {"error": f"Failed to read visual asset: {exc}"}, True

        mime = str(chosen.get("mime") or "image/png")
        meta_summary = {
            "source": norm_src,
            "asset": chosen.get("asset_id"),
            "mime": mime,
            "bytes": len(img_bytes),
            "available_assets": [a.get("asset_id") for a in assets],
        }
        blocks = [
            {"type": "text", "text": json.dumps(meta_summary, ensure_ascii=False)},
            {"type": "image", "data": base64.b64encode(img_bytes).decode("ascii"), "mimeType": mime},
        ]
        return blocks, False

    def tool_remember(self, args: dict, *, principal: Principal | None = None, is_remote: bool = False) -> dict:
        user = principal or Principal(uid=self.identity)
        if is_remote and os.environ.get("WCTX_ALLOW_REMOTE_REMEMBER") != "1":
            if not ({"admin", "all", "writer"} & set(user.groups)):
                return {"error": "Access denied: remote memory write requires admin/writer group or WCTX_ALLOW_REMOTE_REMEMBER=1"}
        text = str(args.get("text") or "").strip()
        if not text:
            return {"error": "text must not be empty"}
        kind = str(args.get("kind") or "insight").strip().lower()
        rel_srcs = [str(x) for x in (args.get("related_sources") or ()) if str(x).strip()]
        # Verify caller has read access to every source they anchor a note to
        for s in rel_srcs:
            norm_s = self.cfg.normalize_source_ref(s)
            if not self.policy.can_access_source(user, norm_s):
                return {"error": f"Access denied to related_source: {norm_s}"}
        tags = [str(x) for x in (args.get("tags") or ()) if str(x).strip()]
        try:
            note = self.context_engine.memory_store.remember(
                text,
                kind=kind,
                related_sources=rel_srcs,
                tags=tags,
                author=user.uid,
            )
        except ValueError as exc:
            return {"error": str(exc)}
        return {
            "saved": True,
            "note_id": note.note_id,
            "kind": note.kind,
            "related_sources": list(note.related_sources),
            "file": f"{self.cfg.notes_dir}/{note.file_path.name}",
        }

    def tool_forget(self, args: dict, *, principal: Principal | None = None, is_remote: bool = False) -> dict:
        user = principal or Principal(uid=self.identity)
        if is_remote and os.environ.get("WCTX_ALLOW_REMOTE_REMEMBER") != "1":
            if not ({"admin", "all", "writer"} & set(user.groups)):
                return {"error": "Access denied: remote memory archival requires admin/writer group"}
        nid = str(args.get("note_id") or "").strip()
        if not nid:
            return {"error": "note_id must not be empty"}
        ok = self.context_engine.memory_store.update_status(nid, "archived")
        if not ok:
            return {"error": f"Memory note not found: {nid}"}
        return {"archived": True, "note_id": nid}

    def dispatch_tool(
        self, name: str, arguments: dict | None, *, principal: Principal | None = None, is_remote: bool = False
    ) -> tuple[str | list[dict], bool]:
        """Execute an MCP tool and return ``(text_or_blocks, is_error)``."""
        args = dict(arguments) if isinstance(arguments, dict) else {}
        args.pop("user", None)  # Never trust client-supplied identity

        t0 = time.perf_counter()
        user = principal or Principal(uid=self.identity)
        is_err = False
        payload_out: str | list[dict]

        canonical = _LEGACY_TOOL_ALIASES.get(name, name)
        if canonical == "wiki_context":
            out = self.tool_context(args, principal=user)
            if "error" in out:
                is_err = True
                payload_out = json.dumps(out, ensure_ascii=False)
            else:
                payload_out = out["context_markdown"]
        elif canonical == "wiki_search":
            out = self.tool_search(args, principal=user)
            is_err = "error" in out
            payload_out = json.dumps(out, ensure_ascii=False)
        elif canonical == "wiki_read":
            out = self.tool_read(args, principal=user)
            is_err = "error" in out
            payload_out = json.dumps(out, ensure_ascii=False)
        elif canonical == "wiki_inspect_visual":
            vis_res, is_err = self.tool_inspect_visual(args, principal=user)
            if isinstance(vis_res, list):
                out = {"asset_blocks": len(vis_res)}
                payload_out = vis_res
            else:
                out = vis_res
                payload_out = json.dumps(out, ensure_ascii=False)
        elif canonical == "wiki_route":
            out = self.tool_route(args)
            payload_out = json.dumps(out, ensure_ascii=False)
        elif canonical == "wiki_list":
            out = self.tool_list(args, principal=user)
            payload_out = json.dumps(out, ensure_ascii=False)
        elif canonical == "wiki_remember":
            out = self.tool_remember(args, principal=user, is_remote=is_remote)
            is_err = "error" in out
            payload_out = json.dumps(out, ensure_ascii=False)
        elif canonical == "wiki_forget":
            out = self.tool_forget(args, principal=user, is_remote=is_remote)
            is_err = "error" in out
            payload_out = json.dumps(out, ensure_ascii=False)
        elif canonical == "wiki_status":
            out = self.tool_status(principal=user)
            payload_out = json.dumps(out, ensure_ascii=False)
        else:
            out = {"error": f"Unknown tool: {name}"}
            is_err = True
            payload_out = json.dumps(out, ensure_ascii=False)

        self._write_audit(user.uid, name, args, out, round((time.perf_counter() - t0) * 1000, 2))
        return payload_out, is_err

    def _write_audit(self, uid: str, tool: str, args: dict, out: dict, elapsed_ms: float) -> None:
        if not self._audit_log:
            return
        record = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "uid": uid,
            "vault": self.cfg.name,
            "tool": tool,
            "query": args.get("question") or args.get("query") or args.get("source") or "",
            "elapsed_ms": elapsed_ms,
            "error": out.get("error") if isinstance(out, dict) else None,
            "missing_concepts": out.get("missing_concepts") if isinstance(out, dict) else None,
        }
        try:
            p = Path(self._audit_log).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            with self._audit_lock:
                with p.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass


# -- Stdio JSON-RPC 2.0 Transport ------------------------------------------- #


def handle_jsonrpc_message(
    svc: KBService,
    msg: dict,
    *,
    principal: Principal | None = None,
    slim_remote: bool = False,
) -> dict | None:
    if not isinstance(msg, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}

    method = msg.get("method")
    has_id = "id" in msg
    req_id = msg.get("id")

    if not has_id:
        return None  # JSON-RPC Notification

    if method == "initialize":
        params = msg.get("params") or {}
        client_ver = str(params.get("protocolVersion") or "")
        proto_ver = client_ver if client_ver in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": proto_ver,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": f"wiki-context ({svc.cfg.name})", "version": __version__},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": svc.build_tools_spec(slim_remote=slim_remote)},
        }
    if method == "tools/call":
        params = msg.get("params") or {}
        tname = str(params.get("name") or "")
        canonical_tname = _LEGACY_TOOL_ALIASES.get(tname, tname)
        targs = params.get("arguments")
        if slim_remote and canonical_tname not in {"wiki_context", "wiki_read", "wiki_inspect_visual", "wiki_remember", "wiki_forget", "wiki_list"}:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": f"Tool '{tname}' disabled on remote gateway"}], "isError": True},
            }
        try:
            payload, is_err = svc.dispatch_tool(tname, targs, principal=principal, is_remote=slim_remote)
        except IndexUnavailableError as exc:
            payload, is_err = json.dumps({"error": str(exc)}, ensure_ascii=False), True
        content_blocks = payload if isinstance(payload, list) else [{"type": "text", "text": payload}]
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": content_blocks, "isError": is_err},
        }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def run_stdio_server(vault: Path | str) -> int:
    svc = KBService(vault)
    try:
        while True:
            raw_line = sys.stdin.buffer.readline(MAX_STDIN_BYTES + 1)
            if not raw_line:
                break
            if len(raw_line) > MAX_STDIN_BYTES:
                err = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Request line too large"}}
                sys.stdout.write(json.dumps(err) + "\n")
                sys.stdout.flush()
                continue
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                err = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
                sys.stdout.write(json.dumps(err) + "\n")
                sys.stdout.flush()
                continue
            resp = handle_jsonrpc_message(svc, msg)
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()
    finally:
        svc.close()
    return 0


def selftest_mcp(vault: Path | str) -> int:
    svc = KBService(vault)
    try:
        r_init = handle_jsonrpc_message(svc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
        assert r_init and "serverInfo" in r_init["result"]
        assert handle_jsonrpc_message(svc, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None

        r_tools = handle_jsonrpc_message(svc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tool_names = [t["name"] for t in (r_tools or {}).get("result", {}).get("tools", ())]
        assert r_tools and {"wiki_context", "wiki_search", "wiki_read", "wiki_inspect_visual", "wiki_route", "wiki_list", "wiki_status"}.issubset(set(tool_names))

        # Path traversal check
        r_trav = handle_jsonrpc_message(
            svc,
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "wiki_read", "arguments": {"source": "sources/../../etc/passwd"}}},
        )
        assert r_trav and r_trav["result"]["isError"] is True

        # Identity spoofing check
        r_stat = handle_jsonrpc_message(
            svc,
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "wiki_status", "arguments": {"user": "spoofed_attacker"}}},
        )
        assert r_stat and json.loads(r_stat["result"]["content"][0]["text"])["identity"] == svc.identity
        print(f"mcp selftest: OK (vault={svc.cfg.name}, tools={len(tool_names)})")
        return 0
    finally:
        svc.close()
