"""Resolve the two compliance rules that the statement alone cannot decide.

``compliance.check_report`` asks seven questions of each 園-year. Five of them the
收支餘絀表 and 附註二 can answer on their own. Two cannot, and both say so in their
own ``detail`` string rather than guessing:

    人事費不得流出      "少支本身不等於流出（可能為缺額未補），
                        但需確認是否將人事費挪用於其他科目"
    收入不得以淨額入帳   "可能為代收代付，需確認是否總額入帳"

So they returned ``None`` -- 資料不足 -- on 131 and 129 of 132 reports. That is the
honest answer to give when the evidence is absent, and it stayed the answer for as
long as the pipeline only read the pages those two questions could not be answered
from. The missing evidence was never missing from the reports; it was on pages
nobody had opened.

This module supplies it from the page-level extraction:

**收入不得以淨額入帳** ← 附註三's 代收代付／代收補助／專案補助 detail tables. These
print 收 and 支 as separate columns per item. Both columns present *is* the
gross-recording the clause requires, so this one does reach 通過: the document
itself is the evidence.

**人事費不得流出** ← 附表二 經費流用及勻支檢查表. This one reads the table and still
stays 資料不足, because what the table supports is weaker than a verdict -- see
``resolve_personnel_outflow``. What it establishes goes into ``evidence_state``
instead, which is rankable without pretending to be a finding.

That asymmetry is the point of this module, and it was got wrong once: an earlier
version marked 131 reports 通過 on the 附表二 evidence while its own docstring said
the evidence only showed 人事費 was "not a necessary source" of the overspends.
Documenting a limitation and then not honouring it in the verdict inflated 通過 by
131 checks and would have contaminated any label built from the column.

Two boundaries kept from ``CLAUDE.md``:

* A resolution never turns 資料不足 into 低風險. Absent tables leave the check
  ``None``, and ``resolve`` says which table it wanted.
* ``passed=False`` here means "this report contradicts its own stated accounting
  policy", not "this 園 broke the law". The wording of every ``detail`` string is
  written to be quoted to a 園 as a question, not as an accusation.
"""

from __future__ import annotations

import collections
import dataclasses
from typing import Any

TOL = 1.0

#: The three 附註三 detail tables that record agency money gross.
AGENCY_SECTIONS = ("agency_passthrough", "agency_subsidy", "project_subsidy")

#: Column-header markers. 收/支 are printed as separate columns per period, so a
#: header is matched on its tail rather than parsed -- the period prefix varies
#: ("110.8.1~111.7.31 收" in one report, "收入" in another).
RECEIPT_MARKS = ("收", "收入")
PAYMENT_MARKS = ("支", "支出")


def _flat(text: object) -> str:
    return "".join(str(text or "").split())


@dataclasses.dataclass
class Resolution:
    """What the page-level evidence changed about one check, and on what basis.

    ``passed`` and ``evidence_state`` are deliberately separate. A resolver can
    establish a great deal about a report without establishing *compliance*, and
    collapsing the two loses exactly the distinction an inspector needs. Only
    direct documentary evidence moves ``passed``; everything else is recorded in
    ``evidence_state`` with the check still 資料不足.
    """

    rule: str
    passed: bool | None
    detail: str
    evidence: str          # which table decided it, or which one was missing
    changed: bool          # did this move the check off 資料不足?
    evidence_state: str = ""   # what the table showed, verdict aside


def _rows_by_section(facts: list[dict], code: str, year: str,
                     section: str) -> list[dict]:
    return [f for f in facts
            if f.get("code") == code and str(f.get("academic_year")) == str(year)
            and f.get("section") == section]


def _num(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


#: The expenditure categories the 收支餘絀表 and 附表二 are organised by. The
#: table is hierarchical -- 業務費 is followed by the fifteen lines that sum to it
#: -- and ``EXTRACTION_GUIDE.md`` rule 8 strips the indent that marked the
#: difference. Summing every printed row would therefore double-count each group,
#: so the arithmetic here uses only these top-level names, taking each one's first
#: appearance (the parent row, whose value equals the sum of its children).
TOP_LEVEL_CATEGORIES = frozenset({
    "人事費", "業務費", "材料費", "維護費", "修繕購置費",
    "雜支", "行政管理費", "業務發展費", "延長照顧服務支出",
})


def resolve_personnel_outflow(facts: list[dict], code: str, year: str) -> Resolution:
    """Did 人事費 budget move to another category? Read 附表二, not the 收支餘絀表.

    A category can only *receive* a transfer by ending above its own budget, so
    the question is whether any overspend required 人事費 to fund it. Two ways it
    demonstrably did not:

    1. Nothing else overspent. The shortfall simply went unspent.
    2. The overspends are smaller than the underspends in the *other* categories,
       so they are fully fundable without touching 人事費 at all.

    Case 2 matters because 流用 between sibling lines is ordinary budgeting, and
    it is what the corpus actually looks like: 106 of 132 園-年 have some category
    over budget, almost always small operating lines (修繕購置費 in 42 reports,
    日常消耗用品 in 31). Treating any overspend as unresolvable would leave the
    rule dead for no reason.

    **Neither case returns 通過.** Both are evidence *towards* compliance and
    neither demonstrates it, for two reasons:

    * "人事費 was not a necessary source" is not "人事費 did not flow out". A
      transfer can happen while sibling slack would also have covered it.
    * The table's 預算數 column may already be the revised, post-流用 budget
      rather than the original. If it is, a transfer has already been absorbed
      into the figures and no comparison of 決算 against it could reveal one. The
      form's own 預決算檢查結果 column, which would settle this, is blank in all
      132 reports.

    So the verdict stays 資料不足 and what was actually established goes in
    ``evidence_state`` -- ``no_overspend`` / ``absorbable`` / ``exceeds_slack`` --
    which is rankable without claiming to be a finding. An earlier version of this
    function returned ``passed=True`` for both cases, inflating 通過 by 131 checks
    while its own docstring said the claim was weaker than that.
    """
    rule = "人事費不得流出"
    rows = _rows_by_section(facts, code, year, "budget_transfer")
    if not rows:
        return Resolution(rule, None, "查無附表二「經費流用及勻支檢查表」",
                          "budget_transfer（缺）", changed=False,
                          evidence_state="no_table")

    # First appearance of each top-level category, from the 差異數 column (or
    # 決算−預算 where the table prints no 差異數).
    diffs: dict[str, float] = {}
    parts: dict[str, dict] = collections.defaultdict(dict)
    for f in rows:
        label = _flat(f.get("item_label"))
        if label not in TOP_LEVEL_CATEGORIES:
            continue
        period = _flat(f.get("period_label"))
        value = _num(f.get("value"))
        # "B-A 差異數" also starts with "B", so the 差異 test must come first or an
        # actual-column test keyed on the prefix would swallow it.
        if "差異數" in period or period.startswith("B-A"):
            if label not in diffs and value is not None:
                diffs[label] = value
        elif "預算" in period or period.startswith("A"):
            parts[label].setdefault("budget", value)
        elif "決算" in period or period.startswith("B"):
            parts[label].setdefault("actual", value)
    for label, cells in parts.items():
        if label in diffs:
            continue
        b, act = cells.get("budget"), cells.get("actual")
        if b is not None and act is not None:
            diffs[label] = act - b

    if "人事費" not in diffs:
        return Resolution(rule, None, "附表二中取不到人事費的預算/決算差異",
                          "budget_transfer（人事費列缺）", changed=False,
                          evidence_state="no_personnel_row")

    others = {k: v for k, v in diffs.items() if k != "人事費"}
    over = {k: v for k, v in others.items() if v > TOL}
    under = {k: -v for k, v in others.items() if v < -TOL}
    total_over, total_under = sum(over.values()), sum(under.values())
    caveat = ("；惟本表無法證明實際未流用"
              "（預算數欄是否為流用後之修正預算未能確認，檢查結果欄為空白）")

    if not over:
        return Resolution(
            rule, None,
            f"已核對附表二經費流用及勻支檢查表：人事費以外 {len(others)} 個科目"
            f"全數未超支，未發現任何科目取得流入{caveat}",
            "budget_transfer", changed=False, evidence_state="no_overspend")
    if total_over <= total_under:
        named = "、".join(f"{k} {v:,.0f}" for k, v in
                          sorted(over.items(), key=lambda x: -x[1])[:3])
        return Resolution(
            rule, None,
            f"已核對附表二：他科目超支合計 {total_over:,.0f}（{named}），"
            f"低於他科目短支合計 {total_under:,.0f}，超支可由同層科目自行勻支，"
            f"未發現必須由人事費支應之情形{caveat}",
            "budget_transfer", changed=False, evidence_state="absorbable")

    named = "、".join(f"{k} 超支 {v:,.0f}" for k, v in
                      sorted(over.items(), key=lambda x: -x[1])[:3])
    return Resolution(
        rule, None,
        f"已核對附表二：他科目超支合計 {total_over:,.0f}（{named}）"
        f"超過他科目短支合計 {total_under:,.0f}，差額 {total_over - total_under:,.0f} "
        f"須另有來源；人事費短支 {-diffs['人事費']:,.0f} 是否為其來源，建議優先查核",
        "budget_transfer", changed=False, evidence_state="exceeds_slack")


def _has_receipt_payment_columns(labels: list[str]) -> bool:
    """Does this table print 收 and 支 as separate columns?

    Two layouts occur and they answer different questions. N01 安溪 110 prints
    「(1) 專案補助收入及支出」 with 收/支 columns per period -- that table can show
    gross recording. N08 鷺江 113 prints 「專案補助收入」 with one column per
    academic year -- a revenue-only listing, which says nothing about whether the
    matching expenditure was offset against it.
    """
    flat = [_flat(x) for x in labels]
    return (any(x.endswith(RECEIPT_MARKS) for x in flat)
            and any(x.endswith(PAYMENT_MARKS) for x in flat))


def resolve_net_recording(facts: list[dict], code: str, year: str) -> Resolution:
    """Are agency receipts recorded gross? 附註三's detail tables print 收 and 支.

    The clause forbids recording revenue net of the expenditure it offsets. A
    detail table that shows both sides for each item is the documentary evidence
    that it was not netted -- which is why the equal 收/支 pairs in the 收支餘絀表
    that first raised the question are, for these items, the expected appearance.

    **This resolver can return 通過 or 資料不足 and never 未通過.** Netting would
    show up as the *absence* of a gross detail table, and absence of evidence here
    is indistinguishable from a table this pipeline failed to locate. Asserting a
    breach from that would be exactly the false accusation ``CLAUDE.md`` forbids;
    the honest output is 資料不足 plus a note saying what an inspector should ask
    for. An earlier version of this function did return 未通過 when it found no
    收/支 columns at all, and produced one against N08 鷺江 113 whose own detail
    string said "0 個項目" -- the bug that prompted this paragraph.
    """
    rule = "收入不得以淨額入帳"
    two_sided: list[str] = []
    single_sided: list[str] = []
    gross_items = partial_items = 0

    by_table: dict[tuple, dict] = collections.defaultdict(
        lambda: {"labels": {}, "items": collections.defaultdict(set), "section": ""})
    for section in AGENCY_SECTIONS:
        for f in _rows_by_section(facts, code, year, section):
            slot = (f.get("pdf_page"), f.get("table_index"))
            entry = by_table[slot]
            entry["section"] = section
            idx = f.get("period_index")
            entry["labels"][idx] = f.get("period_label")
            label = _flat(f.get("item_label"))
            if label.startswith("合") or _num(f.get("value")) is None:
                continue
            period = _flat(f.get("period_label"))
            if period.endswith(PAYMENT_MARKS):
                entry["items"][label].add("pay")
            elif period.endswith(RECEIPT_MARKS):
                entry["items"][label].add("recv")

    for entry in by_table.values():
        labels = [entry["labels"][k] for k in sorted(entry["labels"])]
        if not _has_receipt_payment_columns(labels):
            single_sided.append(entry["section"])
            continue
        two_sided.append(entry["section"])
        for sides in entry["items"].values():
            if {"recv", "pay"} <= sides:
                gross_items += 1
            elif sides:
                partial_items += 1

    if not by_table:
        return Resolution(rule, None,
                          "查無附註三之代收代付／代收補助／專案補助明細表",
                          "agency tables（缺）", changed=False,
                          evidence_state="no_table")
    if gross_items and not partial_items:
        return Resolution(
            rule, True,
            f"附註三明細表（{'、'.join(sorted(set(two_sided)))}）之 {gross_items} 個項目"
            f"均分列收入與支出兩欄，係總額入帳，符合附註二(八)",
            "+".join(sorted(set(two_sided))), changed=True,
            evidence_state="gross_documented")
    if gross_items and partial_items:
        return Resolution(
            rule, None,
            f"附註三明細表 {gross_items} 個項目總額入帳，"
            f"另有 {partial_items} 個項目僅列單邊金額，建議查核時確認其入帳方式",
            "+".join(sorted(set(two_sided))), changed=False,
            evidence_state="partial_sides")
    return Resolution(
        rule, None,
        f"附註三僅見單邊明細表（{'、'.join(sorted(set(single_sided)))}），"
        f"未分列收入與支出兩欄，無法據以認定是否總額入帳，"
        f"建議查核時調閱對應之支出明細",
        "agency tables（僅單邊）", changed=False,
        evidence_state="single_sided_only")


RESOLVERS = {
    "人事費不得流出": resolve_personnel_outflow,
    "收入不得以淨額入帳": resolve_net_recording,
}


def resolve_checks(checks: list[Any], facts: list[dict]) -> tuple[list[dict], dict]:
    """Apply every resolver to the checks it knows about.

    Only ever acts on a check that is currently ``None``: a rule the statement
    already decided is not re-opened here, because the statement is the primary
    document and this is corroboration, not a second opinion.
    """
    out: list[dict] = []
    stats: collections.Counter = collections.Counter()
    for check in checks:
        row = check.as_dict() if hasattr(check, "as_dict") else dict(check)
        rule = row.get("rule")
        resolver = RESOLVERS.get(rule)
        if resolver is None or row.get("passed") is not None:
            row["resolved_by"] = ""
            row["evidence_state"] = ""
            out.append(row)
            stats["未處理" if resolver is None else "原本已判定"] += 1
            continue
        res = resolver(facts, row["code"], str(row["academic_year"]))
        row["detail"] = res.detail or row["detail"]
        row["evidence_state"] = res.evidence_state
        if res.changed:
            row["passed"] = res.passed
            row["resolved_by"] = res.evidence
            stats[f"{rule}→{'通過' if res.passed else '未通過'}"] += 1
        else:
            # Evidence was read and did not compel a verdict. `resolved_by` stays
            # empty because nothing was resolved; `evidence_state` records what
            # the table showed so the row is still rankable.
            row["resolved_by"] = ""
            stats[f"{rule}→待確認（{res.evidence_state or 'no_table'}）"] += 1
        out.append(row)
    return out, dict(stats)
