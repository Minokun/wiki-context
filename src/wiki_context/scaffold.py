"""Turnkey vault scaffolder (`wctx init`).

Supports `--layout en` (`sources/ mirrors/ wiki/ index.md log.md`) and
`--layout zh` (`史料/ 史料_md/ 知识库/ 目录.md 日志.md`). Never overwrites non-empty directories.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path

from .config import LAYOUT_PRESETS, VaultConfig

_IGNORED_DOT_NAMES = frozenset({".DS_Store", ".git", ".obsidian", ".vscode", ".idea", "__pycache__", "Thumbs.db"})
_VAULT_MARKER_NAMES = frozenset({
    "vault.json", ".vault.json",
    "sources", "mirrors", "wiki", "notes",
    "史料", "史料_md", "知识库", "记忆",
})


def init_vault(
    target: Path | str,
    *,
    name: str | None = None,
    layout: str = "en",
    dry_run: bool = False,
) -> dict:
    root = Path(target).expanduser().resolve()
    lay = layout.strip().lower()
    if lay not in LAYOUT_PRESETS:
        raise ValueError(f"Unsupported layout '{layout}'; choose 'en' or 'zh'")

    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"Refusing to initialize non-empty directory: {root}")

    preset = LAYOUT_PRESETS[lay]
    vault_name = (name or root.name or "knowledge-vault").strip()
    today = dt.date.today().isoformat()
    now_hm = dt.datetime.now().strftime("%H:%M")

    sources_d = preset["sources_dir"]
    mirrors_d = preset["mirrors_dir"]
    wiki_d = preset["wiki_dir"]
    notes_d = preset["notes_dir"]
    catalog_f = preset["catalog_file"]
    log_f = preset["log_file"]
    overview_name = "知识库总览.md" if lay == "zh" else "Overview.md"

    planned = [
        f"{sources_d}/",
        f"{mirrors_d}/.index/",
        f"{wiki_d}/",
        f"{notes_d}/",
        "vault.json",
        "synonyms.json",
        "AGENTS.md",
        catalog_f,
        log_f,
        f"{wiki_d}/{overview_name}",
    ]
    if dry_run:
        return {"target": str(root), "name": vault_name, "layout": lay, "planned": planned, "dry_run": True}

    (root / sources_d).mkdir(parents=True, exist_ok=True)
    (root / mirrors_d / ".index").mkdir(parents=True, exist_ok=True)
    (root / wiki_d).mkdir(parents=True, exist_ok=True)
    (root / notes_d).mkdir(parents=True, exist_ok=True)

    vault_cfg = {
        "name": vault_name,
        "layout": lay,
        "max_doc_bytes": 2097152,
        "sensitive": False,
        "exclude_patterns": [],
        "ocr_image_dirs": [],
        "anchor_terms": [],
    }
    (root / "vault.json").write_text(json.dumps(vault_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    synonyms_tmpl = {
        "version": 1,
        "description": "Domain canonical terms <- colloquial aliases (hot-reloaded on save)",
        "groups": [],
    }
    (root / "synonyms.json").write_text(json.dumps(synonyms_tmpl, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if lay == "zh":
        catalog_md = (
            f"# 目录\n\n## 入口\n\n- [[知识库总览]] —— {vault_name}总览与收录边界。\n\n"
            "## 专题页面\n\n<!-- 新增页面后在此登记：- [[页面名]] —— 一句话说明 -->\n"
        )
        log_md = (
            f"# 日志\n\n## {today}\n"
            "| 时间 | 动作 | 涉及页面/资料 | 说明 |\n| --- | --- | --- | --- |\n"
            f"| {now_hm} | init | vault.json; 目录.md; 知识库/知识库总览.md | 初始化三层知识库结构 |\n"
        )
        overview_md = (
            f"---\ntitle: \"知识库总览\"\ntype: index\nstatus: reviewed\nupdated: {today}\nsources: []\n---\n\n"
            f"# 知识库总览\n\n## 概览\n\n本库为「{vault_name}」，采用三层架构：`史料/` 为只读原始事实源，`史料_md/` 为结构化派生镜像与 FTS5 索引，`知识库/` 为持续编译维护的综述页。\n\n"
            "## 关联页面\n\n[[目录.md]]\n"
        )
        agents_md = (
            f"# {vault_name} · Agent 维护与检索规范\n\n"
            "1. **史料只读铁律**：严禁修改、重命名或删除 `史料/` 下任何原始文件；认证凭证严禁输出明文。\n"
            "2. **日常问答**：优先单次调用 MCP `wiki_context` 获取「Wiki 导读 + 一手史料 + 缺口预警」作答；未记载事实直说资料不足。\n"
            "3. **新增文档与整理闭环**：当用户说「新增文档 / 整理 / 入库 / ingest」时，依次执行：\n"
            "   - `wctx sync --vault .`（增量提取镜像并初建索引）\n"
            "   - 阅读新镜像，更新或新建 `知识库/*.md`、`目录.md`、`日志.md`（或运行 `wctx compile --vault .`）\n"
            "   - `wctx freshness --vault . snapshot` → `wctx lint --vault .` → `wctx index --vault .`（刷新 `bm25.sqlite` 热生效）\n"
        )
    else:
        catalog_md = (
            f"# Index\n\n## Entrypoint\n\n- [[Overview]] — Scope and navigation guide for {vault_name}.\n\n"
            "## Topics\n\n<!-- Register compiled Wiki pages here: - [[Page Title]] — One-line summary -->\n"
        )
        log_md = (
            f"# Log\n\n## {today}\n"
            "| Time | Action | Targets | Notes |\n| --- | --- | --- | --- |\n"
            f"| {now_hm} | init | vault.json; index.md; wiki/Overview.md | Initialized three-layer vault |\n"
        )
        overview_md = (
            f"---\ntitle: \"Overview\"\ntype: index\nstatus: reviewed\nupdated: {today}\nsources: []\n---\n\n"
            f"# Overview\n\n## Summary\n\n`{vault_name}` follows the three-layer Compiled Wiki architecture: "
            "`sources/` holds immutable raw documents, `mirrors/` holds extracted Markdown + SQLite FTS5 indexes, "
            "and `wiki/` holds synthesized topic pages with verifiable provenance.\n\n"
            "## Related Pages\n\n[[index.md]]\n"
        )
        agents_md = (
            f"# {vault_name} · Agent Maintenance & Retrieval Rules\n\n"
            "1. **Read-Only Raw Sources**: Never modify, rename, or delete files inside `sources/`. Never echo plaintext credentials.\n"
            "2. **Retrieval**: Call MCP `wiki_context` once to fetch compiled Wiki guidance + primary source chunks + grounding guardrails.\n"
            "3. **Ingest & Compile**: When asked to ingest or organize new documents:\n"
            "   - Run `wctx sync --vault .`\n"
            "   - Synthesize updates into `wiki/*.md`, `index.md`, and `log.md` (or run `wctx compile --vault .`)\n"
            "   - Run `wctx freshness --vault . snapshot`, `wctx lint --vault .`, and `wctx index --vault .`\n"
        )

    (root / catalog_f).write_text(catalog_md, encoding="utf-8")
    (root / log_f).write_text(log_md, encoding="utf-8")
    (root / wiki_d / overview_name).write_text(overview_md, encoding="utf-8")
    (root / "AGENTS.md").write_text(agents_md, encoding="utf-8")

    return {"target": str(root), "name": vault_name, "layout": lay, "created": planned, "dry_run": False}


def _check_safe_target_dir(root: Path) -> None:
    home = Path.home().resolve()
    if root == Path("/") or root == home or len(root.parts) <= 2:
        raise ValueError(f"Refusing to operate directly on system/home root '{root}'; choose a subfolder.")


def inspect_folder(target: Path | str) -> dict:
    """Classify a local directory before onboarding (`existing_vault` | `empty_or_new` | `raw_documents_folder`)."""
    root = Path(target).expanduser().resolve()
    if not root.exists():
        return {
            "path": str(root),
            "exists": False,
            "kind": "empty_or_new",
            "suggested_name": root.name or "knowledge-vault",
            "detected_layout": "zh" if any("\u4e00" <= c <= "\u9fff" for c in root.name) else "en",
            "loose_entries": [],
            "has_index": False,
        }
    if not root.is_dir():
        raise ValueError(f"Target is not a directory: {root}")

    has_vault_json = (root / "vault.json").is_file() or (root / ".vault.json").is_file()
    has_zh_dirs = (root / "史料").is_dir() or (root / "史料_md").is_dir() or (root / "知识库").is_dir()
    has_en_dirs = ((root / "sources").is_dir() and ((root / "mirrors").is_dir() or (root / "wiki").is_dir()))
    if has_vault_json or has_zh_dirs or has_en_dirs:
        cfg = VaultConfig.load(root)
        has_idx = (cfg.index_root / "current.json").is_file() or (cfg.index_root / "bm25.sqlite").is_file()
        return {
            "path": str(root),
            "exists": True,
            "kind": "existing_vault",
            "suggested_name": cfg.name,
            "detected_layout": cfg.layout,
            "loose_entries": [],
            "has_index": has_idx,
        }

    visible_children = [
        p.name for p in sorted(root.iterdir(), key=lambda x: x.name)
        if p.name not in _IGNORED_DOT_NAMES and not p.name.startswith(".")
    ]
    cjk_hint = any("\u4e00" <= c <= "\u9fff" for c in (root.name + "".join(visible_children[:10])))
    sug_layout = "zh" if cjk_hint else "en"
    if not visible_children:
        return {
            "path": str(root),
            "exists": True,
            "kind": "empty_or_new",
            "suggested_name": root.name or "knowledge-vault",
            "detected_layout": sug_layout,
            "loose_entries": [],
            "has_index": False,
        }

    return {
        "path": str(root),
        "exists": True,
        "kind": "raw_documents_folder",
        "suggested_name": root.name or "knowledge-vault",
        "detected_layout": sug_layout,
        "loose_entries": visible_children[:30],
        "loose_total": len(visible_children),
        "has_index": False,
    }


def onboard_folder(
    target: Path | str,
    *,
    name: str | None = None,
    layout: str | None = None,
    adopt_loose_files: bool = True,
    run_sync: bool = True,
) -> dict:
    """Initialize a new folder, adopt a raw document folder, or register an existing vault safely."""
    from .indexer import rebuild_index
    from .sync import sync_vault

    root = Path(target).expanduser().resolve()
    _check_safe_target_dir(root)
    info = inspect_folder(root)
    kind = info["kind"]
    eff_layout = (layout or info["detected_layout"] or "zh").strip().lower()
    if eff_layout not in LAYOUT_PRESETS:
        raise ValueError(f"Unsupported layout '{eff_layout}'; choose 'en' or 'zh'")
    vault_name = (name or info["suggested_name"] or root.name or "knowledge-vault").strip()

    moved_entries: list[str] = []
    if kind == "empty_or_new":
        if not root.exists() or not any(root.iterdir()):
            init_vault(root, name=vault_name, layout=eff_layout)
        else:
            # Has only ignored dotfiles like .DS_Store / .obsidian
            _scaffold_missing_files(root, vault_name=vault_name, layout=eff_layout)
    elif kind == "existing_vault":
        cfg_existing = VaultConfig.load(root)
        eff_layout = cfg_existing.layout
        if name and name.strip() and (root / "vault.json").is_file():
            try:
                raw_v = json.loads((root / "vault.json").read_text(encoding="utf-8"))
                if isinstance(raw_v, dict):
                    raw_v["name"] = vault_name
                    (root / "vault.json").write_text(json.dumps(raw_v, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except (OSError, ValueError):
                pass
        _scaffold_missing_files(root, vault_name=vault_name, layout=eff_layout)
    else:
        # raw_documents_folder
        if not adopt_loose_files:
            raise ValueError(
                f"Folder '{root}' contains {info.get('loose_total', 0)} existing items; enable adopt_loose_files to move them into sources."
            )
        preset = LAYOUT_PRESETS[eff_layout]
        sources_root = root / preset["sources_dir"]
        sources_root.mkdir(parents=True, exist_ok=True)
        reserved = _VAULT_MARKER_NAMES | {"synonyms.json", "AGENTS.md", "index.md", "log.md", "目录.md", "日志.md"}
        for child in sorted(list(root.iterdir()), key=lambda x: x.name):
            if child.name in _IGNORED_DOT_NAMES or child.name.startswith(".") or child.name in reserved:
                continue
            dest = sources_root / child.name
            if not dest.exists():
                shutil.move(str(child), str(dest))
                moved_entries.append(child.name)
        _scaffold_missing_files(root, vault_name=vault_name, layout=eff_layout)

    sync_report: dict = {}
    if run_sync:
        sync_report = sync_vault(root, rebuild=True)
    else:
        cfg_now = VaultConfig.load(root)
        if not (cfg_now.index_root / "current.json").is_file():
            sync_report = {"index": rebuild_index(root, force=True)}

    return {
        "path": str(root),
        "name": VaultConfig.load(root).name,
        "layout": VaultConfig.load(root).layout,
        "onboard_mode": kind,
        "moved_entries": moved_entries,
        "sync": sync_report,
    }


def _scaffold_missing_files(root: Path, *, vault_name: str, layout: str) -> None:
    """Ensure 4-layer directories and non-existent scaffold files exist without overwriting user files."""
    preset = LAYOUT_PRESETS[layout]
    sources_d = preset["sources_dir"]
    mirrors_d = preset["mirrors_dir"]
    wiki_d = preset["wiki_dir"]
    notes_d = preset["notes_dir"]
    catalog_f = preset["catalog_file"]
    log_f = preset["log_file"]
    overview_name = "知识库总览.md" if layout == "zh" else "Overview.md"

    (root / sources_d).mkdir(parents=True, exist_ok=True)
    (root / mirrors_d / ".index").mkdir(parents=True, exist_ok=True)
    (root / wiki_d).mkdir(parents=True, exist_ok=True)
    (root / notes_d).mkdir(parents=True, exist_ok=True)

    if not (root / "vault.json").is_file() and not (root / ".vault.json").is_file():
        vault_cfg = {
            "name": vault_name,
            "layout": layout,
            "max_doc_bytes": 2097152,
            "sensitive": False,
            "exclude_patterns": [],
            "ocr_image_dirs": [],
            "anchor_terms": [],
        }
        (root / "vault.json").write_text(json.dumps(vault_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if not (root / "synonyms.json").is_file():
        synonyms_tmpl = {
            "version": 1,
            "description": "Domain canonical terms <- colloquial aliases (hot-reloaded on save)",
            "groups": [],
        }
        (root / "synonyms.json").write_text(json.dumps(synonyms_tmpl, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    today = dt.date.today().isoformat()
    now_hm = dt.datetime.now().strftime("%H:%M")
    if layout == "zh":
        if not (root / catalog_f).is_file():
            (root / catalog_f).write_text(
                f"# 目录\n\n## 入口\n\n- [[知识库总览]] —— {vault_name}总览与收录边界。\n\n## 专题页面\n",
                encoding="utf-8",
            )
        if not (root / log_f).is_file():
            (root / log_f).write_text(
                f"# 日志\n\n## {today}\n| 时间 | 动作 | 涉及页面/资料 | 说明 |\n| --- | --- | --- | --- |\n"
                f"| {now_hm} | onboard | vault.json | 纳管并初始化知识库结构 |\n",
                encoding="utf-8",
            )
        if not any((root / wiki_d).glob("*.md")):
            (root / wiki_d / overview_name).write_text(
                f"---\ntitle: \"知识库总览\"\ntype: index\nstatus: reviewed\nupdated: {today}\nsources: []\n---\n\n"
                f"# 知识库总览\n\n## 概览\n\n本库为「{vault_name}」，采用四层架构：`史料/` 为只读原件，`史料_md/` 为派生镜像与 FTS5 索引，`知识库/` 为综述页，`记忆/` 为勘误与实战洞察。\n",
                encoding="utf-8",
            )
    else:
        if not (root / catalog_f).is_file():
            (root / catalog_f).write_text(
                f"# Index\n\n## Entrypoint\n\n- [[Overview]] — Scope and navigation guide for {vault_name}.\n\n## Topics\n",
                encoding="utf-8",
            )
        if not (root / log_f).is_file():
            (root / log_f).write_text(
                f"# Log\n\n## {today}\n| Time | Action | Targets | Notes |\n| --- | --- | --- | --- |\n"
                f"| {now_hm} | onboard | vault.json | Onboarded knowledge vault |\n",
                encoding="utf-8",
            )
        if not any((root / wiki_d).glob("*.md")):
            (root / wiki_d / overview_name).write_text(
                f"---\ntitle: \"Overview\"\ntype: index\nstatus: reviewed\nupdated: {today}\nsources: []\n---\n\n"
                f"# Overview\n\n## Summary\n\n`{vault_name}` uses the four-layer Compiled Wiki architecture.\n",
                encoding="utf-8",
            )


def import_external_folder(
    vault_path: Path | str,
    external_folder: Path | str,
    *,
    target_subfolder: str | None = None,
    run_sync: bool = True,
) -> dict:
    """Copy documents from an external folder into `<vault>/<sources_dir>/<subfolder>/` and run incremental sync."""
    from .sync import sync_vault

    cfg = VaultConfig.load(vault_path)
    vroot = cfg.root.resolve()
    ext = Path(external_folder).expanduser().resolve()
    _check_safe_target_dir(ext)
    if not ext.is_dir():
        raise ValueError(f"External folder does not exist: {ext}")
    if ext == vroot or vroot in ext.parents or ext in vroot.parents:
        raise ValueError("External folder must not overlap with or contain the target vault root")

    sub = (target_subfolder or ext.name or "imported").strip().strip("/").replace("..", "_")
    dest_root = (cfg.sources_root / sub).resolve()
    if cfg.sources_root.resolve() not in dest_root.parents and dest_root != cfg.sources_root.resolve():
        raise ValueError(f"Invalid target subfolder: {target_subfolder}")

    copied_files: list[str] = []
    skipped_same = 0
    for src_file in sorted(ext.rglob("*")):
        if src_file.is_symlink() or not src_file.is_file():
            continue
        rel_parts = src_file.relative_to(ext).parts
        if any(pt in _IGNORED_DOT_NAMES or pt.startswith(".") for pt in rel_parts):
            continue
        rel = src_file.relative_to(ext)
        dst_file = dest_root / rel
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        if dst_file.is_file() and dst_file.stat().st_size == src_file.stat().st_size:
            skipped_same += 1
            continue
        shutil.copy2(src_file, dst_file)
        copied_files.append(f"{sub}/{rel.as_posix()}")

    sync_rep = sync_vault(vroot, rebuild=True) if run_sync else {}
    return {
        "vault": cfg.name,
        "external_folder": str(ext),
        "target_subfolder": f"{cfg.sources_dir}/{sub}",
        "copied_count": len(copied_files),
        "skipped_unchanged": skipped_same,
        "copied_files": copied_files[:50],
        "sync": sync_rep,
    }
