"""Read-only mechanical health checker (`lint_vault`) for three-layer vaults.

Detects:
- `dangling-link`: `[[Target]]` wikilinks pointing to non-existent Wiki pages
- `unlisted-wiki-page`: `wiki/*.md` pages missing from `index.md` / `目录.md`
- `missing-source`: Wiki pages lacking YAML `sources:` declarations
- `missing-cited-source`: `sources:` entries whose mirror is absent from `.manifest.json`
- `duplicate-title`: Multiple Wiki pages declaring identical titles
- `stale-index`: Index out of sync with current mirrors/wiki
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .config import VaultConfig
from .freshness import clean_source_ref, parse_wiki_frontmatter, split_frontmatter
from .indexer import check_index

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_FENCED_CODE_RE = re.compile(r"```[\s\S]*?```")
_HTML_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")


def _normalize_wikilink_target(raw: str) -> str:
    """Normalize a wikilink or href target without stripping version dots like `V2.0`."""
    name = raw.strip().replace("\\", "/").rsplit("/", 1)[-1]
    if name.lower().endswith(".md"):
        name = name[:-3]
    return name.lower()


def lint_vault(vault: Path | str) -> dict:
    cfg = VaultConfig.load(vault)
    errors: list[dict] = []
    warnings: list[dict] = []

    wiki_pages: dict[str, Path] = {}
    if cfg.wiki_root.is_dir():
        for wp in sorted(cfg.wiki_root.glob("*.md")):
            if not wp.is_symlink():
                wiki_pages[wp.stem.lower()] = wp

    root_special = {
        cfg.catalog_file.lower(),
        Path(cfg.catalog_file).stem.lower(),
        cfg.log_file.lower(),
        Path(cfg.log_file).stem.lower(),
        "agents.md",
        "agents",
    }

    manifest_sources: set[str] = set()
    manifest_p = cfg.mirrors_root / ".manifest.json"
    if manifest_p.is_file():
        try:
            mdata = json.loads(manifest_p.read_text(encoding="utf-8"))
            if isinstance(mdata, dict):
                manifest_sources = {f"{cfg.source_prefix}{k.lstrip('/')}" for k in mdata}
        except (OSError, ValueError):
            errors.append({"code": "corrupt-manifest", "file": str(manifest_p.name), "detail": "Invalid JSON"})

    # Check catalog registration
    catalog_registered: set[str] = set()
    if cfg.catalog_path.is_file():
        cat_raw = _HTML_COMMENT_RE.sub("", _FENCED_CODE_RE.sub("", cfg.catalog_path.read_text(encoding="utf-8", errors="replace")))
        for m in _WIKILINK_RE.finditer(cat_raw):
            tgt = _normalize_wikilink_target(m.group(1))
            catalog_registered.add(tgt)
            if tgt not in wiki_pages and tgt not in root_special:
                errors.append({
                    "code": "dangling-link",
                    "file": cfg.catalog_file,
                    "detail": f"Catalog links to non-existent page [[{m.group(1).strip()}]]",
                })
        for m in _MD_LINK_RE.finditer(cat_raw):
            href = m.group(2).strip().lstrip("./")
            if not href.startswith(("http://", "https://", "#")):
                catalog_registered.add(_normalize_wikilink_target(href))
    else:
        warnings.append({"code": "missing-catalog", "file": cfg.catalog_file, "detail": "Catalog file not found"})

    titles_seen: dict[str, str] = {}
    overview_stems = {"overview", "知识库总览"}

    for stem_low, wp in wiki_pages.items():
        rel_w = f"{cfg.wiki_dir}/{wp.name}"
        if stem_low not in catalog_registered and stem_low not in overview_stems:
            warnings.append({
                "code": "index-inconsistency",
                "file": rel_w,
                "detail": f"Page not listed in {cfg.catalog_file}",
            })

        raw = wp.read_text(encoding="utf-8", errors="replace")
        fm, body = split_frontmatter(raw)
        meta = parse_wiki_frontmatter(fm)

        title = (meta["title"] or wp.stem).strip()
        if title.lower() in titles_seen:
            errors.append({
                "code": "duplicate-title",
                "file": rel_w,
                "detail": f"Duplicate title '{title}' also in {titles_seen[title.lower()]}",
            })
        else:
            titles_seen[title.lower()] = rel_w

        if meta["type"] != "index" and stem_low not in overview_stems and not meta["sources"]:
            warnings.append({
                "code": "missing-source",
                "file": rel_w,
                "detail": "Wiki page frontmatter has empty `sources:`",
            })

        for s in meta["sources"]:
            clean = clean_source_ref(s)
            if cfg.is_source_ref(clean):
                norm = cfg.normalize_source_ref(clean)
                if manifest_sources and norm not in manifest_sources:
                    warnings.append({
                        "code": "unmirrored-or-missing-source",
                        "file": rel_w,
                        "detail": f"Cited source '{norm}' is not in mirrors/.manifest.json",
                    })

        body_no_code = _HTML_COMMENT_RE.sub("", _FENCED_CODE_RE.sub("", body))
        for m in _WIKILINK_RE.finditer(body_no_code):
            target_raw = m.group(1).strip()
            target_stem = _normalize_wikilink_target(target_raw)
            if target_stem not in wiki_pages and target_stem not in root_special:
                errors.append({
                    "code": "dangling-link",
                    "file": rel_w,
                    "detail": f"Broken wikilink [[{target_raw}]]",
                })

    idx_ok, idx_msgs = check_index(cfg.root, deep=False)
    if not idx_ok:
        warnings.append({
            "code": "stale-or-missing-index",
            "file": f"{cfg.mirrors_dir}/.index",
            "detail": "; ".join(idx_msgs),
        })

    return {
        "vault": cfg.name,
        "ok": len(errors) == 0,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
    }
