"""Regression tests: an incompatible index generation must never take down the MCP server.

Background (2026-11-23): a vault's documented workflow rebuilt ``.index`` with the legacy
``scripts/wiki_index.py`` (schema 3, no ``units.body_toks_z``). ``wctx serve`` hot-swapped to
that generation and the first ``wiki_search`` raised ``sqlite3.OperationalError``, which escaped
the stdio loop and killed the process, so the client saw "MCP connection closed".
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from wiki_context.indexer import SCHEMA_VERSION, rebuild_index  # noqa: E402
from wiki_context.mcp_server import KBService, handle_jsonrpc_message  # noqa: E402
from wiki_context.scaffold import init_vault  # noqa: E402
from wiki_context.search import INDEX_SCHEMA_VERSION, BM25Index, IndexUnavailableError  # noqa: E402
from wiki_context.sync import sync_vault  # noqa: E402


def _call(svc: KBService, name: str, args: dict | None = None, rid: int = 1) -> dict:
    resp = handle_jsonrpc_message(
        svc,
        {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": name, "arguments": args or {}}},
    )
    assert resp is not None
    return resp["result"]


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def _index_root(vault: Path) -> Path:
    return vault / "史料_md" / ".index"


def _publish_legacy_generation(vault: Path) -> str:
    """Clone the active generation and downgrade it to look like a legacy schema-3 build."""
    root = _index_root(vault)
    cur = json.loads((root / "current.json").read_text(encoding="utf-8"))
    src_gen = root / cur["generation_id"]
    legacy_id = "gen-20990101T000000-legacy00"
    dst = root / legacy_id
    shutil.copytree(src_gen, dst)
    con = sqlite3.connect(dst / "bm25.sqlite")
    try:
        con.execute("ALTER TABLE units DROP COLUMN body_toks_z")
        con.execute("ALTER TABLE units ADD COLUMN fn_toks BLOB")
        con.commit()
    finally:
        con.close()
    summary = json.loads((dst / "summary.json").read_text(encoding="utf-8"))
    summary.update({"schema_version": 3, "policy_version": 2, "generation_id": legacy_id})
    summary.pop("generator", None)
    (dst / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    shutil.copy2(dst / "summary.json", root / "summary.json")
    legacy_cur = {"schema_version": 3, "policy_version": 2, "generation_id": legacy_id}
    time.sleep(0.01)  # make sure mtime_ns changes even on coarse filesystems
    (root / "current.json").write_text(json.dumps(legacy_cur), encoding="utf-8")
    return legacy_id


class TestIndexSchemaGuard(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.vault = Path(self._td.name) / "个人信息"
        init_vault(self.vault, name="个人信息", layout="zh")
        (self.vault / "史料" / "服务器.md").write_text(
            "# 服务器\n\n## 腾讯云\n\n- 198.51.100.42：ubuntu 轻量应用服务器 Lighthouse\n", encoding="utf-8"
        )
        sync_vault(self.vault)
        self._old_user = os.environ.get("WCTX_USER")
        os.environ["WCTX_USER"] = "tester"

    def tearDown(self) -> None:
        if self._old_user is None:
            os.environ.pop("WCTX_USER", None)
        else:
            os.environ["WCTX_USER"] = self._old_user
        self._td.cleanup()

    def test_schema_constants_in_lockstep(self) -> None:
        self.assertEqual(SCHEMA_VERSION, INDEX_SCHEMA_VERSION)
        summary = json.loads((_index_root(self.vault) / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["generator"], "wiki-context")
        self.assertEqual(summary["schema_version"], INDEX_SCHEMA_VERSION)
        marker = _index_root(self.vault) / ".wctx-managed"
        self.assertTrue(marker.is_file())
        self.assertEqual(json.loads(marker.read_text(encoding="utf-8"))["generator"], "wiki-context")

    def test_bm25index_rejects_legacy_generation_with_rebuild_hint(self) -> None:
        _publish_legacy_generation(self.vault)
        with self.assertRaises(IndexUnavailableError) as ctx:
            BM25Index(self.vault)
        msg = str(ctx.exception)
        self.assertIn("schema_version=3", msg)
        self.assertIn("wctx index --vault", msg)

    def test_column_check_catches_unversioned_mismatch(self) -> None:
        legacy_id = _publish_legacy_generation(self.vault)
        gen = _index_root(self.vault) / legacy_id
        summary = json.loads((gen / "summary.json").read_text(encoding="utf-8"))
        summary["schema_version"] = INDEX_SCHEMA_VERSION  # lie about the version
        (gen / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        with self.assertRaises(IndexUnavailableError) as ctx:
            BM25Index(self.vault)
        self.assertIn("body_toks_z", str(ctx.exception))

    def test_hot_swap_to_legacy_keeps_last_known_good_and_warns(self) -> None:
        svc = KBService(self.vault)
        try:
            good = _payload(_call(svc, "wiki_status"))
            self.assertNotIn("index_warning", good)

            legacy_id = _publish_legacy_generation(self.vault)
            res = _call(svc, "wiki_search", {"query": "Lighthouse"})
            self.assertFalse(res["isError"])
            self.assertEqual(_payload(res)["generation_id"], good["generation_id"])

            status = _payload(_call(svc, "wiki_status"))
            self.assertEqual(status["generation_id"], good["generation_id"])
            self.assertIn("wctx index", status["index_warning"])

            # Repairing the index clears the warning and swaps off the legacy generation.
            time.sleep(0.01)
            rebuild_index(self.vault, force=True)
            healed = _payload(_call(svc, "wiki_status"))
            self.assertNotIn("index_warning", healed)
            self.assertNotEqual(healed["generation_id"], legacy_id)
            self.assertFalse(_call(svc, "wiki_search", {"query": "Lighthouse"})["isError"])
        finally:
            svc.close()

    def test_server_starts_on_legacy_index_and_returns_tool_errors(self) -> None:
        _publish_legacy_generation(self.vault)
        svc = KBService(self.vault)  # must not raise
        try:
            for name, args in (("wiki_search", {"query": "Lighthouse"}), ("wiki_status", {}), ("wiki_context", {"question": "腾讯云"})):
                res = _call(svc, name, args)
                self.assertTrue(res["isError"], name)
                self.assertIn("wctx index", res["content"][0]["text"])
            rebuild_index(self.vault, force=True)
            res = _call(svc, "wiki_search", {"query": "Lighthouse"})
            self.assertFalse(res["isError"])
        finally:
            svc.close()

    def test_unexpected_tool_exception_is_returned_not_raised(self) -> None:
        svc = KBService(self.vault)
        try:
            def boom(*_a, **_k):
                raise sqlite3.OperationalError("no such column: body_toks_z")

            svc.tool_search = boom  # type: ignore[method-assign]
            res = _call(svc, "wiki_search", {"query": "x"})
            self.assertTrue(res["isError"])
            self.assertIn("OperationalError", res["content"][0]["text"])
        finally:
            svc.close()

    def test_stdio_server_survives_legacy_index(self) -> None:
        _publish_legacy_generation(self.vault)
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "wiki_search", "arguments": {"query": "x"}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "wiki_status", "arguments": {}}},
        ]
        env = {**os.environ, "PYTHONPATH": str(SRC), "WCTX_USER": "tester", "WCTX_TELEMETRY": "0"}
        proc = subprocess.run(
            [sys.executable, "-m", "wiki_context", "serve", "--vault", str(self.vault)],
            input="\n".join(json.dumps(m) for m in msgs) + "\n",
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [json.loads(ln) for ln in proc.stdout.splitlines() if ln.strip()]
        self.assertEqual([ln["id"] for ln in lines], [1, 2, 3])
        self.assertTrue(lines[1]["result"]["isError"])
        self.assertTrue(lines[2]["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
