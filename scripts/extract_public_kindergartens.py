"""Extract all 市立幼兒園 financials from the 決算書 volumes (no OCR, no model calls).

Output: data/extracted/public_kindergartens.csv -- one row per 幼兒園 per fiscal year,
plus an `notes` column recording any page we refused to parse.

Run:  python run.py extract-public
"""

from __future__ import annotations

import csv
import dataclasses
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.ingest.kindergarten_budget import (
    KindergartenYear,
    extract_kindergarten,
)
from smart_watchdog.ingest.public_school import index_volume

RAW = pathlib.Path("data/raw/資料集/公校")
# data/extracted/ 而非 data/processed/：這是「從 PDF 抽出、沒有 data/raw 就重生
# 不了」的產物，與 data/README.md 對 extracted/ 的定義一致，也是版控裡那份的位置。
OUT = pathlib.Path("data/extracted/public_kindergartens.csv")

DERIVED = [
    "gov_dependency", "tuition_execution", "capex_execution",
    "cost_per_staff", "balance_change",
]


def main() -> None:
    records: list[KindergartenYear] = []
    for year_dir in sorted(RAW.iterdir()):
        if not year_dir.is_dir():
            continue
        fiscal_year = year_dir.name.replace("年度決算書", "")
        for pdf in sorted(year_dir.rglob("*.pdf")):
            if "封面" in pdf.name:
                continue
            sections = [s for s in index_volume(pdf, fiscal_year) if s.is_kindergarten]
            if not sections:
                continue
            print(f"{pdf.name}: {len(sections)} 所市立幼兒園", flush=True)
            for sec in sections:
                rec = extract_kindergarten(pdf, sec)
                records.append(rec)
                flag = f"  ⚠ {'; '.join(rec.notes)}" if rec.notes else ""
                print(
                    f"  {rec.fund_code} {rec.name:<12} "
                    f"來源={_fmt(rec.fund_source_actual)} "
                    f"學雜費執行率={_pct(rec.tuition_execution)}{flag}",
                    flush=True,
                )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    base_fields = [f.name for f in dataclasses.fields(KindergartenYear) if f.name != "notes"]
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=[*base_fields, *DERIVED, "notes"])
        w.writeheader()
        for r in records:
            row = {f: getattr(r, f) for f in base_fields}
            for d in DERIVED:
                v = getattr(r, d)
                row[d] = round(v, 4) if isinstance(v, float) else v
            row["notes"] = "; ".join(r.notes)
            w.writerow(row)

    ok = [r for r in records if not r.notes]
    print(f"\nwrote {OUT}  ({len(records)} 筆, 完全乾淨 {len(ok)} 筆)")
    bad = [r for r in records if r.notes]
    if bad:
        print(f"\n需人工/OCR 補的 {len(bad)} 筆：")
        for r in bad:
            print(f"  {r.fiscal_year} {r.fund_code} {r.name}: {'; '.join(r.notes)}")


def _fmt(v: float | None) -> str:
    return "—" if v is None else f"{v:,.0f}"


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


if __name__ == "__main__":
    main()
