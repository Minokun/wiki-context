"""Layer-4 memory wiki-page anchors (with ACL provenance inheritance) and credential scrubbing."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wiki_context.config import VaultConfig  # noqa: E402
from wiki_context.context import PrefixACLPolicy, Principal  # noqa: E402
from wiki_context.mcp_server import KBService, handle_jsonrpc_message, scrub_secrets  # noqa: E402
from wiki_context.memory import MemoryStore  # noqa: E402
from wiki_context.scaffold import init_vault  # noqa: E402
from wiki_context.sync import sync_vault  # noqa: E402


_FAKE_AK_ID = "LT" + "AI" + "0000TestDummyKey1234"
_FAKE_AK_SECRET = "Dummy" + "Secret" * 4


class TestScrubSecrets(unittest.TestCase):
    REDACT = {
        "- 203.0.113.10：ubuntu / ^TestPass1234$": "^TestPass1234$",
        "[203.0.113.11](http://203.0.113.11:3000/x) root S3cret!x": "S3cret!x",
        "密码： ^TestPass1234$": "^TestPass1234$",
        "登录密码：@abc12345": "@abc12345",
        f"AccessKey Secret：{_FAKE_AK_SECRET}": _FAKE_AK_SECRET,
        f"AccessKey ID：{_FAKE_AK_ID}": _FAKE_AK_ID,
        "API_V3_KEY: abcdEFGH1234abcdEFGH1234abcdEFGH": "abcdEFGH1234abcdEFGH1234abcdEFGH",
        "client secret：abcdefghijklmnopqrstuvwxyz123": "abcdefghijklmnopqrstuvwxyz123",
        "13800138000 Pa55word!": "Pa55word!",
        "[a@b.com](mailto:a@b.com) Pa55word1": "Pa55word1",
        "演示账号： ^DemoPass5678$": "^DemoPass5678$",
        "用户 demo_user， 密码 demo_pass123": "demo_pass123",
    }
    KEEP = [
        "token：用于图像检索的特征",
        "验证期 / 2026",
        "mAP@0.5 提升 3.2",
        "联系 13800138000 user@example.com",
        "登录方式（密码/密钥）待补充",
        "Ubuntu / Debian 均可",
        "实测用 ubuntu + `~/.ssh/id_rsa` 密钥登录",
        "198.51.100.42：ubuntu —— 轻量应用服务器（Lighthouse）",
        "- B5: 1TSSD+8Tsata",
        "共：40*30*24=28800",
        "magnet?xt=urn:btih:9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b2a1f0e&dn=x",
        "版本：v1.2.3-beta",
    ]

    def test_redacts_credentials(self) -> None:
        for text, secret in self.REDACT.items():
            out = scrub_secrets(text)
            self.assertNotIn(secret, out, text)
            self.assertIn("[REDACTED", out, text)

    def test_keeps_ordinary_text(self) -> None:
        for text in self.KEEP:
            self.assertEqual(scrub_secrets(text), text)


class TestMemoryWikiAnchors(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.vault = Path(self._td.name) / "kb"
        init_vault(self.vault, name="kb", layout="zh")
        (self.vault / "史料" / "公开" ).mkdir(parents=True, exist_ok=True)
        (self.vault / "史料" / "机密").mkdir(parents=True, exist_ok=True)
        (self.vault / "史料" / "公开" / "a.md").write_text("# A\n公开资料\n", encoding="utf-8")
        (self.vault / "史料" / "机密" / "s.md").write_text("# S\n机密资料\n", encoding="utf-8")
        cfg = VaultConfig.load(self.vault)
        cfg.wiki_root.mkdir(parents=True, exist_ok=True)
        (cfg.wiki_root / "服务器与设备.md").write_text(
            "---\ntitle: 服务器与设备\nsources:\n  - 史料/机密/s.md\n---\n# 服务器与设备\n\n## 腾讯云\n- 198.51.100.42 Lighthouse 新购\n",
            encoding="utf-8",
        )
        (cfg.wiki_root / "公开页.md").write_text(
            "---\ntitle: 公开页\nsources:\n  - 史料/公开/a.md\n---\n# 公开页\n内容\n", encoding="utf-8"
        )
        sync_vault(self.vault)
        self.cfg = cfg
        self._old_user = os.environ.get("WCTX_USER")
        os.environ["WCTX_USER"] = "tester"

    def tearDown(self) -> None:
        if self._old_user is None:
            os.environ.pop("WCTX_USER", None)
        else:
            os.environ["WCTX_USER"] = self._old_user
        self._td.cleanup()

    def test_wiki_refs_are_not_rewritten_into_sources(self) -> None:
        self.assertEqual(self.cfg.normalize_memory_ref("知识库/服务器与设备.md"), "知识库/服务器与设备.md")
        self.assertEqual(self.cfg.normalize_memory_ref("史料/机密/s.md"), "史料/机密/s.md")
        self.assertEqual(self.cfg.normalize_memory_ref("机密/s.md"), "史料/机密/s.md")
        store = MemoryStore(self.vault)
        note = store.remember("Lighthouse 198.51.100.42 用 id_ed25519 登录", related_sources=["知识库/服务器与设备.md"])
        self.assertEqual(note.related_sources, ("知识库/服务器与设备.md",))
        reloaded = MemoryStore(self.vault).get_note(note.note_id)
        assert reloaded is not None
        self.assertEqual(reloaded.related_sources, ("知识库/服务器与设备.md",))

    def test_wiki_anchor_inherits_page_provenance_acl(self) -> None:
        store = MemoryStore(self.vault)
        secret_note = store.remember("机密页备注", related_sources=["知识库/服务器与设备.md"])
        public_note = store.remember("公开页备注", related_sources=["知识库/公开页.md"])
        policy = PrefixACLPolicy({"guest": {"allow": ["史料/公开"]}})
        guest = Principal(uid="g", groups=("guest",))
        self.assertFalse(store.can_access_note(policy, guest, secret_note))
        self.assertTrue(store.can_access_note(policy, guest, public_note))
        admin = Principal(uid="a", groups=("admin",))
        self.assertTrue(store.can_access_note(policy, admin, secret_note))

    def test_wiki_anchored_note_surfaces_with_wiki_hit_and_tool_warns_on_bad_anchor(self) -> None:
        svc = KBService(self.vault)
        try:
            resp = handle_jsonrpc_message(svc, {
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "wiki_remember", "arguments": {
                    "text": "Lighthouse 新机密码：Zx9!abcd 已改为密钥登录",
                    "related_sources": ["知识库/服务器与设备.md"],
                }},
            })
            assert resp is not None
            out = json.loads(resp["result"]["content"][0]["text"])
            self.assertTrue(out["saved"])
            self.assertEqual(out["related_sources"], ["知识库/服务器与设备.md"])
            self.assertNotIn("warning", out)
            self.assertTrue(out.get("redacted"))
            note_text = (self.vault / out["file"]).read_text(encoding="utf-8")
            self.assertNotIn("Zx9!abcd", note_text)

            resp = handle_jsonrpc_message(svc, {
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "wiki_remember", "arguments": {
                    "text": "另一条", "related_sources": ["史料/不存在.md"],
                }},
            })
            assert resp is not None
            self.assertIn("warning", json.loads(resp["result"]["content"][0]["text"]))

            ctx = svc.tool_context({"question": "Lighthouse 198.51.100.42 腾讯云"})
            self.assertIn(out["note_id"], ctx.get("context_markdown", ""))
        finally:
            svc.close()


if __name__ == "__main__":
    unittest.main()
