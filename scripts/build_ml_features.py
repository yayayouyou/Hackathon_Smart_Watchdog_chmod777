"""Turn the page-level facts into one wide 園 × 學年度 table a model can consume.

The extraction produced 144,984 facts in long form -- one row per (園, 學年度,
section, line item, period). That shape is right for auditing a number back to the
page it came from, and wrong for everything else: no feature builder, no model and
no analyst wants to pivot 145k rows before asking a question.

This is the missing middle step. One row per 園-學年度, one column per quantity,
named for what it is.

## Where the columns come from

Only sections with near-complete coverage are used, because a feature present for
40% of the panel is a column of holes rather than a feature:

    income_by_function   132/132   教保費收入淨額、營運成本、核准招收人數、營運月數
    personnel_detail     132/132   薪資、加班費、勞健保、勞退、資遣費
    budget_transfer      132/132   頂層科目的預算／決算
    cash_flow            127/132   期初／期末現金、本期稅前餘絀
    equity_change        119/132   淨值期初／期末餘額

## 營運月數 is read, never assumed

10 of the 132 reports cover a partial year -- N28 新樂 110 runs 111.2.1 to
111.7.31, six months -- because the 園 opened mid-year. Anything per-month or
per-child computed on an assumed 12 months is wrong for those ten, and wrong in
the direction that makes a new 園 look like it collects half the fees it should.
``income_by_function`` prints 營運月數 on every report, so this reads it.

That mistake was made here once, against N28, before the column was found.

## Missing stays missing

Every column is ``None`` when the source line was absent. A zero would be a claim
about the 園 -- the same rule the extraction follows for blank cells. Each row
carries ``n_sections`` so a thin row is visible as thin.

Output
    data/processed/nonprofit_ml_features.csv
"""

from __future__ import annotations

import argparse
import collections
import csv
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pandas as pd

from smart_watchdog.console import use_utf8

FACTS = pathlib.Path("data/processed/nonprofit_pagewise_facts.csv")
PANEL = pathlib.Path("data/processed/nonprofit_panel.csv")
OUT = pathlib.Path("data/processed/nonprofit_ml_features.csv")

#: section -> {column name: printed line label}. Labels are matched after
#: whitespace is stripped, because the reports pad them for alignment.
WANTED: dict[str, dict[str, str]] = {
    "income_by_function": {
        "tuition_gross": "教保費收入",
        "tuition_deduction": "教保費收入減項",
        "tuition_net": "教保費收入淨額",
        "operating_cost": "營運成本",
        "tuition_net_surplus": "教保費收支淨額",
        "interest_income": "利息收入",
        "approved_capacity_fn": "核准招收人數",
        "operating_months": "營運月數",
        "approved_total_fn": "全期核准招生人數",
        "development_fee_fn": "業務發展費",
    },
    "personnel_detail": {
        "pay_educators": "園長及教保服務人員薪資（含職務加給）",
        "pay_support": "會計、總務、廚工、清潔薪資",
        "pay_overtime": "加班費",
        "pay_insurance": "勞、健保費",
        "pay_pension": "勞退金提撥",
        "pay_severance": "資遣費",
        "pay_health_check": "健康檢查",
        "pay_welfare": "自強活動",
    },
    "cash_flow": {
        "cash_open": "期初現金及約當現金餘額",
        "cash_close": "期末現金及約當現金餘額",
        "pretax_surplus": "本期稅前餘絀",
        "cf_receivables": "應收帳款淨額",
        "cf_prepaid_receipts": "預收款項",
        "cf_payables": "應付帳款",
    },
}

#: Top-level expenditure categories in 附表二, kept as budget/actual pairs.
#: 業務發展費 is deliberately absent: it does not appear in 附表二 on any of the
#: 132 reports (it is printed in 收支餘絀表-功能別 instead, and is read from there).
BUDGET_CATEGORIES = ["人事費", "業務費", "材料費", "維護費",
                     "修繕購置費", "雜支", "行政管理費"]


def flat(text: object) -> str:
    return "".join(str(text or "").split())


def num(v: object) -> float | None:
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if x != x else x


#: The first three-digit run in a period label is the 學年度 it starts in. The
#: printed forms vary more than they look: "110.8.1~111.7.31",
#: "民國110年8月1日至111年7月31日" and "民國 110 年 8 月 1 日至 111 年 7 月 31 日"
#: all appear, and an earlier version of this matcher tested only the first two
#: and silently dropped every 功能別 row in the corpus.
YEAR_RUN = re.compile(r"(\d{3})")


def current_period(period: str, year: str) -> bool:
    """Does this column belong to 學年度 ``year`` rather than the comparative one?"""
    found = YEAR_RUN.findall(flat(period))
    return bool(found) and found[0] == str(year)


def collect(facts: list[dict]) -> dict[tuple, dict]:
    """Pull the wanted line items out of the long table, one dict per 園-學年度."""
    out: dict[tuple, dict] = collections.defaultdict(dict)
    seen_sections: dict[tuple, set] = collections.defaultdict(set)

    for f in facts:
        section = f["section"]
        key = (f["code"], str(f["academic_year"]))
        year = str(f["academic_year"])
        label = flat(f["item_label"])
        value = num(f["value"])
        if value is None:
            continue

        if section in WANTED:
            seen_sections[key].add(section)
            for col, want in WANTED[section].items():
                if label != flat(want):
                    continue
                # Single-column tables (功能別, 明細) have one period; multi-period
                # tables need the current one. Take the first current match, or
                # the first value when no period label names a year at all.
                period = flat(f["period_label"])
                if YEAR_RUN.search(period) and not current_period(period, year):
                    continue
                out[key].setdefault(col, value)

        elif section == "budget_transfer" and label in BUDGET_CATEGORIES:
            seen_sections[key].add(section)
            period = flat(f["period_label"])
            if "差異" in period:
                continue
            suffix = "budget" if ("預算" in period or period.startswith("A")) else (
                "actual" if ("決算" in period or period.startswith("B")) else None)
            if suffix:
                out[key].setdefault(f"bt_{label}_{suffix}", value)

    for key, row in out.items():
        row["n_sections"] = len(seen_sections[key])
    return out


def derive(row: dict) -> dict:
    """Ratios that make 園 of different sizes comparable.

    Every denominator is checked: a per-child figure divided by a missing or zero
    headcount is None, not zero and not inf.
    """
    def div(a: str, b: str) -> float | None:
        x, y = row.get(a), row.get(b)
        return None if x is None or not y else x / y

    d: dict = {}
    months = row.get("operating_months")
    cap = row.get("approved_capacity_fn")
    net = row.get("tuition_net")

    # 每生每月教保費：用報告自己印的營運月數，不是假設的 12
    if net is not None and cap and months:
        d["tuition_per_child_month"] = net / cap / months
    d["cost_per_child_month"] = (
        row["operating_cost"] / cap / months
        if row.get("operating_cost") is not None and cap and months else None)
    d["tuition_cost_ratio"] = div("tuition_net", "operating_cost")

    personnel = sum(v for k, v in row.items()
                    if k.startswith("pay_") and v is not None) or None
    d["personnel_detail_total"] = personnel
    if personnel:
        d["educator_pay_share"] = (row["pay_educators"] / personnel
                                   if row.get("pay_educators") is not None else None)
        d["overtime_share"] = (row["pay_overtime"] / personnel
                               if row.get("pay_overtime") is not None else None)
        d["pension_share"] = (row["pay_pension"] / personnel
                              if row.get("pay_pension") is not None else None)
    d["cash_change"] = (
        row["cash_close"] - row["cash_open"]
        if row.get("cash_close") is not None and row.get("cash_open") is not None
        else None)
    for cat in BUDGET_CATEGORIES:
        b, a = row.get(f"bt_{cat}_budget"), row.get(f"bt_{cat}_actual")
        d[f"exec_{cat}"] = a / b if a is not None and b else None
    return d


def main() -> None:
    ap = argparse.ArgumentParser(description="把頁級事實整理成 ML 可用的寬表")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()
    use_utf8()

    if not FACTS.exists():
        sys.exit(f"找不到 {FACTS}，請先跑 build_pagewise_facts.py")
    with FACTS.open(encoding="utf-8", newline="") as fh:
        facts = list(csv.DictReader(fh))

    collected = collect(facts)
    panel = pd.read_csv(PANEL)
    panel["academic_year"] = panel["academic_year"].astype(str)

    rows = []
    for rec in panel.to_dict("records"):
        key = (rec["code"], rec["academic_year"])
        got = dict(collected.get(key, {}))
        row = {"code": rec["code"], "short_name": rec["short_name"],
               "academic_year": rec["academic_year"],
               # 學年度 N 的報告，基準日是 (N+1)/7/31：時序切分時不得早於此使用
               "available_from": f"{int(rec['academic_year']) + 1}/7/31"}
        for col in ("total_assets", "total_liabilities", "current_assets_total",
                    "current_liabilities_total", "cash", "prepaid_receipts",
                    "accumulated_surplus", "current_surplus", "equity_total",
                    "收入合計_決算", "支出合計_決算", "total_staff", "educators",
                    "approved_capacity", "actual_enrolment"):
            v = rec.get(col)
            row[f"panel_{col}"] = None if v is None or pd.isna(v) else float(v)
        row.update(got)
        row.update(derive(got))
        rows.append(row)

    cols = list(dict.fromkeys(k for r in rows for k in r))
    pathlib.Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    with pathlib.Path(a.out).open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c) for c in cols})

    filled = {c: sum(1 for r in rows if r.get(c) is not None) for c in cols}
    print(f"{len(rows)} 個園-學年度 × {len(cols)} 欄 → {a.out}\n")
    print(f"{'欄位':<30}{'非空':>6}{'覆蓋':>8}")
    for c in cols:
        if c in ("code", "short_name", "academic_year", "available_from"):
            continue
        n = filled[c]
        flag = "  ⚠" if n < len(rows) * 0.7 else ""
        print(f"  {c:<28}{n:>6}{n / len(rows):>8.0%}{flag}")
    thin = [r for r in rows if r.get("n_sections", 0) < 3]
    if thin:
        print(f"\n⚠ {len(thin)} 個園-學年度取到的區塊少於 3 個，"
              f"這些列的缺值多，建模時應以 n_sections 篩選")


if __name__ == "__main__":
    main()
