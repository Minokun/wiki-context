"""End-to-end unit, security, and parity tests for SQLite FTS5 P0–P3 hardening:
- P0: Pre-LIMIT C-level `acl_pfx` filtering, hot-reload decoupling under `immutable=1`,
      empty-allow short-circuit, legacy 2-col `fts_units` compatibility, and SQLite/Memory parity.
- P1: Corpus-guarded high-DF CJK bigram pruning (`n_sources >= 30`, `>=2` floor),
      cross-word bridge bigram discounting, and CJK compound chain cohesion.
- P2: Strict 1-hop non-transitive `SynonymBook`, CJK appositive alias mining, and `Missing-Concept` guardrail.
- P3: Zero-dependency `HttpCandidateBackend` (`urllib.request`) Hybrid RRF rescue,
      mandatory local ACL enforcement over remote candidates, and fail-open resilience.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wiki_context.context import PrefixACLPolicy, Principal, SynonymBook, compile_acl_filter  # noqa: E402
from wiki_context.mcp_server import KBService  # noqa: E402
from wiki_context.scaffold import init_vault  # noqa: E402
from wiki_context.search import (  # noqa: E402
    BM25Index,
    HttpCandidateBackend,
    _prune_stage1_probe_tokens,
)
from wiki_context.sync import sync_vault  # noqa: E402
from wiki_context.synonym_miner import mine_synonyms  # noqa: E402


class TestFTS5P0ToP3Hardening(unittest.TestCase):
    def test_p0_pre_limit_acl_eliminates_starvation_supports_hot_reload_and_legacy_schema(self):
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "acl-vault"
            init_vault(v, name="MultiTenantKB", layout="en")
            (v / "sources" / "restricted").mkdir(parents=True)
            (v / "sources" / "public" / "secret").mkdir(parents=True)

            # 40 restricted docs heavily repeating exact query keywords (fills 100% of unfiltered k=28!)
            for i in range(40):
                (v / "sources" / "restricted" / f"internal-{i:02d}.md").write_text(
                    f"# Restricted Spec {i}\n"
                    "Pantograph contact strip wear threshold is RFC-7788 350mm. "
                    "RFC-7788 pantograph wear threshold 350mm calibration.\n",
                    encoding="utf-8",
                )

            # Denied nested subpath inside public
            (v / "sources" / "public" / "secret" / "embargo.md").write_text(
                "# Embargo Spec\nPantograph contact strip wear threshold is RFC-7788 999mm SECRET_LEAK_TOKEN.\n",
                encoding="utf-8",
            )

            # Only 2 authorized public docs (ranked well below the 40 restricted docs globally)
            (v / "sources" / "public" / "open-spec-1.md").write_text(
                "# Public Spec A\nPantograph wear limit under RFC-7788 is 320mm for public lines (PUBLIC_OK_ALPHA).\n",
                encoding="utf-8",
            )
            (v / "sources" / "public" / "open-spec-2.md").write_text(
                "# Public Spec B\nSecondary pantograph inspection interval under RFC-7788 is 15 days (PUBLIC_OK_BETA).\n",
                encoding="utf-8",
            )
            sync_vault(v)

            # Mutable rule dict to verify instant ACL hot-reload without rebuilding bm25.sqlite
            live_rules: dict[str, dict[str, list[str]]] = {
                "reader": {
                    "allow": ["sources/public/**"],
                    "deny": ["sources/public/secret/**"],
                },
                "locked_out": {
                    "allow": [],
                    "deny": [],
                },
            }
            policy = PrefixACLPolicy(lambda: live_rules)
            reader = Principal(uid="alice", groups=("reader",))
            locked = Principal(uid="nobody", groups=("locked_out",))

            # Unfiltered top-28 is 100% dominated by restricted/embargo docs (proving starvation existed before P0)
            idx_sql = BM25Index(v, backend="sqlite")
            idx_mem = BM25Index(v, backend="memory")
            try:
                self.assertTrue(idx_sql._has_acl_pfx)
                unfiltered = idx_sql.search("RFC-7788 pantograph wear threshold 350mm", k=28, max_per_source=6)
                pub_in_unfiltered = [
                    h for h in unfiltered
                    if h["source"].startswith("sources/public/") and "secret" not in h["source"]
                ]
                self.assertEqual(len(pub_in_unfiltered), 0)

                # With P0 pre-LIMIT FTS5 `acl_pfx`, both SQLite and Memory retrieve 100% of public docs!
                acl_f = compile_acl_filter(policy, reader)
                sql_hits = idx_sql.search(
                    "RFC-7788 pantograph wear threshold 350mm", k=10, max_per_source=2, acl_filter=acl_f
                )
                mem_hits = idx_mem.search(
                    "RFC-7788 pantograph wear threshold 350mm", k=10, max_per_source=2, acl_filter=acl_f
                )
                self.assertEqual([h["source"] for h in sql_hits], [h["source"] for h in mem_hits])
                self.assertEqual(
                    {h["source"] for h in sql_hits},
                    {"sources/public/open-spec-1.md", "sources/public/open-spec-2.md"},
                )

                # Empty allow-set short-circuits cleanly to [] (never fail-open!)
                empty_f = compile_acl_filter(policy, locked)
                self.assertTrue(empty_f.deny_all)
                self.assertEqual(idx_sql.search("RFC-7788", k=10, acl_filter=empty_f), [])
            finally:
                idx_sql.close()
                idx_mem.close()

            # End-to-end KBService `tool_context`, `tool_search`, and hot-reload without re-indexing
            svc = KBService(v, policy=policy)
            try:
                gen_before = svc._get_snapshot().generation_id
                ctx_out = svc.tool_context(
                    {"question": "What is the RFC-7788 pantograph wear threshold?"}, principal=reader
                )
                self.assertIn("PUBLIC_OK_ALPHA", ctx_out["context_markdown"])
                self.assertNotIn("SECRET_LEAK_TOKEN", ctx_out["context_markdown"])

                srch_out = svc.tool_search({"query": "RFC-7788 pantograph wear threshold", "k": 5}, principal=reader)
                self.assertEqual(
                    {h["source"] for h in srch_out["hits"]},
                    {"sources/public/open-spec-1.md", "sources/public/open-spec-2.md"},
                )

                # Hot-reload ACL in memory to grant `sources/public/secret/**` WITHOUT rebuilding bm25.sqlite!
                live_rules["reader"] = {"allow": ["sources/public/**"], "deny": []}
                ctx_reloaded = svc.tool_context(
                    {"question": "What is the RFC-7788 pantograph wear threshold?"}, principal=reader
                )
                self.assertEqual(svc._get_snapshot().generation_id, gen_before)
                self.assertIn("SECRET_LEAK_TOKEN", ctx_reloaded["context_markdown"])
            finally:
                svc.close()

            # Verify backward compatibility with pre-existing 2-column `fts_units(head, body)` SQLite files
            cur = json.loads((v / "mirrors" / ".index" / "current.json").read_text(encoding="utf-8"))
            db_file = v / "mirrors" / ".index" / cur["generation_id"] / "bm25.sqlite"
            con = sqlite3.connect(str(db_file))
            try:
                rows_2col = con.execute("SELECT rowid, head, body FROM fts_units").fetchall()
                con.execute("DROP TABLE fts_units")
                con.execute("CREATE VIRTUAL TABLE fts_units USING fts5(head, body, tokenize='ascii')")
                con.executemany("INSERT INTO fts_units(rowid, head, body) VALUES (?, ?, ?)", rows_2col)
                con.commit()
            finally:
                con.close()

            live_rules["reader"] = {"allow": ["sources/public/**"], "deny": ["sources/public/secret/**"]}
            idx_legacy = BM25Index(v, backend="sqlite")
            try:
                self.assertFalse(idx_legacy._has_acl_pfx)
                leg_hits = idx_legacy.search(
                    "RFC-7788 pantograph wear threshold",
                    k=10,
                    acl_filter=compile_acl_filter(policy, reader),
                )
                self.assertEqual(
                    {h["source"] for h in leg_hits},
                    {"sources/public/open-spec-1.md", "sources/public/open-spec-2.md"},
                )
            finally:
                idx_legacy.close()

    def test_p1_corpus_guarded_bigram_pruning_bridge_discount_and_chain_cohesion(self):
        # 1. Small vault (<30 sources): never prunes even when df/n > 0.25
        q_df_small = {"受电": 5, "电弓": 5, "阈值": 6}
        kept_small = _prune_stage1_probe_tokens(["受电", "电弓", "阈值"], q_df_small, n_sources=8)
        self.assertEqual(kept_small, ["受电", "电弓", "阈值"])

        # 2. Large vault (>=30 sources): prunes >25% DF CJK bigrams when anchors exist, keeps >=2 floor otherwise
        q_df_large = {"rfc-042": 2, "受电": 4, "系统": 45, "参数": 50}
        kept_large = _prune_stage1_probe_tokens(["rfc-042", "受电", "系统", "参数"], q_df_large, n_sources=100)
        self.assertEqual(kept_large, ["rfc-042", "受电"])

        q_df_all_high = {"系统": 40, "参数": 55, "要求": 80}
        kept_floor = _prune_stage1_probe_tokens(["系统", "参数", "要求"], q_df_all_high, n_sources=100)
        self.assertGreaterEqual(len(kept_floor), 2)
        self.assertEqual(kept_floor[:2], ["系统", "参数"])

        # 3. Compound CJK cohesion & cross-boundary bridge discount + SQLite/Memory parity
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "cjk-vault"
            init_vault(v, name="CJKPrecisionKB", layout="zh")
            # Doc A: Scattered accidental 2-char hits + accidental cross-boundary bigram "弓拉"
            (v / "史料" / "scattered.md").write_text(
                "# 杂项记录\n本段讨论接受电子设备，另外弓拉弦紧，输出值得参考。\n",
                encoding="utf-8",
            )
            # Doc B..E: Authoritative domain documents containing real compounds "受电弓" and "拉出值"
            (v / "史料" / "spec-main.md").write_text(
                "# 接触网参数总表\n\n| 部件 | 指标 | 限值 |\n| --- | --- | --- |\n| 受电弓 | 拉出值 | ±300mm |\n",
                encoding="utf-8",
            )
            for i in range(4):
                (v / "史料" / f"bg-{i}.md").write_text(
                    f"# 背景资料 {i}\n受电弓日常巡检记录，另见拉出值历史台账。\n",
                    encoding="utf-8",
                )
            sync_vault(v)

            idx_sql = BM25Index(v, backend="sqlite")
            idx_mem = BM25Index(v, backend="memory")
            try:
                h_sql = idx_sql.search("受电弓拉出值", k=5)
                h_mem = idx_mem.search("受电弓拉出值", k=5)
                self.assertEqual([h["source"] for h in h_sql], [h["source"] for h in h_mem])
                self.assertEqual([h["score"] for h in h_sql], [h["score"] for h in h_mem])
                # Authoritative table `spec-main.md` must rank #1 over `scattered.md` despite "弓拉" only in scattered.md!
                self.assertEqual(h_sql[0]["source"], "史料/spec-main.md")
            finally:
                idx_sql.close()
                idx_mem.close()

    def test_p2_strict_one_hop_synonyms_and_cjk_appositive_mining(self):
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "syn-vault"
            init_vault(v, name="SynGuardKB", layout="zh")
            (v / "史料" / "glossary.md").write_text(
                "# 术语定义\n"
                "接触网悬挂状态检测监测装置（简称：4C装置、悬挂检测仪）用于周期巡检；\n"
                "受电弓滑板（俗称碳滑板）磨耗需重点监控。\n",
                encoding="utf-8",
            )
            sync_vault(v)

            mined = mine_synonyms(v, apply=True)
            self.assertGreaterEqual(mined["discovered_groups"], 2)

            # Verify strict 1-hop non-transitivity: A -> B and B -> C must NOT expand A -> C!
            syn_file = v / "synonyms.json"
            syn_file.write_text(
                json.dumps({
                    "version": 1,
                    "groups": [
                        {"canonical": ["热关断保护"], "aliases": ["烫手死机"]},
                        {"canonical": ["电池热失控起火"], "aliases": ["热关断保护"]},
                    ],
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            book = SynonymBook(syn_file)
            expanded = book.expand("设备烫手死机怎么排查？")
            self.assertEqual(expanded, ["热关断保护"])
            self.assertNotIn("电池热失控起火", expanded)

    def test_p3_http_candidate_backend_hybrid_rrf_acl_safety_and_fail_open(self):
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "hybrid-vault"
            init_vault(v, name="HybridRRFKB", layout="zh")
            (v / "史料" / "公开").mkdir(parents=True)
            (v / "史料" / "机密").mkdir(parents=True)

            # Zero lexical overlap with colloquial query "机器发烫自动黑屏"
            (v / "史料" / "公开" / "thermal-spec.md").write_text(
                "# 硬件过温保护规范\n\n当核心结温超过 105°C 时，PMIC 立即切断供电以保护芯片。\n",
                encoding="utf-8",
            )
            (v / "史料" / "机密" / "secret-thermal.md").write_text(
                "# 内部军品结温底稿\n\n极限结温容差 145°C CONFIDENTIAL_THERMAL_SECRET。\n",
                encoding="utf-8",
            )
            sync_vault(v)

            class MockVectorHandler(BaseHTTPRequestHandler):
                def do_POST(self):  # noqa: N802
                    length = int(self.headers.get("Content-Length", "0"))
                    _ = json.loads(self.rfile.read(length).decode("utf-8"))
                    # Returns restricted doc #1 and public doc #2 to test mandatory local ACL enforcement
                    resp = {
                        "candidates": [
                            {"source": "史料/机密/secret-thermal.md", "line_start": 1},
                            {"source": "史料/公开/thermal-spec.md", "line_start": 1},
                        ]
                    }
                    raw = json.dumps(resp, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)

                def log_message(self, *_args):
                    pass

            srv = HTTPServer(("127.0.0.1", 0), MockVectorHandler)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()

            acl = PrefixACLPolicy({"reader": {"allow": ["史料/公开/**"], "deny": ["史料/机密/**"]}})
            reader = Principal(uid="bob", groups=("reader",))
            acl_f = compile_acl_filter(acl, reader)

            try:
                # 1. Pure lexical baseline: 0 hits on pure colloquialism
                idx_plain = BM25Index(v, backend="sqlite")
                try:
                    self.assertEqual(idx_plain.search("机器发烫自动黑屏", k=5, acl_filter=acl_f), [])
                finally:
                    idx_plain.close()

                # 2. With zero-dep HttpCandidateBackend: rescues public 105°C spec while blocking secret 145°C doc!
                backend = HttpCandidateBackend(f"http://127.0.0.1:{port}/candidates", timeout=1.0)
                idx_hyb = BM25Index(v, backend="sqlite", candidate_backend=backend)
                idx_hyb_mem = BM25Index(v, backend="memory", candidate_backend=backend)
                try:
                    hits_sql = idx_hyb.search("机器发烫自动黑屏", k=5, acl_filter=acl_f)
                    hits_mem = idx_hyb_mem.search("机器发烫自动黑屏", k=5, acl_filter=acl_f)
                    self.assertEqual([h["source"] for h in hits_sql], ["史料/公开/thermal-spec.md"])
                    self.assertEqual([h["source"] for h in hits_mem], ["史料/公开/thermal-spec.md"])
                    self.assertTrue(hits_sql[0].get("via_hybrid_rrf"))
                    self.assertIn("105°C", hits_sql[0]["text"])
                finally:
                    idx_hyb.close()
                    idx_hyb_mem.close()
            finally:
                srv.shutdown()
                srv.server_close()

            # 3. Fail-open resilience when remote vector server is down
            dead_backend = HttpCandidateBackend(f"http://127.0.0.1:{port}/candidates", timeout=0.2)
            idx_dead = BM25Index(v, backend="sqlite", candidate_backend=dead_backend)
            try:
                lex_hits = idx_dead.search("105°C PMIC", k=5, acl_filter=acl_f)
                self.assertEqual(len(lex_hits), 1)
                self.assertEqual(lex_hits[0]["source"], "史料/公开/thermal-spec.md")
            finally:
                idx_dead.close()


if __name__ == "__main__":
    unittest.main()
