"""跨來源查核的不變性。

這些不是 does-it-run 測試。跨來源查核比單一文件的檢核更容易講出錯話，因為它把
三個獨立來源的數字放在一起，任一個對齊錯了都會產生看起來很像發現的假陳述。
以下釘住的是四個「錯了會對真實機構做出錯誤財務陳述」的性質：

1. **三態不得塌成二態。** 沒有收費明細就是「資料不足」，不是「通過」也不是
   「未通過」。111 學年度起非營利園收費明細全市歸零，若這時回報通過，
   等於宣稱查過了。
2. **部分年度的報告不得與全年收費表相比。** 新樂 110 只營運 6 個月，
   比值 0.455 是期間造成的，不是收費造成的。
3. **年度與學年度不可混。** 公校用年度、非營利用學年度，每一列都必須帶
   `year_kind`，否則下游會 join 錯。
4. **公校年基準必須是兩個半年額之和。** 年度 N 橫跨學年度 N−1 下學期與
   學年度 N 上學期；只取一邊會讓推估人數差一倍。
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.features.crosscheck import (
    CAPACITY_GAP_TOL,
    FULL_YEAR_MONTHS,
    STAFF_RATIO_LEGAL,
    check_nonprofit,
    check_public,
    cohort_findings,
    nonprofit_guards,
    nonprofit_metrics,
    parse_fee_split,
    period_months,
    public_fee_basis,
    public_guards,
    public_metrics,
)

FULL_PERIOD = "民國113年8月1日至114年7月31日"


def report(**over) -> dict:
    base = {
        "code": "N01", "short_name": "安溪", "academic_year": "113",
        "tuition_income": 9_000_000.0,
        "material_cost": 1_800_000.0,
        "approved_capacity": 90.0,
        "actual_enrolment": 90.0,
        "educators": 9.0,
        "income_period": FULL_PERIOD,
        "registry_indoor_area": 684.3,
        "registry_monthly": None,
        "fee_split": None,
    }
    base.update(over)
    return base


def metrics(fee=100_000.0, caps=(90.0,), **over) -> dict:
    return nonprofit_metrics(report(**over), fee, list(caps))


def rule_of(checks, rule):
    hits = [c for c in checks if c.rule == rule]
    assert len(hits) == 1, f"{rule} 應恰好一項，實得 {len(hits)}"
    return hits[0]


# ── 1. 三態不得塌成二態 ──────────────────────────────────────────────

def test_missing_fee_schedule_is_insufficient_not_pass() -> None:
    """沒有收費明細時，恆等式檢核必須是 None，且不得暗示已查核。"""
    c = rule_of(check_nonprofit(metrics(fee=None), {}), "教保費收入 = 收費 × 人數")
    assert c.passed is None
    assert "資料不足" in c.detail
    assert "未申報" in c.detail  # 明說「非該園未申報」


def test_missing_operand_is_insufficient() -> None:
    for field in ("tuition_income", "actual_enrolment"):
        c = rule_of(check_nonprofit(metrics(**{field: None}), {}),
                    "教保費收入 = 收費 × 人數")
        assert c.passed is None, field


def test_identity_holds_within_tolerance() -> None:
    m = metrics(fee=100_000.0, tuition_income=9_000_000.0, actual_enrolment=90.0)
    assert m["fee_identity_ratio"] == 1.0
    assert rule_of(check_nonprofit(m, {}), "教保費收入 = 收費 × 人數").passed is True


def test_identity_breach_is_reported_with_the_arithmetic() -> None:
    m = metrics(fee=100_000.0, tuition_income=4_500_000.0, actual_enrolment=90.0)
    c = rule_of(check_nonprofit(m, {}), "教保費收入 = 收費 × 人數")
    assert c.passed is False
    assert "4,500,000" in c.detail and "9,000,000" in c.detail  # 兩邊都要看得到
    assert c.severity == "high"


# ── 2. 部分年度的報告不得與全年收費表相比 ────────────────────────────

def test_period_months_parses_both_notations() -> None:
    assert period_months("民國113年8月1日至114年7月31日") == 12
    assert period_months("110.8.1~111.7.31") == 12
    assert period_months("民國111年2月1日至111年7月31日") == 6
    # 同一份報告兩種寫法並列時只取前兩個日期，不能被註解裡的日期帶偏。
    assert period_months(
        "民國110年8月1日至111年7月31日 (110.8.1~111.7.31)") == 12
    assert period_months("") is None
    assert period_months("110年8月") is None


def test_partial_year_report_is_held_not_failed() -> None:
    """新樂 110 的實況：6 個月、比值 0.455。這是期間造成的，不是收費造成的。"""
    m = metrics(fee=110_451.0, tuition_income=10_296_228.0, actual_enrolment=205.0,
                income_period="民國111年2月1日至111年7月31日")
    assert m["period_months"] == 6
    assert m["full_year"] is False
    assert m["fee_identity_ratio"] < 0.5
    c = rule_of(check_nonprofit(m, {}), "教保費收入 = 收費 × 人數")
    assert c.passed is None, "部分年度不得判為未通過"
    assert "6 個月" in c.detail


def test_partial_year_report_excluded_from_its_own_peer_comparison() -> None:
    m = metrics(income_period="民國111年2月1日至111年7月31日")
    peers = {"implied_fee_per_child": [100_000.0] * 20}
    c = rule_of(check_nonprofit(m, peers), "隱含收費年額 同儕偏離")
    assert c.passed is None


def test_annualised_value_only_exists_for_partial_reports() -> None:
    full = metrics()
    part = metrics(income_period="民國111年2月1日至111年7月31日")
    assert full["implied_fee_annualised"] == full["implied_fee_per_child"]
    assert part["implied_fee_annualised"] == part["implied_fee_per_child"] * 2


# ── 3. 年度與學年度不可混 ────────────────────────────────────────────

def test_every_row_carries_year_kind_and_entity_type() -> None:
    npo = check_nonprofit(metrics(), {})
    pub = check_public(
        public_metrics({"fund_code": 13601, "name": "新北市立板橋幼兒園",
                        "fiscal_year": 113, "tuition_actual": 4_161_500.0,
                        "tuition_budget": 4_930_000.0,
                        "gov_transfer_actual": 29_251_497.0,
                        "fund_use_actual": 32_087_165.0},
                       {("新北市立板橋幼兒園", 112): 7675.0,
                        ("新北市立板橋幼兒園", 113): 7675.0},
                       {"新北市立板橋幼兒園": 312.0}, {"新北市立板橋幼兒園": 3}),
        {})
    assert {c.year_kind for c in npo} == {"學年度"}
    assert {c.entity_type for c in npo} == {"非營利"}
    assert {c.year_kind for c in pub} == {"年度"}
    assert {c.entity_type for c in pub} == {"公立"}


KNOWN_SOURCES = ("收支餘絀表", "財報附註一", "收費明細", "園所基本資料",
                 "決算書", "同儕")


def test_every_row_names_a_real_source() -> None:
    """每一項都要說得出「去翻哪一份東西」，否則稽查員無從覆核。"""
    for c in (*check_nonprofit(metrics(), {}),):
        assert c.sources, c.rule
        assert any(s in c.sources for s in KNOWN_SOURCES), c.sources


# ── 4. 公校年基準必須是兩個半年額之和 ────────────────────────────────

def test_public_fee_basis_spans_two_academic_years() -> None:
    half = {("甲園", 112): 7675.0, ("甲園", 113): 7675.0}
    amount, note = public_fee_basis(half, "甲園", 113)
    assert amount == 15350.0
    assert "112 下學期" in note and "113 上學期" in note


def test_public_fee_basis_falls_back_and_says_so() -> None:
    amount, note = public_fee_basis({("甲園", 113): 7675.0}, "甲園", 113)
    assert amount == 15350.0
    assert "推估" in note
    amount, note = public_fee_basis({}, "甲園", 113)
    assert amount is None
    assert "查無" in note


def test_public_implied_enrolment_uses_the_annual_basis() -> None:
    m = public_metrics(
        {"fund_code": 13601, "name": "板橋", "fiscal_year": 113,
         "tuition_actual": 4_161_500.0, "tuition_budget": 4_930_000.0,
         "gov_transfer_actual": 29_251_497.0, "fund_use_actual": 32_087_165.0},
        {("板橋", 112): 7675.0, ("板橋", 113): 7675.0},
        {"板橋": 312.0}, {"板橋": 3}, capacity_retrieved_on="2026-08-10")
    assert round(m["implied_enrolment"], 1) == 271.1
    assert round(m["occupancy"], 3) == 0.869
    assert round(m["gov_per_child"]) == 107_896
    assert "2026-08-10" in m["capacity_basis"], "分母是快照，必須在列上說出來"


def test_public_guard_catches_a_wrong_fee_basis() -> None:
    """預算隱含人數不得超過核定人數。超過代表基準或分班聚合錯了。"""
    good = public_metrics(
        {"fund_code": 1, "name": "甲", "fiscal_year": 113,
         "tuition_actual": 100.0, "tuition_budget": 1_534_000.0},
        {("甲", 112): 7675.0, ("甲", 113): 7675.0}, {"甲": 100.0}, {"甲": 1})
    assert public_guards([good]) == []
    # 半年額誤當年額 → 隱含人數翻倍 → 護欄必須抓到
    bad = public_metrics(
        {"fund_code": 1, "name": "甲", "fiscal_year": 113,
         "tuition_actual": 100.0, "tuition_budget": 1_534_000.0},
        {("甲", 113): 3837.5}, {"甲": 100.0}, {"甲": 1})
    assert len(public_guards([bad])) == 1


# ── 其他不變性 ───────────────────────────────────────────────────────

def test_capacity_gap_tolerance_and_direction() -> None:
    within = metrics(caps=(90.0 + CAPACITY_GAP_TOL,))
    assert rule_of(check_nonprofit(within, {}),
                   "核定人數 財報=登記").passed is True
    beyond = metrics(caps=(484.0,), approved_capacity=176.0)
    c = rule_of(check_nonprofit(beyond, {}), "核定人數 財報=登記")
    assert c.passed is False
    assert c.severity == "high", "差 308 人應為高嚴重度"
    assert "㎡/人" in c.detail, "應附上樓地板面積旁證，供判斷哪一邊可信"


def test_staff_ratio_never_asserts_a_breach_below_the_legal_cap() -> None:
    at_cap = metrics(actual_enrolment=60.0, educators=4.0)
    c = rule_of(check_nonprofit(at_cap, {}), "師生比貼齊法定上限")
    assert at_cap["staff_ratio"] == STAFF_RATIO_LEGAL
    assert c.passed is False and c.severity == "medium"
    assert "超過" not in c.detail, "恰好等於上限不是超過"

    over = metrics(actual_enrolment=64.0, educators=4.0)
    assert rule_of(check_nonprofit(over, {}), "師生比貼齊法定上限").severity == "high"


def test_peer_check_needs_enough_peers() -> None:
    m = metrics()
    thin = {"implied_fee_per_child": [100_000.0] * 3}
    assert rule_of(check_nonprofit(m, thin), "隱含收費年額 同儕偏離").passed is None


def test_cohort_finding_reports_the_coverage_collapse() -> None:
    coverage = {"110": (28, 52), "111": (0, 52), "112": (0, 52)}
    out = cohort_findings([], [], coverage)
    hit = [c for c in out if c.rule == "非營利園收費明細在外部來源停止公開"]
    assert len(hit) == 1
    assert hit[0].code == "COHORT", "母體層級的發現不得掛到某一所園"
    assert hit[0].passed is False
    assert "不得據此認定違反幼照法第 38 條" in hit[0].detail


def test_coverage_finding_says_the_note_still_covers_the_aggregate() -> None:
    """缺口的範圍必須寫對：缺的是費目層級，不是家長／政府拆分。

    第一版寫成「無法確認家長負擔與政府補助各佔多少」，而附註三就有。
    這個測試釘住修正後的措辭，避免又退回去。
    """
    rows = [metrics(fee_split={"parent": 100.0, "gov": 900.0}) for _ in range(5)]
    hit = [c for c in cohort_findings(rows, [], {"111": (0, 52)})
           if c.rule == "非營利園收費明細在外部來源停止公開"]
    assert len(hit) == 1
    d = hit[0].detail
    assert "逐項" in d, "要說清楚缺的是費目層級"
    assert "附註三" in d and f"{len(rows)}/{len(rows)}" in d


def test_cohort_finding_absent_when_coverage_is_complete() -> None:
    assert cohort_findings([], [], {"110": (52, 52), "111": (52, 52)}) == []


def test_full_year_constant_is_twelve() -> None:
    # 幾個 detail 字串直接寫了「12 個月」，改常數就要改字串。
    assert FULL_YEAR_MONTHS == 12


# ── 5. 附註三的家長／政府拆分：只能取本學年度那一欄 ───────────────────

def fact(code, year, label, period, value, heading="(四) 收支明細表 1. 教保費收入"):
    return {"code": code, "academic_year": year, "context_heading": heading,
            "table_title": "", "item_label": label, "period_label": period,
            "value": value}


def test_fee_split_takes_the_reports_own_year_column() -> None:
    rows = [
        fact("N01", "111", "教保費收入（家長繳費）", "111.8.1~112.7.31", "2152116"),
        fact("N01", "111", "教保費收入（家長繳費）", "110.8.1~111.7.31", "1940500"),
        fact("N01", "111", "教保費收入（政府學費差額補助）", "111.8.1~112.7.31",
             "14185738"),
        fact("N01", "111", "教保費收入（政府學費差額補助）", "110.8.1~111.7.31",
             "8128760"),
        fact("N01", "111", "小　計", "111.8.1~112.7.31", "16337854"),
        fact("N01", "111", "小　計", "110.8.1~111.7.31", "10069260"),
    ]
    got = parse_fee_split(rows)[("N01", "111")]
    assert got == {"parent": 2152116.0, "gov": 14185738.0, "subtotal": 16337854.0}


def test_fee_split_rejects_an_opening_period_that_shares_the_year_number() -> None:
    """新樂 111 的實況：比較欄是 111.2.1~111.7.31（開辦半年），也以 111 開頭。

    只比年份會挑到比較欄，把上一期的金額當本期。這個錯誤是被
    「家長+政府 = 收支餘絀表教保費收入」的對帳抓出來的（119/121 → 115/115）。
    """
    rows = [
        fact("N28", "111", "教保費收入（家長繳費）", "111.2.1~111.7.31", "2371500"),
        fact("N28", "111", "教保費收入（政府學費差額補助）", "111.2.1~111.7.31",
             "7924728"),
    ]
    assert parse_fee_split(rows) == {}, "開辦期比較欄不得被當成本期"


def test_fee_split_keeps_the_first_subtotal_not_the_net() -> None:
    """表下半段還有退費小計與淨額，拿淨額對帳會永遠不符。"""
    rows = [
        fact("N01", "113", "教保費收入（家長繳費）", "113.8.1~114.7.31", "100"),
        fact("N01", "113", "教保費收入（政府學費差額補助）", "113.8.1~114.7.31", "900"),
        fact("N01", "113", "小　計", "113.8.1~114.7.31", "1000"),
        fact("N01", "113", "五日未上課退費", "113.8.1~114.7.31", "-50"),
        fact("N01", "113", "小　計", "113.8.1~114.7.31", "-50"),
    ]
    assert parse_fee_split(rows)[("N01", "113")]["subtotal"] == 1000.0


def test_fee_split_drops_a_one_sided_row() -> None:
    rows = [fact("N01", "113", "教保費收入（家長繳費）", "113.8.1~114.7.31", "100")]
    assert parse_fee_split(rows) == {}


def test_split_reconciliation_is_a_guard_not_a_finding() -> None:
    """家長+政府 必須等於收支餘絀表的教保費收入；不符是抽取錯，不是園的問題。"""
    ok = metrics(tuition_income=1000.0,
                 fee_split={"parent": 100.0, "gov": 900.0, "subtotal": 1000.0})
    assert nonprofit_guards([ok]) == []
    bad = metrics(tuition_income=2000.0,
                  fee_split={"parent": 100.0, "gov": 900.0, "subtotal": 1000.0})
    problems = nonprofit_guards([bad])
    assert len(problems) == 1 and "收支餘絀表教保費收入" in problems[0]
    # 而且不得產生任何一項「未通過」的發現
    assert all(c.passed is not False or c.rule != "家長月均實繳 ≤ 登記月費"
               for c in check_nonprofit(bad, {}))


def test_parent_monthly_check_direction() -> None:
    """減免會把平均拉低，所以高於登記月費才是要問的方向。"""
    under = metrics(actual_enrolment=100.0, registry_monthly=2000.0,
                    fee_split={"parent": 1_800_000.0, "gov": 8_000_000.0})
    c = rule_of(check_nonprofit(under, {}), "家長月均實繳 ≤ 登記月費")
    assert round(under["parent_monthly_avg"]) == 1500
    assert c.passed is True

    over = metrics(actual_enrolment=100.0, registry_monthly=2000.0,
                   fee_split={"parent": 2_712_000.0, "gov": 8_000_000.0})
    c = rule_of(check_nonprofit(over, {}), "家長月均實繳 ≤ 登記月費")
    assert c.passed is False
    assert "1.13 倍" in c.detail
    assert "分母偏小" in c.detail, "要說出人數變動會推高平均這個替代解釋"


def test_parent_share_check_is_insufficient_without_the_note() -> None:
    c = rule_of(check_nonprofit(metrics(), {}), "家長分攤比率 同儕偏離")
    assert c.passed is None
    assert "附註三" in c.detail


def test_parent_share_is_computed_from_the_split_not_the_statement() -> None:
    m = metrics(tuition_income=9_999_999.0,
                fee_split={"parent": 200.0, "gov": 800.0})
    assert m["parent_share"] == 0.2
    assert m["fee_split_total"] == 1000.0


def test_cohort_cost_finding_reports_who_bears_the_increase() -> None:
    """成本上升時，必須說出家長端與政府端各自的方向，不能停在總數。"""
    rows = []
    for year, total, parent in (("110", 100_000.0, 21_000.0),
                                ("113", 135_000.0, 15_000.0)):
        for i in range(10):
            m = metrics(actual_enrolment=100.0,
                        tuition_income=total * 100,
                        fee_split={"parent": parent * 100,
                                   "gov": (total - parent) * 100})
            m["year"] = year
            m["short_name"] = f"園{i}"
            rows.append(m)
    hit = [c for c in cohort_findings(rows, [], {})
           if c.rule == "非營利園每生營運成本上升，增幅由政府端承擔"]
    assert len(hit) == 1
    d = hit[0].detail
    assert "每生家長繳費" in d and "每生政府學費差額補助" in d and "家長分攤比率" in d
    assert "家長每生負擔下降" in d
