"""Extract per-year financials for the 市立幼兒園 out of the 決算書 volumes.

Covers the two statements that carry usable監理 signal and parse reliably:

* ``基金來源、用途及餘絀表`` -- 8 value columns
  (本年度預算 金額/%, 本年度決算 金額/%, 比較增減 金額/%, 上年度決算 金額/%)
* ``員工人數彙計表`` -- 3 value columns (預算數, 決算數, 比較增減), unit = 人

Two statements are deliberately **not** parsed:

* ``用人費用彙計表`` splits across 左半頁/右半頁 with a three-deep header whose CJK
  characters interleave across rows; only its 合計 is trustworthy without a
  bespoke parser, and 各項費用彙計表 already carries the same total.
* Anything on a page whose text layer is Private-Use-Area garbage (see
  ``scripts/survey_text_quality.py``); those pages are reported as failures rather
  than silently yielding wrong numbers.
"""

from __future__ import annotations

import dataclasses
import pathlib

import fitz

from .pdf_utils import page_rows
from .public_school import FundSection, find_statement_page
from .tables import Table, parse_table, statement_rows

FUND_STATEMENT = "基金來源、用途及餘絀表"
STAFF_STATEMENT = "員工人數彙計表"

# Column indices within 基金來源、用途及餘絀表.
BUDGET, BUDGET_PCT, ACTUAL, ACTUAL_PCT, DELTA, DELTA_PCT, PRIOR, PRIOR_PCT = range(8)
FUND_COLUMNS = 8
STAFF_COLUMNS = 3

PUA_START, PUA_END = 0xE000, 0xF8FF


@dataclasses.dataclass
class KindergartenYear:
    """One 市立幼兒園 for one fiscal year."""

    fiscal_year: str
    fund_code: str
    name: str

    fund_source_budget: float | None = None
    fund_source_actual: float | None = None
    fund_source_prior: float | None = None
    gov_transfer_actual: float | None = None
    tuition_budget: float | None = None
    tuition_actual: float | None = None
    fund_use_budget: float | None = None
    fund_use_actual: float | None = None
    preschool_plan_actual: float | None = None
    capex_budget: float | None = None
    capex_actual: float | None = None
    surplus_actual: float | None = None
    closing_balance_actual: float | None = None
    closing_balance_prior: float | None = None

    staff_budget: float | None = None
    staff_actual: float | None = None
    kitchen_staff_actual: float | None = None

    notes: list[str] = dataclasses.field(default_factory=list)

    # --- derived ratios ---------------------------------------------------
    @property
    def gov_dependency(self) -> float | None:
        """政府撥入 / 基金來源. 公立園 sit near 0.87, so a drop is a structural change."""
        if not self.fund_source_actual or self.gov_transfer_actual is None:
            return None
        return self.gov_transfer_actual / self.fund_source_actual

    @property
    def tuition_execution(self) -> float | None:
        """學雜費決算 / 預算 -- the cleanest proxy for enrolment shortfall.

        公立園 do not publish enrolment, but tuition income tracks headcount and
        the government transfer arrives regardless, so a shortfall here shows up
        even when total revenue looks healthy.
        """
        if not self.tuition_budget:
            return None
        return (self.tuition_actual or 0) / self.tuition_budget

    @property
    def capex_execution(self) -> float | None:
        """建築及設備計畫 決算 / 預算 -- deferred capital maintenance."""
        if not self.capex_budget:
            return None
        return (self.capex_actual or 0) / self.capex_budget

    @property
    def cost_per_staff(self) -> float | None:
        if not self.staff_actual or self.preschool_plan_actual is None:
            return None
        return self.preschool_plan_actual / self.staff_actual

    @property
    def balance_change(self) -> float | None:
        if self.closing_balance_actual is None or self.closing_balance_prior is None:
            return None
        return self.closing_balance_actual - self.closing_balance_prior


def _pua_ratio(text: str) -> float:
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    return sum(1 for c in chars if PUA_START <= ord(c) <= PUA_END) / len(chars)


def _read_table(
    doc: fitz.Document, page_index: int, expect_columns: int
) -> tuple[Table | None, str | None]:
    """Parse one statement page, refusing pages we cannot read correctly."""
    page = doc[page_index]
    if _pua_ratio(page.get_text()) > 0.5:
        return None, f"page {page_index + 1}: PUA 字型，文字層不可用，需 OCR"
    table = parse_table(statement_rows(page_rows(page)))
    if table.n_columns != expect_columns:
        return None, (
            f"page {page_index + 1}: 偵測到 {table.n_columns} 欄，"
            f"預期 {expect_columns} 欄，跳過以免對錯欄"
        )
    return table, None


def extract_kindergarten(
    path: pathlib.Path, section: FundSection
) -> KindergartenYear:
    """Extract one 幼兒園's year from its 分基金 section."""
    rec = KindergartenYear(
        fiscal_year=section.fiscal_year,
        fund_code=section.fund_code,
        name=section.name,
    )
    doc = fitz.open(path)
    try:
        fund_idx = find_statement_page(path, section, FUND_STATEMENT)
        if fund_idx is None:
            rec.notes.append(f"找不到{FUND_STATEMENT}")
        else:
            table, err = _read_table(doc, fund_idx, FUND_COLUMNS)
            if err:
                rec.notes.append(err)
            elif table is not None:
                rec.fund_source_budget = table.value("基金來源", column=BUDGET)
                rec.fund_source_actual = table.value("基金來源", column=ACTUAL)
                rec.fund_source_prior = table.value("基金來源", column=PRIOR)
                rec.gov_transfer_actual = table.value("政府撥入收入", column=ACTUAL)
                rec.tuition_budget = table.value("學雜費收入", column=BUDGET)
                rec.tuition_actual = table.value("學雜費收入", column=ACTUAL)
                rec.fund_use_budget = table.value("基金用途", column=BUDGET)
                rec.fund_use_actual = table.value("基金用途", column=ACTUAL)
                rec.preschool_plan_actual = table.value("學前教育計畫", column=ACTUAL)
                rec.capex_budget = table.value("建築及設備計畫", column=BUDGET)
                rec.capex_actual = table.value("建築及設備計畫", column=ACTUAL)
                rec.surplus_actual = table.value("本期賸餘", column=ACTUAL)
                rec.closing_balance_actual = table.value("期末基金餘額", column=ACTUAL)
                rec.closing_balance_prior = table.value("期末基金餘額", column=PRIOR)

        staff_idx = find_statement_page(path, section, STAFF_STATEMENT)
        if staff_idx is None:
            rec.notes.append(f"找不到{STAFF_STATEMENT}")
        else:
            table, err = _read_table(doc, staff_idx, STAFF_COLUMNS)
            if err:
                rec.notes.append(err)
            elif table is not None:
                # 專任人員 = 職員 + 廚工 in these statements. There is no separate
                # 教保人員 line, so a legally exact 師生比 is not derivable here --
                # only total staffing. Kitchen staff are split out so they can be
                # excluded from any teaching-ratio proxy.
                rec.staff_budget = table.value("專任人員", column=0)
                rec.staff_actual = table.value("專任人員", column=1)
                rec.kitchen_staff_actual = table.value("廚工", column=1)
    finally:
        doc.close()
    return rec
