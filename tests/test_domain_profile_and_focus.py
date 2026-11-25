"""Tests for AI Domain Profile auto-triggers, `focus_terms` retrieval, synonym cleanup, and telemetry migration."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wiki_context.compiler import (  # noqa: E402
    collect_domain_profile_signals,
    compile_vault,
    generate_domain_profile,
    should_refresh_domain_profile,
)
from wiki_context.config import VaultConfig, load_domain_profile, save_domain_profile  # noqa: E402
from wiki_context.console import ConsoleState  # noqa: E402
from wiki_context.context import ContextPackEngine, SynonymBook  # noqa: E402
from wiki_context.indexer import rebuild_index  # noqa: E402
from wiki_context.mcp_server import KBService  # noqa: E402
from wiki_context.scaffold import init_vault  # noqa: E402
from wiki_context.sync import sync_vault  # noqa: E402
from wiki_context.synonym_miner import mine_synonyms  # noqa: E402
from wiki_context.telemetry import TelemetryStore  # noqa: E402


class TestDomainProfileAndFocus(unittest.TestCase):
    def test_focus_terms_meta_filter_and_anti_hallucination_guard(self):
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "rail_vault"
            init_vault(v, name="RailKB", layout="zh")
            (v / "史料" / "contact_wire.md").write_text(
                "# 接触线选型对比\n\n接触线 CTA120 为冷拔铜银合金接触线，CTA150 用于更高载流区段。\n",
                encoding="utf-8",
            )
            (v / "史料" / "noise_doc.md").write_text(
                "# 简要背景说明\n\n这里仅说明项目基本情况和最近进展，无具体型号参数。\n",
                encoding="utf-8",
            )
            (v / "史料" / "jc24.md").write_text(
                "# JC24-17 上海103号综合检测车\n\n项目交付接触网检测设备与地面分析平台。\n",
                encoding="utf-8",
            )
            sync_vault(v)

            svc = KBService(v)
            try:
                # 1. Meta words ("简要", "基本情况", "最近进展") never trigger false-positive missing_concepts
                res_plain = svc.tool_context({"question": "简要说明：接触线 CTA120 与 CTA150 的区别"})
                self.assertEqual(res_plain["missing_concepts"], [])
                self.assertIn("CTA120", res_plain["context_markdown"])

                # 2. `focus_terms` promotes extracted entities and isolates expanded_terms from missing_concepts
                res_focus = svc.tool_context({
                    "question": "简要说明一下上海103号综合检测车（JC24-17）的项目基本情况和最近进展",
                    "focus_terms": ["JC24-17", "接触网检测设备"],
                    "expanded_terms": ["碳滑板掉块"],
                })
                self.assertEqual(res_focus["focus_terms"], ["JC24-17", "接触网检测设备"])
                self.assertEqual(res_focus["missing_concepts"], [])
                self.assertIn("JC24-17", res_focus["context_markdown"])
                self.assertNotIn("碳滑板掉块", res_focus["missing_concepts"])

                # 3. Anti-hallucination guardrail: omitted technical code / proper noun from `question` is still flagged
                res_gap = svc.tool_context({
                    "question": "JC24-17 的 Kafka 配置参数是什么？",
                    "focus_terms": ["JC24-17"],
                })
                self.assertIn("Kafka", res_gap["missing_concepts"])
            finally:
                svc.close()

    def test_synonym_cleanup_and_clean_cheatsheet_no_duplication(self):
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "clean_vault"
            init_vault(v, name="RailKB", layout="zh")
            (v / "史料" / "terms.md").write_text(
                "# 术语\n\n"
                "系统最高检测速度（km/h）采用汇流排（PAC110）与接触线（CTA120）。\n"
                "中车株洲电力机车有限公司（简称 ZELC、株机）供货。\n"
                "| 中文 | 缩写 |\n| --- | --- |\n| 悬挂监测 | 4C |\n",
                encoding="utf-8",
            )
            # Pre-seed dirty entries mimicking historical auto-mined noise
            (v / "synonyms.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "groups": [
                            {"canonical": ["的冷拔带沟铜银合金接触线"], "aliases": ["CTA120"]},
                            {"canonical": ["系统最高检测速度"], "aliases": ["km", "kmh"]},
                            {"canonical": ["接触线"], "aliases": ["CTA120", "CTA150"]},
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            (v / "目录.md").write_text(
                "# 目录\n\n| 页面名 | 摘要 |\n| --- | --- |\n| [[知识库/总览|总览]] | 汇总 |\n| [[接触线选型]] | CTA |\n",
                encoding="utf-8",
            )
            sync_vault(v)

            rep = mine_synonyms(v, apply=True, clean_existing=True)
            self.assertTrue(rep["applied"])
            self.assertGreaterEqual(rep["cleaned_removed"], 2)
            saved_syn = json.loads((v / "synonyms.json").read_text(encoding="utf-8"))
            all_terms = [t for g in saved_syn["groups"] for t in (*g["canonical"], *g["aliases"])]
            self.assertNotIn("的冷拔带沟铜银合金接触线", all_terms)
            self.assertNotIn("km", all_terms)
            self.assertIn("ZELC", all_terms)
            self.assertIn("CTA120", all_terms)

            svc = KBService(v)
            try:
                cs = svc.context_engine.catalog.cheatsheet(svc.context_engine.synonym_book)
                self.assertNotIn("页面名", cs)
                self.assertNotIn("的冷拔带沟铜", cs)

                # Hot-reload domain_profile without service restart and avoid duplicating full hints in instructions
                save_domain_profile(
                    v,
                    {
                        "domain": "轨道交通弓网监测与工程交付",
                        "entity_hints": ["项目号(JCxx-xx)", "接触线(CTA120/CTA150)"],
                        "query_tips": "优先提取项目号和设备指标，剔除泛化问法",
                        "source": "auto",
                        "locked": False,
                    },
                )
                instr = svc.build_server_instructions(slim_remote=True)
                tools = {t["name"]: t for t in svc.build_tools_spec(slim_remote=True)}
                self.assertIn("轨道交通弓网监测与工程交付", instr)
                self.assertIn("focus_terms", instr)
                self.assertNotIn("提词建议", instr)  # Full tips live in tool description, not duplicated in instructions
                wc_desc = tools["wiki_context"]["description"]
                self.assertIn("轨道交通弓网监测与工程交付", wc_desc)
                self.assertIn("项目号(JCxx-xx)", wc_desc)
                self.assertIn("focus_terms", tools["wiki_context"]["inputSchema"]["properties"])
            finally:
                svc.close()

    def test_domain_profile_triggers_staleness_lock_and_privacy(self):
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "dp_vault"
            init_vault(v, name="ProjKB", layout="zh")
            (v / "史料" / "svn").mkdir(parents=True, exist_ok=True)
            (v / "史料" / "svn" / "S-03101 环线合同 270万.md").write_text(
                "# JC16-4 环线项目\n\n接触网监测。\n", encoding="utf-8"
            )
            sync_vault(v)

            seen_prompts: list[str] = []

            def fake_llm(sys_p: str, usr_p: str) -> str:
                seen_prompts.append(usr_p)
                return json.dumps(
                    {
                        "domain": "城市轨道交通弓网与接触网监测工程",
                        "entity_hints": ["项目号(JCxx-xx)", "线路与车型"],
                        "query_tips": "提炼项目号、线路与传感器型号",
                    },
                    ensure_ascii=False,
                )

            # 1. Missing -> generates and scrubs contract amount (`270万` -> `[金额]`)
            r1 = generate_domain_profile(v, caller=fake_llm)
            self.assertTrue(r1["generated"])
            self.assertEqual(len(seen_prompts), 1)
            self.assertNotIn("270万", seen_prompts[0])
            self.assertEqual(load_domain_profile(v)["source"], "auto")

            # 2. Up-to-date -> skipped
            need2, reason2 = should_refresh_domain_profile(VaultConfig.load(v))
            self.assertFalse(need2)
            self.assertEqual(reason2, "up_to_date")
            self.assertTrue(generate_domain_profile(v, caller=fake_llm)["skipped"])

            # 3. Top-level directory change -> triggers refresh
            (v / "史料" / "算法部").mkdir(parents=True, exist_ok=True)
            (v / "史料" / "算法部" / "alg.md").write_text("# 算法\n\n几何参数。\n", encoding="utf-8")
            sync_vault(v)
            need3, reason3 = should_refresh_domain_profile(VaultConfig.load(v))
            self.assertTrue(need3)
            self.assertEqual(reason3, "top_dirs_changed")

            # 4. >=25% and >=10 doc growth -> triggers refresh
            r3 = generate_domain_profile(v, caller=fake_llm)
            self.assertTrue(r3["generated"])
            for i in range(10):
                (v / "史料" / "svn" / f"extra_{i}.md").write_text(f"# 文档 {i}\n\n内容 {i}。\n", encoding="utf-8")
            sync_vault(v)
            need4, reason4 = should_refresh_domain_profile(VaultConfig.load(v))
            self.assertTrue(need4)
            self.assertEqual(reason4, "scale_growth")

            # 5. Manual lock protects against auto refresh unless force=True
            cur = load_domain_profile(v)
            cur["locked"] = True
            cur["source"] = "manual"
            cur["domain"] = "人工锁定行业画像"
            save_domain_profile(v, cur)
            need5, reason5 = should_refresh_domain_profile(VaultConfig.load(v))
            self.assertFalse(need5)
            self.assertEqual(reason5, "locked_manual")
            self.assertEqual(load_domain_profile(v)["domain"], "人工锁定行业画像")
            self.assertTrue(generate_domain_profile(v, caller=fake_llm, force=True)["generated"])

            # 6. Sensitive vault refuses remote cloud endpoint
            cfg_p = v / "vault.json"
            raw_cfg = json.loads(cfg_p.read_text(encoding="utf-8"))
            raw_cfg["sensitive"] = True
            cfg_p.write_text(json.dumps(raw_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            with self.assertRaises(ValueError):
                generate_domain_profile(
                    v,
                    llm_cfg={"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
                    force=True,
                )

    def test_compile_vault_auto_triggers_domain_profile(self):
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "cmp_vault"
            init_vault(v, name="CmpKB", layout="zh")
            (v / "史料" / "spec.md").write_text("# 弓网监测规范\n\n导高范围 4000mm。\n", encoding="utf-8")
            sync_vault(v)

            def mock_compile_llm(sys_p: str, usr_p: str) -> str:
                if "domain profiler" in sys_p.lower():
                    return '{"domain": "弓网在线监测", "entity_hints": ["导高", "拉出值"], "query_tips": "提取设备指标"}'
                return (
                    "---\n"
                    "title: 弓网监测规范\n"
                    "sources:\n"
                    "  - 史料/spec.md\n"
                    "---\n\n"
                    "# 弓网监测规范\n\n"
                    "## 概览\n导高范围 4000mm。\n\n"
                    "## 关键事实\n- 导高范围 4000mm\n\n"
                    "## 来源\n- 史料/spec.md\n"
                )

            res = compile_vault(v, llm_fn=mock_compile_llm)
            self.assertIsNotNone(res.get("domain_profile"))
            self.assertTrue(res["domain_profile"]["generated"])
            self.assertEqual(load_domain_profile(v)["domain"], "弓网在线监测")

    def test_telemetry_migration_and_console_auto_triggers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # 1. Legacy 18-column telemetry.sqlite auto-migrates in place
            db_p = root / "legacy_tel.sqlite"
            con = sqlite3.connect(str(db_p))
            con.execute(
                """
                CREATE TABLE mcp_invocations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL, ts_epoch REAL NOT NULL, client TEXT NOT NULL,
                    mode TEXT NOT NULL, principal_uid TEXT NOT NULL, vault_id TEXT NOT NULL,
                    tool TEXT NOT NULL, query_redacted TEXT NOT NULL, route TEXT NOT NULL,
                    elapsed_ms REAL NOT NULL, wiki_count INTEGER NOT NULL,
                    primary_count INTEGER NOT NULL, memory_count INTEGER NOT NULL,
                    acl_filtered_count INTEGER NOT NULL, missing_concepts_json TEXT NOT NULL,
                    hit_sources_json TEXT NOT NULL, is_error INTEGER NOT NULL, error_text TEXT NOT NULL
                )
                """
            )
            con.execute(
                "INSERT INTO mcp_invocations VALUES (1,'2026-11-25T00:00:00Z',1.0,'c','http','u','v','wiki_context','q','fact',10.0,1,2,0,0,'[]','[]',0,'')"
            )
            con.commit()
            con.close()

            tel = TelemetryStore(db_p, async_writer=False)
            try:
                tel.record({
                    "client": "http:lsy",
                    "mode": "http",
                    "principal_uid": "lsy",
                    "vault_id": "wiki",
                    "tool": "wiki_context",
                    "query": "简要说明 CTA120",
                    "focus_terms": ["CTA120"],
                    "expanded_terms": ["接触线"],
                    "route": "fact",
                    "elapsed_ms": 12.5,
                })
                items = tel.list_invocations(limit=10)["items"]
                self.assertEqual(items[0]["focus_terms"], ["CTA120"])
                self.assertEqual(items[0]["expanded_terms"], ["接触线"])
                self.assertEqual(items[1]["focus_terms"], [])
            finally:
                tel.close()

            # 2. ConsoleState manual save / unlock + async trigger coalescing
            cfg_dir = root / "wctx-home"
            v_dir = root / "console-vault"
            init_vault(v_dir, name="ConsoleKB", layout="zh")
            (v_dir / "史料" / "doc.md").write_text("# 设备手册\n\n参数定义。\n", encoding="utf-8")
            sync_vault(v_dir)

            state = ConsoleState(
                config_dir=cfg_dir,
                client_home=root / "client-home",
                enable_watcher=False,
                extra_vault_specs={"wiki": v_dir},
            )
            try:
                saved = state.save_vault_domain_profile(
                    "wiki",
                    {
                        "action": "save",
                        "domain": "测试行业画像",
                        "entity_hints": ["项目号", "器件"],
                        "query_tips": "提取项目号",
                    },
                )
                self.assertTrue(saved["domain_profile"]["locked"])
                self.assertEqual(saved["domain_profile"]["source"], "manual")

                unlocked = state.save_vault_domain_profile("wiki", {"action": "unlock"})
                self.assertFalse(unlocked["domain_profile"]["locked"])

                # Mock caller proves background maybe_auto_generate_domain_profile_async updates `vault.json`
                with mock.patch("wiki_context.console.ConsoleState._resolve_llm_runtime") as m_rt:
                    m_rt.return_value = {"ready": True, "base_url": "http://127.0.0.1:8080/v1", "model": "local"}
                    started = state.maybe_auto_generate_domain_profile_async(
                        "wiki",
                        force=True,
                        caller=lambda s, u: '{"domain": "异步生成画像", "entity_hints": ["型号"], "query_tips": "提关键词"}',
                    )
                    self.assertTrue(started)
                    for _ in range(40):
                        if load_domain_profile(v_dir).get("domain") == "异步生成画像":
                            break
                        time.sleep(0.05)
                    self.assertEqual(load_domain_profile(v_dir).get("domain"), "异步生成画像")
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
