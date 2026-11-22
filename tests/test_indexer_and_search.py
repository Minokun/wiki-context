"""Tests for table-aware sub-chunking, duplicate folding, and SQLite FTS5 parity."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wiki_context.indexer import check_index, split_markdown_sections  # noqa: E402
from wiki_context.scaffold import init_vault  # noqa: E402
from wiki_context.search import BM25Index, route, tokenize_query  # noqa: E402
from wiki_context.sync import sync_vault  # noqa: E402


class TestIndexerAndSearch(unittest.TestCase):
    def test_table_subchunking_preserves_header(self):
        rows = ["| ID | Parameter | Threshold |", "| --- | --- | --- |"]
        for i in range(1, 220):
            rows.append(f"| R{i:03d} | Param-{i} | {i * 10}ms |")
        md = "# Long Spec\n\n" + "\n".join(rows) + "\n"
        chunks = split_markdown_sections(md)
        self.assertGreaterEqual(len(chunks), 2)
        for c in chunks:
            self.assertIn("| ID | Parameter | Threshold |", c["table_header"])

    def test_duplicate_folding_and_sqlite_memory_parity(self):
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "vault"
            init_vault(v)
            body = "# Network Timeout\n\nKeepalive idle timeout is 75 seconds under RFC-9110.\n"
            (v / "sources" / "net.md").write_text(body, encoding="utf-8")
            (v / "sources" / "net (1).md").write_text(body, encoding="utf-8")
            (v / "sources" / "auth.md").write_text("# Auth\n\nJWT rotation every 3600s.\n", encoding="utf-8")

            res = sync_vault(v)
            self.assertEqual(res["index"]["duplicate_docs"], 1)

            ok, _ = check_index(v, deep=True)
            self.assertTrue(ok)

            idx_sql = BM25Index(v, backend="sqlite")
            idx_mem = BM25Index(v, backend="memory")
            try:
                h_sql = idx_sql.search("RFC-9110 keepalive timeout", k=5)
                h_mem = idx_mem.search("RFC-9110 keepalive timeout", k=5)
                self.assertEqual([x["source"] for x in h_sql], [x["source"] for x in h_mem])
                self.assertEqual(h_sql[0]["source"], "sources/net.md")
            finally:
                idx_sql.close()
                idx_mem.close()

    def test_bilingual_tokenizer_and_router(self):
        toks_zh, _ = tokenize_query("系统的背景和需求吧？")
        self.assertNotIn("求吧", toks_zh)
        bar_toks, _ = tokenize_query("网吧")
        self.assertIn("网吧", bar_toks)

        self.assertEqual(route("What is the default timeout?")["mode"], "fact")
        self.assertEqual(route("Compare architecture A versus B and list all trade-offs")["mode"], "complex")
        self.assertEqual(route("请大致讲解一下系统整体方案")["mode"], "complex")


if __name__ == "__main__":
    unittest.main()
