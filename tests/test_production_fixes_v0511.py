"""Regression tests for v0.5.11 production fixes:
1. JSONL readers handle Unicode line separators (U+2028/U+2029/U+0085) via streaming file iteration.
2. `wiki_read(page_num=...)` reads per-doc sidecar JSON without loading `pages.jsonl`.
3. `GatewayRoutesMixin` and `ConsoleHTTPHandler` ignore `BrokenPipeError` / `ConnectionResetError` cleanly.
"""

from __future__ import annotations

import io
import json
import shutil
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from wiki_context.console import ConsoleState, create_console_server
from wiki_context.freshness import load_indexed_hashes
from wiki_context.gateway import GatewayRoutesMixin
from wiki_context.mcp_server import KBService
from wiki_context.scaffold import init_vault
from wiki_context.search import BM25Index
from wiki_context.sync import sync_vault


class _BrokenPipeHandler(GatewayRoutesMixin):
    def __init__(self) -> None:
        self.close_connection = False
        self.status_sent: int | None = None
        self.wfile = self

    def send_response(self, code: int) -> None:
        self.status_sent = code

    def send_header(self, _k: str, _v: str) -> None:
        pass

    def end_headers(self) -> None:
        pass

    def write(self, _data: bytes) -> int:
        raise BrokenPipeError("client closed socket")


class _BrokenHeaderEmptyHandler(GatewayRoutesMixin):
    def __init__(self) -> None:
        self.close_connection = False

    def send_response(self, _code: int) -> None:
        pass

    def send_header(self, _k: str, _v: str) -> None:
        pass

    def end_headers(self) -> None:
        raise ConnectionResetError("connection reset during headers")


class ProductionFixesV0511Test(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="wctx-v0511-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_jsonl_unicode_line_separators_and_sidecar_page_read(self) -> None:
        v = self.root / "vault"
        init_vault(v, layout="en")
        (v / "sources" / "report.md").write_text("# Report\n\nPlaceholder.\n", encoding="utf-8")
        sync_vault(v)

        # Inject a sidecar JSON with Unicode U+2028/U+2029/U+0085 inside text
        sidecar = v / "mirrors" / "report.md.json"
        tricky_text = "Alpha\u2028Beta\u2029Gamma\x85Delta"
        sidecar.write_text(
            json.dumps(
                {
                    "pages": [
                        {
                            "page_num": 3,
                            "page_label": "Page 4",
                            "blocks": [
                                {"format": "text", "content": tricky_text},
                                {"format": "placeholder", "content": "[skip]"},
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        svc = KBService(v)
        try:
            snap = svc._get_snapshot()

            # 1) Existing page_num reads straight from sidecar without loading pages.jsonl
            res = svc.tool_read({"source": "sources/report.md", "page_num": 3})
            self.assertEqual(res.get("page_num"), 3)
            self.assertEqual(res.get("page_label"), "Page 4")
            self.assertEqual(res.get("text"), tricky_text)
            self.assertIsNone(snap._pages_by_source_cache)

            # 2) Missing page_num in existing sidecar still does not load pages.jsonl
            miss = svc.tool_read({"source": "sources/report.md", "page_num": 99})
            self.assertIn("not found", miss.get("error", ""))
            self.assertIsNone(snap._pages_by_source_cache)

            # 3) When sidecar is absent, fallback to pages.jsonl containing raw U+2028/U+2029 succeeds
            sidecar.unlink()
            pages_p = snap.gen_dir / "pages.jsonl"
            row_with_u2028 = (
                '{"source": "sources/report.md", "mirror": "mirrors/report.md.md", '
                '"page_num": 7, "page_label": "P8", "text": "LineA\u2028LineB\u2029LineC\x85LineD"}\n'
            )
            pages_p.write_text(row_with_u2028, encoding="utf-8")

            # Also inject U+2028 into docs.jsonl, sections.jsonl, wiki_sections.jsonl
            docs_p = snap.gen_dir / "docs.jsonl"
            orig_docs = docs_p.read_text(encoding="utf-8")
            docs_p.write_text(orig_docs.replace('"Report"', '"Rep\u2028ort"'), encoding="utf-8")
            (v / "mirrors" / ".index" / "docs.jsonl").write_text(
                '{"source": "sources/report.md", "source_hash": "sha\u2028123"}\n',
                encoding="utf-8",
            )
            self.assertEqual(load_indexed_hashes(svc.cfg).get("sources/report.md"), "sha\u2028123")

            fb = svc.tool_read({"source": "sources/report.md", "page_num": 7})
            self.assertEqual(fb.get("page_num"), 7)
            self.assertEqual(fb.get("text"), "LineA\u2028LineB\u2029LineC\x85LineD")
            self.assertIsNotNone(snap._pages_by_source_cache)

            # 4) BM25Index memory fallback parses JSONL with raw U+2028/U+2029 cleanly
            (snap.gen_dir / "sections.jsonl").unlink(missing_ok=True)
            (snap.gen_dir / "wiki_sections.jsonl").write_text(
                '{"source": "wiki/overview.md", "title": "Ov\u2028er", "heading": "H1", '
                '"line_start": 1, "line_end": 2, "sources": ["sources/report.md"], "text": "Wi\u2029ki body"}\n',
                encoding="utf-8",
            )
            mem_idx = BM25Index(v, backend="memory")
            try:
                hits = mem_idx.search("LineA", k=3)
                self.assertTrue(hits)
                self.assertEqual(hits[0]["page_num"], 7)
                whits = mem_idx.search_wiki("body", k=3)
                self.assertTrue(whits)
            finally:
                mem_idx.close()
        finally:
            svc.close()

    def test_gateway_and_console_ignore_broken_pipe_and_connection_reset(self) -> None:
        h1 = _BrokenPipeHandler()
        h1._send_json(200, {"ok": True})
        self.assertTrue(h1.close_connection)

        h2 = _BrokenHeaderEmptyHandler()
        h2._send_empty(204)
        self.assertTrue(h2.close_connection)

        # Real ConsoleHTTPHandler test: drop socket immediately during GET /api/overview (after sending headers)
        cfg_dir = self.root / "cfg"
        fake_home = self.root / "home"
        cfg_dir.mkdir()
        fake_home.mkdir()
        state = ConsoleState(config_dir=cfg_dir, client_home=fake_home)
        errors: list[str] = []
        srv = create_console_server(state, host="127.0.0.1", port=0)
        orig_handle_error = srv.handle_error

        def _capture_error(request, client_address) -> None:
            import sys
            exc = sys.exc_info()[1]
            errors.append(f"{type(exc).__name__}: {exc}")
            orig_handle_error(request, client_address)

        srv.handle_error = _capture_error  # type: ignore[method-assign]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            host, port = srv.server_address
            with socket.create_connection((host, port), timeout=3) as s:
                s.sendall(
                    b"GET / HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\n"
                    b"Connection: close\r\n\r\n"
                )
                s.close()
            # Give server thread a brief moment to finish writing to closed socket
            threading.Event().wait(0.15)
            self.assertEqual(errors, [])
        finally:
            srv.shutdown()
            srv.server_close()
            state.close()


if __name__ == "__main__":
    unittest.main()
