"""Zero-dependency native document extractor for DOCX, PPTX, XLSX, PDF, CSV, and text.

Extracts structured Markdown, table rows, slide notes, cell coordinates, and
page boundaries without calling external LLMs or requiring third-party Python packages.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import posixpath
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Sequence

MAX_ARCHIVE_MEMBER_BYTES = 64 * 1024 * 1024  # 64 MiB zip-bomb guard


@dataclass
class ExtractedDocument:
    method: str
    markdown: str
    pages: list[dict]
    page_count: int
    warnings: list[str] = field(default_factory=list)
    visual_pages: list[int] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    text_char_count: int = 0
    usable: bool = False

    def as_sidecar(self) -> dict:
        return {
            "method": self.method,
            "pages": self.pages,
            "page_count": self.page_count,
            "warnings": list(self.warnings),
            "visual_pages": list(self.visual_pages),
            "metadata": dict(self.metadata),
            "text_char_count": self.text_char_count,
            "usable": self.usable,
        }


def supports_native(path: Path | str) -> bool:
    ext = Path(path).suffix.lower()
    return ext in {".docx", ".pptx", ".xlsx", ".pdf", ".md", ".markdown", ".txt", ".rst", ".csv", ".tsv"}


def extract_document(path: Path | str) -> ExtractedDocument:
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".docx":
        return _extract_docx(p)
    if ext == ".pptx":
        return _extract_pptx(p)
    if ext == ".xlsx":
        return _extract_xlsx(p)
    if ext == ".pdf":
        return _extract_pdf(p)
    if ext in {".csv", ".tsv"}:
        return _extract_delimited(p, delimiter="\t" if ext == ".tsv" else ",")
    if ext in {".md", ".markdown", ".txt", ".rst"}:
        return _extract_plain_text(p)
    return ExtractedDocument(
        method="unsupported",
        markdown="",
        pages=[],
        page_count=0,
        warnings=[f"Unsupported format: {ext}"],
        usable=False,
    )


def _clean_text(val: str) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", val or "").replace("\r\n", "\n").replace("\r", "\n")


def _effective_chars(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _read_zip_member(zf: zipfile.ZipFile, name: str) -> bytes:
    info = zf.getinfo(name)
    if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
        raise ValueError(f"Archive member {name} exceeds {MAX_ARCHIVE_MEMBER_BYTES} bytes")
    return zf.read(name)


def _escape_cell(text: str) -> str:
    return re.sub(r"\s*\n\s*", " ", _clean_text(text)).replace("|", "\\|").strip()


def _markdown_table(rows: Sequence[Sequence[str]]) -> str:
    if not rows:
        return ""
    width = max((len(r) for r in rows), default=0)
    if width == 0:
        return ""
    norm = [[_escape_cell(r[i]) if i < len(r) else "" for i in range(width)] for r in rows]
    if not any(any(cell for cell in r) for r in norm):
        return ""
    header = "| " + " | ".join(norm[0]) + " |"
    sep = "| " + " | ".join("---" for _ in range(width)) + " |"
    body = ["| " + " | ".join(r) + " |" for r in norm[1:]]
    return "\n".join([header, sep, *body])


# -- Plain text & CSV/TSV --------------------------------------------------- #


def _extract_plain_text(path: Path) -> ExtractedDocument:
    raw = _clean_text(path.read_text(encoding="utf-8", errors="replace"))
    chars = _effective_chars(raw)
    return ExtractedDocument(
        method="plain-text-copy",
        markdown=raw.strip() + "\n" if raw.strip() else "",
        pages=[{"page_num": 0, "page_label": "Document", "blocks": [{"type": "Text", "content": raw, "format": "text"}]}] if raw.strip() else [],
        page_count=1 if raw.strip() else 0,
        text_char_count=chars,
        usable=chars > 0,
    )


def _extract_delimited(path: Path, *, delimiter: str = ",") -> ExtractedDocument:
    raw = _clean_text(path.read_text(encoding="utf-8", errors="replace"))
    reader = csv.reader(io.StringIO(raw), delimiter=delimiter)
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    md = _markdown_table(rows)
    chars = _effective_chars(md)
    return ExtractedDocument(
        method="native-csv",
        markdown=(f"# {path.stem}\n\n{md}\n" if md else ""),
        pages=[{"page_num": 0, "page_label": path.stem, "blocks": [{"type": "Table", "content": md, "format": "markdown"}]}] if md else [],
        page_count=1 if md else 0,
        text_char_count=chars,
        usable=chars > 0,
    )


# -- DOCX ------------------------------------------------------------------- #


def _find_all_local(elem: ET.Element, local: str) -> Iterator[ET.Element]:
    for node in elem.iter():
        if _local_name(node.tag) == local:
            yield node


def _extract_docx(path: Path) -> ExtractedDocument:
    warnings: list[str] = []
    try:
        with zipfile.ZipFile(path, "r") as zf:
            if "word/document.xml" not in zf.namelist():
                return ExtractedDocument("native-docx", "", [], 0, ["Missing word/document.xml"], usable=False)
            style_levels: dict[str, int] = {}
            if "word/styles.xml" in zf.namelist():
                sroot = ET.fromstring(_read_zip_member(zf, "word/styles.xml"))
                for st in _find_all_local(sroot, "style"):
                    sid = next((v for k, v in st.attrib.items() if _local_name(k) == "styleId"), "")
                    name_el = next(_find_all_local(st, "name"), None)
                    sname = next((v for k, v in (name_el.attrib.items() if name_el is not None else ()) if _local_name(k) == "val"), "")
                    m = re.match(r"heading\s*([1-6])", sname, re.I) or re.match(r"标题\s*([1-6])", sname)
                    if sid and m:
                        style_levels[sid] = int(m.group(1))

            root = ET.fromstring(_read_zip_member(zf, "word/document.xml"))
            body = next((c for c in root if _local_name(c.tag) == "body"), root)

            md_parts: list[str] = []
            blocks: list[dict] = []
            has_images = False

            for child in body:
                lname = _local_name(child.tag)
                if lname == "p":
                    lvl = 0
                    ppr = next((c for c in child if _local_name(c.tag) == "pPr"), None)
                    if ppr is not None:
                        pstyle = next((c for c in ppr if _local_name(c.tag) == "pStyle"), None)
                        if pstyle is not None:
                            sval = next((v for k, v in pstyle.attrib.items() if _local_name(k) == "val"), "")
                            lvl = style_levels.get(sval, 0)
                            if not lvl and re.match(r"^[1-6]$", sval):
                                lvl = int(sval)
                    img_cnt = sum(1 for n in child.iter() if _local_name(n.tag) in {"blip", "imagedata"})
                    if img_cnt:
                        has_images = True
                    texts = [n.text for n in _find_all_local(child, "t") if n.text]
                    line = _clean_text("".join(texts)).strip()
                    if line:
                        if 1 <= lvl <= 6:
                            md_parts.append(f"{'#' * lvl} {line}")
                            blocks.append({"type": "Title", "content": line, "format": "text"})
                        else:
                            md_parts.append(line)
                            blocks.append({"type": "Text", "content": line, "format": "text"})
                elif lname == "tbl":
                    t_rows: list[list[str]] = []
                    for tr in (c for c in child if _local_name(c.tag) == "tr"):
                        row_cells: list[str] = []
                        for tc in (c for c in tr if _local_name(c.tag) == "tc"):
                            paras = []
                            for p in _find_all_local(tc, "p"):
                                pt = _clean_text("".join(n.text for n in _find_all_local(p, "t") if n.text)).strip()
                                if pt:
                                    paras.append(pt)
                            row_cells.append(" ".join(paras))
                        if any(row_cells):
                            t_rows.append(row_cells)
                    tbl_md = _markdown_table(t_rows)
                    if tbl_md:
                        md_parts.append(tbl_md)
                        blocks.append({"type": "Table", "content": tbl_md, "format": "markdown"})

            markdown = "\n\n".join(md_parts).strip()
            if markdown:
                markdown += "\n"
            chars = _effective_chars(markdown)
            visual_pages = [1] if has_images else []
            if has_images:
                warnings.append("DOCX contains embedded images/diagrams not represented in plain text")
            return ExtractedDocument(
                method="native-docx",
                markdown=markdown,
                pages=[{"page_num": 0, "page_label": "Document", "blocks": blocks}] if blocks else [],
                page_count=1 if blocks else 0,
                warnings=warnings,
                visual_pages=visual_pages,
                text_char_count=chars,
                usable=chars > 0,
            )
    except (OSError, zipfile.BadZipFile, ET.ParseError, ValueError) as exc:
        return ExtractedDocument("native-docx", "", [], 0, [f"DOCX parse error: {exc}"], usable=False)


# -- PPTX ------------------------------------------------------------------- #


def _extract_pptx(path: Path) -> ExtractedDocument:
    warnings: list[str] = []
    try:
        with zipfile.ZipFile(path, "r") as zf:
            slide_files = sorted(
                (n for n in zf.namelist() if re.match(r"^ppt/slides/slide\d+\.xml$", n)),
                key=lambda s: int(re.search(r"(\d+)", s).group(1)),  # type: ignore[union-attr]
            )
            if "ppt/presentation.xml" in zf.namelist() and "ppt/_rels/presentation.xml.rels" in zf.namelist():
                rels_root = ET.fromstring(_read_zip_member(zf, "ppt/_rels/presentation.xml.rels"))
                rid_map = {
                    r.attrib.get("Id", ""): posixpath.normpath(posixpath.join("ppt", r.attrib.get("Target", "").lstrip("/")))
                    for r in _find_all_local(rels_root, "Relationship")
                }
                pres_root = ET.fromstring(_read_zip_member(zf, "ppt/presentation.xml"))
                ordered = []
                for sid in _find_all_local(pres_root, "sldId"):
                    rid = next((v for k, v in sid.attrib.items() if _local_name(k) == "id"), "")
                    tgt = rid_map.get(rid, "")
                    if tgt in zf.namelist():
                        ordered.append(tgt)
                if ordered:
                    slide_files = ordered

            pages: list[dict] = []
            md_sections: list[str] = []
            visual_pages: list[int] = []

            for idx, sfile in enumerate(slide_files):
                sroot = ET.fromstring(_read_zip_member(zf, sfile))
                blocks: list[dict] = []
                slide_lines: list[str] = []
                title_text = ""
                has_pic = any(_local_name(n.tag) in {"pic", "blip"} for n in sroot.iter())
                if has_pic:
                    visual_pages.append(idx + 1)

                for sp in (n for n in sroot.iter() if _local_name(n.tag) in {"sp", "graphicFrame"}):
                    if _local_name(sp.tag) == "graphicFrame":
                        for tbl in _find_all_local(sp, "tbl"):
                            rows = []
                            for tr in (c for c in tbl if _local_name(c.tag) == "tr"):
                                cells = [
                                    _clean_text("".join(t.text for t in _find_all_local(tc, "t") if t.text)).strip()
                                    for tc in (c for c in tr if _local_name(c.tag) == "tc")
                                ]
                                if any(cells):
                                    rows.append(cells)
                            tmd = _markdown_table(rows)
                            if tmd:
                                slide_lines.append(tmd)
                                blocks.append({"type": "Table", "content": tmd, "format": "markdown"})
                    else:
                        is_title = any(
                            _local_name(ph.tag) == "ph" and ph.attrib.get("type", "") in {"title", "ctrTitle"}
                            for ph in sp.iter()
                        )
                        paras = []
                        for p in _find_all_local(sp, "p"):
                            pt = _clean_text("".join(t.text for t in _find_all_local(p, "t") if t.text)).strip()
                            if pt:
                                paras.append(pt)
                        body_txt = "\n".join(paras)
                        if body_txt:
                            if is_title and not title_text:
                                title_text = paras[0]
                            slide_lines.append(body_txt)
                            blocks.append({"type": "Title" if is_title else "Text", "content": body_txt, "format": "text"})

                heading = f"Slide {idx + 1}" + (f": {title_text}" if title_text else "")
                md_sections.append(f"## {heading}\n\n" + "\n\n".join(slide_lines))
                pages.append({"page_num": idx, "page_label": heading, "blocks": blocks})

            markdown = "\n\n".join(md_sections).strip()
            if markdown:
                markdown += "\n"
            chars = _effective_chars(markdown)
            if visual_pages:
                warnings.append(f"PPTX has {len(visual_pages)} slides with embedded figures")
            return ExtractedDocument(
                method="native-pptx",
                markdown=markdown,
                pages=pages,
                page_count=len(pages),
                warnings=warnings,
                visual_pages=visual_pages,
                text_char_count=chars,
                usable=chars > 0,
            )
    except (OSError, zipfile.BadZipFile, ET.ParseError, ValueError) as exc:
        return ExtractedDocument("native-pptx", "", [], 0, [f"PPTX parse error: {exc}"], usable=False)


# -- XLSX ------------------------------------------------------------------- #


def _col_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha()).upper()
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return max(0, idx - 1)


def _extract_xlsx(path: Path) -> ExtractedDocument:
    warnings: list[str] = []
    try:
        with zipfile.ZipFile(path, "r") as zf:
            shared: list[str] = []
            if "xl/sharedStrings.xml" in zf.namelist():
                ss_root = ET.fromstring(_read_zip_member(zf, "xl/sharedStrings.xml"))
                for si in (c for c in ss_root if _local_name(c.tag) == "si"):
                    shared.append(_clean_text("".join(t.text for t in _find_all_local(si, "t") if t.text)))

            date_style_indices: set[int] = set()
            if "xl/styles.xml" in zf.namelist():
                st_root = ET.fromstring(_read_zip_member(zf, "xl/styles.xml"))
                custom_fmts: dict[int, str] = {}
                for nf in _find_all_local(st_root, "numFmt"):
                    try:
                        custom_fmts[int(nf.attrib.get("numFmtId", "-1"))] = nf.attrib.get("formatCode", "")
                    except ValueError:
                        pass
                cell_xfs = next(_find_all_local(st_root, "cellXfs"), None)
                if cell_xfs is not None:
                    for s_idx, xf in enumerate(c for c in cell_xfs if _local_name(c.tag) == "xf"):
                        try:
                            fmt_id = int(xf.attrib.get("numFmtId", "0"))
                        except ValueError:
                            fmt_id = 0
                        code = custom_fmts.get(fmt_id, "").lower()
                        if fmt_id in set(range(14, 23)) | {45, 46, 47} or (
                            any(tok in code for tok in ("yy", "mm", "dd")) and "#" not in code
                        ):
                            date_style_indices.add(s_idx)

            sheets: list[tuple[str, str]] = []
            if "xl/workbook.xml" in zf.namelist() and "xl/_rels/workbook.xml.rels" in zf.namelist():
                rels_root = ET.fromstring(_read_zip_member(zf, "xl/_rels/workbook.xml.rels"))
                rid_map = {
                    r.attrib.get("Id", ""): posixpath.normpath(posixpath.join("xl", r.attrib.get("Target", "").lstrip("/")))
                    for r in _find_all_local(rels_root, "Relationship")
                }
                wb_root = ET.fromstring(_read_zip_member(zf, "xl/workbook.xml"))
                for sh in _find_all_local(wb_root, "sheet"):
                    if sh.attrib.get("state") in {"hidden", "veryHidden"}:
                        continue
                    sname = sh.attrib.get("name", "Sheet")
                    rid = next((v for k, v in sh.attrib.items() if _local_name(k) == "id"), "")
                    tgt = rid_map.get(rid, "")
                    if tgt in zf.namelist():
                        sheets.append((sname, tgt))

            if not sheets:
                for n in sorted(zf.namelist()):
                    if re.match(r"^xl/worksheets/sheet\d+\.xml$", n):
                        sheets.append((Path(n).stem, n))

            pages: list[dict] = []
            md_sections: list[str] = []

            for p_idx, (sname, smember) in enumerate(sheets):
                sroot = ET.fromstring(_read_zip_member(zf, smember))
                rows_grid: list[list[str]] = []
                blocks: list[dict] = []

                for row_el in _find_all_local(sroot, "row"):
                    row_map: dict[int, str] = {}
                    for cell in (c for c in row_el if _local_name(c.tag) == "c"):
                        cref = cell.attrib.get("r", "A1")
                        ctype = cell.attrib.get("t", "")
                        s_idx_raw = cell.attrib.get("s", "")
                        f_el = next((c for c in cell if _local_name(c.tag) == "f"), None)
                        v_el = next((c for c in cell if _local_name(c.tag) == "v"), None)
                        is_el = next((c for c in cell if _local_name(c.tag) == "is"), None)
                        val = ""
                        if ctype == "s" and v_el is not None and v_el.text:
                            try:
                                val = shared[int(v_el.text)]
                            except (ValueError, IndexError):
                                val = v_el.text
                        elif ctype == "inlineStr" and is_el is not None:
                            val = _clean_text("".join(t.text for t in _find_all_local(is_el, "t") if t.text))
                        elif v_el is not None and v_el.text is not None:
                            raw_v = v_el.text.strip()
                            if ctype in {"", "n"} and s_idx_raw.isdigit() and int(s_idx_raw) in date_style_indices:
                                try:
                                    serial = float(raw_v)
                                    base = dt.datetime(1899, 12, 30) + dt.timedelta(days=serial)
                                    val = base.strftime("%Y-%m-%d")
                                except (ValueError, OverflowError):
                                    val = raw_v
                            else:
                                val = raw_v
                        if f_el is not None and f_el.text and not val:
                            val = f"={f_el.text}"
                        val = _clean_text(val).strip()
                        if val:
                            c_idx = _col_index(cref)
                            row_map[c_idx] = val
                            blocks.append({
                                "type": "Cell",
                                "content": f"{cref}: {val}",
                                "format": "formula" if f_el is not None else "text",
                            })
                    if row_map:
                        max_c = min(max(row_map) + 1, 64)
                        rows_grid.append([row_map.get(i, "") for i in range(max_c)])

                tbl_md = _markdown_table(rows_grid)
                if tbl_md:
                    md_sections.append(f"## Sheet: {sname}\n\n{tbl_md}")
                pages.append({"page_num": p_idx, "page_label": f"Sheet: {sname}", "blocks": blocks})

            markdown = "\n\n".join(md_sections).strip()
            if markdown:
                markdown += "\n"
            chars = _effective_chars(markdown)
            return ExtractedDocument(
                method="native-xlsx",
                markdown=markdown,
                pages=pages,
                page_count=len(pages),
                warnings=warnings,
                text_char_count=chars,
                usable=chars > 0,
            )
    except (OSError, zipfile.BadZipFile, ET.ParseError, ValueError) as exc:
        return ExtractedDocument("native-xlsx", "", [], 0, [f"XLSX parse error: {exc}"], usable=False)


# -- PDF (`pdftotext -layout`) ---------------------------------------------- #


def _extract_pdf(path: Path) -> ExtractedDocument:
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        return ExtractedDocument(
            method="native-pdf",
            markdown="",
            pages=[],
            page_count=0,
            warnings=["pdftotext binary not found in PATH; install poppler or configure an OCR provider"],
            usable=False,
        )
    try:
        proc = subprocess.run(
            [pdftotext, "-layout", "-enc", "UTF-8", str(path), "-"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ExtractedDocument("native-pdf", "", [], 0, [f"pdftotext failed: {exc}"], usable=False)

    warnings: list[str] = []
    if proc.returncode != 0:
        return ExtractedDocument(
            "native-pdf", "", [], 0, [f"pdftotext exited with {proc.returncode}: {proc.stderr.strip()}"], usable=False
        )
    if proc.stderr.strip():
        warnings.append(f"pdftotext stderr warning: {proc.stderr.strip()[:240]}")

    raw_pages = proc.stdout.split("\f")
    if raw_pages and not raw_pages[-1].strip():
        raw_pages.pop()

    pages: list[dict] = []
    md_sections: list[str] = []
    visual_pages: list[int] = []

    for idx, ptxt in enumerate(raw_pages):
        cleaned = _clean_text(ptxt).strip()
        eff = _effective_chars(cleaned)
        if eff < 20:
            visual_pages.append(idx + 1)
        label = f"Page {idx + 1}"
        md_sections.append(f"## {label}\n\n{cleaned}" if cleaned else f"## {label}\n\n*(visual/scanned page)*")
        pages.append({
            "page_num": idx,
            "page_label": label,
            "blocks": [{"type": "Text", "content": cleaned, "format": "text"}] if cleaned else [],
        })

    markdown = "\n\n".join(md_sections).strip()
    if markdown:
        markdown += "\n"
    total_chars = _effective_chars("\n".join(_clean_text(p) for p in raw_pages))
    usable = total_chars >= 40 and not proc.stderr.strip() and len(visual_pages) < max(1, len(raw_pages))
    if visual_pages:
        warnings.append(f"PDF contains {len(visual_pages)} sparse/visual page(s): {visual_pages[:10]}")

    return ExtractedDocument(
        method="native-pdf",
        markdown=markdown,
        pages=pages,
        page_count=len(pages),
        warnings=warnings,
        visual_pages=visual_pages,
        text_char_count=total_chars,
        usable=usable,
    )
