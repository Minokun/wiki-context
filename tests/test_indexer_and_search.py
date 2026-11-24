"""Tests for table-aware sub-chunking, duplicate folding, and SQLite FTS5 parity."""

from __future__ import annotations

import sys
import tempfile
import threading
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

    def test_sqlite_read_pool_bounded_under_thread_reuse_and_concurrency(self):
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "pool-vault"
            init_vault(v)
            (v / "sources" / "net.md").write_text(
                "# Network Timeout\n\nKeepalive idle timeout is 75 seconds under RFC-9110.\n",
                encoding="utf-8",
            )
            sync_vault(v)

            idx = BM25Index(v, backend="sqlite")
            try:
                # 40 sequential short-lived threads: pooled connection is reused instead of leaking per-ident handles
                for _ in range(40):
                    t = threading.Thread(target=lambda: idx.search("RFC-9110 keepalive", k=3))
                    t.start()
                    t.join()
                self.assertEqual(len(idx._all_cons), 1)

                # 24 concurrent threads: total created connections must remain bounded by MAX_READ_POOL_SIZE
                barrier = threading.Barrier(24)
                errs: list[BaseException] = []

                def worker() -> None:
                    try:
                        barrier.wait(timeout=5.0)
                        hits = idx.search("RFC-9110 keepalive", k=3)
                        assert hits and hits[0]["source"] == "sources/net.md"
                    except BaseException as exc:  # pragma: no cover
                        errs.append(exc)

                threads = [threading.Thread(target=worker) for _ in range(24)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                self.assertFalse(errs, errs)
                self.assertGreaterEqual(len(idx._all_cons), 1)
                self.assertLessEqual(len(idx._all_cons), BM25Index.MAX_READ_POOL_SIZE)
            finally:
                idx.close()
            self.assertEqual(len(idx._all_cons), 0)


if __name__ == "__main__":
    unittest.main()
