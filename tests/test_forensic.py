"""Tests for forensic signal computation.

These signals drive inspection priority for real institutions, so the arithmetic
has to be pinned down. The cases below encode two things that are easy to get
wrong and expensive to get wrong:

* a missing input must yield ``None``, never a silently-plausible number
* a blank budget cell means "no appropriation", which is a *finding*, and must not
  be read as a zero budget (which would make execution ratios explode or vanish)
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

from smart_watchdog.extract.forensic import compute_signals, validate_forensic


def _ln(label: str, budget, actual, pct) -> dict:
    return {"label": label, "budget": budget, "actual": actual, "execution_pct": pct}


def report(**over) -> dict:
    """安溪 112 學年度 -- every figure here was verified against the source page."""
    payload = {
        "code": "N01",
        "short_name": "安溪",
        "balance_sheet": {
            "cash": 8868744,
            "prepaid_receipts": 4324548,
            "reserve_asset": 13699844,
            "reserve_liability": 15899844,
            "accumulated_surplus": 2471618,
            "current_surplus": -1988771,
            "equity_total": 482847,
            "total_assets": 26187977,
            "total_liabilities": 25705130,
        },
        "income_statement": {
            "lines": [
                _ln("人事費", 13414768, 11797585, 88),
                _ln("業務費", 883400, 394450, 45),
                _ln("材料費", 2375920, 2216621, 93),
                _ln("維護費", 143000, 51581, 36),
                _ln("修繕購置費", 190400, 28074, 15),
                _ln("雜支", 53004, 18950, 36),
                _ln("行政管理費", 325932, 325932, 100),
                _ln("業務發展費", None, 2388000, None),
                _ln("支出合計", 17386424, 18744221, None),
            ]
        },
        "note_1": {
            "approved_capacity": 120,
            "actual_enrolment": 118,
            "total_staff": 18,
            "educators": 12,
        },
        "issues": [],
    }
    for section, fields in over.items():
        payload[section] = {**payload[section], **fields}
    return payload


class TestSignals:
    def test_prepaid_coverage(self) -> None:
        assert compute_signals(report()).prepaid_coverage == pytest.approx(
            8868744 / 4324548
        )

    def test_reserve_gap_is_liability_minus_asset(self) -> None:
        assert compute_signals(report()).reserve_funding_gap == pytest.approx(2_200_000)

    def test_reserve_gap_is_zero_when_funded(self) -> None:
        """新月 112 and 安溪 110 both have the two figures equal -- the normal case."""
        sig = compute_signals(
            report(balance_sheet={"reserve_asset": 6791844, "reserve_liability": 6791844})
        )
        assert sig.reserve_funding_gap == 0

    def test_execution_ratios_match_the_printed_percentages(self) -> None:
        sig = compute_signals(report())
        assert sig.repair_execution == pytest.approx(0.15, abs=0.005)
        assert sig.maintenance_execution == pytest.approx(0.36, abs=0.005)
        assert sig.personnel_execution == pytest.approx(0.88, abs=0.005)
        assert sig.admin_fee_execution == pytest.approx(1.0)

    def test_related_party_priority_is_admin_minus_operating_mean(self) -> None:
        """Positive means the operator's own fee outran spending on the children."""
        sig = compute_signals(report())
        operating = [0.879, 0.4465, 0.9329, 0.3607, 0.1474, 0.3575]
        assert sig.related_party_priority == pytest.approx(
            1.0 - sum(operating) / len(operating), abs=0.01
        )
        assert sig.related_party_priority > 0

    def test_staff_ratio_uses_educators_not_total_staff(self) -> None:
        """幼照法 counts 教保服務人員; kitchen and admin staff do not supervise children."""
        sig = compute_signals(report())
        assert sig.staff_ratio == pytest.approx(118 / 12)
        assert sig.staff_ratio != pytest.approx(118 / 18)

    def test_unbudgeted_spend_excludes_totals(self) -> None:
        """支出合計 has a budget, so it never counts; only genuinely unbudgeted lines do."""
        assert compute_signals(report()).unbudgeted_spend == pytest.approx(2_388_000)

    def test_cost_per_child(self) -> None:
        assert compute_signals(report()).cost_per_child == pytest.approx(18744221 / 118)

    def test_enrolment_utilisation(self) -> None:
        assert compute_signals(report()).enrolment_utilisation == pytest.approx(118 / 120)


class TestMissingInputs:
    """A missing input must produce None, never a plausible-looking number."""

    def test_no_prepaid_receipts_gives_none(self) -> None:
        sig = compute_signals(report(balance_sheet={"prepaid_receipts": None}))
        assert sig.prepaid_coverage is None

    def test_zero_prepaid_receipts_does_not_divide(self) -> None:
        sig = compute_signals(report(balance_sheet={"prepaid_receipts": 0}))
        assert sig.prepaid_coverage is None

    def test_missing_educators_gives_no_staff_ratio(self) -> None:
        sig = compute_signals(report(note_1={"educators": None}))
        assert sig.staff_ratio is None

    def test_blank_budget_yields_no_execution_ratio(self) -> None:
        """A blank 預算數 is 'no appropriation' -- a ratio against it is undefined."""
        payload = report()
        payload["income_statement"]["lines"] = [
            _ln("修繕購置費", None, 28074, None),
        ]
        assert compute_signals(payload).repair_execution is None

    def test_absent_line_yields_none_not_zero(self) -> None:
        payload = report()
        payload["income_statement"]["lines"] = [
            ln for ln in payload["income_statement"]["lines"] if ln["label"] != "維護費"
        ]
        assert compute_signals(payload).maintenance_execution is None

    def test_empty_report_produces_all_none(self) -> None:
        sig = compute_signals({"code": "X", "short_name": "x"})
        values = {k: v for k, v in sig.as_dict().items() if k not in ("code", "short_name")}
        assert all(v is None for v in values.values()), values


class TestValidation:
    def test_consistent_report_passes(self) -> None:
        passed, failed = validate_forensic(report())
        assert failed == []
        assert len(passed) >= 8

    def test_broken_balance_sheet_is_caught(self) -> None:
        _passed, failed = validate_forensic(
            report(balance_sheet={"total_assets": 99999999})
        )
        assert any("資產總計" in f for f in failed)

    def test_wrong_execution_percentage_is_caught(self) -> None:
        payload = report()
        for ln in payload["income_statement"]["lines"]:
            if ln["label"] == "維護費":
                ln["execution_pct"] = 99
        _passed, failed = validate_forensic(payload)
        assert any("維護費" in f for f in failed)

    def test_blank_budget_line_is_not_flagged(self) -> None:
        """業務發展費 has no budget and no printed 執行率: nothing to check, not a fail."""
        _passed, failed = validate_forensic(report())
        assert not any("業務發展費" in f for f in failed)


# --- classify_reserve_gap ----------------------------------------------------
#
# The whole point of this function is that one year's statement cannot tell a
# transfer that arrives late from cash that never arrives, and the two readings
# call for opposite responses. Every case below is a way of getting that wrong.

from smart_watchdog.features.compliance import classify_reserve_gap


def test_funded_both_years_says_nothing():
    """No gap either year is not a finding; it must not appear in the report."""
    assert classify_reserve_gap(500_000, 500_000, 700_000, 700_000) is None


def test_missing_asset_line_is_not_a_zero_gap():
    """東湖 111 carries a severance liability with no asset line at all.

    Treating the absent side as 0 would assert a full shortfall when the honest
    reading is that the statement is incomplete.
    """
    assert classify_reserve_gap(None, 105_517, None, 105_517) is None
    assert classify_reserve_gap(0, 105_517, None, 210_000) is None


def test_one_year_lag_is_not_called_a_shortfall():
    """三多's pattern: this year's asset equals last year's liability.

    Gap grows 1.2M -> 2.2M, but every dollar booked last year did arrive. Calling
    this 缺口擴大 would accuse a 園 that is merely funding a year in arrears.
    """
    verdict, lag = classify_reserve_gap(0, 1_200_000, 1_200_000, 3_400_000)
    assert verdict == "疑似撥付落後一年"
    assert lag is True


def test_compounding_shortfall_is_flagged():
    """Same growing gap, but the asset side did not follow last year's liability."""
    verdict, lag = classify_reserve_gap(0, 1_200_000, 100_000, 3_400_000)
    assert verdict == "缺口擴大"
    assert lag is False


def test_unchanged_gap_stays_ambiguous():
    """A constant gap fits both a fixed timing difference and a standing shortfall.

    It must not be upgraded to 缺口擴大 just because a gap exists.
    """
    verdict, _ = classify_reserve_gap(300_000, 500_000, 800_000, 1_000_000)
    assert verdict == "缺口穩定"


def test_topped_up_gap_is_reported_as_narrowing():
    verdict, _ = classify_reserve_gap(0, 900_000, 700_000, 900_000)
    assert verdict == "缺口縮小"


def test_round_sum_against_odd_liability_is_not_a_new_shortfall():
    """A 園 transferring 1,200,000 against a 1,199,650 liability has funded it."""
    verdict, _ = classify_reserve_gap(0, 1_199_650, 1_200_000, 2_400_000)
    assert verdict == "疑似撥付落後一年"


# --- classify_accountant_change ----------------------------------------------
#
# docs/research/02-forensic-signals.md §7: 簽證會計師更換：連續年度換所，特別是
# 換所後數字大幅變動 → 典型紅旗. Switching firms alone is legal and unremarkable,
# so this must not fire on the change alone -- only on the change plus a
# corroborating signal (a non-unmodified opinion, or a large 本期餘絀 swing).

from smart_watchdog.features.compliance import classify_accountant_change


def test_same_firm_says_nothing():
    """No change is not a finding; it must not appear in the report."""
    assert classify_accountant_change("誠明聯合會計師事務所",
                                       "誠明聯合會計師事務所") is None


def test_missing_firm_name_is_not_treated_as_no_change():
    """A blank firm name is unread data, not evidence the firm stayed the same."""
    assert classify_accountant_change(None, "誠明聯合會計師事務所") is None
    assert classify_accountant_change("誠明聯合會計師事務所", "") is None
    assert classify_accountant_change(None, None) is None


def test_plain_change_with_no_corroborating_signal_is_logged_but_not_escalated():
    """A firm change alone -- unmodified opinion, no surplus data -- is 換所."""
    verdict, reason = classify_accountant_change(
        "誠明聯合會計師事務所", "安永聯合會計師事務所", opinion="unmodified")
    assert verdict == "換所"
    assert reason is None


def test_change_with_non_unmodified_opinion_is_escalated():
    verdict, reason = classify_accountant_change(
        "誠明聯合會計師事務所", "安永聯合會計師事務所", opinion="qualified")
    assert verdict == "換所且有異常訊號"
    assert "qualified" in reason


def test_change_with_large_surplus_swing_is_escalated():
    verdict, reason = classify_accountant_change(
        "誠明聯合會計師事務所", "安永聯合會計師事務所",
        prev_surplus=200_000, surplus=900_000, opinion="unmodified")
    assert verdict == "換所且有異常訊號"
    assert "200,000" in reason and "900,000" in reason


def test_change_with_small_surplus_swing_is_not_escalated():
    """A modest swing is ordinary year-to-year variance, not a red flag."""
    verdict, reason = classify_accountant_change(
        "誠明聯合會計師事務所", "安永聯合會計師事務所",
        prev_surplus=200_000, surplus=250_000, opinion="unmodified")
    assert verdict == "換所"
    assert reason is None


def test_missing_surplus_on_either_side_is_skipped_not_treated_as_zero():
    """A missing operand must not manufacture a swing out of nothing."""
    verdict, reason = classify_accountant_change(
        "誠明聯合會計師事務所", "安永聯合會計師事務所",
        prev_surplus=None, surplus=900_000, opinion="unmodified")
    assert verdict == "換所"
    assert reason is None


# --- corroborating_signals ----------------------------------------------------
#
# Extracted so scripts/check_accountant_change.py can apply the same "is this
# change actually worth escalating" test to a change of *accountant* (not just
# firm), without classify_accountant_change's firm-specific preconditions.

from smart_watchdog.features.compliance import corroborating_signals


def test_corroborating_signals_empty_when_nothing_stands_out():
    assert corroborating_signals(
        prev_surplus=200_000, surplus=250_000, opinion="unmodified") == []


def test_corroborating_signals_names_the_opinion_and_the_swing_independently():
    signals = corroborating_signals(
        prev_surplus=200_000, surplus=900_000, opinion="qualified")
    assert any("qualified" in s for s in signals)
    assert any("200,000" in s and "900,000" in s for s in signals)


# --- clause_5_income_base ----------------------------------------------------
#
# 附註二 turned out to be a template revised clause-by-clause, not versioned as
# a whole document (docs/research/05-phase1-results.md §14.1): (五)'s parenthetical
# definition of 收入總額 survives through 111 and is confirmed absent only once
# 113's own note_2 is re-extracted. Asserting "not defined" from academic_year
# alone was wrong for every 110/111 report -- these tests pin the fix so it can't
# regress to a year-keyed lookup.

from smart_watchdog.features.compliance import (
    clause_5_income_base,
    is_opening_year,
)

_NOTE2_WITH_DEF = (
    "(四) 資遣費準備金\n...\n"
    "(五) 業務發展準備金\n報經直轄市、縣（市）主管機關同意後，至多提列收入總額"
    "（家長繳交之費用；其有政府差額補助費者，應合併計算）之百分之二十為業務發展"
    "準備金。\n(六) 代管財產\n..."
)
_NOTE2_WITHOUT_DEF = (
    "(四) 資遣費準備金\n...\n"
    "(五) 業務發展準備金\n報經委託單位或直轄市、縣（市）主管機關同意後，至多提列"
    "收入總額之百分之二十為業務發展準備金。\n(六) 代管財產\n..."
)


def test_missing_note_2_is_unconfirmed_not_absent():
    """43+ reports (all of 113 so far) have no note_2 at all.

    Returning False here would assert "not defined" for reports we have not
    actually read (五) on -- exactly the mistake the 20% cap check made before
    note_2 existed to check against.
    """
    assert clause_5_income_base(None) is None
    assert clause_5_income_base("") is None


def test_definition_detected_when_present():
    assert clause_5_income_base(_NOTE2_WITH_DEF) is True


def test_definition_detected_when_absent():
    assert clause_5_income_base(_NOTE2_WITHOUT_DEF) is False


def test_clause_9_wording_does_not_leak_into_clause_5():
    """(九)'s 委託單位 wording change (111+) must not affect (五)'s detection.

    An early version of the verification script matched the wrong clause
    boundary and conflated the two; this pins the correct scoping.
    """
    note2 = _NOTE2_WITH_DEF.replace(
        "(六) 代管財產", "(六) 代管財產\n...\n(九) 經費支出\n...委託單位或...\n(十)"
    )
    assert clause_5_income_base(note2) is True


# --- _is_opening_year ---------------------------------------------------------
#
# 新店及人 110, 東湖 111, 板橋員工子女 111 each report a reserve missing one side
# entirely, and each is that 園's first extracted year; all three resolve the
# following year. Confusing "not opened yet" with "short" would flag three 園 for
# a timing gap in their opening year that the report itself couldn't have avoided.

def test_contract_starting_this_academic_year_is_opening_year():
    # 學年度 111 runs 111/8-112/7; a contract starting 111/9/30 falls inside it.
    assert is_opening_year("111年9月30日至115年7月31日", "111") is True


def test_contract_starting_earlier_year_is_not_opening_year():
    assert is_opening_year("108年8月1日至112年7月31日", "111") is False


def test_contract_straddling_academic_year_boundary_counts_as_opening():
    # 新樂 opened 111/2/1, which falls inside 學年度 110 (110/8-111/7).
    assert is_opening_year("111年2月1日至115年7月31日", "110") is True


def test_missing_contract_period_is_not_opening_year():
    assert is_opening_year(None, "111") is False
    assert is_opening_year("", "113") is False


# --- contract_covers_year ------------------------------------------------------
#
# 柏翠, 菁湖, 東湖, 文中, 翠中, 淡海 all had every 學年度 after their opening year
# wrongly flagged as "contract doesn't cover this year" because their
# contract_period carries a "（開園日）" annotation after the true end date.

from smart_watchdog.features.compliance import contract_covers_year


def test_opening_year_annotation_does_not_truncate_the_contract():
    """108-112 的契約不因附註裡多一個 109/2/1 開園日就在 109/2 提前結束。

    Reading the *last* date in the string as the end (an earlier version of this
    check did) picks up the annotation instead, making 110/111 学年度 report as
    "not covered" by a contract that in fact runs to 112/7/31.
    """
    period = "108年8月1日至112年7月31日（109年2月1日開園）"
    assert contract_covers_year(period, "110") is True
    assert contract_covers_year(period, "111") is True


def test_plain_period_without_annotation_still_works():
    assert contract_covers_year("108年8月1日至112年7月31日", "110") is True
    assert contract_covers_year("108年8月1日至112年7月31日", "113") is False


def test_too_few_dates_is_unconfirmed_not_false():
    assert contract_covers_year("民國108年", "110") is None
    assert contract_covers_year(None, "110") is None
