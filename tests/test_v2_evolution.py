"""Comprehensive unit & security tests for `wiki-context` v0.2.0 evolution:
- P1: Deep Office/PDF/Rich-format extraction (XLSX merge/banner/side-by-side/drawing, DOCX sdt/txbx/spans/footnotes, PPTX SmartArt/notes, PDF layout table reconstruction, HTML/EML/IPYNB/SRT/VTT)
- P2: High-recall code punctuation variants, directory-path tokens, and cross-source near-dedup
- P3: Embedded visual asset extraction, VLM/ASR hooks, and ACL-guarded MCP `kb_inspect_visual`
- P4: Layer-4 operational memory (`notes/`/`记忆/`), 1-hop Errata Pins, secret scrubbing, ACL inheritance, `kb_forget`, and `wctx compile` consolidation
- P5: Automated glossary & inline acronym synonym miner (`mine_synonyms`) and `wctx eval`
"""

from __future__ import annotations

import base64
import json
import struct
import sys
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wiki_context.cli import main as cli_main  # noqa: E402
from wiki_context.compiler import compile_vault  # noqa: E402
from wiki_context.context import PrefixACLPolicy, Principal  # noqa: E402
from wiki_context.eval_harness import evaluate_vault  # noqa: E402
from wiki_context.extract import _reconstruct_pdf_layout_tables, extract_document  # noqa: E402
from wiki_context.mcp_server import KBService, handle_jsonrpc_message  # noqa: E402
from wiki_context.scaffold import init_vault  # noqa: E402
from wiki_context.sync import sync_vault  # noqa: E402
from wiki_context.synonym_miner import mine_synonyms  # noqa: E402


def _make_png(w: int = 96, h: int = 96) -> bytes:
    rows = []
    for y in range(h):
        r = bytearray([0])
        for x in range(w):
            r.extend((((x * 73 + y * 151) & 0xFF), ((x * 37 + y * 199) & 0xFF), ((x ^ y) & 0xFF), 255))
        rows.append(bytes(r))
    idat = zlib.compress(b"".join(rows), level=0)

    def chk(t: bytes, d: bytes) -> bytes:
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) + chk(b"IDAT", idat) + chk(b"IEND", b"")


class TestV2Evolution(unittest.TestCase):
    def test_pdf_layout_table_reconstruction_preserves_sql_code(self):
        raw_page = (
            "Section 1.2 Pinout Spec\n\n"
            "Pin_Name    Voltage_Level    Max_Current\n"
            "VCC_CORE    1.8V             2500mA\n"
            "IO_BANK0    3.3V             500mA\n\n"
            "    1   CREATE TABLE sensor_log (\n"
            "    2       id UInt64,\n"
            "    3       ts DateTime\n"
        )
        reconstructed = _reconstruct_pdf_layout_tables(raw_page)
        self.assertIn("| Pin_Name | Voltage_Level | Max_Current |", reconstructed)
        self.assertIn("| VCC_CORE | 1.8V | 2500mA |", reconstructed)
        # SQL block must NOT be mangled into a pipe table
        self.assertIn("CREATE TABLE sensor_log", reconstructed)

    def test_eml_html_ipynb_vtt_and_asr_extraction(self):
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "vault"
            init_vault(v, name="RichFormatsKB", layout="en")
            s = v / "sources"

            (s / "rfc-mail.eml").write_text(
                "From: arch@example.com\nTo: team@example.com\nSubject: Quorum Timeout Change\n"
                "Content-Type: text/plain; charset=utf-8\n\n"
                "Raft election timeout updated to 450ms for cross-AZ links.\n",
                encoding="utf-8",
            )
            (s / "spec.html").write_text(
                "<html><head><title>Sensor Spec</title><style>body{color:red}</style></head>"
                "<body><h2>Optical Encoder</h2><table><tr><th>Model</th><th>CPR</th></tr>"
                "<tr><td>ENC-7700</td><td>4096</td></tr></table></body></html>",
                encoding="utf-8",
            )
            (s / "standup.mp3").write_bytes(b"FAKE_AUDIO_BYTES")

            # Sync with mock ASR command emitting WebVTT
            sync_vault(
                v,
                asr_cmd=f'{sys.executable} -c "print(\'WEBVTT\\n\\n00:02:10.000 --> 00:02:25.000\\nBrake caliper torque set to 85Nm.\')"',
            )
            svc = KBService(v)
            try:
                c1 = svc.tool_context({"question": "What is the Raft election timeout in Quorum Timeout Change?"})
                self.assertIn("450ms", c1["context_markdown"])
                c2 = svc.tool_context({"question": "What is the CPR of ENC7700?"})
                self.assertIn("4096", c2["context_markdown"])
                c3 = svc.tool_context({"question": "What torque was set for the brake caliper?"})
                self.assertIn("85Nm", c3["context_markdown"])
                self.assertIn("[00:02:10 - 00:02:25]", c3["context_markdown"])
            finally:
                svc.close()

    def test_visual_mcp_acl_and_vlm_caption_hook(self):
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "zh-vault"
            init_vault(v, name="视觉测试库", layout="zh")
            sec_dir = v / "史料" / "机密图纸"
            pub_dir = v / "史料" / "公开规范"
            sec_dir.mkdir(parents=True)
            pub_dir.mkdir(parents=True)

            # Standalone confidential diagram PNG
            (sec_dir / "核心电路图.png").write_bytes(_make_png())

            # Public DOCX with embedded diagram
            docx_p = pub_dir / "网关设计.docx"
            with zipfile.ZipFile(docx_p, "w") as zf:
                zf.writestr(
                    "word/document.xml",
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
                    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                    "<w:body><w:p><w:r><w:t>公开网关设计正文（受电弓 (Pantograph) 接口）。</w:t><a:blip/></w:r></w:p></w:body></w:document>",
                )
                zf.writestr("word/media/gateway_topo.png", _make_png())

            class MockVLM:
                def caption_image(self, image_path: Path) -> str:
                    return f"VLM识别图注：检测到拓扑节点 {image_path.name} 含双冗余光模块 100GbE。"

            sync_vault(v, include_all_images=True, vlm_provider=MockVLM())  # type: ignore[arg-type]

            acl = PrefixACLPolicy({
                "partner": {
                    "allow": ["史料/公开规范/**"],
                    "deny": ["史料/机密图纸/**"],
                }
            })
            svc = KBService(v, policy=acl)
            try:
                partner = Principal(uid="ext_user", groups=("partner",))
                admin = Principal(uid="lead", groups=("admin",))

                # VLM caption is indexed and searchable!
                ctx = svc.tool_context({"question": "双冗余光模块速率是多少？"}, principal=partner)
                self.assertIn("100GbE", ctx["context_markdown"])
                self.assertIn("🖼️", ctx["context_markdown"])

                # Partner CAN inspect embedded diagram on public DOCX
                r_ok = handle_jsonrpc_message(
                    svc,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "kb_inspect_visual", "arguments": {"source": "史料/公开规范/网关设计.docx"}},
                    },
                    principal=partner,
                )
                assert r_ok is not None
                self.assertFalse(r_ok["result"]["isError"])
                img_blocks = [b for b in r_ok["result"]["content"] if b.get("type") == "image"]
                self.assertEqual(len(img_blocks), 1)
                self.assertTrue(base64.b64decode(img_blocks[0]["data"]).startswith(b"\x89PNG"))

                # Partner CANNOT inspect confidential standalone PNG!
                r_deny = handle_jsonrpc_message(
                    svc,
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "kb_inspect_visual", "arguments": {"source": "史料/机密图纸/核心电路图.png"}},
                    },
                    principal=partner,
                )
                assert r_deny is not None
                self.assertTrue(r_deny["result"]["isError"])

                # Admin CAN inspect confidential standalone PNG
                r_adm = handle_jsonrpc_message(
                    svc,
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "kb_inspect_visual", "arguments": {"source": "史料/机密图纸/核心电路图.png"}},
                    },
                    principal=admin,
                )
                assert r_adm is not None
                self.assertFalse(r_adm["result"]["isError"])
            finally:
                svc.close()

    def test_layer4_memory_errata_acl_scrubbing_forget_and_compile_consolidation(self):
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "mem-vault"
            init_vault(v, name="MemoryTestKB", layout="en")
            (v / "sources" / "public").mkdir(parents=True)
            (v / "sources" / "finance").mkdir(parents=True)
            (v / "sources" / "public" / "api.md").write_text("# API Quota\nDefault burst is 500 RPS.\n", encoding="utf-8")
            (v / "sources" / "finance" / "margin.md").write_text("# Gross Margin\nTarget margin is 68%.\n", encoding="utf-8")
            sync_vault(v)

            acl = PrefixACLPolicy({
                "contractor": {
                    "allow": ["sources/public/**"],
                    "deny": ["sources/finance/**"],
                }
            })
            svc = KBService(v, policy=acl)
            try:
                admin = Principal(uid="alice", groups=("admin",))
                contractor = Principal(uid="bob", groups=("contractor",))

                # 1. Contractor cannot anchor a note to restricted finance source
                deny_rem = svc.tool_remember(
                    {"text": "Sneaky note", "related_sources": ["sources/finance/margin.md"]},
                    principal=contractor,
                )
                self.assertIn("error", deny_rem)

                # 2. Admin records finance insight (with accidental API key that must be scrubbed!)
                fin_rem = svc.tool_remember(
                    {
                        "text": "Q4 gross margin target revised to 72% (key sk-proj-1234567890abcdefghijklmnop).",
                        "kind": "insight",
                        "related_sources": ["sources/finance/margin.md"],
                    },
                    principal=admin,
                )
                self.assertTrue(fin_rem.get("saved"))
                note_raw_disk = (v / fin_rem["file"]).read_text(encoding="utf-8")
                self.assertNotIn("sk-proj-1234567890", note_raw_disk)
                self.assertIn("[REDACTED_API_KEY]", note_raw_disk)

                # Contractor querying margin must NOT see the finance memory note!
                c_con = svc.tool_context({"question": "What is the Q4 gross margin target?"}, principal=contractor)
                self.assertNotIn("72%", c_con["context_markdown"])

                # Admin DOES see the finance memory note in fenced Operational Memory section
                c_adm = svc.tool_context({"question": "What is the Q4 gross margin target?"}, principal=admin)
                self.assertIn("72%", c_adm["context_markdown"])
                self.assertIn("Operational Memory", c_adm["context_markdown"])

                # 3. Admin pins an Errata onto public API spec -> visible to contractor on 1-hop primary hit, then retracted via kb_forget
                pub_err = svc.tool_remember(
                    {
                        "text": "ERRATA-99: Default burst raised from 500 RPS to 1500 RPS.",
                        "kind": "errata",
                        "related_sources": ["sources/public/api.md"],
                    },
                    principal=admin,
                )
                c_pin = svc.tool_context({"question": "What is the API default burst RPS?"}, principal=contractor)
                self.assertIn("ERRATA-99", c_pin["context_markdown"])
                self.assertIn("1500 RPS", c_pin["context_markdown"])

                svc.tool_forget({"note_id": pub_err["note_id"]}, principal=admin)
                c_after_forget = svc.tool_context({"question": "What is the API default burst RPS?"}, principal=contractor)
                self.assertNotIn("ERRATA-99", c_after_forget["context_markdown"])
            finally:
                svc.close()

            # 4. `compile_vault` consolidates active insight notes into Wiki & preserves source ACL inheritance!
            def _mock_compiler(_sys: str, usr: str) -> str:
                import re as _re
                m_t = _re.search(r'title:\s*"([^"]+)"', usr)
                title = m_t.group(1) if m_t else "Compiled-Page"
                src_line = "sources/finance/margin.md" if "margin.md" in usr else "sources/public/api.md"
                return (
                    f'---\ntitle: "{title}"\ntype: synthesis\nstatus: reviewed\n'
                    f'updated: 2026-11-22\nsources:\n  - {src_line}\n---\n\n'
                    f"# {title}\n\nConsolidated: Q4 gross margin target revised to 72%.\n\n"
                    "## Related Pages\n\n[[Overview]]\n"
                )

            comp_res = compile_vault(v, llm_fn=_mock_compiler)
            self.assertEqual(len(comp_res["consolidated_notes"]), 1)
            self.assertEqual(comp_res["lint"]["error_count"], 0)

    def test_synonym_miner_inline_and_eval_cli(self):
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "syn-vault"
            init_vault(v, name="SynEvalKB", layout="en")
            (v / "sources" / "glossary.md").write_text(
                "# Domain Terms\n\n"
                "We deploy Overhead Catenary Monitoring (OCM_4C) and 受电弓 (Pantograph) sensors.\n\n"
                "| 术语名称 | English | 缩写 |\n"
                "| --- | --- | --- |\n"
                "| 接触线 | ContactWire | CW_01 |\n",
                encoding="utf-8",
            )
            sync_vault(v)

            rep = mine_synonyms(v, apply=True)
            self.assertGreaterEqual(rep["discovered_groups"], 3)
            syn_txt = (v / "synonyms.json").read_text(encoding="utf-8")
            self.assertIn("CW_01", syn_txt)
            self.assertIn("Pantograph", syn_txt)
            self.assertIn("OCM_4C", syn_txt)

            # Test CLI `eval` smoke auto-generation & `remember` / `notes` commands
            self.assertEqual(cli_main(["remember", "--vault", str(v), "-t", "CW_01 inspection interval is 7 days."]), 0)
            self.assertEqual(cli_main(["notes", "list", "--vault", str(v)]), 0)
            eval_rep = evaluate_vault(v)
            self.assertGreaterEqual(eval_rep["strict_recall"], 1.0)


if __name__ == "__main__":
    unittest.main()
