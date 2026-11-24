"""Tests for bilingual VaultConfig auto-detection and `wctx init` scaffolding."""

from __future__ import annotations

import base64
import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wiki_context.cli import main as cli_main  # noqa: E402
from wiki_context.config import VaultConfig  # noqa: E402
from wiki_context.mcp_server import KBService  # noqa: E402
from wiki_context.scaffold import (  # noqa: E402
    create_managed_vault,
    import_external_folder,
    init_vault,
    inspect_folder,
    mount_source_folder,
    onboard_folder,
    relink_source_path,
    unmount_source_folder,
)
from wiki_context.sync import sync_vault  # noqa: E402
from wiki_context.watcher import VaultWatcherManager  # noqa: E402


class TestConfigAndScaffold(unittest.TestCase):
    def test_init_english_and_chinese_vaults(self):
        with tempfile.TemporaryDirectory() as td:
            en_dir = Path(td) / "en-kb"
            res_en = init_vault(en_dir, name="Cloud Docs", layout="en")
            self.assertFalse(res_en["dry_run"])
            self.assertTrue((en_dir / "sources").is_dir())
            self.assertTrue((en_dir / "mirrors" / ".index").is_dir())
            self.assertTrue((en_dir / "wiki" / "Overview.md").is_file())
            self.assertTrue((en_dir / "AGENTS.md").is_file())
            en_agents = (en_dir / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("notes/", en_agents)
            self.assertIn("wiki_remember", en_agents)

            cfg_en = VaultConfig.load(en_dir)
            self.assertEqual(cfg_en.layout, "en")
            self.assertEqual(cfg_en.name, "Cloud Docs")
            self.assertEqual(cfg_en.source_prefix, "sources/")

            zh_dir = Path(td) / "中文知识库"
            init_vault(zh_dir, name="研发知识库", layout="zh")
            self.assertTrue((zh_dir / "史料").is_dir())
            self.assertTrue((zh_dir / "知识库" / "知识库总览.md").is_file())
            self.assertTrue((zh_dir / "AGENTS.md").is_file())
            zh_agents = (zh_dir / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("记忆/", zh_agents)
            self.assertIn("wiki_remember", zh_agents)

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

    def test_onboard_scaffolds_agents_md_and_preserves_custom(self):
        with tempfile.TemporaryDirectory() as td:
            # 1. Raw documents folder -> moves files to 史料/ and scaffolds AGENTS.md
            raw_zh = Path(td) / "研发资料"
            raw_zh.mkdir()
            (raw_zh / "设计说明.md").write_text("# 设计说明\n内容\n", encoding="utf-8")
            res_raw = onboard_folder(raw_zh, name="研发资料", layout="zh", run_sync=False)
            self.assertEqual(res_raw["onboard_mode"], "raw_documents_folder")
            self.assertIn("设计说明.md", res_raw["moved_entries"])
            self.assertTrue((raw_zh / "史料" / "设计说明.md").is_file())
            self.assertTrue((raw_zh / "AGENTS.md").is_file())
            zh_agents = (raw_zh / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("记忆/", zh_agents)
            self.assertIn("wiki_remember", zh_agents)

            # 2. Empty folder containing only ignored dotfiles (.DS_Store) -> scaffolds AGENTS.md
            ds_only = Path(td) / "ds-only-en"
            ds_only.mkdir()
            (ds_only / ".DS_Store").write_bytes(b"\x00\x00")
            res_ds = onboard_folder(ds_only, name="DS Only", layout="en", run_sync=False)
            self.assertEqual(res_ds["onboard_mode"], "empty_or_new")
            self.assertTrue((ds_only / "AGENTS.md").is_file())
            en_agents = (ds_only / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("notes/", en_agents)
            self.assertIn("wiki_remember", en_agents)

            # 3. Existing custom AGENTS.md is preserved for both raw_documents_folder and existing_vault
            custom_text = "# Custom Agent Rules\nKeep intact.\n"
            raw_custom = Path(td) / "raw-custom"
            raw_custom.mkdir()
            (raw_custom / "spec.md").write_text("# Spec\n", encoding="utf-8")
            (raw_custom / "AGENTS.md").write_text(custom_text, encoding="utf-8")
            res_raw_custom = onboard_folder(raw_custom, name="Raw Custom", layout="en", run_sync=False)
            self.assertEqual(res_raw_custom["onboard_mode"], "raw_documents_folder")
            self.assertNotIn("AGENTS.md", res_raw_custom["moved_entries"])
            self.assertFalse((raw_custom / "sources" / "AGENTS.md").exists())
            self.assertEqual((raw_custom / "AGENTS.md").read_text(encoding="utf-8"), custom_text)

            existing_custom = Path(td) / "existing-custom"
            init_vault(existing_custom, name="Existing Custom", layout="zh")
            (existing_custom / "AGENTS.md").write_text(custom_text, encoding="utf-8")
            res_existing = onboard_folder(existing_custom, run_sync=False)
            self.assertEqual(res_existing["onboard_mode"], "existing_vault")
            self.assertEqual((existing_custom / "AGENTS.md").read_text(encoding="utf-8"), custom_text)

    def test_existing_vault_inspect_and_adopt_loose_root_documents(self):
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "量化"
            (v / "史料").mkdir(parents=True)
            (v / "史料" / "第1课.md").write_text("# 第1课（已在史料中）\n", encoding="utf-8")
            # Loose root documents alongside existing 史料/ folder (mimicking partial manual setup)
            (v / "第1课.md").write_text("# 第1课（根目录重复副本）\n", encoding="utf-8")
            (v / "第2课.md").write_text("# 第2课\n内容。\n", encoding="utf-8")
            (v / "课件.pptx").write_bytes(b"PK\x03\x04fake")

            info = inspect_folder(v)
            self.assertEqual(info["kind"], "existing_vault")
            self.assertIn("第2课.md", info["loose_entries"])
            self.assertIn("课件.pptx", info["loose_entries"])

            res = onboard_folder(v, name="量化", layout="zh", adopt_loose_files=True, run_sync=False)
            self.assertEqual(res["onboard_mode"], "existing_vault")
            self.assertIn("第2课.md", res["moved_entries"])
            self.assertIn("课件.pptx", res["moved_entries"])
            self.assertTrue((v / "史料" / "第2课.md").is_file())
            self.assertTrue((v / "史料" / "课件.pptx").is_file())
            # Existing file inside 史料/ must NEVER be overwritten by a same-named root file
            self.assertIn("已在史料中", (v / "史料" / "第1课.md").read_text(encoding="utf-8"))
            self.assertTrue((v / "AGENTS.md").is_file())

    def test_managed_vault_zero_invasion_multi_mount_relink_and_guards(self):
        # 1x1 transparent PNG bytes for visual MCP test
        png_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_dir = root / "用户量化原文件夹"
            (raw_dir / "核心策略").mkdir(parents=True)
            (raw_dir / "核心策略" / "多因子模型.md").write_text(
                "# 多因子模型\n\n动量因子周期固定为 21 个交易日，IC 均值 0.068。\n",
                encoding="utf-8",
            )
            (raw_dir / "核心策略" / "chart.png").write_bytes(png_bytes)

            before_entries = sorted(p.relative_to(raw_dir).as_posix() for p in raw_dir.rglob("*"))

            # 1. Create managed workspace -> ZERO mutation to raw_dir
            ws = root / "managed-vaults" / "quant"
            rep = create_managed_vault(ws, source_dir=raw_dir, name="量化资料库", layout="zh", run_sync=True)
            self.assertEqual(rep["storage_mode"], "managed")
            self.assertEqual(rep["moved_entries"], [])
            after_entries = sorted(p.relative_to(raw_dir).as_posix() for p in raw_dir.rglob("*"))
            self.assertEqual(before_entries, after_entries)
            self.assertFalse((ws / "史料").exists(), "Managed workspace should not create stray empty 史料/ folder")
            self.assertTrue((ws / "AGENTS.md").is_file())
            self.assertIn(str(raw_dir.resolve()), (ws / "AGENTS.md").read_text(encoding="utf-8"))

            cfg = VaultConfig.load(ws)
            self.assertEqual(cfg.storage_mode, "managed")
            self.assertTrue(cfg.is_external_sources)
            self.assertEqual(cfg.sources_root, raw_dir.resolve())

            # 2. Containment guard rejects overlapping sources_path (parent, child, or equal)
            bad_ws = root / "bad-ws"
            with self.assertRaises(ValueError):
                init_vault(bad_ws, source_dir=bad_ws)
            with self.assertRaises(ValueError):
                init_vault(bad_ws, source_dir=bad_ws / "sub")
            with self.assertRaises(ValueError):
                init_vault(bad_ws / "nested", source_dir=bad_ws)

            # 3. Zero-copy extra mount via import_external_folder (must delegate to mount, NEVER write into raw_dir!)
            extra_dir = root / "外部补充研报"
            extra_dir.mkdir()
            (extra_dir / "宏观研报.md").write_text(
                "# 2026 宏观流动性\n\nMLF 基准利率维持 2.15%。\n",
                encoding="utf-8",
            )
            (extra_dir / "diagram.png").write_bytes(png_bytes)

            imp = import_external_folder(ws, extra_dir, target_subfolder="补充研报", run_sync=True)
            self.assertEqual(imp["mode"], "mount")
            self.assertTrue(imp["mounted"])
            self.assertEqual(imp["copied_count"], 0)
            self.assertEqual(sorted(p.relative_to(raw_dir).as_posix() for p in raw_dir.rglob("*")), before_entries)
            self.assertFalse((raw_dir / "补充研报").exists())

            # Reject overlapping duplicate mount
            with self.assertRaises(ValueError):
                mount_source_folder(ws, raw_dir / "核心策略")

            # 4. MCP query, visual inspection, and remember work across BOTH primary sources_path AND source_mounts
            svc = KBService(ws)
            try:
                out_primary = svc.tool_context({"question": "多因子模型动量因子周期是多少？"})
                self.assertIn("21 个交易日", out_primary["context_markdown"])
                out_mount = svc.tool_context({"question": "MLF 基准利率是多少？"})
                self.assertIn("2.15%", out_mount["context_markdown"])

                vis_p, err_p = svc.tool_inspect_visual({"source": "史料/核心策略/chart.png"})
                self.assertFalse(err_p)
                self.assertTrue(any(b.get("type") == "image" for b in vis_p))

                vis_m, err_m = svc.tool_inspect_visual({"source": "史料/补充研报/diagram.png"})
                self.assertFalse(err_m)
                self.assertTrue(any(b.get("type") == "image" for b in vis_m))

                rem = svc.tool_remember({
                    "kind": "errata",
                    "text": "最新修正：周期调为 20 日",
                    "related_sources": ["史料/补充研报/宏观研报.md"],
                })
                self.assertTrue(rem["saved"])
                self.assertNotIn("warning", rem)
            finally:
                svc.close()

            # 5. VaultWatcherManager detects changes inside BOTH external sources_path AND source_mounts
            seen: list[str] = []
            fired = threading.Event()
            wm = VaultWatcherManager(
                on_change=lambda vid: (seen.append(vid), fired.set()),
                poll_interval_sec=1.0,
                debounce_sec=0.5,
            )
            wm.set_vaults({"quant": ws})
            wm.start()
            try:
                (extra_dir / "新研报.md").write_text("# 新研报\n新增内容\n", encoding="utf-8")
                self.assertTrue(fired.wait(timeout=5.0), "Watcher did not detect edit inside mounted source folder")
                self.assertEqual(seen[0], "quant")
            finally:
                wm.stop()

            # 6. Offline primary source protection + zero re-extraction relink
            moved_dir = root / "移动后的量化目录"
            shutil.move(str(raw_dir), str(moved_dir))
            with self.assertRaises(FileNotFoundError):
                sync_vault(ws, rebuild=True)

            # Existing index still serves queries while primary source is offline!
            svc_off = KBService(ws)
            try:
                q_off = svc_off.tool_context({"question": "动量因子周期"})
                self.assertIn("21 个交易日", q_off["context_markdown"])
            finally:
                svc_off.close()

            # Relink to moved_dir -> ZERO re-extraction of unchanged files!
            rel_rep = relink_source_path(ws, moved_dir, run_sync=True)
            self.assertEqual(rel_rep["new_path"], str(moved_dir.resolve()))
            self.assertEqual(rel_rep["sync"]["converted"], 1)  # Only 新研报.md added above
            self.assertGreaterEqual(rel_rep["sync"]["up_to_date"], 2)  # 多因子模型.md & 宏观研报.md stayed up-to-date!

            # 7. Unmount cleans derived mirrors & index while NEVER touching extra_dir on disk
            umnt = unmount_source_folder(ws, "补充研报", purge_mirrors=True, rebuild=True)
            self.assertTrue(umnt["unmounted"])
            self.assertTrue((extra_dir / "宏观研报.md").is_file(), "Unmount must NEVER delete original external files")
            svc_after = KBService(ws)
            try:
                q_umnt = svc_after.tool_context({"question": "MLF 基准利率是多少？"})
                self.assertNotIn("2.15%", q_umnt["context_markdown"])
            finally:
                svc_after.close()

            # 8. CLI `wctx init --source-dir`, `wctx mount`, `wctx relink`, `wctx unmount`
            cli_ws = root / "cli-managed-ws"
            self.assertEqual(
                cli_main(["init", str(cli_ws), "--source-dir", str(moved_dir), "--layout", "zh", "--name", "CLI库"]),
                0,
            )
            self.assertEqual(cli_main(["mount", "--vault", str(cli_ws), str(extra_dir), "--name", "外部"]), 0)
            self.assertEqual(cli_main(["unmount", "--vault", str(cli_ws), "外部"]), 0)
            vjson = json.loads((cli_ws / "vault.json").read_text(encoding="utf-8"))
            self.assertEqual(vjson["sources_path"], str(moved_dir.resolve()))
            self.assertNotIn("source_mounts", vjson)

    def test_invalid_mount_recovery_duplicate_mount_dedup_and_relink_ghost_pruning(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # F4 regression: one invalid mount entry must NOT brick VaultConfig.load or unmount repair
            v = root / "repairable-vault"
            ext_good = root / "ext-good"
            ext_good.mkdir()
            (ext_good / "g.md").write_text("# Good\nOK\n", encoding="utf-8")
            init_vault(v, name="RepairKB", layout="en")
            vjson_p = v / "vault.json"
            vdoc = json.loads(vjson_p.read_text(encoding="utf-8"))
            vdoc["source_mounts"] = {"good": str(ext_good), "bad": str(v / "sources")}
            vjson_p.write_text(json.dumps(vdoc, indent=2), encoding="utf-8")

            cfg = VaultConfig.load(v)
            self.assertEqual([m[0] for m in cfg.source_mounts], ["good"])
            self.assertEqual(len(cfg.invalid_mounts), 1)
            self.assertEqual(cfg.invalid_mounts[0][0], "bad")
            # Product can self-repair via unmount_source_folder
            um = unmount_source_folder(v, "bad")
            self.assertTrue(um["unmounted"])
            self.assertEqual(VaultConfig.load(v).invalid_mounts, ())

            # F5 regression: duplicate normalized mount names ("archive" vs "archive/") must dedup, no sync flip-flop
            ext_a = root / "ext-a"
            ext_b = root / "ext-b"
            ext_a.mkdir()
            ext_b.mkdir()
            (ext_a / "spec.md").write_text("# Spec A\nVersion A\n", encoding="utf-8")
            (ext_b / "spec.md").write_text("# Spec B\nVersion B\n", encoding="utf-8")
            v_dup = root / "dup-mount-vault"
            init_vault(v_dup, name="DupMountKB", layout="en")
            vdup_json = v_dup / "vault.json"
            d_doc = json.loads(vdup_json.read_text(encoding="utf-8"))
            d_doc["source_mounts"] = {"archive": str(ext_a), "archive/": str(ext_b)}
            vdup_json.write_text(json.dumps(d_doc, indent=2), encoding="utf-8")
            cfg_dup = VaultConfig.load(v_dup)
            self.assertEqual(len(cfg_dup.source_mounts), 1)
            self.assertEqual(len(cfg_dup.invalid_mounts), 1)
            s1 = sync_vault(v_dup)
            s2 = sync_vault(v_dup)
            self.assertEqual(s1["converted"], 1)
            self.assertEqual(s2["converted"], 0)
            self.assertEqual(s2["up_to_date"], 1)

            # F9 regression: relinking to an unrelated/empty folder prunes ghost mirrors & manifest entries
            src_v1 = root / "docs-v1"
            src_v2 = root / "docs-v2-unrelated"
            src_v1.mkdir()
            src_v2.mkdir()
            (src_v1 / "old.md").write_text("# Old\nQUANTUM_FLUX_GHOST\n", encoding="utf-8")
            ws_rel = root / "ws-relink"
            create_managed_vault(ws_rel, source_dir=src_v1, name="RelinkPrune", layout="en", run_sync=True)
            self.assertTrue((ws_rel / "mirrors" / "old.md.md").is_file())

            rel = relink_source_path(ws_rel, src_v2, run_sync=True)
            self.assertEqual(rel["pruned_missing"], 1)
            self.assertFalse((ws_rel / "mirrors" / "old.md.md").exists())
            svc = KBService(ws_rel)
            try:
                q = svc.tool_context({"question": "QUANTUM_FLUX_GHOST"})
                self.assertEqual(q["primary_count"], 0)
                self.assertNotIn("sources/old.md", q["context_markdown"])
            finally:
                svc.close()


if __name__ == "__main__":
    unittest.main()
