"""Offline AI Librarian Compiler (`wctx compile`).

Incrementally identifies:
1. Mirrored primary documents not yet cited by any `wiki/*.md` page;
2. Existing `wiki/*.md` pages marked `stale` (whose underlying primary sources changed).

Calls any OpenAI-compatible (`/v1/chat/completions`) or Anthropic Messages
(`/v1/messages`) endpoint via Python stdlib `urllib.request` (zero SDK dependency),
or accepts a custom `llm_fn(system_prompt, user_prompt) -> str` callable.
Updates Wiki pages, `index.md`, `log.md`, snapshots SHA-256 `source_hashes`,
runs `lint_vault`, and refreshes the immutable FTS5 index.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .config import VaultConfig
from .freshness import (
    check_vault_freshness,
    clean_source_ref,
    parse_wiki_frontmatter,
    snapshot_wiki_hashes,
    split_frontmatter,
)
from .indexer import rebuild_index
from .lint import lint_vault
from .memory import MemoryStore
from .policy import IMAGE_EXTS, mirror_skip_reason
from .search import is_excluded_source

LLMCallable = Callable[[str, str], str]

LLM_PROVIDER_PRESETS: list[dict[str, str]] = [
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "protocol": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "protocol": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
    {
        "id": "qwen",
        "label": "通义千问 (DashScope)",
        "protocol": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
    },
    {
        "id": "zhipu",
        "label": "智谱 GLM (BigModel)",
        "protocol": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
    },
    {
        "id": "anthropic",
        "label": "Anthropic Claude",
        "protocol": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-sonnet-4-20250514",
    },
    {
        "id": "custom",
        "label": "自定义兼容接口",
        "protocol": "openai",
        "base_url": "",
        "model": "",
    },
]


def test_llm_connection(
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    protocol: str = "openai",
    timeout_sec: int = 20,
) -> dict[str, Any]:
    """Verify LLM credentials & endpoint connectivity with a minimal ping request."""
    caller = build_http_llm_caller(
        model=model,
        base_url=base_url,
        api_key=api_key,
        protocol=protocol,
        timeout_sec=timeout_sec,
    )
    t0 = time.perf_counter()
    try:
        reply = caller("You are a connectivity check.", "Reply with the single word OK.").strip()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        raise RuntimeError(f"大模型接口返回 HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接大模型接口 ({base_url}): {exc.reason}") from exc
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    return {
        "ok": True,
        "latency_ms": elapsed_ms,
        "reply": reply[:60] or "OK",
        "model": model or "",
        "base_url": base_url or "",
    }


def _slugify_title(name: str) -> str:
    clean = re.sub(r"[\\/:*?\"<>|#\[\]]+", "-", name).strip(" .-")
    return clean[:80] or "compiled-note"


def build_http_llm_caller(
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    protocol: str = "openai",
    timeout_sec: int = 120,
) -> LLMCallable:
    """Create a zero-dependency stdlib HTTP LLM caller (`openai` or `anthropic` wire format)."""
    proto = (protocol or os.environ.get("WCTX_LLM_PROTOCOL") or "openai").strip().lower()
    if proto == "anthropic":
        key = api_key or os.environ.get("WCTX_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or ""
        url = (base_url or os.environ.get("WCTX_BASE_URL") or "https://api.anthropic.com/v1").rstrip("/")
        mdl = model or os.environ.get("WCTX_MODEL") or "claude-sonnet-4-20250514"
        if not key:
            raise ValueError("Missing API key: set WCTX_API_KEY or ANTHROPIC_API_KEY")

        def _call_anthropic(sys_prompt: str, usr_prompt: str) -> str:
            payload = json.dumps({
                "model": mdl,
                "max_tokens": 4096,
                "temperature": 0.1,
                "system": sys_prompt,
                "messages": [{"role": "user", "content": usr_prompt}],
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{url}/messages",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            blocks = body.get("content") or []
            return "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")

        return _call_anthropic

    key = (
        api_key
        or os.environ.get("WCTX_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY")
        or ""
    )
    url = (base_url or os.environ.get("WCTX_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    mdl = model or os.environ.get("WCTX_MODEL") or "gpt-4o-mini"
    if not key:
        raise ValueError("Missing API key: set WCTX_API_KEY, OPENAI_API_KEY, or DEEPSEEK_API_KEY")

    def _call_openai(sys_prompt: str, usr_prompt: str) -> str:
        payload = json.dumps({
            "model": mdl,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": usr_prompt},
            ],
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{url}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError(f"LLM response contained no choices: {body}")
        return str((choices[0].get("message") or {}).get("content") or "")

    return _call_openai


def _strip_outer_markdown_fence(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        first_nl = s.find("\n")
        if first_nl != -1 and s.endswith("```"):
            return s[first_nl + 1: -3].strip()
    return s


_OPERATIONAL_DIR_KEYWORDS = ("发票", "报销", "invoice", "receipt")
_OPERATIONAL_FILE_PREFIXES = ("dzfp_", "inv20")
_BODY_SOURCES_HEADING_RE = re.compile(r"^##\s+(?:来源|sources)\s*$", re.IGNORECASE)


def _extract_body_source_refs(body: str, cfg: VaultConfig) -> list[str]:
    """Extract primary source references listed under ``## 来源`` or ``## Sources``."""
    refs: list[str] = []
    in_sources_section = False
    for raw_line in body.splitlines():
        stripped = raw_line.strip()
        if not in_sources_section:
            if _BODY_SOURCES_HEADING_RE.match(stripped):
                in_sources_section = True
            continue
        if stripped.startswith("## ") or stripped.startswith("# "):
            break
        if not stripped:
            continue
        if stripped.startswith(("- ", "* ")):
            item = stripped[2:].strip().strip("`").strip()
            clean = clean_source_ref(item)
            if cfg.is_source_ref(clean):
                refs.append(clean)
    return refs


def _compile_skip_reason(
    src_rel: str,
    cfg: VaultConfig,
    *,
    include_images: bool = False,
) -> str | None:
    """Return a skip reason when an indexed mirror should not auto-compile into a standalone Wiki page."""
    rel = src_rel.replace("\\", "/").lstrip("/")
    p = PurePosixPath(rel)
    src_ref = f"{cfg.source_prefix}{rel}"
    if not include_images and p.suffix.lower() in IMAGE_EXTS:
        return "image-mirror-excluded-from-compile"
    if cfg.exclude_patterns and is_excluded_source(src_ref, cfg.exclude_patterns):
        return "vault-exclude-pattern"
    if cfg.compile_exclude_patterns and is_excluded_source(src_ref, cfg.compile_exclude_patterns):
        return "compile-exclude-pattern"
    for part in p.parts[:-1]:
        low_part = part.lower()
        if any(kw in low_part for kw in _OPERATIONAL_DIR_KEYWORDS) or part.startswith("DetectionRecognitionDemo"):
            return "non-knowledge-operational-doc"
    if p.name.lower().startswith(_OPERATIONAL_FILE_PREFIXES):
        return "non-knowledge-operational-doc"
    return None


def _enforce_draft_status(md: str) -> str:
    """Prevent machine-compiled pages from self-marking ``status: reviewed``."""
    fm, body = split_frontmatter(md)
    if not fm:
        return md
    changed = False
    out_lines: list[str] = []
    for line in fm.splitlines(keepends=True):
        m = re.match(
            r'^(\s*(?:status|状态)\s*:\s*)(["\']?)reviewed\2(\s*(?:#.*)?)$',
            line.rstrip("\r\n"),
            re.IGNORECASE,
        )
        if m:
            nl = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
            out_lines.append(f"{m.group(1)}{m.group(2)}draft{m.group(2)}{m.group(3)}{nl}")
            changed = True
        else:
            out_lines.append(line)
    if not changed:
        return md
    return "---\n" + "".join(out_lines) + "---\n" + body


def plan_compilation(vault: Path | str, *, include_images: bool = False) -> dict:
    """Inspect vault and return ``uncompiled_sources`` + ``stale_wiki_pages`` without calling an LLM."""
    cfg = VaultConfig.load(vault)
    covered_sources: set[str] = set()
    if cfg.wiki_root.is_dir():
        for wp in sorted(cfg.wiki_root.glob("*.md")):
            raw = wp.read_text(encoding="utf-8", errors="replace")
            fm, body = split_frontmatter(raw)
            meta = parse_wiki_frontmatter(fm)
            for s in [*meta["sources"], *_extract_body_source_refs(body, cfg)]:
                clean = clean_source_ref(s)
                if cfg.is_source_ref(clean):
                    covered_sources.add(cfg.normalize_source_ref(clean))

    manifest_p = cfg.mirrors_root / ".manifest.json"
    manifest: dict = {}
    if manifest_p.is_file():
        try:
            manifest = json.loads(manifest_p.read_text(encoding="utf-8"))
        except ValueError:
            manifest = {}

    uncompiled: list[dict] = []
    for src_rel, entry in sorted(manifest.items()):
        if not isinstance(entry, dict):
            continue
        m_rel = str(entry.get("mirror") or f"{src_rel}.md").lstrip("/")
        m_path = cfg.mirrors_root / m_rel
        if not m_path.is_file() or mirror_skip_reason(m_path, m_rel, cfg=cfg, manifest_entry=entry) is not None:
            continue
        if _compile_skip_reason(src_rel, cfg, include_images=include_images) is not None:
            continue
        src_ref = f"{cfg.source_prefix}{src_rel.lstrip('/')}"
        if src_ref not in covered_sources:
            uncompiled.append({"source": src_ref, "mirror_path": m_path})

    stale_pages = check_vault_freshness(cfg.root, only_stale=True)
    mem_store = MemoryStore(cfg.root)
    active_notes = mem_store.list_notes(status="active")
    return {
        "vault": cfg.name,
        "uncompiled_sources": [u["source"] for u in uncompiled],
        "stale_pages": [s["page"] for s in stale_pages],
        "active_notes": [n.note_id for n in active_notes],
        "_uncompiled_items": uncompiled,
        "_stale_items": stale_pages,
        "_active_notes": active_notes,
    }


def compile_vault(
    vault: Path | str,
    *,
    llm_fn: LLMCallable | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    protocol: str = "openai",
    max_items: int = 10,
    dry_run: bool = False,
    include_images: bool = False,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict:
    """Compile unindexed primary mirrors and refresh stale Wiki pages."""
    cfg = VaultConfig.load(vault)
    plan = plan_compilation(cfg.root, include_images=include_images)
    if dry_run:
        return {
            "vault": cfg.name,
            "dry_run": True,
            "uncompiled_sources": plan["uncompiled_sources"],
            "stale_pages": plan["stale_pages"],
            "active_notes": plan["active_notes"],
        }

    uncompiled_items = plan["_uncompiled_items"][:max_items]
    remaining_budget = max(0, max_items - len(uncompiled_items))
    stale_items = plan["_stale_items"][:remaining_budget]
    active_notes = plan["_active_notes"]
    pending_total = (
        len(plan["_uncompiled_items"])
        + len(plan["_stale_items"])
        + (1 if active_notes else 0)
    )
    remaining_uncompiled = max(0, len(plan["_uncompiled_items"]) - len(uncompiled_items))
    remaining_stale = max(0, len(plan["_stale_items"]) - len(stale_items))

    if not uncompiled_items and not stale_items and not active_notes:
        return {
            "vault": cfg.name,
            "compiled_pages": [],
            "refreshed_pages": [],
            "consolidated_notes": [],
            "remaining_uncompiled": 0,
            "remaining_stale": 0,
            "batch_limit": max_items,
            "noop": True,
        }

    caller = llm_fn or build_http_llm_caller(
        model=model, base_url=base_url, api_key=api_key, protocol=protocol
    )
    cfg.wiki_root.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().isoformat()
    now_hm = dt.datetime.now().strftime("%H:%M")
    is_zh = cfg.layout == "zh"

    overview_link = "[[知识库总览]]" if is_zh else "[[Overview]]"
    sys_prompt = (
        "You are an exacting technical librarian compiling a structured LLM Wiki page from primary source text.\n"
        "Rules:\n"
        "1. Preserve exact numbers, thresholds, table facts, version numbers, and caveats. Never invent facts.\n"
        "2. Output ONLY the complete Markdown document starting with `---` YAML frontmatter (no outer code fences).\n"
        "3. Include sections for Overview, Key Facts, Caveats/Pending Verification, Related Pages, and Sources.\n"
    )

    compiled_pages: list[str] = []
    refreshed_pages: list[str] = []
    catalog_additions: list[tuple[str, str]] = []
    total_steps = len(uncompiled_items) + len(stale_items) + (1 if active_notes else 0)
    done_steps = 0

    def _emit(phase: str, current: str = "") -> None:
        if on_progress is not None:
            try:
                on_progress({
                    "phase": phase,
                    "done": done_steps,
                    "total": total_steps,
                    "pending_total": pending_total,
                    "batch_limit": max_items,
                    "current": current,
                    "compiled_count": len(compiled_pages),
                    "refreshed_count": len(refreshed_pages),
                })
            except Exception:
                pass

    # 1) Compile newly added uncompiled sources
    for item in uncompiled_items:
        src_ref = item["source"]
        _emit("compiling_new", src_ref)
        m_path: Path = item["mirror_path"]
        raw_m = m_path.read_text(encoding="utf-8", errors="replace")
        _, m_body = split_frontmatter(raw_m)
        excerpt = m_body[:14000]
        stem_title = _slugify_title(PurePosixPath(src_ref).stem)

        if is_zh:
            usr_prompt = (
                f"请将以下一手史料编译为一篇结构化 Wiki 页面（标题建议：{stem_title}）。\n"
                f"必须使用如下 YAML frontmatter 开头：\n"
                f"---\ntitle: \"{stem_title}\"\ntype: source\nstatus: draft\nupdated: {today}\n"
                f"sources:\n  - {src_ref}\n---\n\n"
                f"正文包含：`# {stem_title}`、`## 概览`、`## 关键信息`、`## 待核验与口径提示`、"
                f"`## 关联页面`（至少包含 {overview_link}）、`## 来源`（列出 `- {src_ref}`）。\n\n"
                f"=== 一手史料镜像原文 ({src_ref}) ===\n{excerpt}"
            )
        else:
            usr_prompt = (
                f"Compile the following primary source into a structured Wiki page titled '{stem_title}'.\n"
                f"Start with exact YAML frontmatter:\n"
                f"---\ntitle: \"{stem_title}\"\ntype: source\nstatus: draft\nupdated: {today}\n"
                f"sources:\n  - {src_ref}\n---\n\n"
                f"Include `# {stem_title}`, `## Overview`, `## Key Facts`, `## Caveats`, "
                f"`## Related Pages` (include at least {overview_link}), and `## Sources` (`- {src_ref}`).\n\n"
                f"=== Primary Mirror ({src_ref}) ===\n{excerpt}"
            )

        generated = _strip_outer_markdown_fence(caller(sys_prompt, usr_prompt))
        if not generated.startswith("---"):
            generated = (
                f'---\ntitle: "{stem_title}"\ntype: source\nstatus: draft\nupdated: {today}\n'
                f"sources:\n  - {src_ref}\n---\n\n{generated}\n"
            )
        generated = _enforce_draft_status(generated)
        if overview_link not in generated:
            generated = generated.rstrip() + f"\n\n## {'关联页面' if is_zh else 'Related Pages'}\n\n{overview_link}\n"

        page_filename = f"{stem_title}.md"
        target_wp = cfg.wiki_root / page_filename
        idx_suffix = 2
        while target_wp.exists():
            page_filename = f"{stem_title}-{idx_suffix}.md"
            target_wp = cfg.wiki_root / page_filename
            idx_suffix += 1

        target_wp.write_text(generated.rstrip() + "\n", encoding="utf-8")
        compiled_pages.append(page_filename)
        catalog_additions.append((target_wp.stem, f"Compiled from `{src_ref}`"))
        done_steps += 1
        _emit("compiling_new", src_ref)

    # 2) Refresh stale Wiki pages
    for s_item in stale_items:
        wp = cfg.wiki_root / s_item["page"]
        _emit("refreshing_stale", s_item["page"])
        if not wp.is_file():
            continue
        old_raw = wp.read_text(encoding="utf-8", errors="replace")
        src_blobs: list[str] = []
        for s_ref in s_item["sources"]:
            rel = cfg.strip_source_prefix(clean_source_ref(s_ref))
            mp = cfg.mirrors_root / f"{rel}.md"
            if mp.is_file():
                _, mb = split_frontmatter(mp.read_text(encoding="utf-8", errors="replace"))
                src_blobs.append(f"--- SOURCE: {s_ref} ---\n{mb[:8000]}")
        if not src_blobs:
            continue

        usr_prompt = (
            f"Update this stale Wiki page ({wp.name}) to reflect the updated primary source(s) below. "
            f"Keep `updated: {today}` and preserve all valid `sources:` entries and wikilinks.\n\n"
            f"=== Existing Wiki Page ===\n{old_raw[:8000]}\n\n"
            + "\n\n".join(src_blobs)
        )
        updated_md = _strip_outer_markdown_fence(caller(sys_prompt, usr_prompt))
        if updated_md.startswith("---"):
            wp.write_text(updated_md.rstrip() + "\n", encoding="utf-8")
            refreshed_pages.append(wp.name)
        done_steps += 1
        _emit("refreshing_stale", wp.name)

    # 2.5) Consolidate Layer-4 Operational Memory (`notes/` / `记忆/`) into Compiled Wiki
    consolidated_notes: list[str] = []
    if active_notes:
        mem_store = MemoryStore(cfg.root)
        notes_title = "实战经验与勘误汇总" if is_zh else "Operational-Notes-and-Errata"
        notes_wp = cfg.wiki_root / f"{notes_title}.md"
        existing_notes_wiki = notes_wp.read_text(encoding="utf-8", errors="replace") if notes_wp.is_file() else ""
        all_note_srcs: list[str] = []
        note_blobs: list[str] = []
        for n in active_notes[:20]:
            for rs in n.related_sources:
                if rs not in all_note_srcs:
                    all_note_srcs.append(rs)
            note_blobs.append(f"- [{n.note_id} | {n.kind} | author={n.author} | refs={list(n.related_sources)}]: {n.text}")

        srcs_yaml = ("\n" + "\n".join(f"  - {s}" for s in all_note_srcs)) if all_note_srcs else " []"
        note_prompt = (
            f"Consolidate the following verified operational field notes and errata into a clean Wiki synthesis page "
            f"titled `{notes_title}`.\n"
            f"Start with YAML frontmatter:\n---\ntitle: \"{notes_title}\"\ntype: synthesis\nstatus: draft\n"
            f"updated: {today}\nsources:{srcs_yaml}\n---\n\n"
            f"Include `{overview_link}` under Related Pages.\n\n"
            + (f"=== Previous Page ===\n{existing_notes_wiki[:4000]}\n\n" if existing_notes_wiki else "")
            + "=== Pending Operational Notes ===\n"
            + "\n".join(note_blobs)
        )
        gen_notes_md = _strip_outer_markdown_fence(caller(sys_prompt, note_prompt))
        if not gen_notes_md.startswith("---"):
            gen_notes_md = (
                f'---\ntitle: "{notes_title}"\ntype: synthesis\nstatus: draft\nupdated: {today}\n'
                f"sources:{srcs_yaml}\n---\n\n{gen_notes_md}\n"
            )
        gen_notes_md = _enforce_draft_status(gen_notes_md)
        if overview_link not in gen_notes_md:
            gen_notes_md = gen_notes_md.rstrip() + f"\n\n## {'关联页面' if is_zh else 'Related Pages'}\n\n{overview_link}\n"
        was_existing = notes_wp.is_file()
        notes_wp.write_text(gen_notes_md.rstrip() + "\n", encoding="utf-8")
        if was_existing:
            if notes_wp.name not in refreshed_pages:
                refreshed_pages.append(notes_wp.name)
        else:
            compiled_pages.append(notes_wp.name)
            catalog_additions.append((notes_wp.stem, "Consolidated operational notes & field errata"))

        for n in active_notes[:20]:
            if n.kind != "errata":
                # Keep `errata` active so 1-hop Errata Pins remain pinned onto read-only primary chunks!
                mem_store.update_status(n.note_id, "consolidated", consolidated_into=f"{cfg.wiki_prefix}{notes_wp.name}")
            consolidated_notes.append(n.note_id)

    # 3) Update catalog (`index.md` / `目录.md`) and append `log.md` / `日志.md`
    if catalog_additions and cfg.catalog_path.is_file():
        cat_txt = cfg.catalog_path.read_text(encoding="utf-8", errors="replace").rstrip()
        new_lines = [f"- [[{stem}]] — {desc}" for stem, desc in catalog_additions if f"[[{stem}]]" not in cat_txt]
        if new_lines:
            cfg.catalog_path.write_text(cat_txt + "\n" + "\n".join(new_lines) + "\n", encoding="utf-8")

    if cfg.log_path.is_file() and (compiled_pages or refreshed_pages):
        log_txt = cfg.log_path.read_text(encoding="utf-8", errors="replace").rstrip()
        header = f"## {today}"
        touched = ", ".join([*compiled_pages, *refreshed_pages])
        row = f"| {now_hm} | compile | {touched} | Compiled {len(compiled_pages)} new, refreshed {len(refreshed_pages)} stale |"
        if header in log_txt:
            cfg.log_path.write_text(log_txt + "\n" + row + "\n", encoding="utf-8")
        else:
            tbl_hdr = (
                "| 时间 | 动作 | 涉及页面/资料 | 说明 |\n| --- | --- | --- | --- |"
                if is_zh
                else "| Time | Action | Targets | Notes |\n| --- | --- | --- | --- |"
            )
            cfg.log_path.write_text(f"{log_txt}\n\n{header}\n{tbl_hdr}\n{row}\n", encoding="utf-8")

    # 4) Snapshot verified hashes, rebuild FTS5 index, then lint
    _emit("rebuilding_index", cfg.name)
    for pname in (*compiled_pages, *refreshed_pages):
        snapshot_wiki_hashes(cfg.root, page=pname)

    idx_summary = rebuild_index(cfg.root)
    lint_report = lint_vault(cfg.root)

    return {
        "vault": cfg.name,
        "compiled_pages": compiled_pages,
        "refreshed_pages": refreshed_pages,
        "consolidated_notes": consolidated_notes,
        "remaining_uncompiled": remaining_uncompiled,
        "remaining_stale": remaining_stale,
        "batch_limit": max_items,
        "lint": lint_report,
        "index": idx_summary,
    }
