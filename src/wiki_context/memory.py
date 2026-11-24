"""Layer-4 Vault Operational Memory, Source-Anchored Errata Pins & Consolidation (`notes/` / `记忆/`).

Architectural separation:
- Cross-project personal/environment habits stay in external agent memory (e.g. `magpie-memory`).
- Vault-scoped operational discoveries, troubleshooting gotchas, and read-only spec errata
  live in `<vault>/notes/*.md` (`--layout en`) or `<vault>/记忆/*.md` (`--layout zh`):
  1. Never mutates or contaminates Layer-1 `sources/` ground truth;
  2. Automatically inherits `PrefixACLPolicy` restrictions from every linked `related_sources` entry;
  3. Attaches `kind="errata"` notes as 1-hop `📌 Errata Pins` directly onto retrieved `[S1..Sn]` chunks;
  4. Renders non-errata `active` notes in a quarantined section of `<wiki_context>`;
  5. Consolidates `active` notes into permanent `wiki/*.md` pages during `wctx compile`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .config import VaultConfig
from .freshness import parse_wiki_frontmatter, split_frontmatter
from .search import tokenize
from .state_lock import atomic_write_text, file_lock

VALID_MEMORY_KINDS = frozenset({"errata", "insight", "decision", "glossary"})
VALID_MEMORY_STATUSES = frozenset({"active", "consolidated", "archived"})


@dataclass
class MemoryNote:
    note_id: str
    kind: str
    status: str
    author: str
    created_at: str
    related_sources: tuple[str, ...]
    tags: tuple[str, ...]
    text: str
    file_path: Path
    tokens: frozenset[str]


def _parse_simple_note_frontmatter(fm: str) -> dict:
    meta: dict = {
        "id": "",
        "kind": "insight",
        "status": "active",
        "author": "agent",
        "created_at": "",
        "related_sources": [],
        "tags": [],
    }
    cur_list: str | None = None
    for raw_ln in fm.splitlines():
        s = raw_ln.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("- ") and cur_list in {"related_sources", "tags"}:
            val = s[2:].strip().strip('"').strip("'")
            if val:
                meta[cur_list].append(val)
            continue
        if ":" in raw_ln and not raw_ln.startswith((" ", "\t")):
            k, v = raw_ln.split(":", 1)
            key = k.strip().lower()
            val = v.strip().strip('"').strip("'")
            cur_list = None
            if key in {"related_sources", "tags"}:
                if val.startswith("[") and val.endswith("]"):
                    inner = val[1:-1].strip()
                    meta[key] = [x.strip().strip('"').strip("'") for x in inner.split(",") if x.strip()]
                elif not val:
                    cur_list = key
                else:
                    meta[key] = [val]
            elif key in meta:
                meta[key] = val
    return meta


def _serialize_note(
    *,
    note_id: str,
    kind: str,
    status: str,
    author: str,
    created_at: str,
    related_sources: Sequence[str],
    tags: Sequence[str],
    text: str,
    consolidated_into: str = "",
) -> str:
    lines = [
        "---",
        f'id: "{note_id}"',
        f'kind: "{kind}"',
        f'status: "{status}"',
        f'author: "{author}"',
        f'created_at: "{created_at}"',
    ]
    if consolidated_into:
        lines.append(f'consolidated_into: "{consolidated_into}"')
    if related_sources:
        lines.append("related_sources:")
        for s in related_sources:
            lines.append(f'  - "{s}"')
    else:
        lines.append("related_sources: []")
    if tags:
        lines.append("tags:")
        for t in tags:
            lines.append(f'  - "{t}"')
    else:
        lines.append("tags: []")
    lines.append("---")
    lines.append("")
    lines.append(text.strip())
    lines.append("")
    return "\n".join(lines)


class MemoryStore:
    """Thread-safe, mtime-cached operational memory & errata store over `<vault>/<notes_dir>/`."""

    def __init__(self, vault: Path | str, *, scrub_fn: Callable[[str], str] | None = None):
        self.cfg = VaultConfig.load(vault)
        self._scrub = scrub_fn or (lambda s: s)
        self._lock = threading.Lock()
        self._cache_sig: tuple[int, int] | None = None
        self._notes: list[MemoryNote] = []
        self._wiki_refs_cache: dict[str, tuple[int, tuple[str, ...]]] = {}

    def _compute_sig(self) -> tuple[int, int]:
        nroot = self.cfg.notes_root
        if not nroot.is_dir():
            return (0, 0)
        max_m = nroot.stat().st_mtime_ns
        cnt = 0
        for p in nroot.glob("*.md"):
            if p.is_file() and not p.is_symlink():
                cnt += 1
                try:
                    max_m = max(max_m, p.stat().st_mtime_ns)
                except OSError:
                    pass
        return (max_m, cnt)

    def _reload_if_needed(self) -> list[MemoryNote]:
        sig = self._compute_sig()
        with self._lock:
            if sig == self._cache_sig:
                return list(self._notes)
            notes: list[MemoryNote] = []
            nroot = self.cfg.notes_root
            if nroot.is_dir():
                for p in sorted(nroot.glob("*.md")):
                    if p.is_symlink() or not p.is_file():
                        continue
                    try:
                        raw = p.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    fm, body = split_frontmatter(raw)
                    meta = _parse_simple_note_frontmatter(fm) if fm else {
                        "id": p.stem,
                        "kind": "insight",
                        "status": "active",
                        "author": "human",
                        "created_at": "",
                        "related_sources": [],
                        "tags": [],
                    }
                    clean_body = self._scrub(body.strip())
                    if not clean_body:
                        continue
                    norm_srcs = tuple(
                        self.cfg.normalize_memory_ref(s)
                        for s in (meta.get("related_sources") or ())
                        if isinstance(s, str) and s.strip()
                    )
                    tags_tup = tuple(str(t).strip() for t in (meta.get("tags") or ()) if str(t).strip())
                    toks = frozenset(tokenize(f"{clean_body} {' '.join(tags_tup)} {' '.join(norm_srcs)}"))
                    notes.append(
                        MemoryNote(
                            note_id=str(meta.get("id") or p.stem),
                            kind=str(meta.get("kind") or "insight").lower(),
                            status=str(meta.get("status") or "active").lower(),
                            author=str(meta.get("author") or "agent"),
                            created_at=str(meta.get("created_at") or ""),
                            related_sources=norm_srcs,
                            tags=tags_tup,
                            text=clean_body,
                            file_path=p,
                            tokens=toks,
                        )
                    )
            self._notes = notes
            self._cache_sig = sig
            return list(notes)

    def list_notes(self, *, status: str | None = "active") -> list[MemoryNote]:
        all_n = self._reload_if_needed()
        if status is None:
            return all_n
        return [n for n in all_n if n.status == status]

    def _wiki_page_sources(self, wiki_ref: str) -> tuple[str, ...]:
        """Frontmatter ``sources`` of a Layer-3 page (mtime-cached), used for ACL provenance inheritance."""
        rel = wiki_ref[len(self.cfg.wiki_prefix):] if wiki_ref.startswith(self.cfg.wiki_prefix) else wiki_ref
        if not rel or ".." in rel.split("/"):
            return ()
        path = self.cfg.wiki_root / rel
        try:
            st = path.stat()
        except OSError:
            return ()
        cached = self._wiki_refs_cache.get(wiki_ref)
        if cached is not None and cached[0] == st.st_mtime_ns:
            return cached[1]
        try:
            fm, _body = split_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
            refs = tuple(
                self.cfg.normalize_source_ref(r)
                for r in (parse_wiki_frontmatter(fm).get("sources") or ())
                if isinstance(r, str) and r.strip()
            )
        except OSError:
            refs = ()
        self._wiki_refs_cache[wiki_ref] = (st.st_mtime_ns, refs)
        return refs

    def can_access_note(self, policy, principal, note: MemoryNote) -> bool:
        if note.related_sources:
            for ref in note.related_sources:
                if self.cfg.is_wiki_ref(ref):
                    # Wiki-anchored notes inherit the page's own provenance ACL.
                    if not policy.can_access_wiki(principal, ref, list(self._wiki_page_sources(ref))):
                        return False
                elif not policy.can_access_source(principal, ref):
                    return False
            return True
        return policy.can_access_wiki(principal, f"{self.cfg.notes_dir}/{note.file_path.name}", [])

    def remember(
        self,
        text: str,
        *,
        kind: str = "insight",
        related_sources: Sequence[str] | None = None,
        tags: Sequence[str] | None = None,
        author: str = "agent",
    ) -> MemoryNote:
        clean_text = self._scrub((text or "").strip())
        if not clean_text:
            raise ValueError("Memory text must not be empty")
        k_norm = (kind or "insight").strip().lower()
        if k_norm not in VALID_MEMORY_KINDS:
            raise ValueError(f"Unsupported memory kind '{kind}'; choose from {sorted(VALID_MEMORY_KINDS)}")

        norm_srcs: list[str] = []
        for s in (related_sources or ()):
            s_str = str(s).strip()
            if s_str:
                norm_srcs.append(self.cfg.normalize_memory_ref(s_str))

        clean_tags = [re.sub(r"[^\w\u4e00-\u9fff._-]+", "-", str(t).strip()).strip("-") for t in (tags or ())]
        clean_tags = [t for t in clean_tags if t][:10]

        now_utc = dt.datetime.now(dt.timezone.utc)
        ts_slug = now_utc.strftime("%Y%m%d-%H%M%S")
        digest = hashlib.sha256(f"{ts_slug}:{author}:{clean_text}".encode("utf-8")).hexdigest()[:6]
        note_id = f"mem-{ts_slug}-{digest}"
        now_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        nroot = self.cfg.notes_root
        nroot.mkdir(parents=True, exist_ok=True)
        out_path = nroot / f"{note_id}.md"
        with file_lock(nroot / ".memory.lock", blocking=True):
            payload = _serialize_note(
                note_id=note_id,
                kind=k_norm,
                status="active",
                author=(author or "agent").strip()[:64],
                created_at=now_iso,
                related_sources=norm_srcs,
                tags=clean_tags,
                text=clean_text,
            )
            atomic_write_text(out_path, payload)

        with self._lock:
            self._cache_sig = None
        loaded = [n for n in self.list_notes(status=None) if n.note_id == note_id]
        return loaded[0] if loaded else MemoryNote(
            note_id=note_id,
            kind=k_norm,
            status="active",
            author=author,
            created_at=now_iso,
            related_sources=tuple(norm_srcs),
            tags=tuple(clean_tags),
            text=clean_text,
            file_path=out_path,
            tokens=frozenset(tokenize(clean_text)),
        )

    def get_note(self, note_id: str) -> MemoryNote | None:
        return next((n for n in self.list_notes(status=None) if n.note_id == note_id), None)

    def update_status(self, note_id: str, new_status: str, *, consolidated_into: str = "") -> bool:
        if new_status not in VALID_MEMORY_STATUSES:
            raise ValueError(f"Invalid status: {new_status}")
        target = self.get_note(note_id)
        if target is None or not target.file_path.is_file():
            return False
        payload = _serialize_note(
            note_id=target.note_id,
            kind=target.kind,
            status=new_status,
            author=target.author,
            created_at=target.created_at,
            related_sources=target.related_sources,
            tags=target.tags,
            text=target.text,
            consolidated_into=consolidated_into,
        )
        with file_lock(self.cfg.notes_root / ".memory.lock", blocking=True):
            atomic_write_text(target.file_path, payload)
        with self._lock:
            self._cache_sig = None
        return True

    def update_note(
        self,
        note_id: str,
        *,
        text: str | None = None,
        kind: str | None = None,
        status: str | None = None,
        related_sources: Sequence[str] | None = None,
        tags: Sequence[str] | None = None,
    ) -> MemoryNote | None:
        target = self.get_note(note_id)
        if target is None or not target.file_path.is_file():
            return None

        new_text = self._scrub(text.strip()) if text is not None else target.text
        if not new_text:
            raise ValueError("Memory text must not be empty")

        new_kind = kind.strip().lower() if kind is not None else target.kind
        if new_kind not in VALID_MEMORY_KINDS:
            raise ValueError(f"Unsupported memory kind '{kind}'; choose from {sorted(VALID_MEMORY_KINDS)}")

        new_status = status.strip().lower() if status is not None else target.status
        if new_status not in VALID_MEMORY_STATUSES:
            raise ValueError(f"Invalid status: {status}")

        if related_sources is not None:
            norm_srcs: list[str] = []
            for s in related_sources:
                s_str = str(s).strip()
                if s_str:
                    norm_srcs.append(self.cfg.normalize_memory_ref(s_str))
            final_srcs: Sequence[str] = norm_srcs
        else:
            final_srcs = target.related_sources

        if tags is not None:
            clean_tags = [re.sub(r"[^\w\u4e00-\u9fff._-]+", "-", str(t).strip()).strip("-") for t in tags]
            final_tags: Sequence[str] = [t for t in clean_tags if t][:10]
        else:
            final_tags = target.tags

        payload = _serialize_note(
            note_id=target.note_id,
            kind=new_kind,
            status=new_status,
            author=target.author,
            created_at=target.created_at,
            related_sources=final_srcs,
            tags=final_tags,
            text=new_text,
        )
        with file_lock(self.cfg.notes_root / ".memory.lock", blocking=True):
            atomic_write_text(target.file_path, payload)
        with self._lock:
            self._cache_sig = None
        return self.get_note(note_id)

    def delete_note(self, note_id: str) -> bool:
        target = self.get_note(note_id)
        if target is None or not target.file_path.is_file():
            return False
        try:
            target.file_path.unlink()
        except OSError:
            return False
        with self._lock:
            self._cache_sig = None
        return True

    def match_for_context(
        self,
        *,
        weighted_tokens: dict[str, float],
        retrieved_sources: set[str],
        policy,
        principal,
        max_notes: int = 4,
    ) -> tuple[dict[str, list[MemoryNote]], list[MemoryNote]]:
        """Return ``(errata_by_source, standalone_notes)`` authorized for ``principal``."""
        active = self.list_notes(status="active")
        if not active:
            return {}, []

        errata_by_src: dict[str, list[MemoryNote]] = {}
        pinned_ids: set[str] = set()

        for n in active:
            if not self.can_access_note(policy, principal, n):
                continue
            if n.kind == "errata" and n.related_sources:
                matched_srcs = [s for s in n.related_sources if s in retrieved_sources]
                if matched_srcs:
                    for ms in matched_srcs:
                        errata_by_src.setdefault(ms, []).append(n)
                    pinned_ids.add(n.note_id)

        scored: list[tuple[float, MemoryNote]] = []
        for n in active:
            if n.note_id in pinned_ids or not self.can_access_note(policy, principal, n):
                continue
            sc = sum(w for tok, w in weighted_tokens.items() if tok in n.tokens)
            if any(s in retrieved_sources for s in n.related_sources):
                sc += 1.5
            if sc >= 1.2:
                scored.append((sc, n))

        scored.sort(key=lambda x: x[0], reverse=True)
        return errata_by_src, [n for _, n in scored[:max_notes]]

    def import_json_items(self, items: Sequence[dict], *, default_author: str = "imported") -> list[str]:
        """Import external memory items (e.g. exported from `magpie-memory`) into Layer-4 notes."""
        created: list[str] = []
        for row in items:
            if not isinstance(row, dict):
                continue
            txt = str(row.get("text") or row.get("content") or "").strip()
            if not txt:
                continue
            raw_kind = str(row.get("kind") or "insight").lower()
            mapped_kind = raw_kind if raw_kind in VALID_MEMORY_KINDS else "insight"
            note = self.remember(
                txt,
                kind=mapped_kind,
                related_sources=row.get("related_sources") or [],
                tags=row.get("tags") or [],
                author=str(row.get("author") or default_author),
            )
            created.append(note.note_id)
        return created
