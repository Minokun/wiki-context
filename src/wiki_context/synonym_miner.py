"""Automated domain glossary & bilingual acronym synonym miner (`wctx synonyms mine`).

Scans mirrored primary documents and compiled Wiki pages for:
1. Terminology / glossary Markdown tables whose headers map canonical domain names
   (`名称/中文/部件/术语/Term/Component`) to aliases/abbreviations (`英文/简写/缩写/代号/Code/Abbr/Alias`).
2. Inline parenthetical definitions (`受电弓 (Pantograph)`, `悬挂状态检测监测装置（4C）`,
   `Disaster Recovery (DR)`).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .config import VaultConfig
from .freshness import split_frontmatter
from .policy import mirror_skip_reason

_CANON_COL_RE = re.compile(r"(?:中文|名称|部件|构件|零件|状态|术语|标准词|canonical|name|component|term)", re.I)
_ALIAS_COL_RE = re.compile(r"(?:英文|简写|缩写|代号|简称|别名|编码|english|abbr|code|alias|short)", re.I)

# Matches `中文术语 (EnglishOrCode)` or `Full Term (ACRONYM)` or `中文术语（简称/又称/俗称 别名）`
_INLINE_CJK_PAREN_RE = re.compile(
    r"([\u4e00-\u9fff]{2,12})\s*[（(]\s*([A-Za-z][A-Za-z0-9_.\-/ ]{1,28})\s*[)）]"
)
_INLINE_CJK_ALIAS_PAREN_RE = re.compile(
    r"([\u4e00-\u9fff]{2,14})\s*[（(]\s*(?:简称|又称|俗称|亦称|别称|代号)\s*[：:]?\s*([\u4e00-\u9fffA-Za-z0-9_.\-/、,， ]{2,24})\s*[)）]"
)
_INLINE_EN_ACRONYM_RE = re.compile(
    r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,4})\s*\(\s*([A-Z][A-Z0-9_-]{1,9})\s*\)"
)

_BLOCKLIST_ALIAS_WORDS = frozenset({
    "optional", "default", "deprecated", "required", "draft", "todo", "note", "see", "fig", "figure",
    "table", "page", "section", "yes", "no", "true", "false", "null", "none", "tbd", "wip",
    "可选", "默认", "备注", "说明", "参见", "详见", "例如", "必填", "正常", "异常", "其他", "其它",
})

_UNIT_AND_NOISE_ALIASES = frozenset({
    "mm", "nm", "nmm", "cm", "km", "kmh", "kmb", "ms", "fps", "fips", "deg",
    "hz", "khz", "mhz", "kv", "mv", "ma", "kw", "mw", "db", "kg", "min", "sec",
    "ppm", "name", "id", "no", "sn", "type", "code", "num", "max", "avg", "sum",
})

_BAD_CJK_PREFIX_RE = re.compile(
    r"^(?:的|了|着|过|个|选配|总|图示|实测|设定|设计|颜色|用于|采用|使用|包含|其中)"
)
_BAD_CJK_CONNECTOR_RE = re.compile(r"(?:与.*间|的.*用)")


def _clean_term(raw: str) -> str:
    s = re.sub(r"[`*~]+", "", raw or "").strip(" _\t\r\n.,;:，。；：\"'")
    s = re.sub(r"\s+", " ", s)
    return s


def _valid_term(t: str, *, is_alias: bool = False) -> bool:
    if not t or len(t) < 2 or len(t) > 40:
        return False
    low = t.lower()
    if low in _BLOCKLIST_ALIAS_WORDS:
        return False
    if is_alias and low in _UNIT_AND_NOISE_ALIASES:
        return False
    if re.fullmatch(r"[\d.\-+%/ ]+", t):
        return False
    if not is_alias:
        if _BAD_CJK_PREFIX_RE.match(t) or _BAD_CJK_CONNECTOR_RE.search(t):
            return False
    return True


def _mine_from_markdown_tables(md_text: str) -> list[tuple[str, list[str]]]:
    pairs: list[tuple[str, list[str]]] = []
    lines = md_text.splitlines()
    i = 0
    while i < len(lines) - 2:
        hdr_ln = lines[i].strip()
        sep_ln = lines[i + 1].strip()
        if hdr_ln.startswith("|") and hdr_ln.endswith("|") and sep_ln.startswith("|") and "---" in sep_ln:
            headers = [_clean_term(c) for c in hdr_ln.strip("|").split("|")]
            canon_cols = [idx for idx, h in enumerate(headers) if _CANON_COL_RE.search(h) and not _ALIAS_COL_RE.search(h)]
            alias_cols = [idx for idx, h in enumerate(headers) if _ALIAS_COL_RE.search(h)]
            j = i + 2
            if canon_cols and alias_cols:
                c_idx = canon_cols[0]
                while j < len(lines) and lines[j].strip().startswith("|"):
                    cells = [_clean_term(c) for c in lines[j].strip().strip("|").split("|")]
                    if c_idx < len(cells) and _valid_term(cells[c_idx]):
                        canon = cells[c_idx]
                        aliases = [
                            cells[a_i]
                            for a_i in alias_cols
                            if a_i < len(cells)
                            and _valid_term(cells[a_i], is_alias=True)
                            and cells[a_i].lower() != canon.lower()
                        ]
                        if aliases:
                            pairs.append((canon, aliases))
                    j += 1
                i = j
                continue
        i += 1
    return pairs


def _mine_from_inline_prose(md_text: str) -> list[tuple[str, list[str]]]:
    pairs: list[tuple[str, list[str]]] = []
    for m in _INLINE_CJK_PAREN_RE.finditer(md_text):
        c_term = _clean_term(m.group(1))
        raw_in = _clean_term(m.group(2))
        if len(c_term) > 14:
            continue
        # Handle `English Name / ABBR` inside parentheses
        sub_aliases = [_clean_term(p) for p in re.split(r"[/,，;；|]+", raw_in)]
        sub_aliases = [
            a for a in sub_aliases if _valid_term(a, is_alias=True) and a.lower() != c_term.lower()
        ]
        if _valid_term(c_term) and sub_aliases:
            pairs.append((c_term, sub_aliases))

    for m in _INLINE_CJK_ALIAS_PAREN_RE.finditer(md_text):
        c_term = _clean_term(m.group(1))
        raw_in = _clean_term(m.group(2))
        if len(c_term) > 14:
            continue
        sub_aliases = [_clean_term(p) for p in re.split(r"[、/,，;；|]+", raw_in)]
        sub_aliases = [
            a for a in sub_aliases if _valid_term(a, is_alias=True) and a.lower() != c_term.lower()
        ]
        if _valid_term(c_term) and sub_aliases:
            pairs.append((c_term, sub_aliases))

    for m in _INLINE_EN_ACRONYM_RE.finditer(md_text):
        full_en = _clean_term(m.group(1))
        acr = _clean_term(m.group(2))
        if _valid_term(full_en) and _valid_term(acr, is_alias=True):
            # Initials sanity check: first letter of acronym matches first letter of phrase
            if full_en[0].upper() == acr[0].upper():
                pairs.append((full_en, [acr]))
    return pairs


def mine_synonyms(
    vault: Path | str,
    *,
    apply: bool = False,
    max_groups: int = 200,
    clean_existing: bool = False,
) -> dict:
    """Mine candidate canonical-alias groups from mirrors & Wiki pages."""
    cfg = VaultConfig.load(vault)
    files_to_scan: list[Path] = []

    if cfg.mirrors_root.is_dir():
        for mp in sorted(cfg.mirrors_root.rglob("*.md")):
            if mp.is_symlink():
                continue
            rel_m = mp.relative_to(cfg.mirrors_root).as_posix()
            if mirror_skip_reason(mp, rel_m, cfg=cfg) is None:
                files_to_scan.append(mp)

    if cfg.wiki_root.is_dir():
        for wp in sorted(cfg.wiki_root.glob("*.md")):
            if not wp.is_symlink():
                files_to_scan.append(wp)

    grouped: dict[str, dict[str, list[str]]] = {}

    def record(canon: str, aliases: list[str]) -> None:
        key = canon.lower()
        entry = grouped.setdefault(key, {"canonical": [canon], "aliases": []})
        existing_low = {x.lower() for x in entry["canonical"]} | {x.lower() for x in entry["aliases"]}
        for a in aliases:
            if a.lower() not in existing_low:
                entry["aliases"].append(a)
                existing_low.add(a.lower())

    for fp in files_to_scan:
        try:
            raw = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        _, body = split_frontmatter(raw)
        for c_term, a_list in _mine_from_markdown_tables(body):
            record(c_term, a_list)
        for c_term, a_list in _mine_from_inline_prose(body):
            record(c_term, a_list)

    # Load existing `synonyms.json` and merge non-conflicting discoveries
    syn_path = cfg.synonyms_path
    existing_doc: dict = {"version": 1, "groups": []}
    if syn_path.is_file():
        try:
            loaded = json.loads(syn_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing_doc = loaded
        except (OSError, ValueError):
            pass

    existing_groups_raw = list(existing_doc.get("groups") or ())
    cleaned_removed = 0
    existing_groups: list[dict] = []
    if clean_existing:
        for g in existing_groups_raw:
            if not isinstance(g, dict):
                cleaned_removed += 1
                continue
            cans = [
                _clean_term(str(c))
                for c in (g.get("canonical") or ())
                if _valid_term(_clean_term(str(c)), is_alias=False)
            ]
            als = [
                _clean_term(str(a))
                for a in (g.get("aliases") or ())
                if _valid_term(_clean_term(str(a)), is_alias=True)
            ]
            can_low = {c.lower() for c in cans}
            als = [a for a in als if a.lower() not in can_low]
            if cans and als:
                if len(cans) < len(g.get("canonical") or ()) or len(als) < len(g.get("aliases") or ()):
                    cleaned_removed += 1
                existing_groups.append({"canonical": cans, "aliases": als})
            else:
                cleaned_removed += 1
    else:
        existing_groups = existing_groups_raw

    known_terms: set[str] = set()
    for g in existing_groups:
        if isinstance(g, dict):
            for t in (*(g.get("canonical") or ()), *(g.get("aliases") or ())):
                known_terms.add(str(t).strip().lower())

    new_groups: list[dict] = []
    for item in grouped.values():
        if not item["aliases"]:
            continue
        c_main = item["canonical"][0]
        fresh_aliases = [a for a in item["aliases"] if a.lower() not in known_terms]
        if c_main.lower() in known_terms and not fresh_aliases:
            continue
        grp = {
            "canonical": item["canonical"],
            "aliases": fresh_aliases or item["aliases"],
        }
        new_groups.append(grp)
        if len(new_groups) >= max_groups:
            break

    applied = bool(apply and (new_groups or cleaned_removed > 0))
    if applied:
        merged_groups = [*existing_groups, *new_groups]
        out_doc = {
            "version": existing_doc.get("version", 1),
            "description": existing_doc.get("description", "Domain canonical terms <- colloquial aliases (hot-reloaded on save)"),
            "groups": merged_groups,
        }
        tmp = syn_path.with_name(f".{syn_path.name}.tmp-{os.getpid()}")
        tmp.write_text(json.dumps(out_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(syn_path)

    return {
        "vault": cfg.name,
        "scanned_files": len(files_to_scan),
        "discovered_groups": len(new_groups),
        "cleaned_removed": cleaned_removed,
        "applied": applied,
        "groups": new_groups,
    }
