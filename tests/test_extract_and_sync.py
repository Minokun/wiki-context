"""Tests for native OOXML/CSV extraction, admission policy, and incremental sync."""

from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wiki_context.extract import extract_document  # noqa: E402
from wiki_context.policy import source_skip_reason  # noqa: E402
from wiki_context.scaffold import init_vault  # noqa: E402
from wiki_context.sync import sync_vault  # noqa: E402


def _write_minimal_docx(path: Path, heading: str, cell_a: str, cell_b: str) -> None:
    doc_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body>
        <w:p><w:pPr><w:pStyle w:val="1"/></w:pPr><w:r><w:t>{heading}</w:t></w:r></w:p>
        <w:tbl>
          <w:tr>
            <w:tc><w:p><w:r><w:t>Metric</w:t></w:r></w:p></w:tc>
            <w:tc><w:p><w:r><w:t>Target</w:t></w:r></w:p></w:tc>
          </w:tr>
          <w:tr>
            <w:tc><w:p><w:r><w:t>{cell_a}</w:t></w:r></w:p></w:tc>
            <w:tc><w:p><w:r><w:t>{cell_b}</w:t></w:r></w:p></w:tc>
          </w:tr>
        </w:tbl>
      </w:body>
    </w:document>"""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", doc_xml)


def _write_minimal_xlsx(path: Path) -> None:
    wb = """<?xml version="1.0" encoding="UTF-8"?>
    <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
              xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <sheets><sheet name="Quotas" sheetId="1" r:id="rId1"/></sheets>
    </workbook>"""
    rels = """<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Target="worksheets/sheet1.xml"/>
    </Relationships>"""
    ss = """<?xml version="1.0" encoding="UTF-8"?>
    <sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
      <si><t>Plan</t></si><si><t>MaxRPS</t></si><si><t>Pro</t></si>
    </sst>"""
    sheet = """<?xml version="1.0" encoding="UTF-8"?>
    <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
      <sheetData>
        <row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>
        <row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>5000</v></c></row>
      </sheetData>
    </worksheet>"""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/workbook.xml", wb)
        zf.writestr("xl/_rels/workbook.xml.rels", rels)
        zf.writestr("xl/sharedStrings.xml", ss)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)


class TestExtractAndSync(unittest.TestCase):
    def test_docx_xlsx_csv_extraction_and_incremental_sync(self):
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "vault"
            init_vault(v, name="TestVault")
            _write_minimal_docx(v / "sources" / "sla.docx", "Service SLA", "Latency", "12ms")
            _write_minimal_xlsx(v / "sources" / "plans.xlsx")
            (v / "sources" / "matrix.csv").write_text("Code,Limit\nA100,950\n", encoding="utf-8")

            # Machine logs and secret files must be skipped by admission policy
            (v / "sources" / "app.log").write_text("INFO boot\n", encoding="utf-8")
            (v / "sources" / ".env").write_text("SECRET=123\n", encoding="utf-8")
            self.assertEqual(source_skip_reason(v / "sources" / ".env", ".env"), "secret-credential-file")

            res1 = sync_vault(v)
            self.assertEqual(res1["converted"], 3)
            self.assertEqual(res1["failed"], 0)
            self.assertFalse((v / "mirrors" / "app.log.md").exists())
            self.assertFalse((v / "mirrors" / ".env.md").exists())

            # Second sync with unchanged SHA-256 is a fast no-op
            res2 = sync_vault(v)
            self.assertEqual(res2["converted"], 0)
            self.assertEqual(res2["up_to_date"], 3)
            self.assertTrue(res2["index"]["noop"])

            docx_doc = extract_document(v / "sources" / "sla.docx")
            self.assertIn("# Service SLA", docx_doc.markdown)
            self.assertIn("| Latency | 12ms |", docx_doc.markdown)

    def test_pluggable_ocr_command_hook(self):
        with tempfile.TemporaryDirectory() as td:
            v = Path(td) / "ocr-vault"
            init_vault(v)
            img = v / "sources" / "diagram.png"
            img.write_bytes(b"\x89PNG\r\n\x1a\nfake")

            ocr_cmd = f"{sys.executable} -c \"print('# OCR Diagram\\n\\nExtracted port 8443')\""
            res = sync_vault(v, source="sources/diagram.png", ocr_cmd=ocr_cmd)
            self.assertEqual(res["converted"], 1)
            mirror_txt = (v / "mirrors" / "diagram.png.md").read_text(encoding="utf-8")
            self.assertIn("Extracted port 8443", mirror_txt)


if __name__ == "__main__":
    unittest.main()
