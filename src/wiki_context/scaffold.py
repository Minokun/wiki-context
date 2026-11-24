"""Turnkey vault scaffolder (`wctx init`).

Supports `--layout en` (`sources/ mirrors/ wiki/ index.md log.md`) and
`--layout zh` (`史料/ 史料_md/ 知识库/ 目录.md 日志.md`). Never overwrites non-empty directories.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
from pathlib import Path

from .config import LAYOUT_PRESETS, VaultConfig, validate_external_source_path
from .policy import AUDIO_VIDEO_EXTS, IMAGE_EXTS, READABLE_EXTS
from .state_lock import atomic_write_text

_IGNORED_DOT_NAMES = frozenset({".DS_Store", ".git", ".obsidian", ".vscode", ".idea", "__pycache__", "Thumbs.db"})
_VAULT_MARKER_NAMES = frozenset({
    "vault.json", ".vault.json",
    "sources", "mirrors", "wiki", "notes",
    "史料", "史料_md", "知识库", "记忆",
})
_RESERVED_ROOT_NAMES = _VAULT_MARKER_NAMES | {
    "synonyms.json",
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "README.zh-CN.md",
    "index.md",
    "log.md",
    "目录.md",
    "日志.md",
}


def _render_agents_md(
    vault_name: str,
    layout: str,
    *,
    sources_path: Path | None = None,
    source_mounts: tuple[tuple[str, Path], ...] = (),
) -> str:
    """Render default AGENTS.md instructions aligned with the four-layer vault layout."""
    mapping_note = ""
    if sources_path is not None or source_mounts:
        if layout == "zh":
            lines = ["   - **只读外部源映射**（逻辑引用仍写作 `史料/...`）："]
            if sources_path is not None:
                lines.append(f"     - 主源目录：`{sources_path}`")
            for mname, mpath in source_mounts:
                lines.append(f"     - 挂载点 `史料/{mname}/`：`{mpath}`")
            mapping_note = "\n".join(lines) + "\n"
        else:
            lines = ["   - **Read-Only External Source Mapping** (logical citations remain `sources/...`):"]
            if sources_path is not None:
                lines.append(f"     - Primary source root: `{sources_path}`")
            for mname, mpath in source_mounts:
                lines.append(f"     - Mount `sources/{mname}/`: `{mpath}`")
            mapping_note = "\n".join(lines) + "\n"

    if layout == "zh":
        return (
            f"# {vault_name} · Agent 维护与检索规范\n\n"
            "1. **史料只读铁律**：严禁修改、重命名或删除 `史料/` 下任何原始文件；认证凭证严禁输出明文。\n"
            f"{mapping_note}"
            "2. **日常问答**：优先单次调用 MCP `wiki_context` 获取「Wiki 导读 + 一手史料 + 缺口预警」作答；未记载事实直说资料不足。\n"
            "3. **原件勘误与经验沉淀**：发现只读原件有勘误或实战踩坑结论时，不要改写 `史料/`，请通过 MCP `wiki_remember`（或 `wctx remember`）写入第 4 层 `记忆/` 挂接勘误钉子。\n"
            "4. **新增文档与整理闭环**：当用户说「新增文档 / 整理 / 入库 / ingest」时，依次执行：\n"
            "   - `wctx sync --vault .`（增量提取镜像并初建索引）\n"
            "   - 阅读新镜像，更新或新建 `知识库/*.md`、`目录.md`、`日志.md`（或运行 `wctx compile --vault .`）\n"
            "   - `wctx freshness --vault . snapshot` → `wctx lint --vault .` → `wctx index --vault .`（刷新 `bm25.sqlite` 热生效）\n"
        )
    return (
        f"# {vault_name} · Agent Maintenance & Retrieval Rules\n\n"
        "1. **Read-Only Raw Sources**: Never modify, rename, or delete files inside `sources/`. Never echo plaintext credentials.\n"
        f"{mapping_note}"
        "2. **Retrieval**: Call MCP `wiki_context` once to fetch compiled Wiki guidance + primary source chunks + grounding guardrails.\n"
        "3. **Errata & Field Notes**: When spotting errata in read-only sources or recording operational findings, never edit `sources/`; write to Layer-4 `notes/` via MCP `wiki_remember` (or `wctx remember`).\n"
        "4. **Ingest & Compile**: When asked to ingest or organize new documents:\n"
        "   - Run `wctx sync --vault .`\n"
        "   - Synthesize updates into `wiki/*.md`, `index.md`, and `log.md` (or run `wctx compile --vault .`)\n"
        "   - Run `wctx freshness --vault . snapshot`, `wctx lint --vault .`, and `wctx index --vault .`\n"
    )


def init_vault(
    target: Path | str,
    *,
    name: str | None = None,
    layout: str = "en",
    dry_run: bool = False,
    source_dir: Path | str | None = None,
    source_mounts: dict[str, Path | str] | None = None,
) -> dict:
    root = Path(target).expanduser().resolve()
    lay = layout.strip().lower()
    if lay not in LAYOUT_PRESETS:
        raise ValueError(f"Unsupported layout '{layout}'; choose 'en' or 'zh'")

    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"Refusing to initialize non-empty directory: {root}")

    resolved_source_dir: Path | None = None
    if source_dir is not None and str(source_dir).strip():
        resolved_source_dir = Path(source_dir).expanduser().resolve()
        _check_safe_target_dir(resolved_source_dir)
        validate_external_source_path(root, resolved_source_dir)

    resolved_mounts: dict[str, Path] = {}
    if source_mounts:
        for mk, mv in source_mounts.items():
            mname = str(mk).strip().strip("/").replace("\\", "/")
            if not mname or "/" in mname or mname in {".", ".."} or ".." in mname:
                raise ValueError(f"Invalid mount name: {mk!r}")
            mp = Path(mv).expanduser().resolve()
            _check_safe_target_dir(mp)
            validate_external_source_path(root, mp)
            resolved_mounts[mname] = mp

    preset = LAYOUT_PRESETS[lay]
    vault_name = (name or (resolved_source_dir.name if resolved_source_dir else "") or root.name or "knowledge-vault").strip()
    today = dt.date.today().isoformat()
    now_hm = dt.datetime.now().strftime("%H:%M")

    sources_d = preset["sources_dir"]
    mirrors_d = preset["mirrors_dir"]
    wiki_d = preset["wiki_dir"]
    notes_d = preset["notes_dir"]
    catalog_f = preset["catalog_file"]
    log_f = preset["log_file"]
    overview_name = "知识库总览.md" if lay == "zh" else "Overview.md"

    planned = (
        ([] if resolved_source_dir is not None else [f"{sources_d}/"])
        + [
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
    )
    if dry_run:
        return {
            "target": str(root),
            "name": vault_name,
            "layout": lay,
            "sources_path": str(resolved_source_dir) if resolved_source_dir else "",
            "planned": planned,
            "dry_run": True,
        }

    if resolved_source_dir is None:
        (root / sources_d).mkdir(parents=True, exist_ok=True)
    (root / mirrors_d / ".index").mkdir(parents=True, exist_ok=True)
    (root / wiki_d).mkdir(parents=True, exist_ok=True)
    (root / notes_d).mkdir(parents=True, exist_ok=True)

    vault_cfg: dict = {
        "name": vault_name,
        "layout": lay,
        "max_doc_bytes": 2097152,
        "sensitive": False,
        "exclude_patterns": [],
        "ocr_image_dirs": [],
        "anchor_terms": [],
    }
    if resolved_source_dir is not None:
        vault_cfg["sources_path"] = str(resolved_source_dir)
    if resolved_mounts:
        vault_cfg["source_mounts"] = {k: str(v) for k, v in sorted(resolved_mounts.items())}
    (root / "vault.json").write_text(json.dumps(vault_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    synonyms_tmpl = {
        "version": 1,
        "description": "Domain canonical terms <- colloquial aliases (hot-reloaded on save)",
        "groups": [],
    }
    (root / "synonyms.json").write_text(json.dumps(synonyms_tmpl, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if lay == "zh":
        src_desc = f"只读映射外部源目录 `{resolved_source_dir}`（逻辑路径 `史料/`）" if resolved_source_dir else "`史料/` 为只读原始事实源"
        catalog_md = (
            f"# 目录\n\n## 入口\n\n- [[知识库总览]] —— {vault_name}总览与收录边界。\n\n"
            "## 专题页面\n\n<!-- 新增页面后在此登记：- [[页面名]] —— 一句话说明 -->\n"
        )
        log_md = (
            f"# 日志\n\n## {today}\n"
            "| 时间 | 动作 | 涉及页面/资料 | 说明 |\n| --- | --- | --- | --- |\n"
            f"| {now_hm} | init | vault.json; 目录.md; 知识库/知识库总览.md | 初始化四层知识库结构 |\n"
        )
        overview_md = (
            f"---\ntitle: \"知识库总览\"\ntype: index\nstatus: reviewed\nupdated: {today}\nsources: []\n---\n\n"
            f"# 知识库总览\n\n## 概览\n\n本库为「{vault_name}」，采用四层架构：{src_desc}，`史料_md/` 为结构化派生镜像与 FTS5 索引，`知识库/` 为持续编译维护的综述页，`记忆/` 为勘误与实战洞察。\n\n"
            "## 关联页面\n\n[[目录.md]]\n"
        )
    else:
        src_desc = f"read-only external source root `{resolved_source_dir}` (logical prefix `sources/`)" if resolved_source_dir else "`sources/` holds immutable raw documents"
        catalog_md = (
            f"# Index\n\n## Entrypoint\n\n- [[Overview]] — Scope and navigation guide for {vault_name}.\n\n"
            "## Topics\n\n<!-- Register compiled Wiki pages here: - [[Page Title]] — One-line summary -->\n"
        )
        log_md = (
            f"# Log\n\n## {today}\n"
            "| Time | Action | Targets | Notes |\n| --- | --- | --- | --- |\n"
            f"| {now_hm} | init | vault.json; index.md; wiki/Overview.md | Initialized four-layer vault |\n"
        )
        overview_md = (
            f"---\ntitle: \"Overview\"\ntype: index\nstatus: reviewed\nupdated: {today}\nsources: []\n---\n\n"
            f"# Overview\n\n## Summary\n\n`{vault_name}` follows the four-layer Compiled Wiki architecture: "
            f"{src_desc}, `mirrors/` holds extracted Markdown + SQLite FTS5 indexes, "
            "`wiki/` holds synthesized topic pages with verifiable provenance, and `notes/` stores operational memory and source-anchored errata.\n\n"
            "## Related Pages\n\n[[index.md]]\n"
        )

    agents_md = _render_agents_md(
        vault_name,
        lay,
        sources_path=resolved_source_dir,
        source_mounts=tuple(sorted(resolved_mounts.items())),
    )

    (root / catalog_f).write_text(catalog_md, encoding="utf-8")
    (root / log_f).write_text(log_md, encoding="utf-8")
    (root / wiki_d / overview_name).write_text(overview_md, encoding="utf-8")
    (root / "AGENTS.md").write_text(agents_md, encoding="utf-8")

    return {
        "target": str(root),
        "name": vault_name,
        "layout": lay,
        "sources_path": str(resolved_source_dir) if resolved_source_dir else "",
        "storage_mode": "managed" if resolved_source_dir is not None else "in_place",
        "created": planned,
        "dry_run": False,
    }


def _check_safe_target_dir(root: Path) -> None:
    home = Path.home().resolve()
    if root == Path("/") or root == home or len(root.parts) <= 2:
        raise ValueError(f"Refusing to operate directly on system/home root '{root}'; choose a subfolder.")


def _count_readable_files(root: Path, *, limit: int = 5000) -> int:
    """Count non-hidden readable/media files under ``root`` (fast, bounded, symlink-safe)."""
    if not root.is_dir():
        return 0
    exts = READABLE_EXTS | IMAGE_EXTS | AUDIO_VIDEO_EXTS
    count = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DOT_NAMES and not d.startswith(".")]
        for fn in filenames:
            if fn in _IGNORED_DOT_NAMES or fn.startswith("."):
                continue
            ext = Path(fn).suffix.lower()
            if ext in exts:
                count += 1
                if count >= limit:
                    return count
    return count


def _paths_overlap(a: Path, b: Path) -> bool:
    ra = a.expanduser().resolve()
    rb = b.expanduser().resolve()
    return ra == rb or ra in rb.parents or rb in ra.parents


def _load_raw_vault_json(vault_root: Path) -> tuple[Path, dict]:
    for name in ("vault.json", ".vault.json"):
        p = vault_root / name
        if p.is_file():
            loaded = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return p, loaded
    return vault_root / "vault.json", {}


def inspect_folder(target: Path | str) -> dict:
    """Classify a local directory before onboarding (`existing_vault` | `empty_or_new` | `raw_documents_folder`)."""
    root = Path(target).expanduser().resolve()
    if not root.exists():
        return {
            "path": str(root),
            "exists": False,
            "kind": "empty_or_new",
            "recommended_storage_mode": "managed",
            "suggested_name": root.name or "knowledge-vault",
            "detected_layout": "zh" if any("\u4e00" <= c <= "\u9fff" for c in root.name) else "en",
            "loose_entries": [],
            "loose_total": 0,
            "file_count": 0,
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
        loose_exts = READABLE_EXTS | IMAGE_EXTS | AUDIO_VIDEO_EXTS
        loose_docs = [
            p.name
            for p in sorted(root.iterdir(), key=lambda x: x.name)
            if p.is_file()
            and not p.is_symlink()
            and p.name not in _IGNORED_DOT_NAMES
            and not p.name.startswith(".")
            and p.name not in _RESERVED_ROOT_NAMES
            and p.suffix.lower() in loose_exts
        ]
        src_count = sum(_count_readable_files(sr) for _, sr in cfg.iter_source_roots() if sr.is_dir())
        return {
            "path": str(root),
            "exists": True,
            "kind": "existing_vault",
            "storage_mode": cfg.storage_mode,
            "sources_path": str(cfg.sources_root),
            "recommended_storage_mode": "in_place",
            "suggested_name": cfg.name,
            "detected_layout": cfg.layout,
            "loose_entries": loose_docs[:30],
            "loose_total": len(loose_docs),
            "file_count": src_count + len(loose_docs),
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
            "recommended_storage_mode": "managed",
            "suggested_name": root.name or "knowledge-vault",
            "detected_layout": sug_layout,
            "loose_entries": [],
            "loose_total": 0,
            "file_count": 0,
            "has_index": False,
        }

    file_count = _count_readable_files(root)
    return {
        "path": str(root),
        "exists": True,
        "kind": "raw_documents_folder",
        "recommended_storage_mode": "managed",
        "suggested_name": root.name or "knowledge-vault",
        "detected_layout": sug_layout,
        "loose_entries": visible_children[:30],
        "loose_total": len(visible_children),
        "file_count": file_count,
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
        if adopt_loose_files and info.get("loose_entries") and cfg_existing.sources_path is None:
            sources_root = cfg_existing.sources_root
            sources_root.mkdir(parents=True, exist_ok=True)
            for child in sorted(list(root.iterdir()), key=lambda x: x.name):
                if (
                    not child.is_file()
                    or child.is_symlink()
                    or child.name in _IGNORED_DOT_NAMES
                    or child.name.startswith(".")
                    or child.name in _RESERVED_ROOT_NAMES
                    or child.suffix.lower() not in READABLE_EXTS
                ):
                    continue
                dest = sources_root / child.name
                if not dest.exists():
                    shutil.move(str(child), str(dest))
                    moved_entries.append(child.name)
        _scaffold_missing_files(
            root,
            vault_name=vault_name,
            layout=eff_layout,
            create_sources_dir=(cfg_existing.sources_path is None),
            sources_path=cfg_existing.sources_path,
            source_mounts=cfg_existing.source_mounts,
        )
    else:
        # raw_documents_folder
        if not adopt_loose_files:
            raise ValueError(
                f"Folder '{root}' contains {info.get('loose_total', 0)} existing items; enable adopt_loose_files to move them into sources."
            )
        preset = LAYOUT_PRESETS[eff_layout]
        sources_root = root / preset["sources_dir"]
        sources_root.mkdir(parents=True, exist_ok=True)
        for child in sorted(list(root.iterdir()), key=lambda x: x.name):
            if child.name in _IGNORED_DOT_NAMES or child.name.startswith(".") or child.name in _RESERVED_ROOT_NAMES:
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

    cfg_final = VaultConfig.load(root)
    return {
        "path": str(root),
        "workspace_path": str(root),
        "sources_path": str(cfg_final.sources_root),
        "storage_mode": cfg_final.storage_mode,
        "name": cfg_final.name,
        "layout": cfg_final.layout,
        "onboard_mode": kind,
        "moved_entries": moved_entries,
        "sync": sync_report,
    }


def create_managed_vault(
    workspace_dir: Path | str,
    *,
    source_dir: Path | str,
    name: str | None = None,
    layout: str | None = None,
    source_mounts: dict[str, Path | str] | None = None,
    run_sync: bool = True,
) -> dict:
    """Create a managed workspace under ``workspace_dir`` pointing read-only at ``source_dir``.

    Never creates, moves, renames, or writes any file inside ``source_dir`` (except creating
    ``source_dir`` itself as an empty folder if the user picked a brand-new non-existent path).
    """
    from .indexer import rebuild_index
    from .sync import sync_vault

    ws = Path(workspace_dir).expanduser().resolve()
    src = Path(source_dir).expanduser().resolve()
    _check_safe_target_dir(src)
    validate_external_source_path(ws, src)

    if not src.exists():
        src.mkdir(parents=True, exist_ok=True)
    elif not src.is_dir():
        raise ValueError(f"Source path is not a directory: {src}")

    info = inspect_folder(src)
    eff_layout = (layout or info["detected_layout"] or "zh").strip().lower()
    if eff_layout not in LAYOUT_PRESETS:
        raise ValueError(f"Unsupported layout '{eff_layout}'; choose 'en' or 'zh'")
    vault_name = (name or info["suggested_name"] or src.name or "knowledge-vault").strip()

    init_vault(
        ws,
        name=vault_name,
        layout=eff_layout,
        source_dir=src,
        source_mounts=source_mounts,
    )

    sync_report: dict = {}
    if run_sync:
        sync_report = sync_vault(ws, rebuild=True)
    else:
        cfg_now = VaultConfig.load(ws)
        if not (cfg_now.index_root / "current.json").is_file():
            sync_report = {"index": rebuild_index(ws, force=True)}

    cfg_final = VaultConfig.load(ws)
    return {
        "path": str(ws),
        "workspace_path": str(ws),
        "sources_path": str(cfg_final.sources_root),
        "storage_mode": "managed",
        "name": cfg_final.name,
        "layout": cfg_final.layout,
        "onboard_mode": "managed",
        "moved_entries": [],
        "sync": sync_report,
    }


def _scaffold_missing_files(
    root: Path,
    *,
    vault_name: str,
    layout: str,
    create_sources_dir: bool = True,
    sources_path: Path | None = None,
    source_mounts: tuple[tuple[str, Path], ...] = (),
) -> None:
    """Ensure 4-layer directories and non-existent scaffold files exist without overwriting user files."""
    preset = LAYOUT_PRESETS[layout]
    sources_d = preset["sources_dir"]
    mirrors_d = preset["mirrors_dir"]
    wiki_d = preset["wiki_dir"]
    notes_d = preset["notes_dir"]
    catalog_f = preset["catalog_file"]
    log_f = preset["log_file"]
    overview_name = "知识库总览.md" if layout == "zh" else "Overview.md"

    if create_sources_dir:
        (root / sources_d).mkdir(parents=True, exist_ok=True)
    (root / mirrors_d / ".index").mkdir(parents=True, exist_ok=True)
    (root / wiki_d).mkdir(parents=True, exist_ok=True)
    (root / notes_d).mkdir(parents=True, exist_ok=True)

    if not (root / "vault.json").is_file() and not (root / ".vault.json").is_file():
        vault_cfg: dict = {
            "name": vault_name,
            "layout": layout,
            "max_doc_bytes": 2097152,
            "sensitive": False,
            "exclude_patterns": [],
            "ocr_image_dirs": [],
            "anchor_terms": [],
        }
        if sources_path is not None:
            vault_cfg["sources_path"] = str(sources_path)
        (root / "vault.json").write_text(json.dumps(vault_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if not (root / "synonyms.json").is_file():
        synonyms_tmpl = {
            "version": 1,
            "description": "Domain canonical terms <- colloquial aliases (hot-reloaded on save)",
            "groups": [],
        }
        (root / "synonyms.json").write_text(json.dumps(synonyms_tmpl, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    agents_path = root / "AGENTS.md"
    if not agents_path.exists() and not agents_path.is_symlink():
        agents_path.write_text(
            _render_agents_md(vault_name, layout, sources_path=sources_path, source_mounts=source_mounts),
            encoding="utf-8",
        )

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


def mount_source_folder(
    vault_path: Path | str,
    external_folder: Path | str,
    *,
    mount_name: str | None = None,
    run_sync: bool = True,
) -> dict:
    """Zero-copy read-only mount an external folder under ``vault.json`` ``source_mounts``."""
    from .sync import sync_vault

    cfg = VaultConfig.load(vault_path)
    vroot = cfg.root.resolve()
    ext = Path(external_folder).expanduser().resolve()
    _check_safe_target_dir(ext)
    if not ext.is_dir():
        raise ValueError(f"外部文件夹不存在：{ext}")
    validate_external_source_path(vroot, ext)

    for existing_name, existing_root in cfg.iter_source_roots():
        if _paths_overlap(ext, existing_root):
            label = f"挂载点 '{existing_name}'" if existing_name else "主源目录"
            raise ValueError(f"目录 '{ext}' 与当前知识库的{label}（{existing_root}）存在重合或包含关系，无需重复挂载")

    explicit_name = bool(mount_name and mount_name.strip())
    raw_name = (mount_name or ext.name or "mounted").strip().strip("/").replace("\\", "/")
    base_name = raw_name.replace("/", "_").replace("..", "_").strip() or "mounted"
    if base_name in {".", ".."} or base_name.startswith("."):
        raise ValueError(f"非法的挂载点名称：{mount_name!r}")

    cfg_file, raw_cfg = _load_raw_vault_json(vroot)
    mounts = dict(raw_cfg.get("source_mounts") or {}) if isinstance(raw_cfg.get("source_mounts"), dict) else {}

    existing_top: set[str] = {str(k).strip().strip("/").lower() for k in mounts.keys()}
    if cfg.sources_root.is_dir():
        try:
            for ch in cfg.sources_root.iterdir():
                existing_top.add(ch.name.lower())
        except OSError:
            pass

    final_name = base_name
    if final_name.lower() in existing_top:
        if explicit_name:
            raise ValueError(f"挂载点名称 '{final_name}' 已存在，请更换一个名称")
        idx = 2
        while f"{base_name}-{idx}".lower() in existing_top:
            idx += 1
        final_name = f"{base_name}-{idx}"

    mounts[final_name] = str(ext)
    raw_cfg["source_mounts"] = mounts
    atomic_write_text(cfg_file, json.dumps(raw_cfg, ensure_ascii=False, indent=2) + "\n")

    sync_rep = sync_vault(vroot, rebuild=True) if run_sync else {}
    return {
        "vault": cfg.name,
        "mode": "mount",
        "mounted": True,
        "mount_name": final_name,
        "external_folder": str(ext),
        "target_subfolder": f"{cfg.sources_dir}/{final_name}",
        "copied_count": 0,
        "sync": sync_rep,
    }


def _prune_missing_mirrors_for_root(
    cfg: VaultConfig,
    *,
    mount_name: str,
    new_dir: Path,
) -> int:
    """Remove derived mirrors/manifest entries under the relinked root whose files no longer exist in ``new_dir``.

    Unchanged files present at the same relative path in ``new_dir`` are kept untouched so moving/renaming
    a folder incurs zero re-extraction and zero re-compilation.
    """
    mirrors_root = cfg.mirrors_root.resolve()
    if not mirrors_root.is_dir():
        return 0
    assets_root = (mirrors_root / ".assets").resolve()
    mount_prefixes = tuple(f"{mname}/" for mname, _ in cfg.source_mounts)

    def _belongs_and_subrel(rel_key: str) -> str | None:
        clean = str(rel_key).replace("\\", "/").lstrip("/")
        if not clean or ".." in clean.split("/"):
            return None
        if mount_name:
            pfx = f"{mount_name}/"
            if clean.startswith(pfx):
                return clean[len(pfx):]
            return None
        if any(clean.startswith(mpfx) for mpfx in mount_prefixes):
            return None
        return clean

    pruned_rels: list[str] = []
    manifest_file = mirrors_root / ".manifest.json"
    if manifest_file.is_file():
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
        if isinstance(manifest, dict):
            kept_manifest: dict = {}
            for rel_key, entry in manifest.items():
                sub = _belongs_and_subrel(rel_key)
                if sub is not None:
                    cand = new_dir / sub
                    if not (cand.is_file() and not cand.is_symlink()):
                        pruned_rels.append(str(rel_key))
                        continue
                kept_manifest[rel_key] = entry
            if pruned_rels:
                atomic_write_text(
                    manifest_file,
                    json.dumps(kept_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                )

    for rel_key in pruned_rels:
        clean = str(rel_key).replace("\\", "/").lstrip("/")
        for suffix in (".md", ".json"):
            target_f = (mirrors_root / f"{clean}{suffix}").resolve()
            if mirrors_root in target_f.parents and target_f.is_file():
                target_f.unlink(missing_ok=True)
        a_dir = (assets_root / clean).resolve()
        if assets_root in a_dir.parents and a_dir.is_dir():
            shutil.rmtree(a_dir, ignore_errors=True)

    failed_file = mirrors_root / ".failed.json"
    if failed_file.is_file():
        try:
            fdoc = json.loads(failed_file.read_text(encoding="utf-8"))
            if isinstance(fdoc, dict):
                kept_f = {}
                changed_f = False
                for rel_key, val in fdoc.items():
                    sub = _belongs_and_subrel(rel_key)
                    if sub is not None and not (new_dir / sub).is_file():
                        changed_f = True
                        continue
                    kept_f[rel_key] = val
                if changed_f:
                    if kept_f:
                        atomic_write_text(
                            failed_file,
                            json.dumps(kept_f, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        )
                    else:
                        failed_file.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

    return len(pruned_rels)


def unmount_source_folder(
    vault_path: Path | str,
    mount_name: str,
    *,
    purge_mirrors: bool = True,
    rebuild: bool = True,
) -> dict:
    """Remove a read-only external mount from ``vault.json`` (never touches the external folder itself)."""
    from .indexer import rebuild_index

    vroot = Path(vault_path).expanduser().resolve()
    clean_name = (mount_name or "").strip().strip("/")
    cfg_file, raw_cfg = _load_raw_vault_json(vroot)
    mounts = dict(raw_cfg.get("source_mounts") or {}) if isinstance(raw_cfg.get("source_mounts"), dict) else {}

    matched_keys = [
        k for k in list(mounts.keys())
        if str(k).strip().strip("/").replace("\\", "/") == clean_name
    ]
    if not matched_keys:
        raise KeyError(f"未找到挂载点 '{clean_name}'")

    old_path = str(mounts[matched_keys[0]])
    for mk in matched_keys:
        mounts.pop(mk, None)
    if mounts:
        raw_cfg["source_mounts"] = mounts
    else:
        raw_cfg.pop("source_mounts", None)
    atomic_write_text(cfg_file, json.dumps(raw_cfg, ensure_ascii=False, indent=2) + "\n")

    cfg = VaultConfig.load(vroot)
    purged_mirrors = 0
    if purge_mirrors and clean_name and "/" not in clean_name and ".." not in clean_name:
        m_dir = (cfg.mirrors_root / clean_name).resolve()
        if cfg.mirrors_root.resolve() in m_dir.parents and m_dir.is_dir():
            shutil.rmtree(m_dir, ignore_errors=True)
        a_dir = (cfg.mirrors_root / ".assets" / clean_name).resolve()
        if (cfg.mirrors_root / ".assets").resolve() in a_dir.parents and a_dir.is_dir():
            shutil.rmtree(a_dir, ignore_errors=True)
        pfx = f"{clean_name}/"
        for meta_name in (".manifest.json", ".failed.json"):
            mfile = cfg.mirrors_root / meta_name
            if mfile.is_file():
                try:
                    doc = json.loads(mfile.read_text(encoding="utf-8"))
                    if isinstance(doc, dict):
                        before = len(doc)
                        filtered = {k: v for k, v in doc.items() if not str(k).startswith(pfx)}
                        if len(filtered) != before:
                            if meta_name == ".manifest.json":
                                purged_mirrors = before - len(filtered)
                            atomic_write_text(
                                mfile,
                                json.dumps(filtered, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                            )
                except (OSError, ValueError):
                    pass

    idx_summary = rebuild_index(vroot, force=True) if rebuild else None
    return {
        "vault": cfg.name,
        "unmounted": True,
        "mount_name": clean_name,
        "old_path": old_path,
        "purged_mirrors": purged_mirrors,
        "index": idx_summary,
    }


def relink_source_path(
    vault_path: Path | str,
    new_source_dir: Path | str,
    *,
    mount_name: str | None = None,
    run_sync: bool = True,
) -> dict:
    """Update external ``sources_path`` (or a ``source_mounts`` entry) when the user moved/renamed their folder."""
    from .sync import sync_vault

    vroot = Path(vault_path).expanduser().resolve()
    new_dir = Path(new_source_dir).expanduser().resolve()
    _check_safe_target_dir(new_dir)
    if not new_dir.is_dir():
        raise ValueError(f"新的源目录不存在：{new_dir}")
    validate_external_source_path(vroot, new_dir)

    cfg_file, raw_cfg = _load_raw_vault_json(vroot)
    clean_mount = (mount_name or "").strip().strip("/")

    if clean_mount:
        cfg = VaultConfig.load(vroot)
        mounts = dict(raw_cfg.get("source_mounts") or {}) if isinstance(raw_cfg.get("source_mounts"), dict) else {}
        matched_keys = [
            k for k in list(mounts.keys())
            if str(k).strip().strip("/").replace("\\", "/") == clean_mount
        ]
        if not matched_keys:
            raise KeyError(f"未找到挂载点 '{clean_mount}'")
        for other_name, other_root in cfg.iter_source_roots():
            if other_name == clean_mount:
                continue
            if _paths_overlap(new_dir, other_root):
                raise ValueError(f"新目录 '{new_dir}' 与其他已挂载源目录 '{other_root}' 重合")
        old_path = str(mounts[matched_keys[0]])
        for mk in matched_keys:
            mounts.pop(mk, None)
        mounts[clean_mount] = str(new_dir)
        raw_cfg["source_mounts"] = mounts
    else:
        old_raw_sp = str(raw_cfg.get("sources_path") or "").strip()
        if not old_raw_sp:
            raise ValueError("当前知识库为原地自包含模式，原始文件就在知识库目录内，无需重定位外部源目录")
        # Check against any valid existing mounts without letting a broken sources_path block repair
        raw_mounts = raw_cfg.get("source_mounts")
        if isinstance(raw_mounts, dict):
            for mk, mv in raw_mounts.items():
                mv_s = str(mv or "").strip()
                if not mv_s:
                    continue
                mp = Path(mv_s).expanduser()
                mp = (mp if mp.is_absolute() else (vroot / mp)).resolve()
                if _paths_overlap(new_dir, mp):
                    raise ValueError(f"新主源目录 '{new_dir}' 与挂载点 '{mk}'（{mp}）重合")
        old_path = old_raw_sp
        raw_cfg["sources_path"] = str(new_dir)

    atomic_write_text(cfg_file, json.dumps(raw_cfg, ensure_ascii=False, indent=2) + "\n")
    cfg_after = VaultConfig.load(vroot)
    pruned_missing = _prune_missing_mirrors_for_root(cfg_after, mount_name=clean_mount, new_dir=new_dir)
    sync_rep = sync_vault(vroot, rebuild=True) if run_sync else {}
    return {
        "vault": cfg_after.name,
        "mount_name": clean_mount,
        "old_path": old_path,
        "new_path": str(new_dir),
        "pruned_missing": pruned_missing,
        "sync": sync_rep,
    }


def import_external_folder(
    vault_path: Path | str,
    external_folder: Path | str,
    *,
    target_subfolder: str | None = None,
    run_sync: bool = True,
) -> dict:
    """Attach an external folder: zero-copy read-only mount on external-source vaults, or copy on in-place vaults."""
    from .sync import sync_vault

    cfg = VaultConfig.load(vault_path)
    if cfg.sources_path is not None:
        return mount_source_folder(
            vault_path,
            external_folder,
            mount_name=target_subfolder,
            run_sync=run_sync,
        )

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
        "mode": "copy",
        "external_folder": str(ext),
        "target_subfolder": f"{cfg.sources_dir}/{sub}",
        "copied_count": len(copied_files),
        "skipped_unchanged": skipped_same,
        "copied_files": copied_files[:50],
        "sync": sync_rep,
    }
