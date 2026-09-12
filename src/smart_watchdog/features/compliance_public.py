"""Compliance checks for the 22 市立幼兒園, from their 決算書.

``risk/priority.py`` states that 軌 B covers 60 園 -- 38 非營利 and 22 公立. It did
not: the rules in ``compliance.py`` read 附註二, and a 決算書 has no 附註二, so the
22 公立園 had extracted financials (63 園-年 × 26 fields, coordinate-parsed at zero
model cost) with no rule applied to them. This module closes that gap.

## The basis is different, and saying so matters

The 非營利 rules quote the report's own 附註二 -- the institution stated a policy
and the check asks whether the same report obeys it. A 決算書 states no policy. So
these rules can only ask about what the document itself reports: budget against
actual, headcount against establishment, this year's drawdown against the fund
that remains. They are **questions about execution**, not breaches of a stated
undertaking, and every ``rule_text`` here says so rather than borrowing the
authority of a clause it cannot cite.

## Why the identities are not rules

期末餘額 = 期初 + 本期賸餘 holds 56/56. 本期賸餘 = 基金來源 − 基金用途 holds
58/58. 公庫撥款 + 學雜費 ≤ 基金來源 holds 58/58. They are kept in
``integrity_checks`` as extraction guards -- a future failure means the parse
broke, not that a 園 did something -- but a check that has never failed and
cannot plausibly fail is not an audit finding.

## Base rates were computed before the thresholds were set

``CLAUDE.md``: 判定異常之前先算基準率. Of the candidates:

    本期賸餘為負                63.8%  rejected -- this is how these funds work,
                                      they draw down an accumulated balance
    員額缺額 ≥ 3 人             13.8%  kept
    學雜費執行率同年度 z ≤ −2    13.8%  kept
    資本支出執行率 < 50%         13.8%  kept
    資本支出 > 預算 1.5 倍        8.6%  kept
    期末餘額一年即耗竭             6.9%  kept
    公庫依存度 > 0.95            5.2%  kept

## 學雜費執行率 is compared within its own 年度, never against a fixed number

``CLAUDE.md`` records a city-wide decline: the median falls 0.908 → 0.820 → 0.780
across 112–114 (Wilcoxon p=0.026 in the source analysis). A fixed threshold would
clear almost every 園 in 112 and flag almost every 園 in 114 while measuring the
same behaviour. Comparing within the year spreads the hits evenly instead
(2/22, 3/19, 3/17), which is the point.

## 年度, not 學年度

公校 decisions are filed by 年度 (112/113/114); 非營利 reports by 學年度. They do
not align, and ``CLAUDE.md`` says so explicitly. Every row this module emits
carries ``year_kind="年度"`` so nothing downstream can join the two by accident.
"""

from __future__ import annotations

import dataclasses
import statistics

#: Robust-z cutoff for the year-relative tuition check. −2 is deliberately milder
#: than the 3.5 used for the anomaly ranking: here it selects a shortlist to ask
#: about, and the 決算書 gives no second source to confirm against.
TUITION_Z = -2.0
MAD_TO_SD = 1.4826

#: Thresholds, each chosen so the base rate above lands in the 5–15% band. They
#: are shortlist boundaries, not legal limits, and the wording of every finding
#: says which.
STAFF_GAP_MIN = 3
CAPEX_OVER = 1.5
CAPEX_UNDER = 0.5
GOV_DEPENDENCY_HIGH = 0.95


@dataclasses.dataclass
class PublicCheck:
    """One question about one 市立幼兒園's 決算 for one 年度."""

    code: str            # 分基金代號, e.g. 13601
    short_name: str
    academic_year: str   # 年度, NOT 學年度 -- see year_kind
    rule: str
    rule_text: str
    passed: bool | None
    detail: str
    severity: str
    year_kind: str = "年度"

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def _f(v: object) -> float | None:
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if x != x else x


def robust_z(value: float, peers: list[float]) -> float | None:
    xs = [p for p in peers if p is not None]
    if len(xs) < 6:
        return None
    med = statistics.median(xs)
    mad = statistics.median([abs(x - med) for x in xs])
    if mad <= 0:
        return None
    return (value - med) / (MAD_TO_SD * mad)


def integrity_checks(row: dict) -> list[str]:
    """Arithmetic the 決算書 must satisfy. A failure means the parse broke.

    Reported separately from findings: these have never failed across all 58
    園-年, so a failure is news about this pipeline, not about a 幼兒園.
    """
    problems: list[str] = []
    src, use = _f(row.get("fund_source_actual")), _f(row.get("fund_use_actual"))
    sur = _f(row.get("surplus_actual"))
    if None not in (src, use, sur) and abs(sur - (src - use)) > 1:
        problems.append(
            f"本期賸餘 {sur:,.0f} ≠ 基金來源 {src:,.0f} − 基金用途 {use:,.0f}")
    close, prior = _f(row.get("closing_balance_actual")), _f(row.get("closing_balance_prior"))
    if None not in (close, prior, sur) and abs(close - (prior + sur)) > 1:
        problems.append(
            f"期末餘額 {close:,.0f} ≠ 期初 {prior:,.0f} + 本期賸餘 {sur:,.0f}")
    gov, tui = _f(row.get("gov_transfer_actual")), _f(row.get("tuition_actual"))
    if None not in (gov, tui, src) and gov + tui > src + 1:
        problems.append(
            f"公庫撥款 {gov:,.0f} + 學雜費 {tui:,.0f} 超過基金來源合計 {src:,.0f}")
    return problems


def check_year(row: dict, cohort: list[dict]) -> list[PublicCheck]:
    """Every check for one 園-年度. ``cohort`` is the same 年度's other 園."""
    out: list[PublicCheck] = []
    code = str(row.get("fund_code", ""))
    name = str(row.get("name", ""))
    year = str(row.get("fiscal_year", ""))

    def add(rule: str, text: str, passed: bool | None, detail: str, sev: str) -> None:
        out.append(PublicCheck(code=code, short_name=name, academic_year=year,
                               rule=rule, rule_text=text, passed=passed,
                               detail=detail, severity=sev))

    # ── 員額：決算人數低於預算員額 ──────────────────────────────────
    text_staff = ("依決算書「員工人數彙計表」比對預算員額與決算人數。"
                  "缺額本身不是違規，但影響教保服務人員配置，"
                  "屬應向園所確認之事項。")
    sb, sa = _f(row.get("staff_budget")), _f(row.get("staff_actual"))
    if sb is None or sa is None:
        add("員額決算低於預算", text_staff, None, "缺預算員額或決算人數", "medium")
    elif sb - sa >= STAFF_GAP_MIN:
        add("員額決算低於預算", text_staff, False,
            f"預算員額 {sb:.0f} 人，決算 {sa:.0f} 人，缺額 {sb - sa:.0f} 人"
            f"（門檻 {STAFF_GAP_MIN} 人）。請確認缺額原因與教保人員配置是否符合規定",
            "high" if sb - sa >= 10 else "medium")
    else:
        add("員額決算低於預算", text_staff, True,
            f"預算員額 {sb:.0f} 人，決算 {sa:.0f} 人", "medium")

    # ── 學雜費執行率：與同年度同儕比較，不用絕對門檻 ────────────────
    text_tuition = ("學雜費決算／預算，與同一年度其他市立幼兒園比較。"
                    "全市執行率逐年下滑（112→114 中位 0.908→0.780），"
                    "故採年度相對比較；絕對門檻會在後段年度大量誤標。")
    te = _f(row.get("tuition_execution"))
    peers = [_f(r.get("tuition_execution")) for r in cohort]
    peers = [p for p in peers if p is not None]
    if te is None or len(peers) < 6:
        add("學雜費執行率同年度偏低", text_tuition, None,
            "缺學雜費預算或決算，或同年度可比園數不足", "medium")
    else:
        z = robust_z(te, peers)
        med = statistics.median(peers)
        if z is not None and z <= TUITION_Z:
            add("學雜費執行率同年度偏低", text_tuition, False,
                f"執行率 {te:.1%}，同年度中位 {med:.1%}（穩健 z={z:+.1f}）。"
                f"學雜費隨招收人數變動而公庫撥款不受影響，"
                f"請確認是否為招生不足，以及員額與空間是否已配合調整",
                "medium")
        else:
            add("學雜費執行率同年度偏低", text_tuition, True,
                f"執行率 {te:.1%}，同年度中位 {med:.1%}", "medium")

    # ── 資本支出執行 ────────────────────────────────────────────────
    text_capex = ("建築及設備計畫 決算／預算。大幅超支需有追加預算或動支程序，"
                  "大幅低執行則是編列後未執行，兩者都是可查證的程序問題。")
    ce = _f(row.get("capex_execution"))
    if ce is None:
        add("資本支出執行異常", text_capex, None, "無資本支出預算或決算", "low")
    elif ce > CAPEX_OVER:
        add("資本支出執行異常", text_capex, False,
            f"執行率 {ce:.0%}（決算 {_f(row.get('capex_actual')) or 0:,.0f}／"
            f"預算 {_f(row.get('capex_budget')) or 0:,.0f}），超出預算 "
            f"{(ce - 1) * 100:.0f}%。請確認追加預算或動支預備金之核准程序",
            "medium")
    elif ce < CAPEX_UNDER:
        add("資本支出執行異常", text_capex, False,
            f"執行率 {ce:.0%}，編列後大部分未執行。"
            f"請確認是否為工程延宕，以及是否影響園舍安全與設備汰換",
            "low")
    else:
        add("資本支出執行異常", text_capex, True, f"執行率 {ce:.0%}", "low")

    # ── 基金餘額耗竭 ────────────────────────────────────────────────
    # 本期賸餘為負出現在 63.8% 的園-年度，是這類基金的運作常態而非發現。
    # 值得問的是「照這個速度，基金還能撐多久」。
    text_fund = ("本期賸餘為負在市立幼兒園基金屬常態（全體 63.8%），不單獨列為發現。"
                 "此檢核問的是期末餘額是否已不足以再承受一個同樣的年度。")
    sur = _f(row.get("surplus_actual"))
    close = _f(row.get("closing_balance_actual"))
    if sur is None or close is None:
        add("基金餘額耗竭風險", text_fund, None, "缺本期賸餘或期末餘額", "medium")
    elif sur < 0 and close < -sur:
        add("基金餘額耗竭風險", text_fund, False,
            f"本期短絀 {-sur:,.0f}，期末餘額僅剩 {close:,.0f}，"
            f"不足以再承受一個同規模的年度。請確認次年度收支平衡措施",
            "high")
    else:
        state = (f"本期短絀 {-sur:,.0f}，期末餘額 {close:,.0f}（尚可承受）"
                 if sur < 0 else f"本期賸餘 {sur:,.0f}")
        add("基金餘額耗竭風險", text_fund, True, state, "medium")

    # ── 公庫依存度 ──────────────────────────────────────────────────
    text_gov = ("政府撥入／基金來源合計。市立幼兒園普遍落在 0.88 上下且標準差極小，"
                "偏離代表自籌財源結構與同儕不同，屬結構性事實，非違規。")
    gd = _f(row.get("gov_dependency"))
    if gd is None:
        add("公庫依存度偏高", text_gov, None, "缺公庫撥款或基金來源合計", "low")
    elif gd > GOV_DEPENDENCY_HIGH:
        add("公庫依存度偏高", text_gov, False,
            f"公庫依存度 {gd:.1%}（門檻 {GOV_DEPENDENCY_HIGH:.0%}），"
            f"自籌財源占比低於同儕。請確認招收人數與收費結構",
            "low")
    else:
        add("公庫依存度偏高", text_gov, True, f"公庫依存度 {gd:.1%}", "low")

    return out


def check_all(rows: list[dict]) -> tuple[list[PublicCheck], list[str]]:
    """Run every check over the whole 公校 panel, grouped into 年度 cohorts."""
    by_year: dict[str, list[dict]] = {}
    for r in rows:
        by_year.setdefault(str(r.get("fiscal_year", "")), []).append(r)

    checks: list[PublicCheck] = []
    integrity: list[str] = []
    for year, cohort in sorted(by_year.items()):
        for r in cohort:
            problems = integrity_checks(r)
            integrity.extend(
                f"{year} {r.get('name', '')}：{p}" for p in problems)
            checks.extend(check_year(r, cohort))
    return checks, integrity
