"""Tests for bilingual VaultConfig auto-detection and `wctx init` scaffolding."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wiki_context.config import VaultConfig  # noqa: E402
from wiki_context.scaffold import init_vault  # noqa: E402


class TestConfigAndScaffold(unittest.TestCase):
    def test_init_english_and_chinese_vaults(self):
        with tempfile.TemporaryDirectory() as td:
            en_dir = Path(td) / "en-kb"
            res_en = init_vault(en_dir, name="Cloud Docs", layout="en")
            self.assertFalse(res_en["dry_run"])
            self.assertTrue((en_dir / "sources").is_dir())
            self.assertTrue((en_dir / "mirrors" / ".index").is_dir())
            self.assertTrue((en_dir / "wiki" / "Overview.md").is_file())

            cfg_en = VaultConfig.load(en_dir)
            self.assertEqual(cfg_en.layout, "en")
            self.assertEqual(cfg_en.name, "Cloud Docs")
            self.assertEqual(cfg_en.source_prefix, "sources/")

            zh_dir = Path(td) / "中文知识库"
            init_vault(zh_dir, name="研发知识库", layout="zh")
            self.assertTrue((zh_dir / "史料").is_dir())
            self.assertTrue((zh_dir / "知识库" / "知识库总览.md").is_file())

            cfg_zh = VaultConfig.load(zh_dir)
            self.assertEqual(cfg_zh.layout, "zh")
            self.assertEqual(cfg_zh.source_prefix, "史料/")
            self.assertEqual(cfg_zh.normalize_source_ref("sources/a.md"), "史料/a.md")

            # Refuses to overwrite non-empty directory
            with self.assertRaises(FileExistsError):
                init_vault(en_dir)

    def test_autodetect_legacy_chinese_obsidian_vault_without_vault_json(self):
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "legacy-zh"
            (v / "史料").mkdir(parents=True)
            (v / "史料_md").mkdir(parents=True)
            (v / "知识库").mkdir(parents=True)
            cfg = VaultConfig.load(v)
            self.assertEqual(cfg.layout, "zh")
            self.assertEqual(cfg.catalog_file, "目录.md")


if __name__ == "__main__":
    unittest.main()
