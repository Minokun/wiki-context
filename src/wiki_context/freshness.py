"""YAML frontmatter parsing and SHA-256 source freshness tracking.

Supports both English (`title`, `type`, `status`, `updated`, `sources`, `source_hashes`)
and Chinese (`标题`, `类型`, `状态`, `更新`, `来源`) frontmatter keys.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .config import VaultConfig


def split_frontmatter(raw_text: str) -> tuple[str, str]:
    """Return ``(frontmatter_block, markdown_body)``."""
    if not raw_text.startswith("---\n") and not raw_text.startswith("---\r\n"):
        return "", raw_text
    lines = raw_text.splitlines(keepends=True)
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "".join(lines[1:i]), "".join(lines[i + 1:])
    return "", raw_text


def first_scalar_field(fm: str, keys: tuple[str, ...]) -> str:
    for line in fm.splitlines():
        if ":" not in line or line[:1].isspace():
            continue
        k, val = line.split(":", 1)
        if k.strip() in keys:
            return val.strip().strip('"').strip("'")
    return ""


def parse_wiki_frontmatter(fm: str) -> dict:
    """Parse flat YAML frontmatter without external PyYAML dependency."""
    title = first_scalar_field(fm, ("title", "标题"))
    doc_type = first_scalar_field(fm, ("type", "类型"))
    status = first_scalar_field(fm, ("status", "状态"))
    updated = first_scalar_field(fm, ("updated", "更新"))

    sources: list[str] = []
    source_hashes: dict[str, str] = {}

    mode: str | None = None
    for raw_line in fm.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not raw_line[:1].isspace():
            mode = None
            if ":" not in raw_line:
                continue
            key, rest = raw_line.split(":", 1)
            k = key.strip()
            r = rest.strip()
            if k in {"sources", "来源"}:
                if r.startswith("[") and r.endswith("]"):
                    inner = r[1:-1].strip()
                    if inner:
                        for item in inner.split(","):
                            val = item.strip().strip('"').strip("'")
                            if val:
                                sources.append(val)
                else:
                    mode = "sources"
            elif k == "source_hashes":
                if r == "{}":
                    mode = None
                else:
                    mode = "hashes"
            continue

        if mode == "sources" and stripped.startswith("-"):
            item = stripped[1:].strip().strip('"').strip("'")
            if item:
                sources.append(item)
        elif mode == "hashes" and ":" in stripped:
            hk, hv = stripped.rsplit(":", 1)
            k_clean = hk.strip().strip('"').strip("'")
            v_clean = hv.strip().strip('"').strip("'")
            if k_clean and v_clean:
                source_hashes[k_clean] = v_clean

    return {
        "title": title,
        "type": doc_type,
        "status": status,
        "updated": updated,
        "sources": sources,
        "source_hashes": source_hashes,
    }


def compute_wiki_freshness(
    sources: list[str],
    recorded_hashes: dict[str, str],
    indexed_hashes: dict[str, str],
    *,
    cfg: VaultConfig | None = None,
) -> str:
    """Compute freshness purely from indexed source SHA-256 map (never `stat()`s raw `sources/`).

    Returns one of: ``fresh``, ``stale``, ``missing``, ``unverified``, ``skip``.
    """
    primary_refs = []
    for s in sources:
        if not isinstance(s, str):
            continue
        clean = re.split(r"[（(]", s, maxsplit=1)[0].strip()
        if cfg is not None:
            if cfg.is_source_ref(clean):
                primary_refs.append(cfg.normalize_source_ref(clean))
        elif clean.startswith(("sources/", "史料/")):
            primary_refs.append(clean)

    if not primary_refs:
        return "skip"
    if not recorded_hashes:
        return "unverified"

    normalized_rec = {
        (cfg.normalize_source_ref(k) if cfg is not None else k): v
        for k, v in recorded_hashes.items()
    }

    has_unverified = False
    for ref in primary_refs:
        cur_hash = indexed_hashes.get(ref)
        rec_hash = normalized_rec.get(ref)
        if cur_hash is None:
            if rec_hash:
                return "missing"
            has_unverified = True
            continue
        if not rec_hash:
            has_unverified = True
            continue
        if cur_hash != rec_hash:
            return "stale"

    return "unverified" if has_unverified else "fresh"


def load_indexed_hashes(cfg: VaultConfig) -> dict[str, str]:
    docs_jsonl = cfg.index_root / "docs.jsonl"
    hashes: dict[str, str] = {}
    if docs_jsonl.is_file():
        for line in docs_jsonl.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            src = row.get("source")
            sh = row.get("source_hash")
            if isinstance(src, str) and isinstance(sh, str) and sh:
                hashes[cfg.normalize_source_ref(src)] = sh
    manifest_p = cfg.mirrors_root / ".manifest.json"
    if manifest_p.is_file():
        try:
            manifest = json.loads(manifest_p.read_text(encoding="utf-8"))
            if isinstance(manifest, dict):
                for rel, meta in manifest.items():
                    if isinstance(meta, dict) and isinstance(meta.get("hash"), str):
                        hashes.setdefault(cfg.source_prefix + rel.lstrip("/"), meta["hash"])
        except (OSError, ValueError):
            pass
    return hashes


def check_vault_freshness(vault: Path | str, *, only_stale: bool = False) -> list[dict]:
    cfg = VaultConfig.load(vault)
    indexed_hashes = load_indexed_hashes(cfg)
    results: list[dict] = []
    if not cfg.wiki_root.is_dir():
        return results
    for md_path in sorted(cfg.wiki_root.glob("*.md")):
        raw = md_path.read_text(encoding="utf-8", errors="replace")
        fm, _ = split_frontmatter(raw)
        meta = parse_wiki_frontmatter(fm)
        state = compute_wiki_freshness(
            meta["sources"], meta["source_hashes"], indexed_hashes, cfg=cfg
        )
        if only_stale and state not in {"stale", "missing"}:
            continue
        results.append({
            "page": md_path.name,
            "source": f"{cfg.wiki_dir}/{md_path.name}",
            "state": state,
            "sources": meta["sources"],
        })
    return results


def snapshot_wiki_hashes(vault: Path | str, *, page: str | None = None) -> list[str]:
    """Write verified ``source_hashes`` into Wiki frontmatter for ``page`` (or all pages)."""
    cfg = VaultConfig.load(vault)
    indexed_hashes = load_indexed_hashes(cfg)
    if not cfg.wiki_root.is_dir():
        return []

    updated_pages: list[str] = []
    targets: list[Path] = []
    if page:
        clean_name = page if page.endswith(".md") else f"{page}.md"
        candidate = cfg.wiki_root / Path(clean_name).name
        if candidate.is_file():
            targets.append(candidate)
    else:
        targets = sorted(cfg.wiki_root.glob("*.md"))

    for md_path in targets:
        raw = md_path.read_text(encoding="utf-8", errors="replace")
        fm, body = split_frontmatter(raw)
        if not fm:
            continue
        meta = parse_wiki_frontmatter(fm)
        hash_pairs: list[tuple[str, str]] = []
        for s in meta["sources"]:
            clean = re.split(r"[（(]", s, maxsplit=1)[0].strip()
            if cfg.is_source_ref(clean):
                norm_s = cfg.normalize_source_ref(clean)
                sh = indexed_hashes.get(norm_s)
                if sh:
                    hash_pairs.append((norm_s, sh))
        if not hash_pairs:
            continue

        # Remove existing `source_hashes:` block from frontmatter
        cleaned_fm: list[str] = []
        skip_indented = False
        for line in fm.splitlines():
            if not line[:1].isspace() and line.lstrip().startswith("source_hashes:"):
                skip_indented = True
                continue
            if skip_indented:
                if line[:1].isspace() or not line.strip():
                    continue
                skip_indented = False
            cleaned_fm.append(line)

        cleaned_fm.append("source_hashes:")
        for src_ref, sh in hash_pairs:
            cleaned_fm.append(f'  "{src_ref}": "{sh}"')

        new_raw = "---\n" + "\n".join(cleaned_fm) + "\n---\n" + body
        if new_raw != raw:
            tmp = md_path.with_suffix(".md.tmp")
            tmp.write_text(new_raw, encoding="utf-8")
            tmp.replace(md_path)
            updated_pages.append(md_path.name)
    return updated_pages
