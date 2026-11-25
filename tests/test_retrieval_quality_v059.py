"""Regression tests for wiki-context v0.5.9 retrieval quality & guardrail upgrades."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from wiki_context.context import ContextPackEngine, SynonymBook, build_query_plan
from wiki_context.indexer import split_markdown_sections
from wiki_context.mcp_server import KBService
from wiki_context.scaffold import init_vault
from wiki_context.sync import sync_vault


class RetrievalQualityV059Test(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="wctx-v059-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_p01_and_p03_clean_concepts_structured_standards_and_boundary_checks(self) -> None:
        dummy = type("Dummy", (), {"synonym_book": SynonymBook(None)})()

        # P-01: Interrogative & long directive Chinese questions must never produce cross-word fragments
        q1 = "沈阳4号线接触网作业车第一次设计联络会我方有哪些人参会？"
        plan1 = build_query_plan(q1)
        labels1 = [lbl for lbl, _ in plan1.core_concepts]
        self.assertNotIn("人参会", labels1)
        self.assertNotIn("号线接触", labels1)
        self.assertNotIn("络会我方", labels1)

        q2 = "北一支项目合同的技术项点要求有哪些？请列出合同技术条款中的各项技术要求。"
        plan2 = build_query_plan(q2)
        labels2 = [lbl for lbl, _ in plan2.core_concepts]
        self.assertNotIn("请列出合", labels2)
        self.assertNotIn("北一支项目合", labels2)
        self.assertNotIn("各项技术要求", labels2)

        q3 = "JC21-02 是什么项目？红外程序做了什么改动？"
        plan3 = build_query_plan(q3)
        labels3 = [lbl for lbl, _ in plan3.core_concepts]
        self.assertNotIn("02项", labels3)
        self.assertNotIn("外程序做", labels3)

        # P-03: Structured standard `GB/T 49999` stays intact and is NOT satisfied by `GB/T 25119` + `400001至49999`
        q4 = "GB/T 49999 对接触网检测装置有哪些要求？"
        plan4 = build_query_plan(q4)
        labels4 = [lbl for lbl, _ in plan4.core_concepts]
        self.assertIn("GB/T 49999", labels4)
        irrelevant = (
            "gb/t 25119-2010 轨道交通 机车车辆电子装置\n"
            "通讯指令示例 400001 至 49999 是保持寄存器\n"
            "接触网检测装置技术规范\n"
        )
        miss4 = ContextPackEngine._detect_missing_concepts(dummy, plan4, irrelevant.lower())
        self.assertIn("GB/T 49999", miss4)

        # When GB/T 25119 is genuinely present (with -2010 suffix), it must NOT be flagged missing
        q5 = "GB/T 25119 对环境温度有哪些要求？"
        plan5 = build_query_plan(q5)
        miss5 = ContextPackEngine._detect_missing_concepts(dummy, plan5, irrelevant.lower())
        self.assertNotIn("GB/T 25119", miss5)

    def test_p05_section_line_offset_exact_after_frontmatter_blank_line(self) -> None:
        raw = "---\nsource: test\n---\n\n会议在沈阳召开。\n\n1. 事项一\n2. 事项二\n"
        secs = split_markdown_sections(raw)
        self.assertEqual(len(secs), 1)
        sec = secs[0]
        self.assertEqual(sec["line_start"] + sec["text"].count("\n"), sec["line_end"])
        raw_lines = raw.splitlines()
        self.assertEqual(raw_lines[sec["line_start"] - 1], sec["text"].splitlines()[0])
        self.assertEqual(raw_lines[sec["line_end"] - 1], sec["text"].splitlines()[-1])

    def test_p02_p04_p06_p07_and_p09_end_to_end(self) -> None:
        vdir = self.root / "vault"
        init_vault(str(vdir), name="RailKB", layout="zh", dry_run=False)
        src = vdir / "史料"
        (src / "课件").mkdir(parents=True)
        (src / "教材").mkdir(parents=True)

        # Real course + sibling enumeration subsections (1C..6C)
        (src / "课件" / "接触网和6C检测系统介绍.md").write_text(
            "## 接触网6C检测系统\n\n### 6C系统概述\n\n"
            "- 接触网6C系统是“高速铁路供电安全检测监测系统”的简称。\n"
            "- 由6个检测装置和一个数据处理中心组成：\n\n"
            "## 1C - 弓网综合检测装置\n\n"
            "## 2C - 接触网安全巡检装置\n\n"
            "## 3C - 车载接触网运行状态检测装置\n\n"
            "## 4C - 接触网悬挂状态检测监测装置\n\n"
            "## 5C - 受电弓滑板监测装置\n\n"
            "## 6C - 接触网及供电设备地面监测装置\n",
            encoding="utf-8",
        )
        # Competing textbook exam-question section that previously outranked real course
        (src / "教材" / "城市轨道交通概论.md").write_text(
            "# 城市轨道交通概论\n\n"
            + "\n".join(f"第{i}章 概论\n\n讲述城市轨道交通的发展历史若干内容。\n" for i in range(1, 5))
            + "\n## 复习思考题\n\n"
            "1. 接触网由哪几部分组成？\n"
            "2. 什么是导高和拉出值？\n"
            "3. 简述6C检测系统的组成。\n"
            "4. 接触网检测系统包含哪几类检测装置？\n"
            "5. 受电弓检测是其中的哪一类？\n",
            encoding="utf-8",
        )
        # H1-only wiki overview page for P-09
        (vdir / "知识库" / "接触网6C专题-综述.md").write_text(
            "# 接触网6C检测系统总览\n\n"
            "## 概览\n\n"
            "6C 检测体系覆盖 1C 到 6C 六大核心子系统，支撑高速铁路供电安全监测。\n\n"
            "## 关键参数\n\n暂略。\n",
            encoding="utf-8",
        )
        sync_vault(str(vdir), rebuild=True)

        svc = KBService(vdir)
        try:
            # P-04 + P-02 + P-06: real course ranks #1 over 复习思考题, missing_concepts == [], enumeration pulls 1C..6C
            out = svc.tool_context({"question": "接触网6C系统是哪个系统的简称？由哪几部分组成？"})
            self.assertGreaterEqual(len(out["primary_hits"]), 1)
            self.assertIn("接触网和6C检测系统介绍.md", out["primary_hits"][0]["source"])
            self.assertIn("高速铁路供电安全检测监测系统", out["primary_hits"][0]["content"])
            self.assertIn("6C - 接触网及供电设备地面监测装置", out["primary_hits"][0]["content"])
            self.assertEqual(out["missing_concepts"], [])

            # P-09: H1-only wiki block expands to include `## 概览`
            out_w = svc.tool_context({"question": "接触网6C检测系统总览"})
            self.assertTrue(any("1C 到 6C" in w["content"] for w in out_w["wiki_hits"]))

            # P-07: strict small budget `max_tokens=1200` does not explode to >4700 chars
            out_small = svc.tool_context({"question": "接触网6C系统是哪个系统的简称？", "max_tokens": 1200})
            self.assertLessEqual(len(out_small["context_markdown"]), 2900)
        finally:
            svc.close()


if __name__ == "__main__":
    unittest.main()
