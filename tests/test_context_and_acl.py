"""Tests for ContextPackEngine, synonyms, table windowing, anti-hallucination, secret redaction & ACL."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wiki_context.context import AllowAllPolicy, PrefixACLPolicy, Principal  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
