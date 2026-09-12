"""Quantify the personnel-underspend / reserve-transfer pattern across the corpus.

Why this exists: three reports independently surfaced the same structure, and the
extraction agents each flagged it without being asked to look for it --

    漢翔 113  人事費 short 2,420,373 (72% executed), unbudgeted 業務發展費 2,575,405
    昌福 113  人事費 short 2,119,231 (76% executed), unbudgeted 業務發展費 2,247,000
    中正 113  人事費 short 2,241,112 (83% executed), unbudgeted 業務發展費 2,565,534

In each case the 業務發展費 equalled the increase in the 業務發展準備 liability, so
the money moved from staffing into a reserve. 附註二(十)1.(3) states plainly:
**人事費不得流出。**

**Measured result: the conjunction is near-universal, so its presence is not a
flag.** 34 of 37 reports underspend personnel AND record an unbudgeted transfer to
the development reserve; only 2 underspend without a transfer and only 1 fully
spends its personnel budget. The transfer-to-shortfall ratio spans 0.26-3.17
(median 1.34) with just 17 of 34 inside a +/-40% band, so the amounts are not
generally matched either.

Those three opening cases looked striking only because they were read one at a
time. This is the third time in this project that a pattern seen in a handful of
reports turned out to be corpus-wide -- 行政管理費 executing at exactly 100% and
unbudgeted 業務發展費 were the first two. The rule this establishes: **before
treating any accounting behaviour as a red flag, count how many institutions do
it.** A behaviour shared by 92% of the corpus is how the sector operates.

The script is kept because the *distribution* is the finding, and because
personnel execution rate varies materially across it (79%-96%). That rate was
tested separately against future penalties in the matched cohort and did not
separate cases from controls either (rbc -0.185, p=0.268, docs/research/
05-phase1-results.md ss10.4), so it is reported as sector context, not as risk.

Run:  PYTHONPATH=src .venv/bin/python scripts/analyse_personnel_to_reserve.py
"""

from __future__ import annotations

import csv
import json
import pathlib
import re
import statistics
import sys

EXTRACT_DIR = pathlib.Path("data/extracted/nonprofit")
OUT = pathlib.Path("data/processed/personnel_to_reserve.csv")
FILENAME_RE = re.compile(r"^(N\d\d)_(.+?)_(\d{3})$")

# Amounts within this band of each other are "comparable" -- wide enough that an
# exact match is not required, narrow enough that unrelated magnitudes fall out.
COMPARABLE_LOW, COMPARABLE_HIGH = 0.7, 1.4


def _line(lines: list[dict], label: str) -> dict:
    for ln in lines:
        if label in "".join(str(ln.get("label", "")).split()):
            return ln
    return {}


def main() -> None:
    rows: list[dict] = []
    total_reports = 0

    for path in sorted(EXTRACT_DIR.glob("*.json")):
        m = FILENAME_RE.match(path.stem)
        if not m:
            continue
        code, short, year = m.groups()
        payload = json.loads(path.read_text(encoding="utf-8"))
        lines = (payload.get("income_statement") or {}).get("lines") or []
        if not lines:
            continue
        total_reports += 1

        personnel = _line(lines, "人事費")
        development = _line(lines, "業務發展費")
        p_budget, p_actual = personnel.get("budget"), personnel.get("actual")
        d_budget, d_actual = development.get("budget"), development.get("actual")

        if p_budget is None or p_actual is None:
            continue
        shortfall = float(p_budget) - float(p_actual)
        # d_budget is None means the line carried a dash: no appropriation at all.
        unbudgeted_transfer = d_budget is None and d_actual not in (None, 0)

        rows.append(
            {
                "code": code, "short_name": short, "academic_year": int(year),
                "personnel_budget": p_budget, "personnel_actual": p_actual,
                "personnel_shortfall": round(shortfall),
                "personnel_execution": round(float(p_actual) / float(p_budget), 4)
                if p_budget else None,
                "development_expense": d_actual,
                "development_unbudgeted": int(bool(unbudgeted_transfer)),
                "ratio_transfer_to_shortfall": (
                    round(float(d_actual) / shortfall, 3)
                    if unbudgeted_transfer and shortfall > 0 else None
                ),
            }
        )

    if not rows:
        sys.exit("沒有可分析的抽取結果")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: -r["personnel_shortfall"]))
    print(f"wrote {OUT}  ({len(rows)} 筆有人事費預算/決算的報告)\n")

    conjunction = [
        r for r in rows
        if r["personnel_shortfall"] > 0 and r["development_unbudgeted"]
    ]
    shortfall_only = [
        r for r in rows
        if r["personnel_shortfall"] > 0 and not r["development_unbudgeted"]
    ]
    no_shortfall = [r for r in rows if r["personnel_shortfall"] <= 0]

    print("=== 三種情形的分布 ===")
    print(f"  人事費短支 + 業務發展費未編列預算   {len(conjunction):>3} 筆")
    print(f"  僅人事費短支（無未預算轉列）        {len(shortfall_only):>3} 筆")
    print(f"  人事費足額或超支                    {len(no_shortfall):>3} 筆")

    if not conjunction:
        print("\n本批資料未出現該併存情形")
        return

    print(f"\n=== 併存情形明細（{len(conjunction)} 筆，依人事費短支金額排序）===")
    header = (
        f"{'園':<12}{'年度':>5}{'人事費短支':>13}"
        f"{'執行率':>8}{'未預算業發費':>14}{'倍數':>7}"
    )
    print(header)
    for r in sorted(conjunction, key=lambda r: -r["personnel_shortfall"]):
        ratio = r["ratio_transfer_to_shortfall"]
        print(
            f"{r['short_name']:<12}{r['academic_year']:>5}"
            f"{r['personnel_shortfall']:>13,}"
            f"{r['personnel_execution'] * 100:>7.0f}%"
            f"{r['development_expense']:>14,.0f}"
            f"{ratio if ratio is not None else 0:>7.2f}"
        )

    ratios = [r["ratio_transfer_to_shortfall"] for r in conjunction
              if r["ratio_transfer_to_shortfall"] is not None]
    if ratios:
        comparable = [x for x in ratios if COMPARABLE_LOW <= x <= COMPARABLE_HIGH]
        print(
            f"\n業務發展費 / 人事費短支 倍數："
            f"中位數 {statistics.median(ratios):.2f}，"
            f"範圍 {min(ratios):.2f}–{max(ratios):.2f}"
        )
        print(
            f"金額相當者（倍數 {COMPARABLE_LOW}–{COMPARABLE_HIGH}）："
            f"{len(comparable)}/{len(ratios)} 筆"
        )
        share = len(conjunction) / len(rows) * 100
        print(
            f"\n⚠️ 此併存情形佔全體 {len(conjunction)}/{len(rows)} = {share:.0f}%，"
            "**普遍存在，因此其「有無」不是紅旗**。"
            "\n   倍數亦分散（0.26–3.17），並非普遍相當，無法據此推論資金流向。"
            "\n   人事費執行率本身在配對 cohort 檢定中亦不顯著"
            "（rbc −0.185, p=0.268，見 05-phase1-results.md §10.4）。"
            "\n   → 本表定位為產業結構描述，不是風險訊號。"
            "要斷定流用需調閱預算流用核准文件與專戶存入憑證。"
        )


if __name__ == "__main__":
    main()
