"""Quantify extraction accuracy using the statements' own accounting identities.

The credibility question a judge will ask is "how do you know the extracted numbers
are right?". These statements answer it themselves: every 決算書 carries internal
identities that must hold, so we can measure accuracy without a hand-labelled
ground truth.

Checks
    1. 期末基金餘額 = 期初基金餘額 + 本期賸餘(短絀) − 解繳公庫
    2. 比較增減 = 本年度決算 − 本年度預算  (for 基金來源 / 基金用途 / 學雜費收入)
    3. 基金用途 = 學前教育計畫 + 建築及設備計畫
    4. 期初基金餘額 = 上年度決算欄的期末基金餘額

A failure means either a bad extraction or a genuine restatement -- both need to
surface, neither should be silently averaged away.

A *uniform* failure means neither: it means the check is wrong. Check 2 originally
asserted 增減 = 決算 − 上年度決算 and failed 58/58, which is how we learned that
this column is the budget-execution variance rather than a year-over-year one.
Likewise, asserting 基金來源預算 = 基金用途預算 failed 28/58 with round-number
differences -- these funds budget a planned drawdown of the opening balance, so
that equality was never an identity. It is reported as information instead.

Run:  PYTHONPATH=src .venv/bin/python scripts/validate_extraction.py
"""

from __future__ import annotations

import pathlib
import sys

import fitz
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.ingest.kindergarten_budget import (
    ACTUAL,
    BUDGET,
    DELTA,
    FUND_COLUMNS,
    FUND_STATEMENT,
    PRIOR,
)
from smart_watchdog.ingest.pdf_utils import page_rows
from smart_watchdog.ingest.public_school import find_statement_page, index_volume
from smart_watchdog.ingest.tables import parse_table, statement_rows

RAW = pathlib.Path("data/raw/資料集/公校")
TOL = 1.0  # 元 -- these statements are whole-NTD, so anything above 1 is an error


def main() -> None:
    checks: list[dict] = []
    deficits: list[dict] = []
    for year_dir in sorted(RAW.iterdir()):
        if not year_dir.is_dir():
            continue
        fy = year_dir.name.replace("年度決算書", "")
        for pdf in sorted(year_dir.rglob("*.pdf")):
            if "封面" in pdf.name:
                continue
            secs = [s for s in index_volume(pdf, fy) if s.is_kindergarten]
            if not secs:
                continue
            doc = fitz.open(pdf)
            try:
                for sec in secs:
                    idx = find_statement_page(pdf, sec, FUND_STATEMENT)
                    if idx is None:
                        continue
                    t = parse_table(statement_rows(page_rows(doc[idx])))
                    if t.n_columns != FUND_COLUMNS:
                        continue
                    checks.extend(_check_section(fy, sec.name, t))
                    deficit = _planned_deficit(t)
                    if deficit:
                        deficits.append(
                            {"年度": fy, "園": sec.name, "計畫動支期初餘額": deficit}
                        )
            finally:
                doc.close()

    df = pd.DataFrame(checks)
    print(
        f"共執行 {len(df)} 項恆等式檢查，"
        f"涵蓋 {df['園'].nunique()} 所 × {df['年度'].nunique()} 年度\n"
    )

    g = df.groupby("檢查").agg(
        項數=("通過", "size"), 通過=("通過", "sum"), 可算=("可算", "sum")
    )
    g["通過率%"] = (g["通過"] / g["可算"] * 100).round(1)
    print(g.to_string())

    total_ok = int(df["通過"].sum())
    total_eval = int(df["可算"].sum())
    print(f"\n整體：{total_ok}/{total_eval} 通過 = {total_ok / total_eval * 100:.2f}%")

    if deficits:
        dd = pd.DataFrame(deficits)
        print(
            f"\n（資訊）{len(dd)} 個分基金編列計畫赤字，由期初餘額支應，"
            f"中位數 {dd['計畫動支期初餘額'].median():,.0f} 元 —— 屬正常編列，非異常"
        )

    fails = df[(~df["通過"]) & (df["可算"])]
    if not fails.empty:
        print(f"\n未通過 {len(fails)} 項（需人工判讀：抽取錯誤 or 追溯重編）：")
        for _, r in fails.iterrows():
            print(f"  {r['年度']} {r['園']:<12} {r['檢查']:<22} 差異 {r['差異']:>14,.0f}")


def _check_section(fy: str, name: str, t) -> list[dict]:
    def v(*needles: str, column: int) -> float | None:
        return t.value(*needles, column=column)

    out: list[dict] = []

    def add(check: str, lhs: float | None, rhs: float | None) -> None:
        computable = lhs is not None and rhs is not None
        diff = (lhs - rhs) if computable else float("nan")
        out.append(
            {
                "年度": fy, "園": name, "檢查": check,
                "可算": computable,
                "通過": bool(computable and abs(diff) <= TOL),
                "差異": diff,
            }
        )

    # 1. fund balance roll-forward
    opening = v("期初基金餘額", column=ACTUAL)
    surplus = v("本期賸餘", column=ACTUAL)
    remit = v("解繳公庫", column=ACTUAL) or 0.0
    closing = v("期末基金餘額", column=ACTUAL)
    if None not in (opening, surplus, closing):
        add("期末=期初+賸餘-解繳", closing, opening + surplus - remit)
    else:
        add("期末=期初+賸餘-解繳", None, None)

    # 2. The 比較增減 column is 決算 − *預算* (budget execution variance), not a
    #    year-over-year comparison. Verified on 板橋 113: 基金來源 決算 33,417,982
    #    − 預算 29,493,000 = 3,924,982 = the printed 增減, whereas 決算 − 上年度決算
    #    29,359,365 = 4,058,617 which is not. Checking it the other way round
    #    failed on 58/58 sections -- a uniform failure like that is a wrong
    #    assumption on our side, not 58 misprinted statements.
    for label in ["基金來源", "基金用途", "學雜費收入"]:
        a, b, dl = v(label, column=ACTUAL), v(label, column=BUDGET), v(label, column=DELTA)
        add(f"{label} 增減=決算-預算", dl, (a - b) if None not in (a, b) else None)

    # 3. 基金用途 = 學前教育計畫 + 建築及設備計畫
    use = v("基金用途", column=ACTUAL)
    plan = v("學前教育計畫", column=ACTUAL)
    capex = v("建築及設備計畫", column=ACTUAL) or 0.0
    if None not in (use, plan):
        add("基金用途=學前+建設", use, plan + capex)
    else:
        add("基金用途=學前+建設", None, None)

    # 4. 上年度決算 roll-forward: this year's 期初餘額 = last year's 期末餘額,
    #    which the statement prints in its 上年度 column.
    add(
        "期初=上年度期末",
        v("期初基金餘額", column=ACTUAL),
        v("期末基金餘額", column=PRIOR),
    )
    return out


def _planned_deficit(t) -> float | None:
    """基金用途預算 − 基金來源預算.

    Not an identity check: these funds routinely budget a deficit covered by the
    opening balance, so 來源 ≠ 用途 in the budget column is normal. Asserting
    equality failed on 28/58 sections with suspiciously round differences
    (-1,500,000, -300,000 ...), which is the signature of planned drawdown rather
    than of extraction error. Reported as information only.
    """
    src = t.value("基金來源", column=BUDGET)
    use = t.value("基金用途", column=BUDGET)
    if None in (src, use):
        return None
    return use - src


if __name__ == "__main__":
    main()
