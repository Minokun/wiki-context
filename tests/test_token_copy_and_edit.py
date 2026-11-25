"""Issued-token one-click copy (raw secret kept in a private side file) and in-place editing."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from wiki_context.console import TOKEN_SECRETS_FILENAME, ConsoleState


class TokenCopyAndEditTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.cfg_dir = root / "cfg"
        self.home = root / "home"
        self.home.mkdir()
        self.state = ConsoleState(config_dir=self.cfg_dir, client_home=self.home, enable_watcher=False)

    def tearDown(self) -> None:
        self.state.close()
        self._tmp.cleanup()

    def _row(self, tid: str) -> dict:
        return next(t for t in self.state.list_tokens_public() if t["id"] == tid)

    def test_new_token_is_copyable_and_secret_stays_out_of_tokens_json(self) -> None:
        res = self.state.create_token(uid="alice", label="Alice", groups=["reader"], allowed_vaults=["wiki"])
        tid = res["record"]["id"]
        self.assertTrue(self._row(tid)["copyable"])
        self.assertEqual(self.state.reveal_token(tid)["token"], res["token"])

        tokens_raw = (self.cfg_dir / "tokens.json").read_text(encoding="utf-8")
        self.assertNotIn(res["token"], tokens_raw)
        self.assertNotIn("token", self._row(tid))
        if os.name == "posix":
            mode = stat.S_IMODE((self.cfg_dir / TOKEN_SECRETS_FILENAME).stat().st_mode)
            self.assertEqual(mode & 0o077, 0)

    def test_rotate_updates_copied_secret_and_delete_drops_it(self) -> None:
        res = self.state.create_token(uid="bob")
        tid = res["record"]["id"]
        rot = self.state.rotate_token(tid)
        self.assertNotEqual(rot["token"], res["token"])
        self.assertEqual(self.state.reveal_token(tid)["token"], rot["token"])

        self.assertTrue(self.state.delete_token(tid))
        doc = json.loads((self.cfg_dir / TOKEN_SECRETS_FILENAME).read_text(encoding="utf-8"))
        self.assertNotIn(tid, doc["secrets"])

    def test_legacy_hash_only_token_is_not_copyable(self) -> None:
        res = self.state.create_token(uid="legacy")
        tid = res["record"]["id"]
        (self.cfg_dir / TOKEN_SECRETS_FILENAME).unlink()
        self.assertFalse(self._row(tid)["copyable"])
        with self.assertRaises(ValueError):
            self.state.reveal_token(tid)

    def test_stale_secret_after_external_rotation_is_rejected(self) -> None:
        res = self.state.create_token(uid="carol")
        tid = res["record"]["id"]
        doc = json.loads((self.cfg_dir / "tokens.json").read_text(encoding="utf-8"))
        for r in doc["tokens"]:
            if r["id"] == tid:
                r["token_sha256"] = "0" * 64  # rotated by another process without updating side file
        (self.cfg_dir / "tokens.json").write_text(json.dumps(doc), encoding="utf-8")
        self.assertFalse(self._row(tid)["copyable"])
        with self.assertRaises(ValueError):
            self.state.reveal_token(tid)

    def test_edit_label_groups_and_vault_scope_takes_effect(self) -> None:
        res = self.state.create_token(uid="dave", label="Dave", groups=["reader"], allowed_vaults=["*"])
        tid = res["record"]["id"]
        self.assertTrue(
            self.state.update_token(tid, label="Dave 的笔记本", groups=["reader", "writer"], allowed_vaults=["wiki"])
        )
        row = self._row(tid)
        self.assertEqual(row["label"], "Dave 的笔记本")
        self.assertEqual(row["groups"], ["reader", "writer"])
        self.assertEqual(row["allowed_vaults"], ["wiki"])
        # Gateway registry sees the new principal immediately; the secret is unchanged.
        principal = self.state.token_registry.authenticate(f"Bearer {res['token']}")
        self.assertIsNotNone(principal)
        self.assertEqual(set(principal.groups), {"reader", "writer"})
        self.assertEqual(tuple(principal.allowed_vaults), ("wiki",))
        self.assertTrue(self._row(tid)["copyable"])

        # Blank label falls back to uid instead of an empty name.
        self.state.update_token(tid, label="   ")
        self.assertEqual(self._row(tid)["label"], "dave")
        self.assertFalse(self.state.update_token("tok_missing", label="x"))


if __name__ == "__main__":
    unittest.main()
