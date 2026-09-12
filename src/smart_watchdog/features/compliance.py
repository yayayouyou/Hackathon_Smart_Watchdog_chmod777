"""Compliance checks against the accounting policy each report states about itself.

This replaces the invented-ratio approach in ``extract/forensic.py``, which was
tested on a matched 9-vs-9 cohort and failed: 0 of 12 signals reached p<0.05 and
direction matched prediction on only 5 of 12 (see docs/research/05-phase1-results.md
§10). The problem was that those ratios were our guesses about what misconduct
looks like.

Every 非營利園 report prints 附註二「重大會計政策之彙總說明」, and that note contains
explicit, numeric, mechanically checkable rules -- quoted here from N01 安溪
113 學年度, p.11-12, and worded near-identically across the corpus:

    (四) 資遣費準備金
        每年最多提撥全園專任人員月薪總額之 10%，應以專戶或定期存款方式儲存。
        以專戶或定期存款方式儲存之資遣費準備金，帳列非流動資產項下，
        **基金數額應與非流動負債數額相符**。

    (五) 業務發展準備金
        非營利幼兒園**未發生虧損之年度**，於年度結算後三個月內…報經委託單位或
        直轄市、縣（市）主管機關**同意後**，**至多提列收入總額之百分之二十**為
        業務發展準備金；並應於同意後一個月內，以專戶或定期存款方式儲存。

    (六) 代管財產  應付代管財產餘額…應與非流動資產項下之代管財產數額相符。
    (七) 購置財產  應付購置財產餘額…應與非流動資產項下之購置財產數額相符。
    (八) 經費收入  所有收入…應以收入總額入帳，**不得以收支相抵後淨額入帳**。
    (十) 1.(3) **人事費不得流出。**

    (一) 非營利幼兒園之會計基礎
        …但非屬所得稅正常繳納期間、不屬非營利幼兒園營運所得或**未提撥足額業務
        發展準備金衍生之所得稅，由非營利法人自行繳納，不得以非營利幼兒園營運
        成本支應。**

Note (一) matters for how a reserve shortfall is reported. The policy *contemplates*
under-funding and attaches a consequence -- the tax arising from it falls on the
法人, not on the 園's operating cost -- rather than prohibiting it outright. So the
finding for a shortfall is "confirm who bore the resulting tax", which is a
question with a paper trail, not a bare accusation. Reading it as a flat violation
would overstate what the document says.

Why this is a better basis than our ratios:

* **Citable.** A finding quotes the institution's own filed accounting policy, not
  our threshold. The inspector can point at page 11.
* **Mechanical.** Four asset-equals-liability identities and two numeric caps.
* **Legally grounded.** The note itself cites 非營利幼兒園實施辦法.
* **No peer group needed.** Compliance is absolute, so it works on a single report
  and does not need the cohort-year normalisation that ratio comparison does.

A failed check is a **question to ask**, not a finding of wrongdoing. Reserve
timing differences and approved exceptions both exist, so every result carries the
rule text and the arithmetic for a human to judge.
"""

from __future__ import annotations

import dataclasses
import re

# 附註二(五): the 業務發展準備金 ceiling, as a share of total income.
DEVELOPMENT_RESERVE_CAP = 0.20
# 附註二(四): the 資遣費準備金 ceiling, as a share of annual full-time payroll.
SEVERANCE_RESERVE_CAP = 0.10
# Reserves are whole-NTD; anything above this is a real difference, not rounding.
TOL = 1.0

#: Relative materiality for 附註五's disclosed amount vs the year-end payable.
#: These two figures legitimately differ by whatever was settled in cash during
#: the year, so an absolute tolerance reports routine settlement as a breach.
#: See the note at the 附註五 check for the distribution this was chosen from.
NOTE5_MATERIALITY = 0.10


@dataclasses.dataclass
class Check:
    """One compliance question about one 園-year."""

    code: str
    short_name: str
    academic_year: str
    rule: str
    rule_text: str
    passed: bool | None  # None = 資料不足，無法判斷
    detail: str
    severity: str  # high | medium | low

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def _f(value: object) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _line(lines: list[dict], label: str) -> dict | None:
    for ln in lines:
        if label in "".join(str(ln.get("label", "")).split()):
            return ln
    return None


# 附註二 is a template revised clause-by-clause, not as a whole document: full-text
# comparison across 42 reports found (五)'s "收入總額" definition present in every
# 110/111 report (42/42) and (九) already switched to "委託單位或" wording in 111
# while (五) had not -- so a report's academic_year alone does not tell you which
# wording (五) uses; only that report's own note_2 does, and 113 has not been
# re-extracted with note_2 yet.
def clause_5_income_base(note2_text: str | None) -> bool | None:
    """Whether this report's own 附註二(五) defines "收入總額" via a parenthetical.

    Returns ``None`` when the report has no ``note_2.text`` at all -- the note_2
    field was added after 43 reports were already extracted, so for those (and for
    every 113 report so far) the wording is genuinely unconfirmed, not "no".
    Asserting "the note doesn't define this" from academic_year alone was wrong
    for every 110/111 report it would have been applied to.
    """
    if not note2_text:
        return None
    m = re.search(r"[（(]五[)）](.*?)[（(]六[)）]", note2_text, re.S)
    if not m:
        return None
    return "家長繳交之費用" in m.group(1)


# Some agents append a helpful annotation after the real end date, e.g.
# "108年8月1日至112年7月31日（109年2月1日開園）". A regex that just grabs every
# date in the string and reads dates[0]/dates[-1] as start/end takes the
# annotation's date as the "end", making a contract that actually runs to
# 112/7/31 look like it stopped in 109/2 -- and then every later 學年度 report
# from that 園 fails a coverage check it should pass
# (scripts/verify_extraction_identity.py once had exactly this bug, caught by a
# sudden run of "contract doesn't cover this year" flags on otherwise-normal
# 園). The real start and end are always the first two dates; anything after is
# annotation, so callers here only ever read dates[0] or dates[0:2].
_CONTRACT_DATE_RE = re.compile(r"(\d{2,3})年(\d{1,2})月(?:(\d{1,2})日)?")


def _contract_dates(contract_period: object) -> list[tuple[int, int]]:
    return [
        (int(y), int(m))
        for y, m, _d in _CONTRACT_DATE_RE.findall(
            "".join(str(contract_period or "").split())
        )
    ]


def academic_year_span(
    academic_year: object,
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """(start, end) of 學年度 N as (year, month) pairs: N/8 through (N+1)/7."""
    try:
        ay = int(academic_year)
    except (TypeError, ValueError):
        return None
    return (ay, 8), (ay + 1, 7)


def contract_covers_year(contract_period: object, academic_year: object) -> bool | None:
    """Whether a 受託辦理期間 string overlaps the given 學年度's date range at all.

    Overlap, not full containment, is the right test: a 園's first and last
    report legitimately cover only part of a 學年度 -- 新樂 opened 111/2/1,
    which falls inside 學年度 110 (110/8/1-111/7/31) even though its start year
    (111) is numerically after 110, so comparing bare ROC year numbers instead
    of full (year, month) dates would wrongly flag a 園's own opening-year
    report as not covered.

    Returns ``None`` when the string has fewer than two dates to compare.
    """
    span = academic_year_span(academic_year)
    dates = _contract_dates(contract_period)
    if span is None or len(dates) < 2:
        return None
    ay_start, ay_end = span
    start, end = dates[0], dates[1]
    return start <= ay_end and end >= ay_start


def is_opening_year(contract_period: object, academic_year: str) -> bool:
    """Whether this report's own contract began during its own 學年度.

    All three known cases of a reserve missing one side entirely -- 新店及人 110,
    東湖 111, 板橋員工子女 111 -- are each that 園's first extracted year, and
    each resolves on its own the following year (東湖 113 books 343,928 on both
    sides; 板橋員工子女 113 books 278,849). 附註二 doesn't say when a newly
    contracted 園 must open its 專戶, so a missing side in an opening year reads
    as "not opened yet", not as a shortfall -- the same distinction 附註二(一)
    already draws for under-funding versus outright violation.
    """
    span = academic_year_span(academic_year)
    dates = _contract_dates(contract_period)
    if span is None or not dates:
        return False
    ay_start, ay_end = span
    return ay_start <= dates[0] <= ay_end


def check_report(payload: dict) -> list[Check]:
    """Run every compliance check derivable from the extracted fields."""
    code = payload.get("code", "")
    short = payload.get("short_name", "")
    year = str(payload.get("academic_year", ""))
    bs = payload.get("balance_sheet") or {}
    inc = payload.get("income_statement") or {}
    lines = inc.get("lines") or []

    opening = is_opening_year(
        (payload.get("note_1") or {}).get("contract_period"), year
    )

    out: list[Check] = []

    def add(
        rule: str, rule_text: str, passed: bool | None, detail: str, severity: str
    ) -> None:
        out.append(
            Check(
                code=code, short_name=short, academic_year=year,
                rule=rule, rule_text=rule_text,
                passed=passed, detail=detail, severity=severity,
            )
        )

    # --- 附註二(四)：資遣費準備金 基金數額應與非流動負債數額相符 -------------
    sa, sl = _f(bs.get("severance_asset")), _f(bs.get("severance_liability"))
    text_4 = (
        "附註二(四)：以專戶或定期存款方式儲存之資遣費準備金，帳列非流動資產項下，"
        "基金數額應與非流動負債數額相符。"
    )
    if sa is None and sl is None:
        add("資遣費準備金 資產=負債", text_4, None, "兩側皆未列示", "medium")
    elif sa is None or sl is None:
        present = f"負債 {sl:,.0f}" if sa is None else f"資產 {sa:,.0f}"
        if opening:
            add(
                "資遣費準備金 資產=負債", text_4, None,
                f"僅單側列示（{present}）——本學年度為開辦首年，"
                f"專戶可能尚未開立，非短撥；請追次學年度是否補列",
                "medium",
            )
        else:
            add(
                "資遣費準備金 資產=負債", text_4, False,
                f"僅單側列示（{present}），另一側科目不存在——附註要求兩側相符",
                "medium",
            )
    else:
        gap = sl - sa
        add(
            "資遣費準備金 資產=負債", text_4, abs(gap) <= TOL,
            f"資產 {sa:,.0f} vs 負債 {sl:,.0f}，差額 {gap:+,.0f}",
            "medium",
        )

    # --- 附註二(五)：業務發展準備金 三個條件 --------------------------------
    ra, rl = _f(bs.get("reserve_asset")), _f(bs.get("reserve_liability"))
    text_5_store = (
        "附註二(五)：應於同意後一個月內，以專戶或定期存款方式儲存"
        "（帳列非流動資產，與非流動負債相符）。"
    )
    if ra is None and rl is None:
        add("業務發展準備金 資產=負債", text_5_store, None, "兩側皆未列示", "medium")
    elif ra is None or rl is None:
        present = f"負債 {rl:,.0f}" if ra is None else f"資產 {ra:,.0f}"
        if opening:
            add(
                "業務發展準備金 資產=負債", text_5_store, None,
                f"僅單側列示（{present}）——本學年度為開辦首年，"
                f"專戶可能尚未開立，非短撥；請追次學年度是否補列",
                "medium",
            )
        else:
            add(
                "業務發展準備金 資產=負債", text_5_store, False,
                f"僅單側列示（{present}）——提列了準備但未見對應專戶資產", "high",
            )
    else:
        gap = rl - ra
        if abs(gap) <= TOL:
            add(
                "業務發展準備金 資產=負債", text_5_store, True,
                f"資產 {ra:,.0f} = 負債 {rl:,.0f}", "high",
            )
        else:
            # 附註二(一) contemplates under-funding rather than forbidding it, and
            # attaches a specific consequence: the income tax arising from the
            # shortfall must be borne by the 法人, not charged to the 園's
            # operating cost. So the finding is not "violation" but "confirm who
            # paid the resulting tax" -- a question with a paper trail.
            tax = _f((_line(lines, "所得稅") or {}).get("actual"))
            tax_note = (
                f"本期所得稅費用 {tax:,.0f}" if tax else "本期未列所得稅費用"
            )
            add(
                "業務發展準備金 資產=負債", text_5_store, False,
                f"資產 {ra:,.0f} vs 負債 {rl:,.0f}，未提撥足額 {gap:+,.0f}。"
                f"依附註二(一)，未提撥足額衍生之所得稅應由非營利法人自行繳納，"
                f"不得以園營運成本支應（{tax_note}）——請確認負擔主體",
                "high",
            )

    transfer = _f((_line(lines, "業務發展費") or {}).get("actual"))
    revenue = _f((_line(lines, "收入合計") or {}).get("actual"))
    surplus = _f(bs.get("current_surplus"))

    # 未發生虧損之年度 才得提列。
    # The transfer is itself an expense, so a year can be pushed into deficit *by*
    # the transfer. Both readings are reported: whether the year was already a
    # deficit before the transfer is the question that decides compliance.
    text_5_loss = (
        "附註二(五)：非營利幼兒園未發生虧損之年度…始得提列業務發展準備金。"
    )
    if transfer is None or surplus is None:
        add(
            "虧損年度不得提列業務發展準備", text_5_loss, None,
            "缺業務發展費或本期餘絀", "high",
        )
    elif transfer <= 0:
        add("虧損年度不得提列業務發展準備", text_5_loss, True, "本年度未提列", "high")
    else:
        pre = surplus + transfer
        if surplus >= 0:
            add(
                "虧損年度不得提列業務發展準備", text_5_loss, True,
                f"提列 {transfer:,.0f}，本期餘絀 {surplus:+,.0f}（未虧損）", "high",
            )
        elif pre >= 0:
            add(
                "虧損年度不得提列業務發展準備", text_5_loss, None,
                f"提列 {transfer:,.0f} 後本期餘絀 {surplus:+,.0f}（虧損），"
                f"但提列前為 {pre:+,.0f}（未虧損）——虧損係提列本身造成，"
                f"是否符合「未發生虧損之年度」需主管機關認定",
                "high",
            )
        else:
            add(
                "虧損年度不得提列業務發展準備", text_5_loss, False,
                f"提列 {transfer:,.0f}，本期餘絀 {surplus:+,.0f}，"
                f"扣除提列前仍為 {pre:+,.0f}（虧損）——虧損非提列造成",
                "high",
            )

    _pct = int(DEVELOPMENT_RESERVE_CAP * 100)
    defines_base = clause_5_income_base(payload.get("note_2", {}).get("text"))
    if defines_base is True:
        text_5_cap = (
            f"附註二(五)：至多提列收入總額（家長繳交之費用；其有政府差額補助費者，"
            f"應合併計算）之百分之{_pct}為業務發展準備金。"
        )
        base_caveat = (
            "本份附註已明定收入總額為家長繳交之費用（合併政府差額補助費），"
            "本檢核仍以收入合計決算數為分母，兩者範圍是否一致未逐項核對"
        )
    elif defines_base is False:
        text_5_cap = f"附註二(五)：至多提列收入總額之百分之{_pct}為業務發展準備金。"
        base_caveat = "本份附註未定義「收入總額」之認定基礎"
    else:
        text_5_cap = f"附註二(五)：至多提列收入總額之百分之{_pct}為業務發展準備金。"
        base_caveat = "本份報告尚未抽取附註二全文，「收入總額」認定基礎未確認"

    if transfer is None or not revenue:
        add("業務發展準備提列上限 20%", text_5_cap, None, "缺業務發展費或收入合計", "high")
    else:
        cap = revenue * DEVELOPMENT_RESERVE_CAP
        share = transfer / revenue
        # We use 收入合計 決算數 as the denominator regardless of what the note
        # says, because that is the one figure every report's schema captures.
        # A different reading (budgeted income, or the note's own narrower
        # definition above) moves the ceiling, which only matters for cases
        # sitting just over the line. Saying "you breached the cap" to a 園 at
        # 20.8% would be irresponsible when the denominator itself is arguable,
        # so marginal cases are flagged for confirmation rather than asserted as
        # breaches.
        MARGINAL = 0.22
        if transfer <= cap + TOL:
            add(
                "業務發展準備提列上限 20%", text_5_cap, True,
                f"提列 {transfer:,.0f} = 收入合計 {revenue:,.0f} 的 {share * 100:.1f}%",
                "high",
            )
        elif share <= MARGINAL:
            add(
                "業務發展準備提列上限 20%", text_5_cap, None,
                f"提列 {transfer:,.0f} = 收入合計 {revenue:,.0f} 的 {share * 100:.1f}%"
                f"（上限 {cap:,.0f}，超出 {transfer - cap:,.0f}）。"
                f"**臨界超出**：{base_caveat}；若以預算數或該定義計算可能符合，"
                f"須先確認基礎再判斷",
                "high",
            )
        else:
            add(
                "業務發展準備提列上限 20%", text_5_cap, False,
                f"提列 {transfer:,.0f} = 收入合計 {revenue:,.0f} 的 {share * 100:.1f}%"
                f"（上限 {cap:,.0f}，超出 {transfer - cap:,.0f}）。{base_caveat}",
                "high",
            )

    # --- 附註二(十)1.(3)：人事費不得流出 -----------------------------------
    personnel = _line(lines, "人事費") or {}
    p_budget, p_actual = _f(personnel.get("budget")), _f(personnel.get("actual"))
    total = _line(lines, "支出合計") or {}
    t_budget, t_actual = _f(total.get("budget")), _f(total.get("actual"))
    text_10 = "附註二(十)1.(3)：人事費不得流出。"
    if None in (p_budget, p_actual, t_budget, t_actual):
        add("人事費不得流出", text_10, None, "缺人事費或支出合計的預算/決算", "high")
    elif p_actual >= p_budget - TOL:
        add(
            "人事費不得流出", text_10, True,
            f"人事費決算 {p_actual:,.0f} ≥ 預算 {p_budget:,.0f}", "high",
        )
    else:
        shortfall = p_budget - p_actual
        overall = t_actual - t_budget
        add(
            "人事費不得流出", text_10, None,
            f"人事費少支 {shortfall:,.0f}（決算 {p_actual:,.0f} / 預算 {p_budget:,.0f}），"
            f"同期支出合計{'超支' if overall > 0 else '節餘'} {abs(overall):,.0f}。"
            f"少支本身不等於流出（可能為缺額未補），"
            f"但需確認是否將人事費挪用於其他科目",
            "high",
        )

    # --- 附註五 關係人交易：揭露數 vs 年末應付受託法人餘額 -------------------
    # 中園 113 first raised this: the statement shows 行政管理費 311,956 while
    # 附註五 discloses only 203,350 -- but checking that premise against every
    # report with both fields (47 報告年) shows 附註五's 揭露數 matches the
    # *year-end payable balance* in 45/47 (96%), not the income statement's full-
    # year 行政管理費 (only 24/47, no better than chance). The 108,606 gap on 中園
    # is exactly what you'd expect if 108,606 was paid in cash during the year --
    # a routine relationship, not a contradiction. Comparing against 行政管理費
    # would have manufactured a "finding" on almost every report.
    #
    # The two reports where 揭露數 ≠ 應付餘額 are the genuinely low-base-rate
    # signal: N26 新店及人 111 discloses 244,855 for the year, but the payable
    # balance is 500,518 -- exactly 244,855 + 110's own 255,663 unpaid balance.
    # That reads as an unpaid balance carrying forward across years rather than
    # being settled, which the single-year check below can only surface as a
    # mismatch; confirming the multi-year carry needs the same cross-year
    # treatment as classify_reserve_gap, not built here.
    note5 = payload.get("note_5") or {}
    disclosed = _f(note5.get("admin_fee_disclosed"))
    payable = _f(note5.get("payable_to_operator"))
    admin_actual = _f((_line(lines, "行政管理費") or {}).get("actual"))
    text_n5 = (
        "附註五 關係人交易：揭露予受託法人（關係人）之年末應付款餘額；"
        "全語料庫 96% 的報告中，此揭露數即為該年末應付受託法人餘額。"
    )
    if disclosed is None or payable is None:
        add(
            "附註五揭露 = 年末應付受託法人餘額", text_n5, None,
            "缺附註五揭露金額或年末應付受託法人款", "medium",
        )
    else:
        gap = payable - disclosed
        # Materiality is relative, not a 1-dollar absolute tolerance.
        #
        # The absolute TOL used here originally flagged 17 園-年, and the gaps
        # split into two populations with nothing in between:
        #
        #   real outliers   N01 110 +140.1%   N26 111 +104.4%
        #                   N26 112 +107.9%   N13 113  +69.9%
        #   everything else −7.2% … −0.5%, thirteen of them inside ±3%
        #
        # The median gap across all 127 園-年 that report both figures is
        # 0.00%: the normal case is exact equality, and the small negatives are
        # the year's cash settlements, which ``EXTRACTION_GUIDE.md`` already
        # records as "差額 = 當年度已現金支付金額，是正常關係". A 1-dollar
        # tolerance therefore reported N25 碧城 111 -- 0.5% from exact -- as a
        # compliance failure against a real institution.
        #
        # 10% sits inside the empty band between the two populations, so any
        # threshold from 10% to 50% selects the same four reports; the choice is
        # not balanced on a knife edge.
        limit = max(TOL, abs(disclosed) * NOTE5_MATERIALITY)
        pct = gap / disclosed * 100 if disclosed else 0.0
        paid_in_cash = (
            f"；本年度以現金支付約 {admin_actual - disclosed:,.0f}"
            f"（表列行政管理費 {admin_actual:,.0f} − 揭露數）"
            if admin_actual is not None
            else ""
        )
        immaterial = (
            f"；差額 {abs(pct):.1f}% 未達重大性門檻 {NOTE5_MATERIALITY:.0%}，"
            f"係年度內現金結算之時間差"
            if abs(gap) <= limit and abs(gap) > TOL
            else ""
        )
        add(
            "附註五揭露 = 年末應付受託法人餘額", text_n5, abs(gap) <= limit,
            f"附註五揭露 {disclosed:,.0f} vs 年末應付受託法人款 {payable:,.0f}，"
            f"差額 {gap:+,.0f}（{pct:+.1f}%）{paid_in_cash}{immaterial}",
            "medium",
        )

    # --- 附註二(八)：不得以收支相抵後淨額入帳 -------------------------------
    text_8 = (
        "附註二(八)：所有收入，均應列入各相關收入項目，並以收入總額入帳，"
        "不得以收支相抵後淨額入帳。"
    )
    net_named = [
        "".join(str(ln.get("label", "")).split())
        for ln in lines
        if "淨額" in "".join(str(ln.get("label", "")).split())
    ]
    matched_pairs = _equal_revenue_expense_pairs(lines)
    if net_named or matched_pairs:
        parts = []
        if net_named:
            parts.append(f"科目名稱含「淨額」：{', '.join(net_named)}")
        if matched_pairs:
            parts.append(
                "收入與支出金額完全相同："
                + "；".join(f"{a} = {b} = {v:,.0f}" for a, b, v in matched_pairs)
            )
        add(
            "收入不得以淨額入帳", text_8, None,
            "；".join(parts) + "——可能為代收代付，需確認是否總額入帳", "medium",
        )
    else:
        add(
            "收入不得以淨額入帳", text_8, True,
            "未見淨額科目或收支等額配對", "medium",
        )

    return out


def _equal_revenue_expense_pairs(lines: list[dict]) -> list[tuple[str, str, float]]:
    """Revenue and expense lines carrying exactly the same amount.

    A gross-recorded pass-through legitimately produces equal amounts, so this is a
    prompt to check the note rather than a violation. It is worth surfacing because
    it recurs across the corpus (其他收入 = 其他支出 in several reports) and the
    detail lives in 附註三, outside the pages we extract.
    """
    revenue: dict[float, str] = {}
    expense: dict[float, str] = {}
    for ln in lines:
        label = "".join(str(ln.get("label", "")).split())
        amount = _f(ln.get("actual"))
        if amount is None or amount == 0 or "合計" in label or "餘絀" in label:
            continue
        bucket = revenue if ("收入" in label or "收益" in label) else expense
        bucket.setdefault(amount, label)
    return [
        (revenue[amt], expense[amt], amt)
        for amt in sorted(set(revenue) & set(expense), reverse=True)
    ]


# --- 跨年度：區分「撥付時間差」與「缺口累積」 ---------------------------------
#
# 附註二(六)(七) require a reserve booked as a liability to be matched by cash in a
# designated account. A single year's statement cannot say why the two sides differ,
# and the two explanations call for opposite responses:
#
#   時間差   the 園 books at year end and transfers next year -- the gap recurs at a
#            constant size, and this year's asset equals last year's liability.
#   缺口累積 the cash never arrives -- the gap compounds year on year.
#
# 三多 113 學年度 prompted this: a 2,200,000 gap against 1,200,000 the year before,
# with the current asset equal to the prior liability. Reading a lag as a shortfall
# accuses a compliant 園; reading a compounding shortfall as a lag misses the case
# we exist to find. So 缺口擴大 is only returned when the gap grows AND the lag
# pattern is absent.

# A 園 may transfer a round sum against an odd liability, so exact equality is not
# required for the gap to be considered unchanged.
GAP_TOL = 1_000.0
# How closely this year's asset must track last year's liability for the lag reading
# to hold. 5% tolerates a partial extra transfer without admitting coincidences.
LAG_TOL = 0.05


def classify_reserve_gap(
    prev_asset: object,
    prev_liability: object,
    asset: object,
    liability: object,
) -> tuple[str, bool] | None:
    """Classify a reserve funding gap across two consecutive 學年度.

    Returns ``(verdict, lag_match)``, or ``None`` when the pair says nothing —
    either a side is missing from one of the statements, or both years are funded.

    A missing side is not zero. 東湖 111 carries a 105,517 severance liability with
    no asset line at all; scoring that as a 105,517 shortfall would assert a breach
    where the honest reading is that the statement is incomplete.
    """
    pa, pl = _f(prev_asset), _f(prev_liability)
    ca, cl = _f(asset), _f(liability)
    if None in (pa, pl, ca, cl):
        return None

    prev_gap, gap = pl - pa, cl - ca
    if abs(prev_gap) <= GAP_TOL and abs(gap) <= GAP_TOL:
        return None  # funded both years, nothing to explain

    lag_match = bool(pl) and abs(ca - pl) <= LAG_TOL * abs(pl)
    delta = gap - prev_gap
    if delta < -GAP_TOL:
        return "缺口縮小", lag_match
    # The lag test comes before the size test on purpose. A 園 funding a year in
    # arrears reaches a steady state -- each year it transfers last year's balance
    # and books a similar new one -- so its gap barely moves. Checking the size
    # first would file that recurring, explained pattern under 缺口穩定 as though
    # nothing accounted for it, when the asset side names exactly what does.
    if lag_match:
        return "疑似撥付落後一年", True
    if abs(delta) <= GAP_TOL:
        # Indistinguishable from a standing shortfall on the face of the statement.
        return "缺口穩定", False
    return "缺口擴大", False
