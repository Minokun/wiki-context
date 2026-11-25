"""Citation line-range exactness: every ``L<a>-L<b>`` label on a ``[S]``/``[W]`` block must be exactly the
file lines the block shows.

Regression for the P-06 enumeration-tail append (v0.5.9/v0.5.10): the tail was spliced in after the indexed
``line_end`` instead of right after the chunk text, dropping the blank separator line(s) that a legacy index
keeps inside ``line_end``. The pack then showed one line more than its label claimed (end line off by one).
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wiki_context import indexer
from wiki_context.context import ContextPackEngine, build_query_plan
from wiki_context.mcp_server import KBService
from wiki_context.scaffold import init_vault
from wiki_context.sync import sync_vault

# Verbatim lines L313..L342 of the production mirror that mis-cited in the 46-question benchmark (case D05-1,
# S1): a `6C系统概述` section followed, across one blank line, by the sibling sub-heading `## 1C - …`.
COURSE_WINDOW: list[str] = [
    "", "## 接触网6C检测系统", "", "## 6C系统概述", "", "- 系统简称", "",
    "- 接触网6C系统是“高速铁路供电安全检测监测系统”的简称。", "", "- 系统组成", "",
    "- 由6个检测装置和一个数据处理中心组成。", "", "- 系统用途", "", "- 用于供电设备的检测监测和分析。", "",
    "## 1C - 弓网综合检测装置", "", "- 安装位置", "", "- 安装在接触网检测车上。", "", "- 功能描述", "",
    "- 对接触网参数和弓网运行状态进行实时检测，结果以波形形式呈现，类似接触网“心电图”，助检测人员找故障点。", "",
    "- 检测周期", "", "- 检测周期为3个月。",
]
COURSE_FILE = [f"<第 {n} 行>" for n in range(1, 313)] + COURSE_WINDOW + [f"<第 {n} 行>" for n in range(343, 401)]
SECTION_START = 316  # `## 6C系统概述`
QUESTION = "接触网6C系统是哪个系统的简称？由哪几部分组成？"

# End-to-end fixture: an overview section whose sibling sub-headings follow across one blank line. The file name,
# the document title and the sibling sections share no query token of `QUESTION` (path and title tokens are
# indexed with every section, and the tokenizer splits `6C` into `6`, `c`, `6c`), so the siblings are not
# retrieval candidates, are never merged into the top chunk, and can only reach the pack through the
# enumeration-tail append.
COURSE_MD = (
    "# 培训讲义\n\n"
    "## 培训内容\n\n"
    "## 6C系统概述\n\n"
    "- 系统简称\n\n"
    "- 接触网6C系统是“高速铁路供电安全检测监测系统”的简称。\n\n"
    "- 系统组成\n\n"
    "- 由6个检测装置和一个数据处理中心组成。\n\n"
    "- 系统用途\n\n"
    "- 用于供电设备的检测监测和分析。\n\n"
    "## 一、车顶单元\n\n"
    "- 安装位置\n\n"
    "- 安装在车顶。\n\n"
    "## 二、巡检单元\n\n"
    "- 安装位置\n\n"
    "- 安装在车顶。\n\n"
    "## 三、车载单元\n\n"
    "- 安装位置\n\n"
    "- 安装在设备舱。\n"
)


class _NoScrub:
    _scrub = staticmethod(lambda s: s)


def _walk_label(case: unittest.TestCase, content: str, a: int, b: int, file_lines: list[str]) -> None:
    """The cited range, blank lines aside, must be exactly the displayed lines: every non-blank displayed line
    is the file's next non-blank line starting at `a`, and nothing non-blank follows it up to `b`."""
    shown = [ln.strip() for ln in content.strip().splitlines() if ln.strip()]
    case.assertTrue(shown, "block shows no text")
    pos, last = a, None
    for ln in shown:
        while pos <= len(file_lines) and not file_lines[pos - 1].strip():
            pos += 1
        case.assertLessEqual(pos, len(file_lines), f"displayed line is beyond the end of the file: {ln!r}")
        case.assertEqual(file_lines[pos - 1].strip(), ln, f"displayed line is not file line {pos}")
        last = pos
        pos += 1
    case.assertLessEqual(last, b, "a displayed line lies beyond the cited end line")
    case.assertTrue(
        all(not file_lines[k - 1].strip() for k in range(last + 1, b + 1)),
        "the cited range continues past the last displayed line with content that is not shown",
    )


def _legacy_trim(lines: list[str], base_start: int) -> tuple[int, int, str]:
    """Splitter behaviour of wiki-context <= 0.5.8 (and of every index built by it): ``line_end`` is the line
    before the next heading, trailing blank lines included, while the stored text is ``strip()``ped."""
    return base_start, base_start + len(lines) - 1, "\n".join(lines).strip()


class CitationLineRangeTest(unittest.TestCase):
    def test_tail_append_keeps_label_exact_for_legacy_and_trimmed_indexes(self) -> None:
        # legacy index: line_end covers the blank separator; current index: text end
        shapes = [(329, "\n".join(COURSE_FILE[SECTION_START - 1:329]).strip()), (328, "\n".join(COURSE_FILE[SECTION_START - 1:328]).strip())]
        # merged chunk read verbatim from the mirror: it keeps the trailing blank line as a final "\n"
        shapes.append((329, "\n".join(COURSE_FILE[SECTION_START - 1:329])))
        for sec_e, text in shapes:
            with self.subTest(sec_e=sec_e, trailing_newline=text.endswith("\n")):
                expanded, new_e = ContextPackEngine._expand_enumeration_tail(
                    text,
                    sec_s=SECTION_START,
                    sec_e=sec_e,
                    mirror_lines=COURSE_FILE,
                    question=QUESTION,
                    max_chars=4000,
                )
                self.assertNotEqual(expanded, text, "sibling sub-headings must be appended")
                self.assertEqual(
                    expanded.splitlines(),
                    COURSE_FILE[SECTION_START - 1:SECTION_START - 1 + len(expanded.splitlines())],
                    "expanded text must be the file's own consecutive lines",
                )
                content, shown_s, shown_e, windowed = ContextPackEngine._window_block_safely(
                    _NoScrub(),
                    expanded,
                    plan=build_query_plan(QUESTION),
                    q_idf=None,
                    table_header="",
                    max_chars=4000,
                    base_line_start=SECTION_START,
                )
                self.assertFalse(windowed)
                self.assertEqual(shown_s, SECTION_START)
                self.assertEqual(shown_e, new_e)
                _walk_label(self, content, shown_s, shown_e, COURSE_FILE)

    def test_tail_append_is_a_contiguous_slice_for_windowed_output(self) -> None:
        text = "\n".join(COURSE_FILE[SECTION_START - 1:329]).strip()
        expanded, _ = ContextPackEngine._expand_enumeration_tail(
            text, sec_s=SECTION_START, sec_e=329, mirror_lines=COURSE_FILE, question=QUESTION, max_chars=4000
        )
        self.assertEqual(
            expanded.splitlines(),
            COURSE_FILE[SECTION_START - 1:SECTION_START - 1 + len(expanded.splitlines())],
            "expanded text must be the file's own consecutive lines (blank separator included)",
        )
        # Window the already-expanded text so the slice reaches into the appended tail.
        content, shown_s, shown_e, windowed = ContextPackEngine._window_block_safely(
            _NoScrub(),
            expanded,
            plan=build_query_plan("弓网综合检测装置安装在接触网检测车上，功能描述是什么？"),
            q_idf=None,
            table_header="",
            max_chars=150,
            base_line_start=SECTION_START,
        )
        self.assertTrue(windowed)
        self.assertGreater(shown_s, SECTION_START)  # the window really starts inside the appended tail
        _walk_label(self, content, shown_s, shown_e, COURSE_FILE)

    def test_tail_append_is_skipped_when_index_text_is_not_in_the_mirror(self) -> None:
        text = "\n".join(COURSE_FILE[SECTION_START - 1:329]).strip()
        stale_shapes = {
            "edited line": text.replace("系统用途", "系统用途（旧版本）"),
            "trailing newline over a non-blank line": "\n".join(COURSE_FILE[SECTION_START - 1:327]) + "\n",
            "shifted start": text.replace("## 6C系统概述", "## 6C系统概述（旧）", 1),
        }
        for label, stale in stale_shapes.items():
            for sec_s in (SECTION_START, None):
                with self.subTest(stale=label, sec_s=sec_s):
                    out, new_e = ContextPackEngine._expand_enumeration_tail(
                        stale, sec_s=sec_s, sec_e=329, mirror_lines=COURSE_FILE, question=QUESTION, max_chars=4000
                    )
                    self.assertEqual(out, stale, "a stale index must not get lines appended at a guessed position")
                    self.assertEqual(new_e, 329)

    def test_context_pack_labels_are_the_file_lines_shown(self) -> None:
        for legacy_index in (True, False):
            with self.subTest(legacy_index=legacy_index):
                root = Path(tempfile.mkdtemp(prefix="wctx-cite-"))
                try:
                    vdir = root / "vault"
                    init_vault(str(vdir), name="RailKB", layout="zh", dry_run=False)
                    (vdir / "史料" / "课件").mkdir(parents=True)
                    (vdir / "史料" / "课件" / "讲义.md").write_text(COURSE_MD, encoding="utf-8")
                    (vdir / "知识库" / "接触网6C专题-综述.md").write_text(
                        "# 接触网6C检测系统总览\n\n## 概览\n\n6C 检测体系覆盖 1C 到 6C 六大核心子系统。\n", encoding="utf-8"
                    )
                    if legacy_index:
                        with mock.patch.object(indexer, "_trim_section_lines", _legacy_trim):
                            sync_vault(str(vdir), rebuild=True)
                    else:
                        sync_vault(str(vdir), rebuild=True)

                    svc = KBService(vdir)
                    try:
                        checked = 0
                        for question in (
                            QUESTION,
                            "车顶单元和巡检单元的安装位置分别在哪里？",
                            "车载单元安装在什么地方？",
                            "接触网6C检测系统总览",
                        ):
                            out = svc.tool_context({"question": question})
                            self.assertNotIn("error", out)
                            for blk in [*out["primary_hits"], *out["wiki_hits"]]:
                                a, b = blk.get("line_start"), blk.get("line_end")
                                if not isinstance(a, int) or not isinstance(b, int):
                                    continue
                                self.assertIn(f"L{a}-L{b}", out["context_markdown"])
                                if blk["source"].startswith(svc.cfg.wiki_prefix):
                                    file_lines = (svc.cfg.wiki_root / blk["source"][len(svc.cfg.wiki_prefix):]).read_text(
                                        encoding="utf-8"
                                    ).splitlines()
                                else:
                                    rel = svc.cfg.strip_source_prefix(blk["source"])
                                    file_lines = (svc.cfg.mirrors_root / f"{rel}.md").read_text(encoding="utf-8").splitlines()
                                _walk_label(self, blk["content"], a, b, file_lines)
                                checked += 1
                        self.assertGreaterEqual(checked, 4)
                        top = svc.tool_context({"question": QUESTION})["primary_hits"][0]
                        self.assertIn("一、车顶单元", top["content"], "the sibling sub-heading must be pulled in")
                        self.assertIn("由6个检测装置和一个数据处理中心组成", top["content"])
                    finally:
                        svc.close()
                finally:
                    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
