"""Zero-dependency native document extractor v2.

Supported formats (zero third-party Python packages):
- OOXML Office v2:
  - `.docx`: recursive `w:sdt` content controls, `w:txbxContent` diagram textboxes
    (deduping `mc:AlternateContent`), `gridSpan`/`vMerge` table spans, headers/footers,
    footnotes/endnotes, comments, and embedded media manifest (`word/media/*`).
  - `.pptx`: ordered slides, recursive `p:grpSp` group shapes, `ppt/diagrams/data*.xml`
    SmartArt diagrams, `ppt/notesSlides/` speaker notes, `ppt/charts/` titles/series,
    and embedded slide media (`ppt/media/*`).
  - `.xlsx`: `<mergeCells>` category/header propagation, single-cell banner promotion
    above true column headers, side-by-side sub-table splitting across empty column
    gutters, `xl/drawings/drawing*.xml` flowchart shape text, and embedded media.
- `.pdf`: `pdftotext -layout` with multi-column table reconstruction + sparse `visual_pages`.
- Rich & Web stdlib formats: `.html`/`.htm` (`html.parser`), `.eml` (`email`),
  `.ipynb` (`json`), `.srt`/`.vtt`/`.lrc` (timestamped blocks), `.rtf` + legacy
  `.doc`/`.xls`/`.ppt` (auto-detecting macOS `/usr/bin/textutil` or `soffice --headless`).
- Delimited & Plain text: `.csv`, `.tsv`, `.md`, `.markdown`, `.txt`, `.rst` with
  automatic UTF-8 / UTF-8-BOM / GB18030 / UTF-16 encoding detection.
"""

from __future__ import annotations

import csv
import datetime as dt
import email
import email.policy
import html.parser
import io
import json
import posixpath
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Sequence

MAX_ARCHIVE_MEMBER_BYTES = 64 * 1024 * 1024  # 64 MiB zip-bomb guard
MIN_EMBEDDED_IMAGE_BYTES = 4096             # Skip <4 KiB bullet icons & 1x1 spacers
MAX_EMBEDDED_ASSETS_PER_DOC = 24
SUPPORTED_MEDIA_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


@dataclass
class ExtractedDocument:
    method: str
    markdown: str
    pages: list[dict]
    page_count: int
    warnings: list[str] = field(default_factory=list)
    visual_pages: list[int] = field(default_factory=list)
    embedded_media: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    text_char_count: int = 0
    usable: bool = False

    def as_sidecar(self) -> dict:
        out = {
            "method": self.method,
            "pages": self.pages,
            "page_count": self.page_count,
            "warnings": list(self.warnings),
            "visual_pages": list(self.visual_pages),
            "metadata": dict(self.metadata),
            "text_char_count": self.text_char_count,
            "usable": self.usable,
        }
        if self.embedded_media:
            out["assets"] = list(self.embedded_media)
        return out


NATIVE_SUPPORTED_EXTS = frozenset({
    ".docx", ".pptx", ".xlsx", ".pdf",
    ".md", ".markdown", ".txt", ".rst", ".csv", ".tsv",
    ".html", ".htm", ".eml", ".ipynb", ".srt", ".vtt", ".lrc",
    ".rtf", ".doc", ".xls", ".ppt",
})


def supports_native(path: Path | str) -> bool:
    return Path(path).suffix.lower() in NATIVE_SUPPORTED_EXTS


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
    if ext in {".html", ".htm"}:
        return _extract_html_file(p)
    if ext == ".eml":
        return _extract_eml(p)
    if ext == ".ipynb":
        return _extract_ipynb(p)
    if ext in {".srt", ".vtt", ".lrc"}:
        return _extract_transcript(p)
    if ext in {".rtf", ".doc", ".xls", ".ppt"}:
        return _extract_legacy_or_rtf(p)
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


def _read_text_smart(path: Path) -> str:
    raw_bytes = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


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


def _collect_zip_media(zf: zipfile.ZipFile, prefix: str, *, min_bytes: int = MIN_EMBEDDED_IMAGE_BYTES) -> list[dict]:
    items: list[dict] = []
    for info in zf.infolist():
        if not info.filename.startswith(prefix) or info.is_dir():
            continue
        ext = Path(info.filename).suffix.lower()
        if ext not in SUPPORTED_MEDIA_EXTS:
            continue
        if info.file_size < min_bytes or info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
            continue
        items.append({
            "archive_member": info.filename,
            "name": Path(info.filename).name,
            "bytes": info.file_size,
            "mime": _mime_for_ext(ext),
        })
        if len(items) >= MAX_EMBEDDED_ASSETS_PER_DOC:
            break
    return items


def _mime_for_ext(ext: str) -> str:
    low = ext.lower()
    if low in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if low == ".webp":
        return "image/webp"
    if low == ".gif":
        return "image/gif"
    return "image/png"


def extract_archive_assets(source_path: Path, asset_dir: Path, assets_meta: Sequence[dict]) -> list[dict]:
    """Extract embedded OOXML images referenced in ``assets_meta`` to ``asset_dir``."""
    if not assets_meta or not source_path.is_file():
        return []
    saved: list[dict] = []
    try:
        with zipfile.ZipFile(source_path, "r") as zf:
            names = set(zf.namelist())
            asset_dir.mkdir(parents=True, exist_ok=True)
            for idx, item in enumerate(assets_meta, 1):
                member = str(item.get("archive_member") or "")
                if not member or member not in names:
                    continue
                data = _read_zip_member(zf, member)
                safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(member).name)
                out_name = f"fig-{idx:02d}-{safe_name}"
                out_file = asset_dir / out_name
                out_file.write_bytes(data)
                entry = dict(item)
                entry["asset_id"] = out_name
                entry["bytes"] = len(data)
                saved.append(entry)
    except (OSError, zipfile.BadZipFile, ValueError):
        return saved
    return saved


def _escape_cell(text: str) -> str:
    return re.sub(r"\s*\n\s*", " ", _clean_text(text)).replace("|", "\\|").strip()


def _markdown_table(rows: Sequence[Sequence[str]]) -> str:
    if not rows:
        return ""
    width = max((len(r) for r in rows), default=0)
    if width == 0:
        return ""
    norm = [[_escape_cell(r[i]) if i < len(r) else "" for i in range(width)] for r in rows]
    norm = [r for r in norm if any(c for c in r)]
    if not norm:
        return ""
    header = "| " + " | ".join(norm[0]) + " |"
    sep = "| " + " | ".join("---" for _ in range(width)) + " |"
    body = ["| " + " | ".join(r) + " |" for r in norm[1:]]
    return "\n".join([header, sep, *body])


def _find_all_local(elem: ET.Element, local: str) -> Iterator[ET.Element]:
    for node in elem.iter():
        if _local_name(node.tag) == local:
            yield node


# -- Plain text & CSV/TSV --------------------------------------------------- #


def _strip_inner_duplicate_frontmatter(raw: str) -> str:
    """Keep clipped Markdown clean while preserving body headings."""
    if not raw.startswith("---\n"):
        return raw
    end = raw.find("\n---\n", 4)
    if end == -1:
        return raw
    fm = raw[4:end]
    body = raw[end + 5:].lstrip("\n")
    meta_lines = []
    for ln in fm.splitlines():
        if ":" in ln:
            k, v = ln.split(":", 1)
            k_s = k.strip().lower()
            v_s = v.strip().strip('"').strip("'")
            if k_s in {"title", "source", "author", "published"} and v_s:
                meta_lines.append(f"- **{k.strip()}**: {v_s}")
    if meta_lines:
        return "\n".join(meta_lines) + "\n\n" + body
    return body


def _extract_plain_text(path: Path) -> ExtractedDocument:
    raw = _clean_text(_read_text_smart(path))
    if path.suffix.lower() in {".md", ".markdown"}:
        raw = _strip_inner_duplicate_frontmatter(raw)
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
    raw = _clean_text(_read_text_smart(path))
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


# -- HTML / EML / IPYNB / SRT-VTT / Legacy Office --------------------------- #


class _StdlibHTMLToMarkdown(html.parser.HTMLParser):
    _SKIP_TAGS = {"script", "style", "noscript", "svg", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.title = ""
        self._in_title = False
        self.out: list[str] = []
        self._in_table = False
        self._table_rows: list[list[str]] = []
        self._cur_row: list[str] | None = None
        self._cur_cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t == "title":
            self._in_title = True
            return
        if t in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if t in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            lvl = int(t[1])
            self.out.append(f"\n\n{'#' * lvl} ")
        elif t in {"p", "div", "section", "article", "blockquote", "pre"}:
            self.out.append("\n\n")
        elif t == "br":
            self.out.append("\n")
        elif t == "li":
            self.out.append("\n- ")
        elif t == "table":
            self._in_table = True
            self._table_rows = []
        elif t == "tr" and self._in_table:
            self._cur_row = []
        elif t in {"td", "th"} and self._in_table and self._cur_row is not None:
            self._cur_cell = []
        elif t == "img":
            alt = dict(attrs).get("alt") or ""
            if alt.strip():
                self.out.append(f" [Image: {alt.strip()}] ")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "title":
            self._in_title = False
            return
        if t in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if t in {"td", "th"} and self._in_table and self._cur_row is not None and self._cur_cell is not None:
            self._cur_row.append("".join(self._cur_cell).strip())
            self._cur_cell = None
        elif t == "tr" and self._in_table and self._cur_row is not None:
            if any(self._cur_row):
                self._table_rows.append(self._cur_row)
            self._cur_row = None
        elif t == "table" and self._in_table:
            tmd = _markdown_table(self._table_rows)
            if tmd:
                self.out.append(f"\n\n{tmd}\n\n")
            self._in_table = False
            self._table_rows = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip_depth:
            return
        if self._cur_cell is not None:
            self._cur_cell.append(data)
        else:
            self.out.append(data)

    def get_markdown(self) -> str:
        raw = "".join(self.out)
        raw = re.sub(r"[ \t]+\n", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw).strip()
        if self.title.strip() and not raw.startswith("# "):
            raw = f"# {self.title.strip()}\n\n{raw}".strip()
        return raw


def _html_to_md(html_str: str) -> str:
    parser = _StdlibHTMLToMarkdown()
    parser.feed(_clean_text(html_str))
    parser.close()
    return parser.get_markdown()


def _extract_html_file(path: Path) -> ExtractedDocument:
    md = _html_to_md(_read_text_smart(path))
    chars = _effective_chars(md)
    return ExtractedDocument(
        method="native-html",
        markdown=(md + "\n") if md else "",
        pages=[{"page_num": 0, "page_label": path.stem, "blocks": [{"type": "Text", "content": md, "format": "markdown"}]}] if md else [],
        page_count=1 if md else 0,
        text_char_count=chars,
        usable=chars > 0,
    )


def _extract_eml(path: Path) -> ExtractedDocument:
    msg = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
    subj = str(msg.get("subject") or path.stem).strip()
    sender = str(msg.get("from") or "").strip()
    recip = str(msg.get("to") or "").strip()
    date_s = str(msg.get("date") or "").strip()

    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            cdisp = part.get_content_disposition()
            fname = part.get_filename()
            if cdisp == "attachment" or fname:
                if fname:
                    attachments.append(fname)
                continue
            ctype = part.get_content_type()
            try:
                content = part.get_content()
            except Exception:
                continue
            if isinstance(content, str):
                if ctype == "text/plain":
                    plain_parts.append(_clean_text(content))
                elif ctype == "text/html":
                    html_parts.append(content)
    else:
        try:
            content = msg.get_content()
        except Exception:
            content = ""
        if isinstance(content, str):
            if msg.get_content_type() == "text/html":
                html_parts.append(content)
            else:
                plain_parts.append(_clean_text(content))

    body = "\n\n".join(p.strip() for p in plain_parts if p.strip())
    if not body and html_parts:
        body = "\n\n".join(_html_to_md(h) for h in html_parts if h.strip())

    hdr_lines = [f"# {subj}"]
    if sender:
        hdr_lines.append(f"- **From**: {sender}")
    if recip:
        hdr_lines.append(f"- **To**: {recip}")
    if date_s:
        hdr_lines.append(f"- **Date**: {date_s}")
    if attachments:
        hdr_lines.append(f"- **Attachments**: {', '.join(attachments)}")
    md = "\n".join(hdr_lines) + ("\n\n" + body if body else "") + "\n"
    chars = _effective_chars(md)
    return ExtractedDocument(
        method="native-eml",
        markdown=md,
        pages=[{"page_num": 0, "page_label": subj, "blocks": [{"type": "Text", "content": md, "format": "markdown"}]}],
        page_count=1,
        text_char_count=chars,
        usable=chars > 0,
    )


def _extract_ipynb(path: Path) -> ExtractedDocument:
    try:
        nb = json.loads(_read_text_smart(path))
    except ValueError as exc:
        return ExtractedDocument("native-ipynb", "", [], 0, [f"Invalid notebook JSON: {exc}"], usable=False)

    parts: list[str] = [f"# {path.stem}"]
    for idx, cell in enumerate(nb.get("cells") or (), 1):
        if not isinstance(cell, dict):
            continue
        ctype = cell.get("cell_type")
        src = cell.get("source") or ""
        if isinstance(src, list):
            src = "".join(str(x) for x in src)
        src = _clean_text(str(src)).strip()
        if not src:
            continue
        if ctype == "markdown":
            parts.append(src)
        elif ctype == "code":
            parts.append(f"```python\n{src}\n```")
            out_texts: list[str] = []
            for out_item in (cell.get("outputs") or ())[:4]:
                if not isinstance(out_item, dict):
                    continue
                txt_d = out_item.get("text") or (out_item.get("data") or {}).get("text/plain")
                if isinstance(txt_d, list):
                    txt_d = "".join(str(x) for x in txt_d)
                if isinstance(txt_d, str) and txt_d.strip():
                    out_texts.append(_clean_text(txt_d).strip()[:1200])
            if out_texts:
                parts.append("Output:\n```text\n" + "\n".join(out_texts) + "\n```")

    md = "\n\n".join(parts).strip() + "\n"
    chars = _effective_chars(md)
    return ExtractedDocument(
        method="native-ipynb",
        markdown=md if chars else "",
        pages=[{"page_num": 0, "page_label": path.stem, "blocks": [{"type": "Text", "content": md, "format": "markdown"}]}] if chars else [],
        page_count=1 if chars else 0,
        text_char_count=chars,
        usable=chars > 0,
    )


_CUE_TIME_RE = re.compile(
    r"((?:\d{1,2}:)?\d{2}:\d{2}(?:[.,]\d{1,3})?)\s*-->\s*((?:\d{1,2}:)?\d{2}:\d{2}(?:[.,]\d{1,3})?)"
)
_LRC_TIME_RE = re.compile(r"^\[(\d{1,2}:\d{2}(?:\.\d{1,3})?)\](.*)$")


def _extract_transcript(path: Path) -> ExtractedDocument:
    raw = _clean_text(_read_text_smart(path))
    lines = raw.splitlines()
    segments: list[tuple[str, str, list[str]]] = []

    if path.suffix.lower() == ".lrc":
        for ln in lines:
            m = _LRC_TIME_RE.match(ln.strip())
            if m and m.group(2).strip():
                segments.append((m.group(1), m.group(1), [m.group(2).strip()]))
    else:
        cur_start, cur_end = "", ""
        cur_buf: list[str] = []
        for ln in lines:
            s = ln.strip()
            if not s or s.upper() == "WEBVTT" or s.isdigit():
                continue
            m = _CUE_TIME_RE.search(s)
            if m:
                if cur_buf:
                    segments.append((cur_start, cur_end, cur_buf))
                    cur_buf = []
                cur_start = m.group(1).split(".")[0].split(",")[0]
                cur_end = m.group(2).split(".")[0].split(",")[0]
            else:
                clean_s = re.sub(r"<[^>]+>", "", s).strip()
                if clean_s:
                    cur_buf.append(clean_s)
        if cur_buf:
            segments.append((cur_start, cur_end, cur_buf))

    if not segments:
        return _extract_plain_text(path)

    # Group adjacent cues into ~8-cue blocks for clean timestamped citations
    md_blocks = [f"# {path.stem}"]
    chunk_size = 8
    for i in range(0, len(segments), chunk_size):
        grp = segments[i: i + chunk_size]
        t0 = grp[0][0] or "00:00"
        t1 = grp[-1][1] or t0
        body_lines = [" ".join(c[2]) for c in grp]
        md_blocks.append(f"## [{t0} - {t1}]\n\n" + "\n".join(body_lines))

    md = "\n\n".join(md_blocks).strip() + "\n"
    chars = _effective_chars(md)
    return ExtractedDocument(
        method="native-transcript",
        markdown=md,
        pages=[{"page_num": 0, "page_label": path.stem, "blocks": [{"type": "Text", "content": md, "format": "markdown"}]}],
        page_count=1,
        text_char_count=chars,
        usable=chars > 0,
    )


def _strip_rtf_basic(raw: str) -> str:
    # Minimal stdlib RTF fallback when neither textutil nor soffice is installed
    out = re.sub(r"\\par[d]?\b", "\n", raw)
    out = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes.fromhex(m.group(1)).decode("latin1", errors="ignore"), out)
    out = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) & 0xFFFF), out)
    out = re.sub(r"\{\\(?:fonttbl|colortbl|stylesheet|info|pict)[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", "", out)
    out = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", out)
    return out.replace("{", "").replace("}", "").strip()


def _extract_legacy_or_rtf(path: Path) -> ExtractedDocument:
    ext = path.suffix.lower()
    # 1) macOS `/usr/bin/textutil` handles `.doc` and `.rtf` cleanly out of the box
    textutil = shutil.which("textutil")
    if textutil and ext in {".doc", ".rtf"}:
        try:
            proc = subprocess.run(
                [textutil, "-convert", "txt", "-stdout", str(path)],
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
            if proc.returncode == 0 and _effective_chars(proc.stdout) >= 10:
                cleaned = _clean_text(proc.stdout).strip() + "\n"
                return ExtractedDocument(
                    method="os-textutil",
                    markdown=cleaned,
                    pages=[{"page_num": 0, "page_label": path.stem, "blocks": [{"type": "Text", "content": cleaned, "format": "text"}]}],
                    page_count=1,
                    text_char_count=_effective_chars(cleaned),
                    usable=True,
                )
        except (OSError, subprocess.TimeoutExpired):
            pass

    # 2) Headless LibreOffice (`soffice`) converts `.doc/.xls/.ppt/.rtf` to OOXML for full v2 extraction
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice:
        target_fmt = {".doc": "docx", ".rtf": "docx", ".xls": "xlsx", ".ppt": "pptx"}.get(ext, "txt")
        with tempfile.TemporaryDirectory(prefix="wctx-soffice-") as td:
            try:
                proc = subprocess.run(
                    [soffice, "--headless", "--convert-to", target_fmt, "--outdir", td, str(path)],
                    capture_output=True,
                    text=True,
                    timeout=90,
                    check=False,
                )
                converted = list(Path(td).glob(f"*.{target_fmt}"))
                if proc.returncode == 0 and converted:
                    sub_doc = extract_document(converted[0])
                    sub_doc.method = f"soffice-{target_fmt}"
                    return sub_doc
            except (OSError, subprocess.TimeoutExpired):
                pass

    # 3) Pure stdlib RTF fallback
    if ext == ".rtf":
        txt = _clean_text(_strip_rtf_basic(_read_text_smart(path)))
        chars = _effective_chars(txt)
        return ExtractedDocument(
            method="native-rtf",
            markdown=(txt + "\n") if chars else "",
            pages=[{"page_num": 0, "page_label": path.stem, "blocks": [{"type": "Text", "content": txt, "format": "text"}]}] if chars else [],
            page_count=1 if chars else 0,
            text_char_count=chars,
            usable=chars > 0,
        )

    return ExtractedDocument(
        method="ocr-required",
        markdown="",
        pages=[],
        page_count=0,
        warnings=[f"Legacy format {ext} requires LibreOffice (`soffice`), macOS `textutil`, or `WCTX_OCR_CMD`"],
        usable=False,
    )


# -- DOCX v2 ---------------------------------------------------------------- #


def _docx_run_texts_excluding_txbx(node: ET.Element) -> list[str]:
    """Collect paragraph `<w:t>` text while skipping nested `w:txbxContent`, `w:del`, and `mc:Fallback`."""
    pieces: list[str] = []

    def walk(el: ET.Element) -> None:
        ln = _local_name(el.tag)
        if ln in {"txbxContent", "del", "Fallback", "instrText"}:
            return
        if ln == "t" and el.text:
            pieces.append(el.text)
            return
        if ln == "tab":
            pieces.append("\t")
        elif ln == "br":
            pieces.append("\n")
        for ch in el:
            walk(ch)

    walk(node)
    return pieces


def _docx_extract_txbx_paragraphs(node: ET.Element) -> list[str]:
    """Extract unique textbox paragraphs inside a paragraph/drawing, skipping `mc:Fallback` duplicates."""
    results: list[str] = []
    seen: set[str] = set()

    def walk_for_txbx(el: ET.Element) -> None:
        ln = _local_name(el.tag)
        if ln == "Fallback":
            return
        if ln == "txbxContent":
            for p_el in (c for c in el.iter() if _local_name(c.tag) == "p"):
                txt = _clean_text("".join(_docx_run_texts_excluding_txbx(p_el))).strip()
                if txt and txt not in seen:
                    seen.add(txt)
                    results.append(txt)
            return
        for ch in el:
            walk_for_txbx(ch)

    walk_for_txbx(node)
    return results


def _docx_parse_table(tbl_el: ET.Element) -> str:
    """Parse `<w:tbl>` supporting horizontal `gridSpan` and vertical `vMerge` continuation."""
    raw_rows: list[list[tuple[str, bool]]] = []
    max_cols = 0

    for tr in (c for c in tbl_el if _local_name(c.tag) == "tr"):
        row_spec: list[tuple[str, bool]] = []
        for tc in (c for c in tr if _local_name(c.tag) == "tc"):
            span = 1
            is_vmerge_cont = False
            tc_pr = next((c for c in tc if _local_name(c.tag) == "tcPr"), None)
            if tc_pr is not None:
                gs = next((c for c in tc_pr if _local_name(c.tag) == "gridSpan"), None)
                if gs is not None:
                    val_s = next((v for k, v in gs.attrib.items() if _local_name(k) == "val"), "1")
                    if val_s.isdigit():
                        span = max(1, min(int(val_s), 32))
                vm = next((c for c in tc_pr if _local_name(c.tag) == "vMerge"), None)
                if vm is not None:
                    vm_val = next((v for k, v in vm.attrib.items() if _local_name(k) == "val"), "continue")
                    if vm_val.lower() != "restart":
                        is_vmerge_cont = True

            paras: list[str] = []
            for p in (c for c in tc.iter() if _local_name(c.tag) == "p"):
                pt = _clean_text("".join(_docx_run_texts_excluding_txbx(p))).strip()
                if pt:
                    paras.append(pt)
            cell_txt = " ".join(paras).strip()
            for s_i in range(span):
                row_spec.append((cell_txt if s_i == 0 else "", is_vmerge_cont and s_i == 0))
        if row_spec:
            max_cols = max(max_cols, len(row_spec))
            raw_rows.append(row_spec)

    if not raw_rows or max_cols == 0:
        return ""

    carry = [""] * max_cols
    resolved_rows: list[list[str]] = []
    for rspec in raw_rows:
        row_vals: list[str] = []
        for c_i in range(max_cols):
            if c_i < len(rspec):
                txt, is_cont = rspec[c_i]
                if is_cont and not txt:
                    val = carry[c_i]
                else:
                    val = txt
                    carry[c_i] = txt
                row_vals.append(val)
            else:
                row_vals.append("")
        if any(row_vals):
            resolved_rows.append(row_vals)
    return _markdown_table(resolved_rows)


def _extract_docx(path: Path) -> ExtractedDocument:
    warnings: list[str] = []
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = set(zf.namelist())
            if "word/document.xml" not in names:
                return ExtractedDocument("native-docx", "", [], 0, ["Missing word/document.xml"], usable=False)

            style_levels: dict[str, int] = {}
            if "word/styles.xml" in names:
                sroot = ET.fromstring(_read_zip_member(zf, "word/styles.xml"))
                for st in _find_all_local(sroot, "style"):
                    sid = next((v for k, v in st.attrib.items() if _local_name(k) == "styleId"), "")
                    name_el = next(_find_all_local(st, "name"), None)
                    sname = next((v for k, v in (name_el.attrib.items() if name_el is not None else ()) if _local_name(k) == "val"), "")
                    m = re.match(r"heading\s*([1-6])", sname, re.I) or re.match(r"标题\s*([1-6])", sname)
                    if sid and m:
                        style_levels[sid] = int(m.group(1))

            md_parts: list[str] = []
            blocks: list[dict] = []
            has_images = False

            # Optional headers (deduped, concise metadata banners)
            seen_hf: set[str] = set()
            for hf_name in sorted(n for n in names if re.match(r"^word/header\d*\.xml$", n)):
                hroot = ET.fromstring(_read_zip_member(zf, hf_name))
                h_lines = []
                for p in (c for c in hroot.iter() if _local_name(c.tag) == "p"):
                    pt = _clean_text("".join(_docx_run_texts_excluding_txbx(p))).strip()
                    if pt and pt not in seen_hf:
                        seen_hf.add(pt)
                        h_lines.append(pt)
                if h_lines:
                    hdr_str = " | ".join(h_lines)
                    md_parts.append(f"> Header: {hdr_str}")
                    blocks.append({"type": "Text", "content": hdr_str, "format": "text"})

            root = ET.fromstring(_read_zip_member(zf, "word/document.xml"))
            body = next((c for c in root if _local_name(c.tag) == "body"), root)

            def walk_blocks(container: ET.Element) -> None:
                nonlocal has_images
                for child in container:
                    lname = _local_name(child.tag)
                    if lname == "Fallback":
                        continue
                    if lname in {"sdt", "sdtContent", "customXml", "AlternateContent", "Choice"}:
                        walk_blocks(child)
                        continue
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
                        if any(_local_name(n.tag) in {"blip", "imagedata"} for n in child.iter()):
                            has_images = True

                        line = _clean_text("".join(_docx_run_texts_excluding_txbx(child))).strip()
                        if line:
                            if 1 <= lvl <= 6:
                                md_parts.append(f"{'#' * lvl} {line}")
                                blocks.append({"type": "Title", "content": line, "format": "text"})
                            else:
                                md_parts.append(line)
                                blocks.append({"type": "Text", "content": line, "format": "text"})

                        for txbx_line in _docx_extract_txbx_paragraphs(child):
                            md_parts.append(f"- [Diagram/Textbox] {txbx_line}")
                            blocks.append({"type": "Text", "content": txbx_line, "format": "text"})
                    elif lname == "tbl":
                        tbl_md = _docx_parse_table(child)
                        if tbl_md:
                            md_parts.append(tbl_md)
                            blocks.append({"type": "Table", "content": tbl_md, "format": "markdown"})

            walk_blocks(body)

            # Footnotes, endnotes & comments
            extra_notes: list[str] = []
            for fn_member in ("word/footnotes.xml", "word/endnotes.xml", "word/comments.xml"):
                if fn_member not in names:
                    continue
                froot = ET.fromstring(_read_zip_member(zf, fn_member))
                for item in froot:
                    fid = next((v for k, v in item.attrib.items() if _local_name(k) == "id"), "")
                    if fid in {"-1", "0"}:
                        continue
                    f_paras = []
                    for p in (c for c in item.iter() if _local_name(c.tag) == "p"):
                        pt = _clean_text("".join(_docx_run_texts_excluding_txbx(p))).strip()
                        if pt:
                            f_paras.append(pt)
                    if f_paras:
                        extra_notes.append(" ".join(f_paras))
            if extra_notes:
                notes_md = "## Footnotes & Annotations\n\n" + "\n".join(f"- {n}" for n in extra_notes)
                md_parts.append(notes_md)
                blocks.append({"type": "Text", "content": notes_md, "format": "markdown"})

            media_items = _collect_zip_media(zf, "word/media/")
            if media_items:
                has_images = True

            markdown = "\n\n".join(md_parts).strip()
            if markdown:
                markdown += "\n"
            chars = _effective_chars(markdown)
            visual_pages = [1] if has_images else []
            if has_images:
                warnings.append(f"DOCX contains embedded images/diagrams ({len(media_items)} extractable asset(s))")
            return ExtractedDocument(
                method="native-docx",
                markdown=markdown,
                pages=[{"page_num": 0, "page_label": "Document", "blocks": blocks}] if blocks else [],
                page_count=1 if blocks else 0,
                warnings=warnings,
                visual_pages=visual_pages,
                embedded_media=media_items,
                text_char_count=chars,
                usable=chars > 0 or bool(media_items),
            )
    except (OSError, zipfile.BadZipFile, ET.ParseError, ValueError) as exc:
        return ExtractedDocument("native-docx", "", [], 0, [f"DOCX parse error: {exc}"], usable=False)


# -- PPTX v2 ---------------------------------------------------------------- #


def _extract_pptx_xml_paragraphs(root_el: ET.Element) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for p in _find_all_local(root_el, "p"):
        pt = _clean_text("".join(t.text for t in _find_all_local(p, "t") if t.text)).strip()
        if pt and pt not in seen:
            seen.add(pt)
            out.append(pt)
    return out


def _extract_pptx(path: Path) -> ExtractedDocument:
    warnings: list[str] = []
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = set(zf.namelist())
            slide_files = sorted(
                (n for n in names if re.match(r"^ppt/slides/slide\d+\.xml$", n)),
                key=lambda s: int(re.search(r"(\d+)", s).group(1)),  # type: ignore[union-attr]
            )
            if "ppt/presentation.xml" in names and "ppt/_rels/presentation.xml.rels" in names:
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
                    if tgt in names:
                        ordered.append(tgt)
                if ordered:
                    slide_files = ordered

            pages: list[dict] = []
            md_sections: list[str] = []
            visual_pages: list[int] = []
            used_diagrams: set[str] = set()
            used_notes: set[str] = set()

            for idx, sfile in enumerate(slide_files):
                sroot = ET.fromstring(_read_zip_member(zf, sfile))
                blocks: list[dict] = []
                slide_lines: list[str] = []
                title_text = ""
                has_pic = any(_local_name(n.tag) in {"pic", "blip"} for n in sroot.iter())
                if has_pic:
                    visual_pages.append(idx + 1)

                def walk_shapes(container: ET.Element) -> None:
                    nonlocal title_text
                    for sp in container:
                        ln = _local_name(sp.tag)
                        if ln in {"cSld", "spTree", "grpSp"}:
                            walk_shapes(sp)
                        elif ln == "graphicFrame":
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
                        elif ln in {"sp", "cxnSp"}:
                            is_title = any(
                                _local_name(ph.tag) == "ph" and ph.attrib.get("type", "") in {"title", "ctrTitle"}
                                for ph in sp.iter()
                            )
                            paras = _extract_pptx_xml_paragraphs(sp)
                            body_txt = "\n".join(paras)
                            if body_txt:
                                if is_title and not title_text:
                                    title_text = paras[0]
                                slide_lines.append(body_txt)
                                blocks.append({"type": "Title" if is_title else "Text", "content": body_txt, "format": "text"})

                walk_shapes(sroot)

                # Discover linked SmartArt diagrams, Charts, and Speaker Notes via slide `.rels` + numeric fallback
                s_num_m = re.search(r"slide(\d+)\.xml$", sfile)
                s_num = s_num_m.group(1) if s_num_m else str(idx + 1)
                rels_file = f"ppt/slides/_rels/{Path(sfile).name}.rels"
                linked_targets: list[str] = []
                if rels_file in names:
                    sr_root = ET.fromstring(_read_zip_member(zf, rels_file))
                    for rel_el in _find_all_local(sr_root, "Relationship"):
                        tgt = posixpath.normpath(posixpath.join("ppt/slides", rel_el.attrib.get("Target", "")))
                        if tgt in names:
                            linked_targets.append(tgt)

                fallback_diag = f"ppt/diagrams/data{s_num}.xml"
                if fallback_diag in names and fallback_diag not in linked_targets:
                    linked_targets.append(fallback_diag)
                fallback_note = f"ppt/notesSlides/notesSlide{s_num}.xml"
                if fallback_note in names and fallback_note not in linked_targets:
                    linked_targets.append(fallback_note)

                for tgt in linked_targets:
                    if tgt.startswith("ppt/diagrams/data") and tgt.endswith(".xml") and tgt not in used_diagrams:
                        used_diagrams.add(tgt)
                        d_paras = _extract_pptx_xml_paragraphs(ET.fromstring(_read_zip_member(zf, tgt)))
                        if d_paras:
                            d_txt = "### SmartArt / Diagram\n" + "\n".join(f"- {x}" for x in d_paras)
                            slide_lines.append(d_txt)
                            blocks.append({"type": "Text", "content": d_txt, "format": "markdown"})
                    elif tgt.startswith("ppt/charts/chart") and tgt.endswith(".xml"):
                        c_root = ET.fromstring(_read_zip_member(zf, tgt))
                        c_vals = [
                            _clean_text(v.text).strip()
                            for v in c_root.iter()
                            if _local_name(v.tag) in {"t", "v"} and v.text and not v.text.strip().replace(".", "", 1).isdigit()
                        ]
                        c_uniq = list(dict.fromkeys(x for x in c_vals if x))
                        if c_uniq:
                            c_txt = "### Chart Labels\n- " + " | ".join(c_uniq[:20])
                            slide_lines.append(c_txt)
                            blocks.append({"type": "Text", "content": c_txt, "format": "markdown"})
                    elif tgt.startswith("ppt/notesSlides/notesSlide") and tgt.endswith(".xml") and tgt not in used_notes:
                        used_notes.add(tgt)
                        n_paras = [
                            x for x in _extract_pptx_xml_paragraphs(ET.fromstring(_read_zip_member(zf, tgt)))
                            if not x.isdigit()
                        ]
                        if n_paras:
                            n_txt = "### Speaker Notes\n" + "\n".join(n_paras)
                            slide_lines.append(n_txt)
                            blocks.append({"type": "Text", "content": n_txt, "format": "markdown"})

                heading = f"Slide {idx + 1}" + (f": {title_text}" if title_text else "")
                md_sections.append(f"## {heading}\n\n" + "\n\n".join(slide_lines))
                pages.append({"page_num": idx, "page_label": heading, "blocks": blocks})

            media_items = _collect_zip_media(zf, "ppt/media/")
            markdown = "\n\n".join(md_sections).strip()
            if markdown:
                markdown += "\n"
            chars = _effective_chars(markdown)
            if visual_pages or media_items:
                warnings.append(f"PPTX has {len(visual_pages)} visual slide(s) and {len(media_items)} extractable media asset(s)")
            return ExtractedDocument(
                method="native-pptx",
                markdown=markdown,
                pages=pages,
                page_count=len(pages),
                warnings=warnings,
                visual_pages=visual_pages,
                embedded_media=media_items,
                text_char_count=chars,
                usable=chars > 0 or bool(media_items),
            )
    except (OSError, zipfile.BadZipFile, ET.ParseError, ValueError) as exc:
        return ExtractedDocument("native-pptx", "", [], 0, [f"PPTX parse error: {exc}"], usable=False)


# -- XLSX v2 ---------------------------------------------------------------- #


def _col_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha()).upper()
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return max(0, idx - 1)


def _parse_cell_coord(ref: str) -> tuple[int, int]:
    """Return `(row_1based, col_0based)` for an A1-style coordinate."""
    m = re.match(r"^([A-Za-z]+)(\d+)$", ref.strip())
    if not m:
        return (1, 0)
    return (int(m.group(2)), _col_index(m.group(1)))


def _format_xlsx_subtables(grid: list[list[str]]) -> list[str]:
    """Split side-by-side tables on empty column gutters and promote 1-cell title banners."""
    if not grid:
        return []
    width = max((len(r) for r in grid), default=0)
    if width == 0:
        return []
    norm = [[r[c].strip() if c < len(r) else "" for c in range(width)] for r in grid]
    norm = [r for r in norm if any(r)]
    if not norm:
        return []

    # Check if row 0 is a single-cell overall sheet title before column-gutter detection
    global_banner = ""
    non_empty_r0 = [x for x in norm[0] if x]
    if len(norm) >= 2 and len(non_empty_r0) == 1 and sum(1 for x in norm[1] if x) >= 2:
        global_banner = non_empty_r0[0]
        norm = norm[1:]

    # Detect completely empty column gutters across remaining rows
    col_has_data = [any(norm[r][c] for r in range(len(norm))) for c in range(width)]
    spans: list[tuple[int, int]] = []
    c = 0
    while c < width:
        if not col_has_data[c]:
            c += 1
            continue
        start_c = c
        while c < width and col_has_data[c]:
            c += 1
        spans.append((start_c, c))

    parts: list[str] = []
    if global_banner:
        parts.append(f"### {global_banner}")

    for s_c, e_c in spans:
        sub = [[row[i] for i in range(s_c, e_c)] for row in norm]
        sub = [r for r in sub if any(r)]
        if not sub:
            continue
        sub_banner = ""
        r0_vals = [x for x in sub[0] if x]
        if len(sub) >= 2 and len(r0_vals) == 1 and sum(1 for x in sub[1] if x) >= 2:
            sub_banner = r0_vals[0]
            sub = sub[1:]
        tmd = _markdown_table(sub)
        if tmd:
            parts.append(f"### {sub_banner}\n\n{tmd}" if sub_banner else tmd)
    return parts


def _extract_xlsx(path: Path) -> ExtractedDocument:
    warnings: list[str] = []
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = set(zf.namelist())
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                ss_root = ET.fromstring(_read_zip_member(zf, "xl/sharedStrings.xml"))
                for si in (c for c in ss_root if _local_name(c.tag) == "si"):
                    shared.append(_clean_text("".join(t.text for t in _find_all_local(si, "t") if t.text)))

            date_style_indices: set[int] = set()
            if "xl/styles.xml" in names:
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
            if "xl/workbook.xml" in names and "xl/_rels/workbook.xml.rels" in names:
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
                    if tgt in names:
                        sheets.append((sname, tgt))

            if not sheets:
                for n in sorted(names):
                    if re.match(r"^xl/worksheets/sheet\d+\.xml$", n):
                        sheets.append((Path(n).stem, n))

            pages: list[dict] = []
            md_sections: list[str] = []

            for p_idx, (sname, smember) in enumerate(sheets):
                sroot = ET.fromstring(_read_zip_member(zf, smember))
                cell_matrix: dict[int, dict[int, str]] = {}
                blocks: list[dict] = []
                max_row_seen = 0
                max_col_seen = 0

                for r_seq, row_el in enumerate(_find_all_local(sroot, "row"), 1):
                    r_attr = row_el.attrib.get("r", "")
                    r_num = int(r_attr) if r_attr.isdigit() else r_seq
                    row_map = cell_matrix.setdefault(r_num, {})
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
                            c_idx = min(_col_index(cref), 63)
                            row_map[c_idx] = val
                            max_row_seen = max(max_row_seen, r_num)
                            max_col_seen = max(max_col_seen, c_idx)
                            blocks.append({
                                "type": "Cell",
                                "content": f"{cref}: {val}",
                                "format": "formula" if f_el is not None else "text",
                            })

                # Expand `<mergeCells>` (vertical category merges & grouped subheaders; keep full-width single banners clean)
                for mc in _find_all_local(sroot, "mergeCell"):
                    ref_range = mc.attrib.get("ref", "")
                    if ":" not in ref_range:
                        continue
                    p1, p2 = ref_range.split(":", 1)
                    r1, c1 = _parse_cell_coord(p1)
                    r2, c2 = _parse_cell_coord(p2)
                    c2 = min(c2, 63)
                    r2 = min(r2, max(max_row_seen, r1))
                    origin_val = (cell_matrix.get(r1) or {}).get(c1, "")
                    if not origin_val:
                        continue
                    # If single-row horizontal banner where no other cells exist on row r1, keep as 1-cell banner!
                    if r1 == r2 and len([v for v in (cell_matrix.get(r1) or {}).values() if v]) == 1:
                        continue
                    for rr in range(r1, r2 + 1):
                        rmap = cell_matrix.setdefault(rr, {})
                        for cc in range(c1, c2 + 1):
                            if not rmap.get(cc):
                                rmap[cc] = origin_val

                rows_grid: list[list[str]] = []
                for rr in sorted(cell_matrix):
                    rmap = cell_matrix[rr]
                    if rmap:
                        width = min(max(max_col_seen, max(rmap)) + 1, 64)
                        rows_grid.append([rmap.get(i, "") for i in range(width)])

                sheet_parts = _format_xlsx_subtables(rows_grid)

                # Extract DrawingML shapes / flowcharts linked to this worksheet (`xl/drawings/drawing*.xml`)
                sh_rels = posixpath.join(posixpath.dirname(smember), "_rels", f"{Path(smember).name}.rels")
                drawing_targets: list[str] = []
                if sh_rels in names:
                    sr_root = ET.fromstring(_read_zip_member(zf, sh_rels))
                    for rel_el in _find_all_local(sr_root, "Relationship"):
                        tgt = posixpath.normpath(posixpath.join(posixpath.dirname(smember), rel_el.attrib.get("Target", "")))
                        if tgt in names and "drawings/" in tgt and tgt.endswith(".xml"):
                            drawing_targets.append(tgt)

                shape_lines: list[str] = []
                seen_sh: set[str] = set()
                for dtgt in drawing_targets:
                    droot = ET.fromstring(_read_zip_member(zf, dtgt))
                    for p in _find_all_local(droot, "p"):
                        pt = _clean_text("".join(t.text for t in _find_all_local(p, "t") if t.text)).strip()
                        if pt and pt not in seen_sh:
                            seen_sh.add(pt)
                            shape_lines.append(pt)
                if shape_lines:
                    drw_md = "### Flowchart & Diagram Shapes\n\n" + "\n".join(f"- {s}" for s in shape_lines)
                    sheet_parts.append(drw_md)
                    blocks.append({"type": "Text", "content": drw_md, "format": "markdown"})

                if sheet_parts:
                    md_sections.append(f"## Sheet: {sname}\n\n" + "\n\n".join(sheet_parts))
                else:
                    warnings.append(f"Sheet '{sname}' contains no extractable cells or shape text")
                pages.append({"page_num": p_idx, "page_label": f"Sheet: {sname}", "blocks": blocks})

            media_items = _collect_zip_media(zf, "xl/media/")
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
                embedded_media=media_items,
                text_char_count=chars,
                usable=chars > 0 or bool(media_items),
            )
    except (OSError, zipfile.BadZipFile, ET.ParseError, ValueError) as exc:
        return ExtractedDocument("native-xlsx", "", [], 0, [f"XLSX parse error: {exc}"], usable=False)


# -- PDF (`pdftotext -layout` + Aligned Table Reconstruction) --------------- #

_SQL_OR_CODE_LINE_RE = re.compile(
    r"^\s*(?:\d+\s+(?:--|CREATE|DROP|SELECT|INSERT|ALTER|def |class |import |return )|CREATE\s+TABLE|DROP\s+TABLE|\{|\}|//)",
    re.I,
)


def _reconstruct_pdf_layout_tables(page_text: str) -> str:
    """Convert contiguous multi-column `pdftotext -layout` blocks into pipe Markdown tables."""
    lines = page_text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        stripped = ln.strip()
        if not stripped or stripped.startswith("|") or _SQL_OR_CODE_LINE_RE.match(ln):
            out.append(ln)
            i += 1
            continue
        cols = [c.strip() for c in re.split(r"\s{2,}", stripped) if c.strip()]
        if 2 <= len(cols) <= 12:
            # Look ahead for at least 2 more consecutive compatible multi-column lines
            run_rows = [cols]
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                nxt_s = nxt.strip()
                if not nxt_s or _SQL_OR_CODE_LINE_RE.match(nxt):
                    break
                ncols = [c.strip() for c in re.split(r"\s{2,}", nxt_s) if c.strip()]
                if len(ncols) == len(cols) or (len(cols) >= 3 and abs(len(ncols) - len(cols)) <= 1 and len(ncols) >= 2):
                    run_rows.append(ncols)
                    j += 1
                else:
                    break
            if len(run_rows) >= 3:
                out.append(_markdown_table(run_rows))
                i = j
                continue
        out.append(ln)
        i += 1
    return "\n".join(out)


def _extract_pdf(path: Path) -> ExtractedDocument:
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        return ExtractedDocument(
            method="native-pdf",
            markdown="",
            pages=[],
            page_count=0,
            warnings=["pdftotext binary not found in PATH; install poppler or configure an OCR/VLM provider"],
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
    while raw_pages and not raw_pages[-1].strip():
        raw_pages.pop()

    pages: list[dict] = []
    md_sections: list[str] = []
    visual_pages: list[int] = []

    for idx, ptxt in enumerate(raw_pages):
        cleaned = _clean_text(ptxt).strip()
        eff = _effective_chars(cleaned)
        if eff < 25:
            visual_pages.append(idx + 1)
        formatted = _reconstruct_pdf_layout_tables(cleaned) if cleaned else ""
        label = f"Page {idx + 1}"
        md_sections.append(f"## {label}\n\n{formatted}" if formatted else f"## {label}\n\n*(visual/scanned page)*")
        pages.append({
            "page_num": idx,
            "page_label": label,
            "blocks": [{"type": "Text", "content": formatted, "format": "markdown"}] if formatted else [],
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
