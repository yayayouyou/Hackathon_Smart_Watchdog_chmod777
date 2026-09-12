"""Invariants of the page-level compliance resolution.

These checks decide whether a real 幼兒園 gets named in an audit letter, so the
tests here are mostly about what the resolver is *not* allowed to conclude. The
N08 case is the reason the file exists: an earlier version returned 未通過 for
鷺江 113 on evidence that amounted to "this table does not have the columns I
expected", and its own explanation read "0 個項目僅列單邊金額" -- a finding
against a real institution, from zero observations.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.features.compliance_pagewise import (
    resolve_net_recording,
    resolve_personnel_outflow,
)


def fact(code="N01", year="110", section="budget_transfer", page=24, table=1,
         item="人事費", period="A 預算數", index=0, value=None):
    return {"code": code, "academic_year": year, "section": section,
            "pdf_page": page, "table_index": table, "item_label": item,
            "period_label": period, "period_index": index, "value": value}


def budget_rows(items):
    """items: [(label, budget, actual, diff)] -> long-format facts."""
    rows = []
    for label, b, act, diff in items:
        rows.append(fact(item=label, period="A 預算數", index=0, value=b))
        rows.append(fact(item=label, period="B 決算數", index=1, value=act))
        rows.append(fact(item=label, period="B-A 差異數", index=2, value=diff))
    return rows


def agency_rows(section, items, two_sided=True, period="113.8.1~114.7.31",
                code="N01", year="110"):
    rows = []
    for label, recv, pay in items:
        if two_sided:
            rows.append(fact(code=code, year=year, section=section, page=14,
                             item=label, period=f"{period} 收", index=0, value=recv))
            rows.append(fact(code=code, year=year, section=section, page=14,
                             item=label, period=f"{period} 支", index=1, value=pay))
        else:
            rows.append(fact(code=code, year=year, section=section, page=14,
                             item=label, period=period, index=0, value=recv))
    return rows


# ── 人事費不得流出 ──────────────────────────────────────────────────────


def test_no_category_overspent_is_evidence_not_a_pass() -> None:
    """N01 安溪 110's shape: everything under budget.

    Strong evidence, but not 通過. The table's 預算數 column may already be the
    post-流用 revised budget, in which case a transfer is invisible to any
    comparison against it -- and the form's own 預決算檢查結果 column, which
    would settle that, is blank in all 132 reports.
    """
    rows = budget_rows([("人事費", 7645622, 6763467, -882155),
                        ("業務費", 676700, 300404, -376296),
                        ("合　計", 10132904, 8687375, -1445529)])
    r = resolve_personnel_outflow(rows, "N01", "110")
    assert r.passed is None
    assert not r.changed
    assert r.evidence_state == "no_overspend"
    assert "全數未超支" in r.detail
    assert "無法證明實際未流用" in r.detail


def test_overspend_absorbable_by_siblings_is_not_an_outflow() -> None:
    """流用 between sibling lines is ordinary budgeting, not a 人事費 outflow.

    106 of the corpus's 132 園-年 have some category over budget. If the other
    categories' underspends more than cover it, 人事費 was not a necessary source
    and the rule is answered without an inspector.
    """
    rows = budget_rows([("人事費", 7645622, 6763467, -882155),
                        ("業務費", 676700, 700000, 23300),
                        ("材料費", 1401300, 1245534, -155766)])
    r = resolve_personnel_outflow(rows, "N01", "110")
    assert r.passed is None
    assert not r.changed
    assert r.evidence_state == "absorbable"
    assert "勻支" in r.detail


def test_overspend_beyond_sibling_slack_stays_undecided() -> None:
    """When the overspends cannot be funded from elsewhere, a human must look."""
    rows = budget_rows([("人事費", 7645622, 6763467, -882155),
                        ("業務費", 676700, 1200000, 523300),
                        ("材料費", 1401300, 1390000, -11300)])
    r = resolve_personnel_outflow(rows, "N01", "110")
    assert r.passed is None
    assert not r.changed
    assert r.evidence_state == "exceeds_slack"
    assert "業務費" in r.detail and "須另有來源" in r.detail


def test_only_top_level_categories_are_summed() -> None:
    """The table is hierarchical and the indent is stripped, so children must not
    be double-counted: 業務費 here equals its two children exactly."""
    rows = budget_rows([("人事費", 100000, 90000, -10000),
                        ("業務費", 50000, 55000, 5000),
                        ("活動費", 30000, 35000, 5000),
                        ("水費", 20000, 20000, 0),
                        ("材料費", 40000, 34000, -6000)])
    r = resolve_personnel_outflow(rows, "N01", "110")
    # 業務費 +5,000 against 材料費 -6,000: absorbable. 活動費/水費 are children of
    # 業務費 and must not be added on top of it.
    assert r.evidence_state == "absorbable"


def test_totals_row_is_not_treated_as_a_category() -> None:
    """A 合計 row that overspends must not masquerade as a transfer target."""
    rows = budget_rows([("人事費", 100, 90, -10), ("合　計", 100, 150, 50)])
    r = resolve_personnel_outflow(rows, "N01", "110")
    assert r.evidence_state == "no_overspend"


def test_missing_table_stays_insufficient() -> None:
    r = resolve_personnel_outflow([], "N01", "110")
    assert r.passed is None
    assert not r.changed
    assert "查無" in r.detail


# ── 收入不得以淨額入帳 ──────────────────────────────────────────────────


def test_gross_columns_prove_gross_recording() -> None:
    rows = agency_rows("agency_passthrough",
                       [("團保費", 30304, 30304), ("代購用品", 100674, 100364)])
    r = resolve_net_recording(rows, "N01", "110")
    assert r.passed is True
    assert r.changed
    assert "總額入帳" in r.detail


def test_revenue_only_table_never_yields_a_failure() -> None:
    """The N08 鷺江 113 regression.

    「專案補助收入」 lists revenue by academic year with no 支出 column. That is
    silence on the netting question, and silence must read as 資料不足.
    """
    rows = agency_rows("project_subsidy", [("財團法人彭婉如文教基金會", 11515, None)],
                       two_sided=False, code="N08", year="113")
    r = resolve_net_recording(rows, "N08", "113")
    assert r.passed is None, "單邊明細表不得產生未通過"
    assert not r.changed
    assert "單邊" in r.detail


def test_no_agency_table_stays_insufficient() -> None:
    r = resolve_net_recording([], "N01", "110")
    assert r.passed is None
    assert "查無" in r.detail


def test_resolver_never_returns_false_for_net_recording() -> None:
    """Whatever the evidence, this rule may only reach 通過 or 資料不足.

    A breach shows up as an *absent* gross table, which is indistinguishable from
    a table the extraction failed to find -- so 未通過 is not a conclusion this
    evidence can support, and the inspector is told what to ask for instead.
    """
    cases = [
        [],
        agency_rows("project_subsidy", [("X", 1, None)], two_sided=False),
        agency_rows("agency_subsidy", [("Y", 5, 5)]),
        agency_rows("agency_subsidy", [("Y", 5, 5)])
        + agency_rows("project_subsidy", [("Z", 9, None)], two_sided=False),
    ]
    for rows in cases:
        assert resolve_net_recording(rows, "N01", "110").passed is not False


def test_personnel_outflow_never_reaches_a_verdict() -> None:
    """附表二 evidence may rank a report; it may not clear or condemn one.

    This is the regression for the inflation bug: 131 reports were marked 通過 on
    evidence that only showed 人事費 was not a *necessary* source of the
    overspends, which is not the same claim.
    """
    cases = [
        [],
        budget_rows([("人事費", 100, 90, -10), ("業務費", 100, 90, -10)]),
        budget_rows([("人事費", 100, 90, -10), ("業務費", 100, 105, 5),
                     ("材料費", 100, 80, -20)]),
        budget_rows([("人事費", 100, 90, -10), ("業務費", 100, 200, 100)]),
    ]
    for rows in cases:
        r = resolve_personnel_outflow(rows, "N01", "110")
        assert r.passed is None, "本規則不得由附表二單獨判定通過或未通過"
        assert not r.changed
