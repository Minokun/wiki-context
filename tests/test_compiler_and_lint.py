"""Tests for offline AI Librarian compiler (`wctx compile`), freshness, and linter."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wiki_context.compiler import compile_vault  # noqa: E402
from wiki_context.freshness import check_vault_freshness  # noqa: E402
from wiki_context.lint import lint_vault  # noqa: E402
from wiki_context.mcp_server import KBService  # noqa: E402
from wiki_context.scaffold import init_vault  # noqa: E402
from wiki_context.sync import sync_vault  # noqa: E402


class TestCompilerAndLint(unittest.TestCase):
    def test_compile_new_and_stale_pages_and_lint_clean(self):
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "vault"
            init_vault(v, name="AutoCompileKB", layout="en")
            src_file = v / "sources" / "storage-sla.md"
            src_file.write_text("# Storage SLA\n\nBlock volume durability is 99.999% with 3 replicas.\n", encoding="utf-8")
            sync_vault(v)

            dry = compile_vault(v, dry_run=True)
            self.assertEqual(dry["uncompiled_sources"], ["sources/storage-sla.md"])

            def fake_llm(sys_p: str, usr_p: str) -> str:
                version_note = "5 replicas" if "5 replicas" in usr_p else "3 replicas"
                return (
                    '---\ntitle: "storage-sla"\ntype: source\nstatus: reviewed\nupdated: 2026-11-22\n'
                    "sources:\n  - sources/storage-sla.md\n---\n\n"
                    f"# storage-sla\n\n## Overview\nDurability spec ({version_note}).\n\n"
                    "## Related Pages\n\n[[Overview]]\n\n## Sources\n\n- sources/storage-sla.md\n"
                )

            res1 = compile_vault(v, llm_fn=fake_llm)
            self.assertEqual(res1["compiled_pages"], ["storage-sla.md"])
            self.assertTrue(res1["lint"]["ok"], res1["lint"])

            fresh = check_vault_freshness(v)
            by_page = {r["page"]: r["state"] for r in fresh}
            self.assertEqual(by_page["storage-sla.md"], "fresh")

            # Mutate raw source -> sync -> page becomes `stale` -> compile refreshes it!
            src_file.write_text("# Storage SLA\n\nBlock volume durability upgraded to 5 replicas.\n", encoding="utf-8")
            sync_vault(v)
            stale_rows = check_vault_freshness(v, only_stale=True)
            self.assertEqual([r["page"] for r in stale_rows], ["storage-sla.md"])

            res2 = compile_vault(v, llm_fn=fake_llm)
            self.assertEqual(res2["refreshed_pages"], ["storage-sla.md"])

            svc = KBService(v)
            try:
                ctx = svc.tool_context({"question": "How many replicas does block volume durability use?"})
                self.assertIn("5 replicas", ctx["context_markdown"])
            finally:
                svc.close()

            # Verify linter catches broken wikilink
            (v / "wiki" / "Broken.md").write_text(
                '---\ntitle: "Broken"\nsources:\n  - sources/storage-sla.md\n---\n\nSee [[GhostPage]].\n',
                encoding="utf-8",
            )
            rep = lint_vault(v)
            self.assertFalse(rep["ok"])
            self.assertTrue(any(e["code"] == "dangling-link" for e in rep["errors"]))


if __name__ == "__main__":
    unittest.main()
