"""Turnkey vault scaffolder (`wctx init`).

Supports `--layout en` (`sources/ mirrors/ wiki/ index.md log.md`) and
`--layout zh` (`史料/ 史料_md/ 知识库/ 目录.md 日志.md`). Never overwrites non-empty directories.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .config import LAYOUT_PRESETS


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
    catalog_f = preset["catalog_file"]
    log_f = preset["log_file"]
    overview_name = "知识库总览.md" if lay == "zh" else "Overview.md"

    planned = [
        f"{sources_d}/",
        f"{mirrors_d}/.index/",
        f"{wiki_d}/",
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
            "2. **日常问答**：优先单次调用 MCP `kb_context` 获取「Wiki 导读 + 一手史料 + 缺口预警」作答；未记载事实直说资料不足。\n"
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
            "2. **Retrieval**: Call MCP `kb_context` once to fetch compiled Wiki guidance + primary source chunks + grounding guardrails.\n"
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
