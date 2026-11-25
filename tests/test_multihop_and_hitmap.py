"""Regression tests for MultiHop-RAG optimizations: cache, routing, facets, Hit Map, and English guardrail."""

from __future__ import annotations

import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from wiki_context.context import ContextPackEngine, build_query_plan
from wiki_context.mcp_server import KBService
from wiki_context.scaffold import init_vault
from wiki_context.search import BM25Index, route
from wiki_context.sync import sync_vault


def _walk_label_strict(case: unittest.TestCase, content: str, a: int, b: int, file_lines: list[str]) -> None:
    shown = [ln.strip() for ln in content.strip().splitlines() if ln.strip()]
    case.assertTrue(shown)
    pos, last = a, None
    for ln in shown:
        while pos <= len(file_lines) and not file_lines[pos - 1].strip():
            pos += 1
        case.assertLessEqual(pos, len(file_lines))
        case.assertEqual(file_lines[pos - 1].strip(), ln)
        last = pos
        pos += 1
    case.assertLessEqual(last, b)


class MultihopAndHitMapTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="wctx-mh-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_route_long_english_multihop_vs_short_fact(self) -> None:
        short_q = "Who is Sam Bankman-Fried?"
        self.assertEqual(route(short_q)["mode"], "fact")

        short_def = "What is the definition of RRF?"
        self.assertEqual(route(short_def)["mode"], "fact")

        long_mh = (
            "Which individual associated with FTX was convicted of fraud, as reported by TechCrunch, "
            "while also being accused of misusing customer funds according to The Verge?"
        )
        r = route(long_mh)
        self.assertEqual(r["mode"], "complex")
        self.assertIn("en_multihop_clause", r["complex_hits"])

    def test_english_proper_phrase_facets_and_word_boundary_coverage(self) -> None:
        q = (
            "Does the New York Times article about Sam Bankman-Fried differ from the "
            "Buffalo Bills report in Epoch Times?"
        )
        plan = build_query_plan(q)
        labels = [lbl for lbl, _ in plan.core_concepts]
        self.assertIn("New York Times", labels)
        self.assertIn("Sam Bankman-Fried", labels)
        self.assertIn("Buffalo Bills", labels)
        self.assertIn("Epoch Times", labels)
        self.assertNotIn("article", [x.lower() for x in labels])
        self.assertNotIn("report", [x.lower() for x in labels])

        # Word-boundary check: "ai" must not be considered covered by "said"
        self.assertFalse(ContextPackEngine._facet_covered_in_blob("AI", {"ai"}, "he said nothing about technology"))
        self.assertTrue(ContextPackEngine._facet_covered_in_blob("AI", {"ai"}, "generative ai adoption grew"))

        # Multi-word English facet requires multi-token / high-IDF match, not just one generic/shared token
        self.assertFalse(
            ContextPackEngine._facet_covered_in_blob(
                "New York Times",
                {"new", "york", "times"},
                "a new policy was announced several times yesterday",
            )
        )
        self.assertTrue(
            ContextPackEngine._facet_covered_in_blob(
                "New York Times",
                {"new", "york", "times"},
                "according to the new york times investigation",
            )
        )

    def test_bm25_unit_cache_eviction_and_thread_safety(self) -> None:
        vdir = self.root / "cache_vault"
        init_vault(str(vdir), name="CacheKB", layout="en", dry_run=False)
        for i in range(12):
            (vdir / "sources" / f"doc-{i:02d}.md").write_text(
                f"# Alpha Topic {i}\n\n" + f"Keyword alpha_{i} and common_entity details. " * 20 + "\n",
                encoding="utf-8",
            )
        sync_vault(str(vdir), rebuild=True)

        idx = BM25Index(vdir)
        try:
            # Force tiny cache limit to exercise LRU eviction cleanly
            idx._unit_cache_max_bytes = 2500
            r1 = idx.search("alpha_1 common_entity", k=5)
            r2 = idx.search("alpha_1 common_entity", k=5)
            self.assertEqual([x["source"] for x in r1], [x["source"] for x in r2])
            self.assertLessEqual(idx._unit_cache_bytes, idx._unit_cache_max_bytes)

            # Batch DF lookup parity
            dfs = idx.tokens_df(["common_entity", "alpha_3", "nonexistent_xyz_token"])
            self.assertEqual(dfs["common_entity"], 12)
            self.assertEqual(dfs["alpha_3"], 1)
            self.assertEqual(dfs["nonexistent_xyz_token"], 0)

            errors: list[Exception] = []

            def _worker(tid: int) -> None:
                try:
                    for step in range(15):
                        res = idx.search(f"alpha_{(tid + step) % 12} common_entity", k=4)
                        if not res:
                            raise AssertionError("empty search result under concurrency")
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)

            threads = [threading.Thread(target=_worker, args=(t,)) for t in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
        finally:
            idx.close()

    def test_hitmap_other_matches_diversity_and_english_guardrail(self) -> None:
        vdir = self.root / "mh_vault"
        init_vault(str(vdir), name="MHVault", layout="en", dry_run=False)

        # Long document where lead paragraph mentions main entity heavily,
        # while the specific answer sentence (`prosecution_penalty_42m`) sits deep in line ~35.
        filler_paras = "\n".join(
            f"Paragraph {i}: Sam Bankman-Fried and FTX courtroom proceedings continued today with routine motions."
            for i in range(1, 28)
        )
        doc_a = (
            "# FTX Trial Overview\n\n"
            "## 1\n\n"
            "Sam Bankman-Fried attended the FTX fraud trial in Manhattan as crypto observers watched closely.\n\n"
            + filler_paras
            + "\n\n## 2\n\n"
            "Prosecutors stated the restitution figure reached exact_penalty_42m under the sentencing brief.\n\n"
            + "\n".join(f"Closing note {i}: routine procedural summary." for i in range(1, 8))
            + "\n"
        )
        # Multiple chunks on doc-a vs separate evidence on doc-b (OpenAI) and doc-c (Nvidia)
        (vdir / "sources" / "doc-a.md").write_text(doc_a, encoding="utf-8")
        (vdir / "sources" / "doc-b.md").write_text(
            "# OpenAI Leadership\n\n## 1\n\nOpenAI announced governance changes following board review.\n",
            encoding="utf-8",
        )
        (vdir / "sources" / "doc-c.md").write_text(
            "# Nvidia Earnings\n\n## 1\n\nNvidia posted record datacenter GPU revenue in Q3.\n",
            encoding="utf-8",
        )
        sync_vault(str(vdir), rebuild=True)

        svc = KBService(vdir)
        try:
            # 1) Hit Map & line-range integrity on long windowed doc
            out_long = svc.tool_context({
                "question": (
                    "What happened at the Sam Bankman-Fried FTX trial, and what did prosecutors state "
                    "about exact_penalty_42m in the sentencing brief?"
                ),
                "max_tokens": 2200,
            })
            self.assertGreaterEqual(len(out_long["primary_hits"]), 1)
            for blk in out_long["primary_hits"]:
                rel = svc.cfg.strip_source_prefix(blk["source"])
                flines = (svc.cfg.mirrors_root / f"{rel}.md").read_text(encoding="utf-8").splitlines()
                _walk_label_strict(self, blk["content"], blk["line_start"], blk["line_end"], flines)
                for om in blk.get("other_matches") or []:
                    self.assertGreaterEqual(om["start"], 1)
                    self.assertLessEqual(om["end"], len(flines))
                    # Must be strictly disjoint from primary shown window
                    self.assertTrue(om["end"] < blk["line_start"] or om["start"] > blk["line_end"])

            # 2) Multi-facet cross-doc diversity + zero false-positive missing_concepts on answerable English query
            out_mh = svc.tool_context({
                "question": (
                    "According to reports on Sam Bankman-Fried, OpenAI governance changes, "
                    "and Nvidia Q3 GPU revenue, which entities had major announcements?"
                )
            })
            self.assertEqual(out_mh["route"], "complex")
            srcs = {h["source"] for h in out_mh["primary_hits"]}
            self.assertIn("sources/doc-a.md", srcs)
            self.assertIn("sources/doc-b.md", srcs)
            self.assertIn("sources/doc-c.md", srcs)
            self.assertEqual(out_mh["missing_concepts"], [])

            # 3) Absent proper noun (`Sridevi` / `HyperLoopVacuumPump99`) MUST be flagged in missing_concepts
            out_null = svc.tool_context({
                "question": (
                    "Considering the article detailing Sridevi achievements and OpenAI governance, "
                    "what is the connection?"
                )
            })
            self.assertIn("Sridevi", out_null["missing_concepts"])
        finally:
            svc.close()

    def test_facet_rescued_cannot_hijack_rank1(self) -> None:
        vdir = self.root / "rescue_vault"
        init_vault(str(vdir), name="RescueKB", layout="en", dry_run=False)
        (vdir / "sources" / "doc-main.md").write_text(
            "# AlphaBeta Merger\n\nAlphaBeta announced a joint merger with GammaCorp and EpsilonBank on Tuesday.\n",
            encoding="utf-8",
        )
        # Side-facet doc mentions ZetaToken repeatedly but misses the main merger entities.
        # In the main query `ZetaToken` has df=2 while GammaCorp/EpsilonBank have df=1, so doc-main wins rank #1.
        # Without the score cap, a facet subquery on ZetaToken alone would outscore doc-main.
        (vdir / "sources" / "doc-side.md").write_text(
            "# Background Briefing\n\n"
            + ("Routine commentary. " * 25)
            + "\n"
            + ("ZetaToken " * 12)
            + "\n",
            encoding="utf-8",
        )
        (vdir / "sources" / "doc-side-2.md").write_text(
            "# Secondary ZetaToken\n\nZetaToken minor mention.\n",
            encoding="utf-8",
        )
        for i in range(6):
            (vdir / "sources" / f"doc-fill-{i}.md").write_text(
                f"# AlphaBeta Merger Update {i}\n\nAlphaBeta announced merger details {i}.\n",
                encoding="utf-8",
            )
        sync_vault(str(vdir), rebuild=True)

        svc = KBService(vdir)
        try:
            out = svc.tool_context({
                "question": (
                    "According to AlphaBeta Merger with GammaCorp and EpsilonBank, "
                    "plus ZetaToken, what happened?"
                )
            })
            self.assertGreaterEqual(len(out["primary_hits"]), 2)
            self.assertEqual(out["primary_hits"][0]["source"], "sources/doc-main.md")
            self.assertIn("sources/doc-side.md", {h["source"] for h in out["primary_hits"]})
        finally:
            svc.close()


if __name__ == "__main__":
    unittest.main()
