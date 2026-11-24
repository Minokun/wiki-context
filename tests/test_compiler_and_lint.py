"""Tests for offline AI Librarian compiler (`wctx compile`), freshness, and linter."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wiki_context.compiler import (  # noqa: E402
    build_http_llm_caller,
    compile_vault,
    plan_compilation,
)
from wiki_context.config import VaultConfig  # noqa: E402
from wiki_context.freshness import check_vault_freshness, clean_source_ref, parse_wiki_frontmatter, split_frontmatter  # noqa: E402
from wiki_context.lint import lint_vault  # noqa: E402
from wiki_context.mcp_server import KBService  # noqa: E402
from wiki_context.policy import mirror_skip_reason  # noqa: E402
from wiki_context.scaffold import init_vault  # noqa: E402
from wiki_context.sync import sync_vault  # noqa: E402


class TestCompilerAndLint(unittest.TestCase):
    def test_compile_new_and_stale_pages_and_lint_clean(self):
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "vault"
            init_vault(v, name="AutoCompileKB", layout="en")
            src_file = v / "sources" / "storage-sla.md"
            src_file.write_text("# Storage SLA\n\nBlock volume durability is 99.999% with 3 replicas.\n", encoding="utf-8")
            sync_vault(v)

            dry = compile_vault(v, dry_run=True)
            self.assertEqual(dry["uncompiled_sources"], ["sources/storage-sla.md"])

            def fake_llm(sys_p: str, usr_p: str) -> str:
                version_note = "5 replicas" if "5 replicas" in usr_p else "3 replicas"
                return (
                    '---\ntitle: "storage-sla"\ntype: source\nstatus: reviewed\nupdated: 2026-11-22\n'
                    "sources:\n  - sources/storage-sla.md\n---\n\n"
                    f"# storage-sla\n\n## Overview\nDurability spec ({version_note}).\n\n"
                    "## Related Pages\n\n[[Overview]]\n\n## Sources\n\n- sources/storage-sla.md\n"
                )

            res1 = compile_vault(v, llm_fn=fake_llm)
            self.assertEqual(res1["compiled_pages"], ["storage-sla.md"])
            self.assertTrue(res1["lint"]["ok"], res1["lint"])

            fresh = check_vault_freshness(v)
            by_page = {r["page"]: r["state"] for r in fresh}
            self.assertEqual(by_page["storage-sla.md"], "fresh")

            # Mutate raw source -> sync -> page becomes `stale` -> compile refreshes it!
            src_file.write_text("# Storage SLA\n\nBlock volume durability upgraded to 5 replicas.\n", encoding="utf-8")
            sync_vault(v)
            stale_rows = check_vault_freshness(v, only_stale=True)
            self.assertEqual([r["page"] for r in stale_rows], ["storage-sla.md"])

            res2 = compile_vault(v, llm_fn=fake_llm)
            self.assertEqual(res2["refreshed_pages"], ["storage-sla.md"])

            svc = KBService(v)
            try:
                ctx = svc.tool_context({"question": "How many replicas does block volume durability use?"})
                self.assertIn("5 replicas", ctx["context_markdown"])
            finally:
                svc.close()

            # Verify linter catches broken wikilink
            (v / "wiki" / "Broken.md").write_text(
                '---\ntitle: "Broken"\nsources:\n  - sources/storage-sla.md\n---\n\nSee [[GhostPage]].\n',
                encoding="utf-8",
            )
            rep = lint_vault(v)
            self.assertFalse(rep["ok"])
            self.assertTrue(any(e["code"] == "dangling-link" for e in rep["errors"]))

    def test_parentheses_in_source_filenames_not_recompiled(self):
        self.assertEqual(clean_source_ref("sources/a.md（第2节）"), "sources/a.md")
        self.assertEqual(clean_source_ref("sources/dir/（批注）"), "sources/dir/")
        self.assertEqual(
            clean_source_ref("史料/(已瘦身)方案(2).md"),
            "史料/(已瘦身)方案(2).md",
        )
        self.assertEqual(
            clean_source_ref("史料/开发方案（工控机基线）.md（第1章）"),
            "史料/开发方案（工控机基线）.md",
        )

        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "vault"
            init_vault(v, name="ParenKB", layout="zh")
            f1 = v / "史料" / "(已瘦身)方案(2).md"
            f2 = v / "史料" / "开发方案（工控机基线）.md"
            f1.write_text("# 瘦身方案\n\nGPU 内存占用降低 40%。\n", encoding="utf-8")
            f2.write_text("# 工控机基线\n\n支持双网口与看门狗。\n", encoding="utf-8")
            sync_vault(v)

            def fake_llm(sys_p: str, usr_p: str) -> str:
                if "(已瘦身)方案(2).md" in usr_p:
                    src = "史料/(已瘦身)方案(2).md"
                    stem = "(已瘦身)方案(2)"
                else:
                    src = "史料/开发方案（工控机基线）.md"
                    stem = "开发方案（工控机基线）"
                return (
                    f'---\ntitle: "{stem}"\ntype: source\nstatus: reviewed\nupdated: 2026-11-24\n'
                    f"sources:\n  - {src}\n---\n\n"
                    f"# {stem}\n\n## 概览\n摘要。\n\n## 关联页面\n\n[[知识库总览]]\n\n## 来源\n\n- {src}\n"
                )

            res = compile_vault(v, llm_fn=fake_llm, max_items=10)
            self.assertEqual(len(res["compiled_pages"]), 2)
            self.assertEqual(res["remaining_uncompiled"], 0)

            plan_after = plan_compilation(v)
            self.assertEqual(plan_after["uncompiled_sources"], [])
            self.assertEqual(plan_after["stale_pages"], [])

            for page_name in res["compiled_pages"]:
                fm, _ = split_frontmatter((v / "知识库" / page_name).read_text(encoding="utf-8"))
                meta = parse_wiki_frontmatter(fm)
                self.assertTrue(meta["source_hashes"], f"{page_name} missing source_hashes")

            rep = lint_vault(v)
            self.assertTrue(rep["ok"], rep)
            self.assertFalse(
                any(w["code"] == "unmirrored-or-missing-source" for w in rep["warnings"]),
                rep["warnings"],
            )

    def test_compiler_guardrails_draft_status_body_sources_and_exclusions(self):
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "zh-guard"
            init_vault(v, name="GuardKB", layout="zh")
            (v / "史料" / "spec.md").write_text("# 架构规范\n\n核心延迟低于 10ms。\n", encoding="utf-8")
            (v / "史料" / "汇总文档.md").write_text("# 汇总文档\n\n已在总览页整理。\n", encoding="utf-8")
            (v / "史料" / "发票报销" / "2026-04").mkdir(parents=True)
            (v / "史料" / "发票报销" / "2026-04" / "dzfp_001.md").write_text("# 发票\n\n金额 100 元。\n", encoding="utf-8")
            (v / "史料" / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")

            # Frontmatter-less Wiki page citing `史料/汇总文档.md` under `## 来源`
            (v / "知识库" / "专题汇总.md").write_text(
                "# 专题汇总\n\n## 概览\n\n人工综述。\n\n## 来源\n\n- 史料/汇总文档.md\n",
                encoding="utf-8",
            )

            ocr_cmd = f"{sys.executable} -c \"print('# OCR Diagram\\n\\nPort 8443')\""
            sync_vault(v)
            sync_vault(v, source="史料/diagram.png", ocr_cmd=ocr_cmd)

            cfg = VaultConfig.load(v)
            legacy_img_mirror = v / "史料_md" / "legacy.png.md"
            legacy_img_mirror.write_text("# legacy\n\nMinerU extracted text\n", encoding="utf-8")
            self.assertEqual(
                mirror_skip_reason(
                    legacy_img_mirror,
                    "legacy.png.md",
                    cfg=cfg,
                    manifest_entry={"converter": "MinerU 3.4.4"},
                ),
                "unwhitelisted-image-mirror",
            )
            self.assertIsNone(
                mirror_skip_reason(
                    v / "史料_md" / "diagram.png.md",
                    "diagram.png.md",
                    cfg=cfg,
                    manifest_entry={"converter": "vlm-caption"},
                )
            )

            plan = plan_compilation(v)
            self.assertEqual(plan["uncompiled_sources"], ["史料/spec.md"])

            plan_with_imgs = plan_compilation(v, include_images=True)
            self.assertEqual(plan_with_imgs["uncompiled_sources"], ["史料/diagram.png", "史料/spec.md"])

            def fake_llm_self_promoting(_sys: str, _usr: str) -> str:
                return (
                    '---\ntitle: "spec"\ntype: source\nstatus: reviewed\nupdated: 2026-11-24\n'
                    "sources:\n  - 史料/spec.md\n---\n\n"
                    "# spec\n\n## 概览\n核心延迟低于 10ms。\n\n## 关联页面\n\n[[知识库总览]]\n\n## 来源\n\n- 史料/spec.md\n"
                )

            res = compile_vault(v, llm_fn=fake_llm_self_promoting, max_items=10)
            self.assertEqual(res["compiled_pages"], ["spec.md"])
            fm, _ = split_frontmatter((v / "知识库" / "spec.md").read_text(encoding="utf-8"))
            self.assertEqual(parse_wiki_frontmatter(fm)["status"], "draft")
            self.assertEqual(plan_compilation(v)["uncompiled_sources"], [])

    def test_clean_double_ext_dotted_links_and_dangling_wikilink_sanitizer(self):
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "clean-kb"
            init_vault(v, name="CleanKB", layout="zh")
            (v / "史料" / "工艺策划书V2.0.pdf.md").write_text("# 工艺策划书\n\n工艺要求 V2.0。\n", encoding="utf-8")
            (v / "史料" / "机械电气评审.docx.docx.md").write_text("# 机械电气评审\n\n评审通过。\n", encoding="utf-8")
            sync_vault(v)

            def fake_llm(_sys: str, usr: str) -> str:
                import re as _re
                m_t = _re.search(r"标题：([^）\n]+)", usr)
                title = m_t.group(1) if m_t else "未命名"
                m_s = _re.search(r"sources:\n  - (.+)", usr)
                src = m_s.group(1).strip() if m_s else ""
                return (
                    f'---\ntitle: "{title}"\ntype: source\nstatus: reviewed\nupdated: 2026-11-24\n'
                    f"sources:\n  - {src}\n---\n\n"
                    f"# {title}\n\n## 概览\n供应商 [[成都某供应商]] 与 [[GhostPage|显示别名]] 已确认。\n\n"
                    f"```markdown\n[[KeepInsideCodeBlock]]\n```\n\n"
                    f"## 关联页面\n\n[[知识库总览]]\n\n## 来源\n\n- {src}\n"
                )

            res = compile_vault(v, llm_fn=fake_llm, max_items=10)
            self.assertEqual(sorted(res["compiled_pages"]), ["工艺策划书V2.0.md", "机械电气评审.md"])
            self.assertTrue(res["lint"]["ok"], res["lint"])
            self.assertEqual(res["lint"]["error_count"], 0)

            p1 = (v / "知识库" / "工艺策划书V2.0.md").read_text(encoding="utf-8")
            self.assertIn("供应商 成都某供应商 与 显示别名 已确认。", p1)
            self.assertNotIn("[[成都某供应商]]", p1)
            self.assertNotIn("[[GhostPage", p1)
            self.assertIn("[[KeepInsideCodeBlock]]", p1)
            self.assertIn("[[知识库总览]]", p1)

    def test_auto_triage_skips_daily_reports_and_clusters_project_sources(self):
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "cluster-kb"
            init_vault(v, name="ClusterKB", layout="zh")
            # Routine daily/weekly report dir should be skipped from compile, but remain searchable
            (v / "史料" / "周报日报" / "2026-11").mkdir(parents=True)
            (v / "史料" / "周报日报" / "2026-11" / "周报.md").write_text("# 本周周报\n\n例行汇报。\n", encoding="utf-8")

            # Multi-file project under svn/ should be clustered & prioritized behind high-value knowledge docs
            proj_dir = v / "史料" / "svn" / "JC16-4 重庆环线(1)" / "04设计开发"
            proj_dir.mkdir(parents=True)
            (proj_dir / "方案.docx.md").write_text("# 技术方案\n\n系统采用高速线阵相机，采样率 500Hz。\n", encoding="utf-8")
            (proj_dir / "BOM清单.xlsx.csv").write_text("物料编码,名称,数量\nA01,工控机,2\n", encoding="utf-8")

            # High-value knowledge doc should receive higher triage priority than raw svn project files
            kb_dir = v / "史料" / "钉钉知识库" / "系统部" / "核心规范"
            kb_dir.mkdir(parents=True)
            (kb_dir / "平台架构设计规范.md").write_text(
                "# 平台架构设计规范\n\n" + ("核心规范要求全链路延迟控制在 15ms 以内。" * 30) + "\n",
                encoding="utf-8",
            )
            sync_vault(v)

            plan = plan_compilation(v)
            self.assertFalse(any("周报日报" in s for s in plan["uncompiled_sources"]))

            seen_titles: list[str] = []

            def fake_cluster_llm(_sys: str, usr: str) -> str:
                import re as _re
                m_t = _re.search(r"标题：([^）\n]+)", usr)
                title = m_t.group(1) if m_t else "综述"
                seen_titles.append(title)
                # Intentionally omit one source in frontmatter to verify deterministic source-list integrity
                m_s = _re.search(r"sources:\n  - (.+)", usr)
                first_src = m_s.group(1).strip() if m_s else ""
                return (
                    f'---\ntitle: "{title}"\ntype: synthesis\nstatus: draft\nupdated: 2026-11-24\n'
                    f"sources:\n  - {first_src}\n---\n\n"
                    f"# {title}\n\n## 概览\n综合整理。\n\n## 关联页面\n\n[[知识库总览]]\n"
                )

            res = compile_vault(v, llm_fn=fake_cluster_llm, max_items=10)
            self.assertEqual(len(res["compiled_pages"]), 2)
            self.assertEqual(res["remaining_uncompiled"], 0)
            self.assertTrue(res["lint"]["ok"], res["lint"])
            # High-value knowledge spec compiled first, then clustered project overview
            self.assertEqual(seen_titles[0], "平台架构设计规范")
            self.assertEqual(seen_titles[1], "JC16-4 重庆环线-综述")

            cluster_wp = v / "知识库" / "JC16-4 重庆环线-综述.md"
            fm, _ = split_frontmatter(cluster_wp.read_text(encoding="utf-8"))
            meta = parse_wiki_frontmatter(fm)
            self.assertEqual(len(meta["sources"]), 2)
            self.assertEqual(len(meta["source_hashes"]), 2)
            self.assertEqual(plan_compilation(v)["uncompiled_sources"], [])

    def test_concurrent_compile_failure_isolation_catalog_summary_and_reviewed_protection(self):
        import json as _json
        import threading as _threading

        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "conc-kb"
            init_vault(v, name="ConcKB", layout="zh")
            for idx in range(1, 5):
                (v / "史料" / f"专题{idx}.md").write_text(
                    f"# 专题{idx}\n\n核心参数阈值 V{idx} 为 {idx * 100}ms。\n",
                    encoding="utf-8",
                )
            # Duplicate file with exact same content as 专题1.md should be deduped from compilation
            (v / "史料" / "专题1_转发副本.md").write_text(
                "# 专题1\n\n核心参数阈值 V1 为 100ms。\n",
                encoding="utf-8",
            )
            sync_vault(v)

            plan = plan_compilation(v)
            self.assertEqual(plan["duplicate_sources_skipped"], 1)
            self.assertEqual(len(plan["uncompiled_sources"]), 4)

            # Inject a visual sidecar on 专题1 to verify visual hint propagation
            sidecar = v / "史料_md" / "专题1.md.json"
            sidecar.write_text(
                _json.dumps({
                    "page_count": 12,
                    "visual_pages": [3, 8],
                    "assets": [{"asset_id": "page-003.png", "page_num": 2}],
                }),
                encoding="utf-8",
            )

            seen_visual_hint = {"ok": False}
            lock = _threading.Lock()

            def fake_parallel_llm(_sys: str, usr: str) -> str:
                import re as _re
                if "page-003.png" in usr and "wiki_inspect_visual" in usr:
                    with lock:
                        seen_visual_hint["ok"] = True
                m_t = _re.search(r"标题：([^）\n]+)", usr)
                title = m_t.group(1) if m_t else "未命名"
                if title == "专题3":
                    raise RuntimeError("simulated single-unit timeout")
                m_s = _re.search(r"sources:\n  - (.+)", usr)
                src = m_s.group(1).strip() if m_s else ""
                return (
                    f'---\ntitle: "{title}"\ntype: source\nstatus: draft\nupdated: 2026-11-24\n'
                    f"sources:\n  - {src}\n---\n\n"
                    f"# {title}\n\n## 概览\n\n总结 {title} 的核心指标与部署约束。\n\n"
                    f"## 关联页面\n\n[[知识库总览]]\n\n## 来源\n\n- {src}\n"
                )

            res = compile_vault(v, llm_fn=fake_parallel_llm, max_items=10, workers=4)
            self.assertTrue(seen_visual_hint["ok"])
            self.assertEqual(len(res["compiled_pages"]), 3)
            self.assertEqual(len(res["failed_units"]), 1)
            self.assertEqual(res["failed_units"][0]["title"], "专题3")
            self.assertTrue(res["lint"]["ok"], res["lint"])

            # Verify catalog uses extracted Chinese summary rather than mechanical `Compiled from`
            cat_txt = (v / "目录.md").read_text(encoding="utf-8")
            self.assertIn("总结 专题1 的核心指标与部署约束", cat_txt)
            self.assertNotIn("Compiled from", cat_txt)

            # Mark 专题1.md as human `status: reviewed`, mutate source, and verify compiler protects it by default
            p1_path = v / "知识库" / "专题1.md"
            p1_reviewed = p1_path.read_text(encoding="utf-8").replace("status: draft", "status: reviewed", 1)
            p1_path.write_text(p1_reviewed, encoding="utf-8")
            (v / "史料" / "专题1.md").write_text("# 专题1\n\n核心参数阈值 V1 调整为 999ms。\n", encoding="utf-8")
            sync_vault(v)

            res_prot = compile_vault(v, llm_fn=fake_parallel_llm, max_items=10, workers=2)
            self.assertIn("专题1.md", res_prot["skipped_reviewed_pages"])
            self.assertNotIn("专题1.md", res_prot["refreshed_pages"])
            self.assertIn("status: reviewed", p1_path.read_text(encoding="utf-8"))

    def test_long_doc_map_reduce_include_patterns_skip_csv_and_project_rollup(self):
        import json as _json

        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "rollup-kb"
            init_vault(v, name="RollupKB", layout="zh")
            # Long document (>24000 chars) with distinct marker at the very tail
            long_body = "# 超长手册\n\n" + ("## Page 1\n常规背景介绍内容，用于填充长度。\n" * 900) + "\n## Page 55\n压轴结论：TAIL_MARKER_9981。\n"
            (v / "史料" / "核心手册.md").write_text(long_body, encoding="utf-8")
            # Operational folders & CSV
            (v / "史料" / "svn" / "JC24-11重轨27号线" / "06生产和调试" / "36发货记录").mkdir(parents=True)
            (v / "史料" / "svn" / "JC24-11重轨27号线" / "06生产和调试" / "36发货记录" / "发货单.md").write_text("装箱明细\n", encoding="utf-8")
            proj_tech = v / "史料" / "svn" / "JC24-11重轨27号线" / "03方案设计" / "12总体技术方案"
            proj_tech.mkdir(parents=True)
            for i in range(1, 12):
                (proj_tech / f"子方案{i:02d}.md").write_text(f"# 子方案 {i}\n\n指标 {i}。\n", encoding="utf-8")
            (proj_tech / "参数表.csv").write_text("项,值\nA,1\n", encoding="utf-8")
            sync_vault(v)

            # Operational `36发货记录` is skipped by default; CSV skipped when skip_csv=True
            plan_no_csv = plan_compilation(v, skip_csv=True)
            self.assertFalse(any("36发货记录" in s for s in plan_no_csv["uncompiled_sources"]))
            self.assertFalse(any(s.endswith(".csv") for s in plan_no_csv["uncompiled_sources"]))

            # Project rollup compresses all 11 project docs into 1 single `项目总览` unit instead of 2 split chunks
            dry = compile_vault(v, dry_run=True, project_rollup=True, skip_csv=True, max_items=10)
            self.assertEqual(dry["cluster_count"], 2)
            titles = {c["title"] for c in dry["top_clusters"]}
            self.assertIn("JC24-11重轨27号线-项目总览", titles)

            # Verify Map-Reduce preserves TAIL_MARKER_9981 from the end of the 30k-char document
            saw_tail_in_reduce = {"ok": False}

            def fake_mr_llm(sys_p: str, usr_p: str) -> str:
                if "分段" in usr_p:
                    return "包含 TAIL_MARKER_9981 压轴结论。" if "TAIL_MARKER_9981" in usr_p else "常规背景要点。"
                if "TAIL_MARKER_9981" in usr_p:
                    saw_tail_in_reduce["ok"] = True
                return (
                    '---\ntitle: "核心手册"\ntype: source\nstatus: draft\nupdated: 2026-11-24\n'
                    "sources:\n  - 史料/核心手册.md\n---\n\n# 核心手册\n\n## 概览\n\n覆盖首尾全篇压轴结论 TAIL_MARKER_9981。\n\n"
                    "## 关联页面\n\n[[知识库总览]]\n\n## 来源\n\n- 史料/核心手册.md\n"
                )

            # Test compile_include_patterns whitelist restricting compile to only `核心手册.md`
            vcfg = _json.loads((v / "vault.json").read_text(encoding="utf-8"))
            vcfg["compile_include_patterns"] = ["核心手册.md"]
            (v / "vault.json").write_text(_json.dumps(vcfg, ensure_ascii=False, indent=2), encoding="utf-8")

            res = compile_vault(v, llm_fn=fake_mr_llm, max_items=10)
            self.assertEqual(res["compiled_pages"], ["核心手册.md"])
            self.assertTrue(saw_tail_in_reduce["ok"])

    def test_http_caller_local_keyless_and_disable_thinking_fallback(self):
        import json as _json
        import threading as _threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        seen_payloads: list[dict] = []

        class _LocalHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or "0")
                body = _json.loads(self.rfile.read(length).decode("utf-8"))
                seen_payloads.append(body)
                # Simulate strict OpenAI server rejecting `chat_template_kwargs` with HTTP 400
                if "chat_template_kwargs" in body:
                    err = b'{"error": "unknown parameter chat_template_kwargs"}'
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(err)))
                    self.end_headers()
                    self.wfile.write(err)
                    return
                out = b'{"choices": [{"message": {"content": "OK"}}]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        srv = HTTPServer(("127.0.0.1", 0), _LocalHandler)
        port = srv.server_address[1]
        _threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            # Keyless loopback endpoint + disable_thinking fallback + max_tokens
            caller = build_http_llm_caller(
                model="local-qwen",
                base_url=f"http://127.0.0.1:{port}/v1",
                api_key="",
                protocol="openai",
                max_tokens=1200,
                disable_thinking=True,
                retries=0,
            )
            self.assertEqual(caller("sys", "first"), "OK")
            # First call tried chat_template_kwargs, got 400, retried cleanly without it
            self.assertEqual(len(seen_payloads), 2)
            self.assertIn("chat_template_kwargs", seen_payloads[0])
            self.assertNotIn("chat_template_kwargs", seen_payloads[1])
            self.assertEqual(seen_payloads[1]["max_tokens"], 1200)

            # Second call remembers unsupported kwarg and sends only 1 request
            self.assertEqual(caller("sys", "second"), "OK")
            self.assertEqual(len(seen_payloads), 3)
            self.assertNotIn("chat_template_kwargs", seen_payloads[2])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_stale_body_only_refresh_finish_reason_length_and_inline_code_sanitizer(self):
        import json as _json
        import threading as _threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from wiki_context.compiler import _sanitize_dangling_wikilinks

        # F10 regression: inline code spans `[[x]]` must stay untouched while prose dangling links are unwrapped
        san = _sanitize_dangling_wikilinks(
            "Use `[[raw_var]]` and ``[[literal]]`` in code, see [[Missing Page]] and [[Overview]].",
            {"overview"},
        )
        self.assertIn("`[[raw_var]]`", san)
        self.assertIn("``[[literal]]``", san)
        self.assertIn("see Missing Page and [[Overview]].", san)

        # F3 regression: body-only stale-page answer synthesizes frontmatter & refreshes instead of silent None
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "stale-body-only"
            init_vault(v, name="StaleBody", layout="en")
            (v / "sources" / "sla.md").write_text("# SLA\n\nReplicas: 3\n", encoding="utf-8")
            sync_vault(v)

            def first_llm(_s: str, _u: str) -> str:
                return (
                    '---\ntitle: "SLA"\ntype: source\nstatus: draft\nupdated: 2026-11-24\n'
                    "sources:\n  - sources/sla.md\n---\n\n# SLA\n\n## Overview\n\nReplicas: 3.\n\n## Related Pages\n\n[[Overview]]\n"
                )

            r1 = compile_vault(v, llm_fn=first_llm, max_items=5)
            self.assertEqual(r1["compiled_pages"], ["sla.md"])

            # Modify source -> stale; LLM returns body WITHOUT frontmatter
            (v / "sources" / "sla.md").write_text("# SLA\n\nReplicas: 9\n", encoding="utf-8")
            sync_vault(v)
            r2 = compile_vault(
                v,
                llm_fn=lambda _s, _u: "# SLA\n\n## Overview\n\nUpdated replicas: 9.\n",
                max_items=5,
            )
            self.assertEqual(r2["refreshed_pages"], ["sla.md"])
            self.assertEqual(r2["failed_units"], [])
            refreshed_txt = (v / "wiki" / "sla.md").read_text(encoding="utf-8")
            self.assertTrue(refreshed_txt.startswith("---\n"))
            self.assertIn("Updated replicas: 9.", refreshed_txt)
            self.assertIn("sources/sla.md", refreshed_txt)
            self.assertEqual(plan_compilation(v)["stale_pages"], [])

        # F6 regression: finish_reason="length" retries once with larger max_tokens, then raises if still truncated
        req_max_tokens: list[int | None] = []
        mode = {"always_truncate": False}

        class _TruncHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("Content-Length") or "0")
                body = _json.loads(self.rfile.read(n).decode("utf-8"))
                mt = body.get("max_tokens")
                req_max_tokens.append(mt)
                truncated = mode["always_truncate"] or (mt is not None and mt <= 1000)
                resp = {
                    "choices": [{
                        "finish_reason": "length" if truncated else "stop",
                        "message": {"content": "partial..." if truncated else "COMPLETE PAGE"},
                    }]
                }
                raw = _json.dumps(resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        srv = HTTPServer(("127.0.0.1", 0), _TruncHandler)
        port = srv.server_address[1]
        _threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            caller = build_http_llm_caller(
                model="local-test",
                base_url=f"http://127.0.0.1:{port}/v1",
                api_key="",
                protocol="openai",
                max_tokens=512,
                retries=0,
            )
            self.assertEqual(caller("sys", "usr"), "COMPLETE PAGE")
            self.assertEqual(len(req_max_tokens), 2)
            self.assertEqual(req_max_tokens[0], 512)
            self.assertGreater(req_max_tokens[1], 1000)

            mode["always_truncate"] = True
            with self.assertRaises(RuntimeError) as ctx:
                caller("sys", "usr")
            self.assertIn("finish_reason=length", str(ctx.exception))
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
