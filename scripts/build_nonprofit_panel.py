"""Consolidate every extracted 非營利園 report into one tidy panel.

This is the table teammates should start from: one row per 園 per 學年度, with the
raw fields, the derived forensic signals, the arithmetic-check result, and the
model's own `issues` count all in one place.

Outputs
    data/processed/nonprofit_panel.csv     one row per 園-year
    data/processed/nonprofit_issues.csv    one row per reported issue (the anomaly
                                           hunting ground -- see below)

`issues` gets its own table on purpose. The extraction model reports document
contradictions it was never asked to look for, and two of the most interesting
findings so far came from there rather than from any signal we designed:
資遣費準備金 not matching its own liability and contradicting the report's own note,
and a 核定人數 that differs from the official registry by 302 places.

Run:  PYTHONPATH=src .venv/bin/python scripts/build_nonprofit_panel.py
"""

from __future__ import annotations

import csv
import dataclasses
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.extract.forensic import (
    ForensicSignals,
    compute_signals,
    validate_forensic,
)

EXTRACT_DIR = pathlib.Path("data/extracted/nonprofit")
OUT = pathlib.Path("data/processed")
FILENAME_RE = re.compile(r"^(N\d\d)_(.+?)_(\d{3})$")

# Expense lines carried through to the panel so peer comparison does not require
# re-opening the JSON.
EXPENSE_LINES = (
    "人事費", "業務費", "材料費", "維護費", "修繕購置費",
    "雜支", "行政管理費", "業務發展費",
)

RAW_FIELDS = (
    "cash", "prepaid_receipts", "current_assets_total", "current_liabilities_total",
    "reserve_asset", "reserve_liability", "severance_asset", "severance_liability",
    "accumulated_surplus", "current_surplus", "equity_total",
    "total_assets", "total_liabilities",
)
SIGNAL_FIELDS = tuple(
    f.name for f in dataclasses.fields(ForensicSignals)
    if f.name not in ("code", "short_name")
)


def _line(lines: list[dict], label: str) -> dict | None:
    for ln in lines:
        if label in "".join(str(ln.get("label", "")).split()):
            return ln
    return None


def main() -> None:
    files = sorted(EXTRACT_DIR.glob("*.json"))
    if not files:
        sys.exit(f"{EXTRACT_DIR} 裡沒有抽取結果")

    rows: list[dict] = []
    issue_rows: list[dict] = []
    for path in files:
        m = FILENAME_RE.match(path.stem)
        if not m:
            print(f"  ⚠ 檔名不符 Nxx_名稱_學年度：{path.name}")
            continue
        code, short, year = m.groups()
        payload = json.loads(path.read_text(encoding="utf-8"))
        passed, failed = validate_forensic(payload)
        sig = compute_signals(payload)
        bs = payload.get("balance_sheet") or {}
        note = payload.get("note_1") or {}
        inc = payload.get("income_statement") or {}
        lines = inc.get("lines") or []

        row: dict = {
            "code": code, "short_name": short, "academic_year": int(year),
            "balance_sheet_date": bs.get("date_current") or "",
            "income_period": inc.get("period") or "",
            "operator": note.get("operator") or "",
            "contract_period": note.get("contract_period") or "",
            "approved_capacity": note.get("approved_capacity"),
            "actual_enrolment": note.get("actual_enrolment"),
            "total_staff": note.get("total_staff"),
            "educators": note.get("educators"),
        }
        row.update({f: bs.get(f) for f in RAW_FIELDS})
        for label in EXPENSE_LINES:
            ln = _line(lines, label) or {}
            row[f"{label}_預算"] = ln.get("budget")
            row[f"{label}_決算"] = ln.get("actual")
        total = _line(lines, "支出合計") or {}
        revenue = _line(lines, "收入合計") or {}
        row["支出合計_決算"] = total.get("actual")
        row["收入合計_決算"] = revenue.get("actual")
        signals = sig.as_dict()
        row.update({
            f: (round(signals[f], 6) if isinstance(signals[f], float) else signals[f])
            for f in SIGNAL_FIELDS
        })
        row.update({
            "identity_passed": len(passed),
            "identity_failed": len(failed),
            "n_issues": len(payload.get("issues") or []),
        })
        rows.append(row)

        issue_rows.extend(
            {
                "code": code, "short_name": short, "academic_year": int(year),
                "issue": " ".join(str(issue).split()),
            }
            for issue in payload.get("issues") or []
        )

    rows.sort(key=lambda r: (r["code"], r["academic_year"]))
    OUT.mkdir(parents=True, exist_ok=True)

    panel = OUT / "nonprofit_panel.csv"
    with panel.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    issues_path = OUT / "nonprofit_issues.csv"
    with issues_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["code", "short_name", "academic_year", "issue"])
        w.writeheader()
        w.writerows(issue_rows)

    years = sorted({r["academic_year"] for r in rows})
    parks = sorted({r["code"] for r in rows})
    print(f"wrote {panel}  ({len(rows)} 筆，{len(parks)} 所園 × 學年度 {years}）")
    print(f"wrote {issues_path}  ({len(issue_rows)} 條模型自報事項)")

    bad = [r for r in rows if r["identity_failed"]]
    print(f"\n恆等式：通過 {sum(r['identity_passed'] for r in rows)}"
          f" / 失敗 {sum(r['identity_failed'] for r in rows)}")
    if bad:
        print("⚠ 有恆等式未通過的園-年度（這些數字先不要用）：")
        for r in bad:
            print(f"    {r['code']} {r['short_name']} {r['academic_year']}"
                  f"（失敗 {r['identity_failed']} 項）")

    print("\n每年度筆數：")
    for y in years:
        n = sum(1 for r in rows if r["academic_year"] == y)
        print(f"  {y} 學年度  {n:>3} 筆")

    multi = [c for c in parks if sum(1 for r in rows if r["code"] == c) > 1]
    print(f"\n有兩年以上資料、可做時序比較的園：{len(multi)} 所")


if __name__ == "__main__":
    main()
