"""The 附註五 disclosure check must measure materiality, not exact equality.

附註五 discloses what is owed to the 受託法人 at year end. The income statement
shows the whole year's 行政管理費. The two differ by whatever was settled in cash
during the year, so they are *expected* to diverge slightly -- a relationship
``EXTRACTION_GUIDE.md`` already records as normal.

The check originally compared them with a one-dollar absolute tolerance. Across
the 127 園-年 that report both figures the median gap is 0.00%, and the gaps that
exceeded a dollar fell into two populations with an empty band between them:

    genuine      +69.9% … +140.1%   (four reports)
    settlement    −7.2% …   −0.5%   (thirteen, ten of them inside ±3%)

So the absolute tolerance reported N25 碧城 111 -- half a percent from exact --
as a compliance failure against a real 幼兒園, and inflated 113 學年度's failure
count from 4 to 15. These tests pin the threshold's *shape*: relative, and wide
enough that routine settlement is not a finding.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.features.compliance import NOTE5_MATERIALITY, check_report

RULE = "附註五揭露 = 年末應付受託法人餘額"


def payload(disclosed: float, payable: float, admin: float | None = None) -> dict:
    """Minimal report carrying only what the 附註五 check reads."""
    return {
        "code": "N99", "short_name": "測試", "academic_year": "113",
        "balance_sheet": {},
        "income_statement": {"lines": (
            [{"label": "行政管理費", "budget": admin, "actual": admin}]
            if admin is not None else []
        )},
        "note_1": {}, "note_5": {"admin_fee_disclosed": disclosed,
                                 "payable_to_operator": payable},
    }


def note5(report: dict):
    return next(c for c in check_report(report) if c.rule == RULE)


def test_exact_match_passes() -> None:
    assert note5(payload(290580, 290580)).passed is True


def test_half_a_percent_is_not_a_finding() -> None:
    """N25 碧城 111: disclosed 243,980 against a payable of 242,872."""
    check = note5(payload(243980, 242872))
    assert check.passed is True, "0.5% 的差額不得判為違規"
    assert "未達重大性門檻" in check.detail


def test_the_113_settlement_cluster_passes() -> None:
    """Every report the old tolerance wrongly flagged, at its real magnitude."""
    cluster = [
        (220215, 215691),   # N02 山北    -2.1%
        (179804, 175663),   # N03 龍埔成長 -2.3%
        (197754, 192692),   # N05 漢翔    -2.6%
        (277604, 274883),   # N06 昌平    -1.0%
        (282955, 278623),   # N14 積穗    -1.5%
        (290580, 285622),   # N17 中正    -1.7%
        (208236, 193331),   # N21 文創    -7.2%
        (400000, 376093),   # N28 新樂    -6.0%
        (341798, 335455),   # N33 淡海    -1.9%
    ]
    for disclosed, payable in cluster:
        assert note5(payload(disclosed, payable)).passed is True, (
            f"揭露 {disclosed} / 應付 {payable} 屬年度內現金結算，不應判為違規")


def test_the_four_real_outliers_still_fail() -> None:
    """The reports the rule exists to find: a payable far above the disclosure."""
    outliers = [
        (124680, 299357),   # N01 安溪 110     +140.1%
        (244855, 500518),   # N26 新店及人 111 +104.4%
        (226890, 471745),   # N26 新店及人 112 +107.9%
        (214418, 364393),   # N13 三多 113      +69.9%
    ]
    for disclosed, payable in outliers:
        assert note5(payload(disclosed, payable)).passed is False, (
            f"揭露 {disclosed} / 應付 {payable} 是真正的離群，必須保留")


def test_threshold_is_relative_not_absolute() -> None:
    """A large 園 and a small 園 must be judged on the same proportion."""
    small = note5(payload(10_000, 10_000 + 10_000 * 0.05))
    large = note5(payload(10_000_000, 10_000_000 + 10_000_000 * 0.05))
    assert small.passed is large.passed is True
    # ...and both fail at the same proportion, well past the threshold.
    assert note5(payload(10_000, 10_000 * 2)).passed is False
    assert note5(payload(10_000_000, 10_000_000 * 2)).passed is False


def test_detail_reports_the_percentage() -> None:
    """An inspector reading the finding needs the magnitude, not just the delta."""
    assert "%" in note5(payload(214418, 364393)).detail


def test_threshold_sits_in_the_empty_band() -> None:
    """10% is not balanced on a knife edge: the data has nothing between 8% and 69%."""
    assert 0.08 <= NOTE5_MATERIALITY <= 0.50
