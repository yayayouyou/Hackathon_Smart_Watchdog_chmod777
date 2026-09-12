"""Parse the 新北市地方教育發展基金 決算書 (public schools & 市立幼兒園).

Each yearly 決算書 ships as five volume PDFs of ~750 pages. A volume concatenates
many 分基金 (one per school / kindergarten), and every page carries a footer of the
form ``13601-4`` -- ``<fund code>-<page number within that fund>``. That footer is
what lets us segment a volume back into per-institution documents without relying
on the table of contents.

Fund codes we care about most: ``136xx`` are the 市立幼兒園, which are the actual
教保機構 in scope for this competition. ``13230 新北市各國民小學`` aggregates the
公立國小附設幼兒園 and is handled separately.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re

import fitz

from .pdf_utils import Row, page_rows

# Page footer, e.g. "13601-4" or "13230-127"
FOOTER_RE = re.compile(r"^(\d{5})-(\d+)$")
# Section heading, e.g. "新北市地方教育發展基金—新北市立板橋幼兒園"
TITLE_RE = re.compile(r"新北市地方教育發展基金[—\-–]\s*(.+?)$")

KINDERGARTEN_CODE_RE = re.compile(r"^136\d\d$")


@dataclasses.dataclass
class FundSection:
    """One 分基金 within a volume: which pages it owns and what it is called."""

    fund_code: str
    name: str
    volume: pathlib.Path
    fiscal_year: str
    page_indices: list[int] = dataclasses.field(default_factory=list)

    @property
    def is_kindergarten(self) -> bool:
        return bool(KINDERGARTEN_CODE_RE.match(self.fund_code))

    def __len__(self) -> int:
        return len(self.page_indices)


def _footer_code(rows: list[Row]) -> str | None:
    """Read the fund code out of the last few rows of a page."""
    for row in reversed(rows[-4:]):
        m = FOOTER_RE.match(row.text.replace(" ", ""))
        if m:
            return m.group(1)
    return None


def _title_name(rows: list[Row]) -> str | None:
    for row in rows[:6]:
        m = TITLE_RE.search(row.text.strip())
        if m:
            # Strip the trailing statement name if it ran into the same line.
            return m.group(1).strip()
    return None


def index_volume(path: pathlib.Path, fiscal_year: str) -> list[FundSection]:
    """Segment one volume PDF into :class:`FundSection` records.

    Pages before the first footer-bearing page (總目錄, 總說明 covers) are skipped.
    """
    doc = fitz.open(path)
    sections: dict[str, FundSection] = {}
    try:
        for page in doc:
            rows = page_rows(page)
            code = _footer_code(rows)
            if code is None:
                continue
            sec = sections.get(code)
            if sec is None:
                sec = FundSection(
                    fund_code=code,
                    name=_title_name(rows) or "",
                    volume=path,
                    fiscal_year=fiscal_year,
                )
                sections[code] = sec
            elif not sec.name:
                sec.name = _title_name(rows) or ""
            sec.page_indices.append(page.number)
    finally:
        doc.close()
    return sorted(sections.values(), key=lambda s: s.fund_code)


def find_statement_page(
    path: pathlib.Path, section: FundSection, statement: str
) -> int | None:
    """Locate the page index holding a named statement, e.g. ``基金來源、用途及餘絀表``.

    Matches on the heading rows only, so the 目次 entry (which lists every statement
    name on one page) does not produce a false hit.
    """
    doc = fitz.open(path)
    try:
        for idx in section.page_indices:
            rows = page_rows(doc[idx])
            head = " ".join(r.text for r in rows[:5])
            if statement in head and "目　次" not in head and "目次" not in head:
                return idx
    finally:
        doc.close()
    return None
