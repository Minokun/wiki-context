#!/usr/bin/env python3
"""Comprehensive v2 Evolution Benchmark Suite (`run_hard_benchmark.py`).

Builds a realistic multi-format synthetic hard-tech vault exercising:
- H1: XLSX Row-1 single-cell banner + vertically merged category cells (`<mergeCells>`)
- H2: XLSX side-by-side sub-tables separated by empty column gutter + glossary abbreviations
- H3: XLSX DrawingML flowchart shapes (`xl/drawings/drawing1.xml`) with zero worksheet cells
- H4: DOCX `w:sdt` content controls + `w:txbxContent` diagram labels + `word/footnotes.xml` + embedded PNG diagram
- H5: PPTX SmartArt (`ppt/diagrams/data1.xml`) + speaker notes (`ppt/notesSlides/notesSlide1.xml`)
- H6: HTML technical clipping + Jupyter `.ipynb` notebook + `.eml` RFC email + `.vtt` meeting transcript
- H7: Punctuation-variant technical code query (`RFC042` / `SNK908X` matching `RFC-042` / `SN-K908X`) + single-letter `Type-A`
- H8: Parent-folder-scoped query (`2026-09 reimbursement` where `2026-09` is only in directory path)
- H9: Chinese compound technical query with cross-word boundary bigrams (`受电弓羊角异常检测算法`)
- H10: Cross-format near-duplicate folding (`.docx` + `.pdf`/`.md` duplicate not wasting Top-K slots)
- H11: Multimodal MCP native visual inspection (`kb_inspect_visual` returning base64 PNG content block)
- H12: Layer-4 source-anchored Errata Pin (`kb_remember(kind='errata')` surfacing on read-only primary spec)
- H13: Automated synonym mining (`mine_synonyms`) discovering glossary abbreviations
- H14: Unanswerable hallucination guardrail probe (`HyperLoopVacuumPump99`)

Outputs `HARD_COMPOSITE_SCORE=<0.0..100.0>` on the final line for deterministic `autoresearch` tracking.
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from wiki_context.eval_harness import evaluate_vault  # noqa: E402
from wiki_context.mcp_server import KBService, handle_jsonrpc_message  # noqa: E402
from wiki_context.scaffold import init_vault  # noqa: E402
from wiki_context.sync import sync_vault  # noqa: E402

# Minimal valid 80x80 RGBA PNG (~4.5 KiB uncompressible random-ish payload so >= 4 KiB threshold)
import struct
import zlib as _zlib


def _make_diagram_png(width: int = 96, height: int = 96) -> bytes:
    raw_rows = []
    for y in range(height):
        row = bytearray([0])  # filter type 0
        for x in range(width):
            # deterministic non-trivial pattern so compressed PNG > 4.2 KiB
            v1 = ((x * 73 + y * 151 + (x ^ y) * 19) * 2654435761) & 0xFF
            v2 = ((x * 199 + y * 37 + (x * y)) * 2246822519) & 0xFF
            v3 = ((x * 43 + y * 241) ^ 0xA5) & 0xFF
            row.extend((v1, v2, v3, 255))
        raw_rows.append(bytes(row))
    idat_data = _zlib.compress(b"".join(raw_rows), level=0)

    def chunk(ctype: bytes, data: bytes) -> bytes:
        c = ctype + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", _zlib.crc32(c) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat_data) + chunk(b"IEND", b"")


def _build_complex_xlsx(path: Path) -> None:
    """Create XLSX with Row-1 banner, merged category cells, side-by-side glossary tables, and DrawingML flowchart."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            "<sheets>"
            '<sheet name="HardwareSpec" sheetId="1" r:id="rId1"/>'
            '<sheet name="GlossarySideBySide" sheetId="2" r:id="rId2"/>'
            '<sheet name="LabelingFlow" sheetId="3" r:id="rId3"/>'
            "</sheets></workbook>",
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Target="worksheets/sheet2.xml"/>'
            '<Relationship Id="rId3" Target="worksheets/sheet3.xml"/>'
            "</Relationships>",
        )
        # Sheet 1: Row 1 single-cell banner, Row 2 true headers, A3:A28 vertically merged category "PantographSubsystem",
        # with 25 filler rows so slicing kicks in, and row 28 holding target "CarbonStrip-TypeA | 22mm | 850V"
        rows_xml = [
            '<row r="1"><c r="A1" t="inlineStr"><is><t>2026 Annual Hardware &amp; Mechanical BOM Banner</t></is></c></row>',
            '<row r="2">'
            '<c r="A2" t="inlineStr"><is><t>Subsystem_Category</t></is></c>'
            '<c r="B2" t="inlineStr"><is><t>Part_Model</t></is></c>'
            '<c r="C2" t="inlineStr"><is><t>Wear_Limit_Thickness</t></is></c>'
            '<c r="D2" t="inlineStr"><is><t>Rated_Voltage</t></is></c>'
            "</row>",
        ]
        for idx in range(3, 95):
            cat_cell = (
                f'<c r="A{idx}" t="inlineStr"><is><t>PantographSubsystem</t></is></c>'
                if idx == 3
                else ""
            )
            if idx < 94:
                rows_xml.append(
                    f'<row r="{idx}">{cat_cell}'
                    f'<c r="B{idx}" t="inlineStr"><is><t>Spacer-Bolt-{idx:02d}</t></is></c>'
                    f'<c r="C{idx}" t="inlineStr"><is><t>{10 + idx}mm</t></is></c>'
                    f'<c r="D{idx}" t="inlineStr"><is><t>24V-Filler-Padding-Description-For-Row-{idx:02d}-Testing-Window-Slice-Budget</t></is></c>'
                    "</row>"
                )
            else:
                rows_xml.append(
                    f'<row r="{idx}">{cat_cell}'
                    '<c r="B94" t="inlineStr"><is><t>CarbonStrip-Type-A</t></is></c>'
                    '<c r="C94" t="inlineStr"><is><t>7.5mm</t></is></c>'
                    '<c r="D94" t="inlineStr"><is><t>1500VDC</t></is></c>'
                    "</row>"
                )
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{''.join(rows_xml)}</sheetData>"
            '<mergeCells count="2"><mergeCell ref="A1:D1"/><mergeCell ref="A3:A94"/></mergeCells>'
            "</worksheet>",
        )

        # Sheet 2: Two side-by-side glossary tables separated by empty column E
        zf.writestr(
            "xl/worksheets/sheet2.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            '<row r="1">'
            '<c r="A1" t="inlineStr"><is><t>构件名称表</t></is></c>'
            '<c r="F1" t="inlineStr"><is><t>缺陷状态表</t></is></c>'
            "</row>"
            '<row r="2">'
            '<c r="A2" t="inlineStr"><is><t>部件中文名</t></is></c>'
            '<c r="B2" t="inlineStr"><is><t>英文名</t></is></c>'
            '<c r="C2" t="inlineStr"><is><t>简写</t></is></c>'
            '<c r="F2" t="inlineStr"><is><t>状态名称</t></is></c>'
            '<c r="G2" t="inlineStr"><is><t>EnglishCode</t></is></c>'
            "</row>"
            '<row r="3">'
            '<c r="A3" t="inlineStr"><is><t>承力索</t></is></c>'
            '<c r="B3" t="inlineStr"><is><t>MessengerWire</t></is></c>'
            '<c r="C3" t="inlineStr"><is><t>C_CLS</t></is></c>'
            '<c r="F3" t="inlineStr"><is><t>电弧烧伤</t></is></c>'
            '<c r="G3" t="inlineStr"><is><t>ArcBurnDefect</t></is></c>'
            "</row>"
            "</sheetData></worksheet>",
        )

        # Sheet 3: DrawingML flowchart shapes only (zero worksheet cells!)
        zf.writestr(
            "xl/worksheets/sheet3.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            "<sheetData/>"
            '<drawing r:id="rIdDrw1"/>'
            "</worksheet>",
        )
        zf.writestr(
            "xl/worksheets/_rels/sheet3.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rIdDrw1" Target="../drawings/drawing1.xml"/>'
            "</Relationships>",
        )
        zf.writestr(
            "xl/drawings/drawing1.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            "<xdr:twoCellAnchor><xdr:sp><xdr:txBody>"
            "<a:p><a:r><a:t>AutoLabelPipeline Step1: SegGPT Few-Shot Mask Proposal</a:t></a:r></a:p>"
            "<a:p><a:r><a:t>AutoLabelPipeline Step2: IoU>=0.88 Human Audit Gate</a:t></a:r></a:p>"
            "</xdr:txBody></xdr:sp></xdr:twoCellAnchor>"
            "</xdr:wsDr>",
        )


def _build_complex_docx(path: Path) -> None:
    """Create DOCX with w:sdt content control, w:txbxContent textbox, footnotes, and >=4KiB embedded PNG."""
    png_bytes = _make_diagram_png()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            "<w:body>"
            "<w:p><w:r><w:t>受电弓羊角区域的异常检测算法总体说明（设备序列号：SN-K908X，协议号：RFC-042）。</w:t></w:r></w:p>"
            "<w:sdt><w:sdtContent>"
            "<w:p><w:r><w:t>SDT控件章节：线阵相机行频必须锁定为 42kHz，曝光上限 18us。</w:t></w:r></w:p>"
            "</w:sdtContent></w:sdt>"
            "<w:p><w:r><w:drawing><a:graphic><a:graphicData>"
            '<a:blip r:embed="rIdImg1"/>'
            "<w:txbxContent><w:p><w:r><w:t>架构图框内标注：FPGA边缘触发延迟 &lt;= 1.2ms</w:t></w:r></w:p></w:txbxContent>"
            "</a:graphicData></a:graphic></w:drawing></w:r></w:p>"
            "</w:body></w:document>",
        )
        zf.writestr(
            "word/_rels/document.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rIdImg1" Target="media/arch_diagram.png"/>'
            "</Relationships>",
        )
        zf.writestr(
            "word/footnotes.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:footnote w:id="1"><w:p><w:r><w:t>脚注补充：低温工况指 -35C 极寒环境启动测试。</w:t></w:r></w:p></w:footnote>'
            "</w:footnotes>",
        )
        zf.writestr("word/media/arch_diagram.png", png_bytes)


def _build_complex_pptx(path: Path) -> None:
    """Create PPTX with SmartArt (`ppt/diagrams/data1.xml`) and Speaker Notes (`ppt/notesSlides/notesSlide1.xml`)."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "ppt/slides/slide1.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            "<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>6C Inspection Architecture</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld>"
            "</p:sld>",
        )
        zf.writestr(
            "ppt/diagrams/data1.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<dgm:dataModel xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            "<dgm:ptLst>"
            "<dgm:pt><dgm:t><a:p><a:r><a:t>SmartArtNode: Edge-TensorRT-INT8-Calibration</a:t></a:r></a:p></dgm:t></dgm:pt>"
            "</dgm:ptLst></dgm:dataModel>",
        )
        zf.writestr(
            "ppt/notesSlides/notesSlide1.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            "<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>SpeakerNote: Jetson Orin NX power budget capped at 25W.</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld>"
            "</p:notes>",
        )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="wctx-hard-bench-") as td:
        vault = Path(td) / "hard-vault"
        init_vault(vault, name="HardTech R&D Benchmark Vault", layout="en")
        # Relax max_tokens if needed; default config is fine
        s_root = vault / "sources"
        (s_root / "specs").mkdir(parents=True, exist_ok=True)
        (s_root / "expenses" / "2026-09-shanghai-trip").mkdir(parents=True, exist_ok=True)
        (s_root / "clippings").mkdir(parents=True, exist_ok=True)
        (s_root / "meetings").mkdir(parents=True, exist_ok=True)

        _build_complex_xlsx(s_root / "specs" / "bom-and-glossary.xlsx")
        _build_complex_docx(s_root / "specs" / "pantograph-4c-spec.docx")
        _build_complex_pptx(s_root / "specs" / "6c-roadmap.pptx")

        # Near-duplicate export of pantograph-4c-spec in Markdown + filler docs so Top-K slot competition matters
        (s_root / "specs" / "pantograph-4c-spec-exported-copy.md").write_text(
            "受电弓羊角区域的异常检测算法总体说明（设备序列号：SN-K908X，协议号：RFC-042）。\n"
            "SDT控件章节：线阵相机行频必须锁定为 42kHz，曝光上限 18us。\n"
            "架构图框内标注：FPGA边缘触发延迟 <= 1.2ms\n",
            encoding="utf-8",
        )

        # Folder-scoped invoice files where trip month/city ONLY appear in parent path
        (s_root / "expenses" / "2026-09-shanghai-trip" / "receipt-item.md").write_text(
            "Document #INV-9921: Accommodation fee settled, Total Paid CNY 1,280.00.\n",
            encoding="utf-8",
        )
        for m_idx in range(1, 7):
            d_other = s_root / "expenses" / f"2025-0{m_idx}-shenzhen-trip"
            d_other.mkdir(parents=True, exist_ok=True)
            (d_other / "receipt-item.md").write_text(
                f"Document #INV-100{m_idx}: Accommodation fee settled, Total Paid CNY {600 + m_idx * 50}.00.\n",
                encoding="utf-8",
            )

        # HTML + IPYNB + EML + VTT formats
        (s_root / "clippings" / "thermal-camera-note.html").write_text(
            "<html><head><title>IR Camera Spec</title><script>var x=1;</script></head>"
            "<body><h1>Uncooled VOx Microbolometer</h1><p>NETD sensitivity is &lt;= 35mK at 50Hz.</p></body></html>",
            encoding="utf-8",
        )
        (s_root / "clippings" / "calibration-exp.ipynb").write_text(
            json.dumps({
                "cells": [
                    {"cell_type": "markdown", "source": ["# Laser Triangulation Calibration\n", "Baseline residual RMS error: `0.042mm`."]},
                    {"cell_type": "code", "source": ["rms_threshold = 0.042\n"], "outputs": []},
                ],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }),
            encoding="utf-8",
        )
        (s_root / "meetings" / "dr-review.vtt").write_text(
            "WEBVTT\n\n00:14:10.000 --> 00:14:45.000\nChief Architect: Cold standby switchover SLA is strictly 18 seconds via BGP Anycast.\n",
            encoding="utf-8",
        )

        # Sync vault
        sync_vault(vault)

        # Check bonus capabilities (P3 visual MCP, P4 Layer-4 Errata memory, P5 synonym miner)
        bonus_checks = {
            "visual_mcp": False,
            "errata_memory": False,
            "synonym_miner": False,
            "near_dedup": False,
        }

        svc = KBService(vault)
        try:
            # Test P4: Layer-4 Errata Pin if `kb_remember` exists
            r_rem = handle_jsonrpc_message(
                svc,
                {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {
                        "name": "kb_remember",
                        "arguments": {
                            "text": "ERRATA-HOTFIX-812: 线阵相机行频已由 42kHz 上调至 68kHz（2026-11 现场技改通知）。",
                            "kind": "errata",
                            "related_sources": ["sources/specs/pantograph-4c-spec.docx"],
                        },
                    },
                },
            )
            if r_rem and not r_rem.get("result", {}).get("isError", True):
                ctx_err = svc.tool_context({"question": "线阵相机行频上限是多少？"})
                if "ERRATA-HOTFIX-812" in (ctx_err.get("context_markdown") or "") and "68kHz" in (ctx_err.get("context_markdown") or ""):
                    bonus_checks["errata_memory"] = True

            # Test P3: Native MCP visual inspection if `kb_inspect_visual` exists
            r_vis = handle_jsonrpc_message(
                svc,
                {
                    "jsonrpc": "2.0",
                    "id": 11,
                    "method": "tools/call",
                    "params": {
                        "name": "kb_inspect_visual",
                        "arguments": {"source": "sources/specs/pantograph-4c-spec.docx"},
                    },
                },
            )
            if r_vis and not r_vis.get("result", {}).get("isError", True):
                blocks = r_vis.get("result", {}).get("content") or []
                if any(isinstance(b, dict) and b.get("type") == "image" and len(base64.b64decode(b.get("data", ""))) > 1000 for b in blocks):
                    bonus_checks["visual_mcp"] = True

            # Test P2 near-duplicate folding across .docx and exported .md
            ctx_dup = svc.tool_context({"question": "受电弓羊角异常检测算法总体说明"})
            ev_srcs = [e.get("source") for e in (ctx_dup.get("primary_hits") or ())]
            if not ("sources/specs/pantograph-4c-spec.docx" in ev_srcs and "sources/specs/pantograph-4c-spec-exported-copy.md" in ev_srcs):
                if ev_srcs:
                    bonus_checks["near_dedup"] = True
        finally:
            svc.close()

        # Test P5 synonym miner if available
        try:
            from wiki_context.synonym_miner import mine_synonyms  # type: ignore
            mined = mine_synonyms(vault, apply=True)
            flat_str = json.dumps(mined, ensure_ascii=False)
            if "MessengerWire" in flat_str and "C_CLS" in flat_str:
                bonus_checks["synonym_miner"] = True
        except ImportError:
            pass

        # Add a spec that ONLY uses canonical "承力索" (without "C_CLS") to prove mined synonym expansion!
        (s_root / "specs" / "catenary-tension-rule.md").write_text(
            "# 接触网张力维护细则\n\n承力索标准额定工作张力必须保持在 15kN，断股超过 3 根必须立即更换。\n",
            encoding="utf-8",
        )
        sync_vault(vault)

        gold_suite = {
            "vault": "HardTech R&D Benchmark Vault",
            "questions": [
                {
                    "id": "H1_XLSX_MERGE_BANNER",
                    "question": "What are the Subsystem_Category, Wear_Limit_Thickness, and Rated_Voltage for CarbonStrip-Type-A?",
                    "answerable": True,
                    "expected_sources": ["sources/specs/bom-and-glossary.xlsx"],
                    "strict_groups": [["PantographSubsystem"], ["Wear_Limit_Thickness"], ["7.5mm"], ["1500VDC"]],
                },
                {
                    "id": "H2_XLSX_FLOWCHART_DRAWING",
                    "question": "What are the steps and IoU audit gate in AutoLabelPipeline?",
                    "answerable": True,
                    "expected_sources": ["sources/specs/bom-and-glossary.xlsx"],
                    "strict_groups": [["SegGPT"], ["0.88"]],
                },
                {
                    "id": "H3_DOCX_SDT_TXBX_FOOTNOTE",
                    "question": "线阵相机行频、FPGA边缘触发延迟与低温工况温度分别是多少？",
                    "answerable": True,
                    "expected_sources": ["sources/specs/pantograph-4c-spec.docx", "sources/specs/pantograph-4c-spec-exported-copy.md"],
                    "strict_groups": [["42kHz"], ["1.2ms"], ["-35C"]],
                },
                {
                    "id": "H4_PPTX_SMARTART_NOTES",
                    "question": "In 6C Inspection Architecture, what calibration is in SmartArt and what is the Jetson Orin NX power budget?",
                    "answerable": True,
                    "expected_sources": ["sources/specs/6c-roadmap.pptx"],
                    "strict_groups": [["INT8"], ["25W"]],
                },
                {
                    "id": "H5_HTML_IPYNB_VTT",
                    "question": "Compare VOx Microbolometer NETD sensitivity, Laser Triangulation RMS error, and BGP Anycast switchover SLA.",
                    "answerable": True,
                    "strict_groups": [["35mK"], ["0.042mm"], ["18 seconds"]],
                },
                {
                    "id": "H6_CODE_PUNCTUATION_VARIANTS",
                    "question": "Lookup registration SNK908X",
                    "answerable": True,
                    "strict_groups": [["SN-K908X"], ["RFC-042"]],
                },
                {
                    "id": "H7_FOLDER_PATH_SCOPED",
                    "question": "What was the Total Paid amount on the 2026-09 shanghai trip receipt?",
                    "answerable": True,
                    "expected_sources": ["sources/expenses/2026-09-shanghai-trip/receipt-item.md"],
                    "strict_groups": [["1,280.00"], ["INV-9921"]],
                },
                {
                    "id": "H8_GLOSSARY_SYNONYM_RECALL",
                    "question": "What is the rated kN limit for C_CLS?",
                    "answerable": True,
                    "expected_sources": ["sources/specs/catenary-tension-rule.md"],
                    "strict_groups": [["15kN"], ["承力索"]],
                },
                {
                    "id": "H9_UNANSWERABLE_GUARD",
                    "question": "What is the HyperLoopVacuumPump99 maintenance interval?",
                    "answerable": False,
                    "missing_expect": ["HyperLoopVacuumPump99"],
                },
            ],
        }

        report = evaluate_vault(vault, questions_data=gold_suite)
        qa_part = report["composite_score"] * 0.72  # 72 pts from QA recall + MRR + guardrail
        cap_pts = sum(7.0 if ok else 0.0 for ok in bonus_checks.values())  # 28 pts from 4 v2 architectural gates
        total_score = round(qa_part + cap_pts, 2)

        for r in report["rows"]:
            status = (
                ("PASS" if r["context_ok"] else "MISS")
                if r["answerable"]
                else ("GUARD_OK" if r["guard_ok"] else "GUARD_MISS")
            )
            print(f"{r['id']:<28} {status:<10} {r['elapsed_ms']:>6.2f}ms  {r['question'][:52]}")
        print("-" * 92)
        print(f"QA Strict Recall: {report['context_pack_hits']}/{report['answerable_total']} ({report['strict_recall']:.1%}) | "
              f"MRR: {report['mrr']:.3f} | Guardrail: {report['guardrail_accuracy']:.1%} | "
              f"Capabilities: {bonus_checks}")
        print(f"HARD_COMPOSITE_SCORE={total_score}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
