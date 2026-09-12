"""Locate the statement pages inside a scanned 非營利園 financial report.

These reports have no text layer at all, so nothing on the page can be read
without a vision model. But we do not need to read them to find them: page count
varies 30-49 because the 附註 length varies, while the *statement* pages sit in a
stable order near the front. That means locating them costs nothing, and only the
located pages need to be sent to a model.

Ordering observed across the corpus (N01 安溪 113 學年度, 42 pages):

    1     封面 (the one page that usually *does* carry text)
    2     目次
    3-4   會計師查核報告
    5     資產負債表
    6     收支餘絀表 (本期)
    7     收支餘絀表 (前期)
    8     淨值變動表
    9     現金流量表
    10+   財務報表附註

Rather than trusting those indices, ``locate_pages`` derives them from the cover
page's text where possible and falls back to the structural offsets, always
reporting which method it used so the caller can tell a located page from a
guessed one.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re

import fitz

# The cover page is the only page with a usable text layer in most reports.
COVER_INSTITUTION_RE = re.compile(r"(新北市\S*?(?:非營利|)幼兒園)")
COVER_OPERATOR_RE = re.compile(r"[（(](?:委託)?(.+?)(?:申請)?辦理[）)]")
COVER_YEAR_RE = re.compile(r"民國\s*(\d{3})\s*學年度")
FILENAME_RE = re.compile(r"^(N\d\d)(.+?)_(\d{3})學年度")

# Offsets from the first statement page, used when the cover cannot be read.
STATEMENT_ORDER = [
    "資產負債表",
    "收支餘絀表_本期",
    "收支餘絀表_前期",
    "淨值變動表",
    "現金流量表",
]
DEFAULT_FIRST_STATEMENT_INDEX = 4  # 0-based: page 5


@dataclasses.dataclass
class ReportLayout:
    """Where each statement lives in one report, and how confident we are."""

    path: pathlib.Path
    code: str
    short_name: str
    academic_year: str
    page_count: int
    institution: str | None = None
    operator: str | None = None
    cover_text_ok: bool = False
    pages: dict[str, int] = dataclasses.field(default_factory=dict)
    notes: list[str] = dataclasses.field(default_factory=list)

    @property
    def priority_pages(self) -> list[int]:
        """The pages Phase 1 needs: balance sheet, income statement, note 1.

        Deliberately a short list. Without a Files API on Bedrock every image is
        base64-inlined into the request, so page selection *is* cost control --
        3 pages × 132 reports is ~400 calls instead of 5,162.
        """
        wanted = ["資產負債表", "收支餘絀表_本期", "附註一"]
        return [self.pages[k] for k in wanted if k in self.pages]


def _cover_text(doc: fitz.Document) -> str:
    return doc[0].get_text() if doc.page_count else ""


def locate_pages(path: pathlib.Path) -> ReportLayout:
    """Work out the statement layout of one scanned report."""
    m = FILENAME_RE.match(path.name)
    doc = fitz.open(path)
    try:
        layout = ReportLayout(
            path=path,
            code=m.group(1) if m else "",
            short_name=m.group(2) if m else path.stem,
            academic_year=m.group(3) if m else "",
            page_count=doc.page_count,
        )
        cover = _cover_text(doc)
        if len(cover.strip()) > 40:
            layout.cover_text_ok = True
            inst = COVER_INSTITUTION_RE.search(cover)
            layout.institution = inst.group(1) if inst else None
            op = COVER_OPERATOR_RE.search(cover)
            layout.operator = op.group(1) if op else None
            yr = COVER_YEAR_RE.search(cover)
            if yr and not layout.academic_year:
                layout.academic_year = yr.group(1)
        else:
            layout.notes.append("封面無可用文字層，機構名稱僅取自檔名")

        first = DEFAULT_FIRST_STATEMENT_INDEX
        for offset, name in enumerate(STATEMENT_ORDER):
            idx = first + offset
            if idx < doc.page_count:
                layout.pages[name] = idx
        # 附註一 follows the last statement; its exact page is the first note page.
        note_idx = first + len(STATEMENT_ORDER)
        if note_idx < doc.page_count:
            layout.pages["附註一"] = note_idx

        layout.notes.append(
            "頁碼為結構性推定（封面+目次+查核報告2頁後為報表），"
            "須以視覺模型回報的表頭確認"
        )
        return layout
    finally:
        doc.close()


def render_page(path: pathlib.Path, page_index: int, dpi: int = 200) -> bytes:
    """Rasterise one page to PNG bytes for a vision model.

    The source scans are 300dpi A4. Re-rendering at 200dpi keeps the printed
    figures legible while cutting the image token count; raise it only if a
    verification pass shows digits being misread.
    """
    doc = fitz.open(path)
    try:
        return doc[page_index].get_pixmap(dpi=dpi).tobytes("png")
    finally:
        doc.close()
