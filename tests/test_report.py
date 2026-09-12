"""The audit letter's safety layer.

These letters are addressed to named institutions and quote their filed
accounting policy. The failure that matters is not an awkward sentence, it is a
figure or an accusation the facts do not support reaching a real 園. Every test
below is one way that could happen.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.report.backends import TemplateBackend
from smart_watchdog.report.facts import AuditFacts, Finding
from smart_watchdog.report.verify import verify_letter


def _facts(**over) -> AuditFacts:
    base = {
        "institution_id": "abc12345",
        "title": "新北市安溪非營利幼兒園",
        "establishment_type": "非營利",
        "town": "三峽區",
        "approved_capacity": 120,
        "priority_rank": 856,
        "priority_total": 1213,
        "priority_score": 0.12,
        "review_reasons": ["財報法遵未通過(高)"],
        "prior_penalties": 0,
        "days_since_last_penalty": None,
        "events_365d": 0,
        "latest_event_type": "",
        "latest_event_date": "",
        "evaluation_result": "基礎評鑑－全數指標通過",
        "evaluation_date": "2025-10-23",
        "evaluation_partial": False,
        "findings": [
            Finding(
                academic_year=113,
                rule="業務發展準備金 資產=負債",
                rule_text="附註二(五)：應於同意後一個月內，以專戶或定期存款方式儲存。",
                detail="資產 13,699,844 vs 負債 15,899,844，未提撥足額 +2,200,000。",
                severity="high",
            )
        ],
        "reserve_verdicts": [],
        "staffing": {},
        "coverage": ["本系統之排序不構成違法認定"],
    }
    base.update(over)
    return AuditFacts(**base)


def test_template_output_always_verifies():
    """The deterministic floor must never fail its own checks.

    It did once: 「非違法認定」 is mandatory and contains 「違法」, so the naive
    forbidden-word scan rejected all 143 letters it had just written.
    """
    facts = _facts()
    result = TemplateBackend().write(facts)
    assert result["verified"], result["problems"]
    assert "非違法認定" in result["text"]


def test_invented_amount_is_rejected():
    """The failure mode that matters: a figure no source supports."""
    draft = ("受文者：新北市安溪非營利幼兒園\n"
             "未提撥足額 9,876,543 元。\n※ 本文非違法認定。")
    r = verify_letter(draft, _facts())
    assert not r.ok
    assert any("9,876,543" in p for p in r.problems)


def test_amount_present_in_facts_is_accepted():
    draft = ("受文者：新北市安溪非營利幼兒園\n"
             "資產 13,699,844 vs 負債 15,899,844。\n※ 本文非違法認定。")
    assert verify_letter(draft, _facts()).ok


def test_small_numbers_are_not_treated_as_amounts():
    """Clause numbers and counts must not need a source entry."""
    draft = ("受文者：新北市安溪非營利幼兒園\n第 3 項、共 12 件。\n※ 本文非違法認定。")
    assert verify_letter(draft, _facts()).ok


def test_another_institution_is_rejected():
    """A letter naming a different 園 mis-attributes the finding entirely."""
    draft = ("受文者：新北市安溪非營利幼兒園\n"
             "另新北市三多非營利幼兒園亦有相同情形。\n※ 本文非違法認定。")
    r = verify_letter(draft, _facts(), known_titles={"新北市三多非營利幼兒園"})
    assert not r.ok
    assert any("三多" in p for p in r.problems)


def test_verdict_language_is_rejected():
    """輸出定位：建議查核，不是違法認定（CLAUDE.md）。"""
    for word in ("違規", "舞弊", "不法"):
        draft = f"受文者：新北市安溪非營利幼兒園\n貴園{word}事證明確。\n※ 本文非違法認定。"
        r = verify_letter(draft, _facts())
        assert not r.ok
        assert any(word in p for p in r.problems)


def test_missing_disclaimer_is_rejected():
    draft = "受文者：新北市安溪非營利幼兒園\n資產 13,699,844。"
    r = verify_letter(draft, _facts())
    assert not r.ok
    assert any("非違法認定" in p for p in r.problems)


def test_clause_quoted_from_a_finding_detail_is_accepted():
    """安溪's detail itself cites 附註二(一); quoting it back is not fabrication."""
    facts = _facts(findings=[Finding(
        academic_year=113, rule="業務發展準備金 資產=負債",
        rule_text="附註二(五)：應於同意後一個月內，以專戶方式儲存。",
        detail="未提撥足額 +2,200,000。依附註二(一)，衍生之所得稅應由非營利法人自行繳納",
        severity="high")])
    draft = ("受文者：新北市安溪非營利幼兒園\n"
             "依附註二(一)，衍生之所得稅應由非營利法人自行繳納\n※ 本文非違法認定。")
    assert verify_letter(draft, facts).ok


def test_unheld_clause_quote_is_rejected():
    draft = ("受文者：新北市安溪非營利幼兒園\n"
             "依附註二(九)，經費支用應符合委託單位核定範圍\n※ 本文非違法認定。")
    r = verify_letter(draft, _facts())
    assert not r.ok
    assert any("未經核對的條文" in p for p in r.problems)


def test_shortfall_language_without_findings_is_rejected():
    """A 園 we could not check must never be described as short of funds."""
    draft = ("受文者：新北市私立某某幼兒園\n未提撥足額。\n※ 本文非違法認定。")
    r = verify_letter(draft, _facts(findings=[], title="新北市私立某某幼兒園"))
    assert not r.ok
    assert any("無財務發現" in p for p in r.problems)


def test_letter_without_statements_says_absence_is_not_clearance():
    """The 94.8% with no financial report must not read as examined and clean."""

    class Row:
        financial_data_available = 0
        compliance_years_checked = 0
        eval_result = ""

    from smart_watchdog.report.facts import _coverage_notes

    notes = " ".join(_coverage_notes(Row(), [], {}))
    assert "非該園財務無虞" in notes
    assert "未受評鑑不等於評鑑通過" in notes
