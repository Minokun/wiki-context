"""Tests for ContextPackEngine, synonyms, table windowing, anti-hallucination, secret redaction & ACL."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wiki_context.context import AllowAllPolicy, PrefixACLPolicy, Principal  # noqa: E402
from wiki_context.freshness import snapshot_wiki_hashes  # noqa: E402
from wiki_context.mcp_server import KBService  # noqa: E402
from wiki_context.scaffold import init_vault  # noqa: E402
from wiki_context.sync import sync_vault  # noqa: E402


class TestContextAndACL(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.v = Path(self._tmp.name) / "vault"
        init_vault(self.v, name="CoreKB", layout="en")
        (self.v / "sources" / "public").mkdir(parents=True)
        (self.v / "sources" / "confidential").mkdir(parents=True)

        # Synonym mapping: "oom kill" -> "memory-cgroup-limit"
        (self.v / "synonyms.json").write_text(
            json.dumps({
                "version": 1,
                "groups": [{"canonical": ["memory-cgroup-limit"], "aliases": ["oom kill", "out of memory"]}],
            }),
            encoding="utf-8",
        )

        rows = ["| Tier | MaxMem | BurstIOPS | Note |", "| --- | --- | --- | --- |"]
        for i in range(1, 90):
            if i == 78:
                rows.append("| Platinum-XL | 256GiB | 80000 | test key sk-abcdefghijklmnopqrstuvwxyz123456 |")
            else:
                rows.append(f"| Tier-{i:02d} | 4GiB | 1000 | filler padding row description text |")

        (self.v / "sources" / "public" / "limits.md").write_text(
            "# Compute Limits\n\n## Cgroup Guard\n"
            "Containers exceeding `memory-cgroup-limit` soft watermark above 85% emit early eviction events.\n\n"
            "## Tier Table\n" + "\n".join(rows) + "\n",
            encoding="utf-8",
        )
        (self.v / "sources" / "public" / "unrelated.md").write_text(
            "# Office Wifi\nGuest SSID rotates weekly.\n",
            encoding="utf-8",
        )
        (self.v / "sources" / "confidential" / "pricing.md").write_text(
            "# Internal Reserve Price\nEnterprise floor price is $42,000/year.\n",
            encoding="utf-8",
        )

        # Wiki page citing both limits.md and unrelated.md (tests 1-hop collateral protection)
        (self.v / "wiki" / "Compute-Guide.md").write_text(
            "---\ntitle: Compute-Guide\ntype: synthesis\nstatus: reviewed\n"
            "sources:\n  - sources/public/limits.md\n  - sources/public/unrelated.md\n---\n\n"
            "# Compute-Guide\n\n## Overview\nMonitors `memory-cgroup-limit` watermarks.\n\n"
            "## Sources\n- sources/public/limits.md\n",
            encoding="utf-8",
        )
        # Confidential Wiki page (tests provenance ACL inheritance)
        (self.v / "wiki" / "Deal-Desk.md").write_text(
            "---\ntitle: Deal-Desk\nsources:\n  - sources/confidential/pricing.md\n---\n\n"
            "# Deal-Desk\nSummary of floor pricing.\n",
            encoding="utf-8",
        )
        (self.v / "index.md").write_text(
            "# Index\n- [[Overview]] — Entry\n- [[Compute-Guide]] — Cgroup limits\n",
            encoding="utf-8",
        )
        sync_vault(self.v)

    def tearDown(self):
        self._tmp.cleanup()

    def test_synonym_one_hop_and_no_collateral_pollution(self):
        svc = KBService(self.v)
        try:
            out = svc.tool_context({"question": "What watermark triggers eviction before an oom kill?"})
            self.assertIn("memory-cgroup-limit", out["auto_synonyms"])
            self.assertIn("85%", out["context_markdown"])
            srcs = [h["source"] for h in out["primary_hits"]]
            self.assertIn("sources/public/limits.md", srcs)
            self.assertNotIn("sources/public/unrelated.md", srcs)
        finally:
            svc.close()

    def test_deep_table_header_pinning_and_secret_scrubbing(self):
        svc = KBService(self.v)
        try:
            out = svc.tool_context({"question": "What are MaxMem and BurstIOPS for Platinum-XL?"})
            md = out["context_markdown"]
            self.assertIn("256GiB", md)
            self.assertIn("| Tier | MaxMem | BurstIOPS | Note |", md)
            self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", md)
            self.assertIn("[REDACTED_API_KEY]", md)
        finally:
            svc.close()

    def test_missing_concept_anti_hallucination_alert(self):
        svc = KBService(self.v)
        try:
            out = svc.tool_context({"question": "What is the InfiniBand RDMA MTU requirement?"})
            self.assertTrue(
                any(k.lower() in {m.lower() for m in out["missing_concepts"]} for k in ("InfiniBand", "RDMA")),
                out["missing_concepts"],
            )
            self.assertIn("Missing-Concept Alert", out["context_markdown"])
        finally:
            svc.close()

    def test_prefix_acl_and_wiki_provenance_inheritance(self):
        acl = PrefixACLPolicy({
            "eng": {"allow": ["sources/public/**"], "deny": ["sources/confidential/**"]},
        })
        svc = KBService(self.v, policy=acl)
        try:
            eng = Principal(uid="bob", groups=("eng",))
            out = svc.tool_context({"question": "What is the Enterprise floor price?"}, principal=eng)
            self.assertNotIn("$42,000", out["context_markdown"])
            self.assertNotIn("wiki/Deal-Desk.md", {w["source"] for w in out["wiki_hits"]})

            denied = svc.tool_read({"source": "sources/confidential/pricing.md", "start": 1, "end": 5}, principal=eng)
            self.assertIn("Access denied", denied.get("error", ""))

            svc_all = KBService(self.v, policy=AllowAllPolicy())
            try:
                full = svc_all.tool_context({"question": "What is the Enterprise floor price?"})
                self.assertIn("$42,000", full["context_markdown"])
            finally:
                svc_all.close()
        finally:
            svc.close()

    def test_cjk_long_concepts_html_table_freshness_and_skipped_images(self):
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "zh_vault"
            init_vault(v, name="个人库测试", layout="zh")
            (v / "史料" / "摩托车保险").mkdir(parents=True)
            (v / "史料" / "股市").mkdir(parents=True)
            (v / "史料" / "附件" / "wxk-体检报告图片").mkdir(parents=True)
            (v / "史料" / "机密图片").mkdir(parents=True)

            filler_rows = "".join(
                f"<tr><td>条款项{i}</td><td>说明内容填充文本用于构造超过两千字符的单行HTML保单表格_{i:02d}</td></tr>"
                for i in range(1, 28)
            )
            html_table_one_line = (
                "<table>"
                "<tr><td>被保险人</td><td>张三</td><td>号牌号码</td><td>川AL039A</td><td>车辆</td><td>两轮摩托车</td></tr>"
                "<tr><td>责任限额</td><td>死亡伤残180,000元；医疗18,000元；财产2000元</td></tr>"
                "<tr><td>保险费合计</td><td>￥120.00元</td><td>保险期间</td><td>2026年08月25日至2027年08月24日</td></tr>"
                f"{filler_rows}"
                "<tr><td>公司名称：中国人民财产保险股份有限公司成都分公司</td><td>服务电话：95518</td></tr>"
                "</table>"
            )
            self.assertGreater(len(html_table_one_line), 1600)
            (v / "史料" / "摩托车保险" / "交强险.md").write_text(
                "# 机动车交通事故责任强制保险摩托车保险单\n\n"
                "保险单号：PDZA202651010001131960\n\n"
                f"{html_table_one_line}\n\n"
                "# 2027\n",
                encoding="utf-8",
            )
            (v / "史料" / "摩托车保险" / "商业险.md").write_text(
                "# 摩托车商业保险单\n\n"
                "保险单号：PAIC999999\n被保险人：张三\n车辆：两轮摩托车\n"
                "保险公司：中国平安财产保险股份有限公司\n保费合计：177.67元\n"
                "保险期间：2026年08月25日至2027年08月24日\n责任限额：第三者责任保险100万元\n",
                encoding="utf-8",
            )
            (v / "史料" / "股市" / "结题报告.md").write_text(
                "# 全市场复现结题报告\n\n"
                + "\n".join(f"第{i}段过渡回测说明。" for i in range(1, 15))
                + "\n\n可保留为个股体检卡的描述性标签，不进入选股打分。\n",
                encoding="utf-8",
            )

            tiny_png = (
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
                b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
                b"\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            (v / "史料" / "附件" / "wxk-体检报告图片" / "肝功能检查.png").write_bytes(tiny_png)
            (v / "史料" / "机密图片" / "体检内部底稿.png").write_bytes(tiny_png)

            (v / "知识库" / "摩托车保险资料.md").write_text(
                "---\ntitle: 摩托车保险资料\nsources:\n  - 史料/摩托车保险/\n---\n\n"
                "# 摩托车保险资料\n\n## 关键信息\n"
                "包含交强险与商业险保单，具体保险期间和责任限额以原件为准，不转录保单号、保费、车辆或被保险人信息。\n",
                encoding="utf-8",
            )
            (v / "知识库" / "交强险专页.md").write_text(
                "---\ntitle: 摩托车交强险专页\nsources:\n  - 史料/摩托车保险/交强险.md\n---\n\n"
                "# 摩托车交强险专页\n\n## 概览\n"
                "摩托车交强险保单号 PDZA202651010001131960，被保险人为张三，车辆为两轮摩托车，保险期间 2026-08-25 至 2027-08-24，保险费合计 120.00元。\n",
                encoding="utf-8",
            )
            sync_vault(v)
            snapshot_wiki_hashes(v, page="交强险专页.md")
            sync_vault(v)

            acl = PrefixACLPolicy({
                "reader": {"allow": ["史料/摩托车保险/**", "史料/股市/**", "史料/附件/**"], "deny": ["史料/机密图片/**"]},
            })
            svc = KBService(v, policy=acl)
            try:
                reader = Principal(uid="u1", groups=("reader",))
                # 1. Expanded insurance query: fresh Wiki ranks #1, commercial noise excluded, all 7 fields present
                q_ins = "我的摩托车交强险的具体信息（保单号、保险公司、保险期间、保费、车辆、被保险人、责任限额）"
                res_ins = svc.tool_context({"question": q_ins}, principal=reader)
                self.assertEqual(res_ins["wiki_hits"][0]["source"], "知识库/交强险专页.md")
                self.assertEqual([h["source"] for h in res_ins["primary_hits"]], ["史料/摩托车保险/交强险.md"])
                self.assertIn("中国人民财产保险股份有限公司", res_ins["context_markdown"])
                self.assertIn("95518", res_ins["context_markdown"])
                self.assertEqual(res_ins["missing_concepts"], [])

                read_res = svc.tool_read({"source": "史料/摩托车保险/交强险.md", "start": 1, "end": 200}, principal=reader)
                self.assertEqual(res_ins["primary_hits"][0]["line_end"], read_res["total_lines"])

                # Tight budget still windows single-line HTML table cleanly at </tr> boundaries and clamps read_more.end
                res_tight = svc.tool_context({"question": "给我看一下我的摩托车交强险的具体信息", "max_tokens": 1000}, principal=reader)
                self.assertIn("川AL039A", res_tight["context_markdown"])
                rm = res_tight["primary_hits"][0].get("read_more")
                if rm:
                    self.assertLessEqual(rm["end"], read_res["total_lines"])

                # 2. Unindexed health check query: flags both >6-char split concepts AND surfaces allowed skipped image dir
                res_med = svc.tool_context({"question": "我的体检报告肝功能怎么样"}, principal=reader)
                self.assertIn("肝功能", res_med["missing_concepts"])
                self.assertIn("体检报告", res_med["missing_concepts"])
                joined_bn = "\n".join(res_med["boundary_notes"])
                self.assertIn("史料/附件/wxk-体检报告图片", joined_bn)
                self.assertNotIn("史料/机密图片", joined_bn)
            finally:
                svc.close()


if __name__ == "__main__":
    unittest.main()
