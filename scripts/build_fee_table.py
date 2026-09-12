"""Fetch 新北市 收費明細 for 學年度 109-114 and build the fee tables.

Outputs
    data/processed/fees_ntpc.csv           tidy: one row per fee line
    data/processed/fee_totals_ntpc.csv     per institution/year/class: declared vs summed

Run:  PYTHONPATH=src .venv/bin/python scripts/build_fee_table.py
"""

from __future__ import annotations

import csv
import dataclasses
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.scrape.fees import FeeRow, fetch_city, semester_total

CITY = "新北市"
YEARS = (109, 110, 111, 112, 113, 114)
OUT = pathlib.Path("data/processed")


def main() -> None:
    print(f"抓取 {CITY} slip{YEARS[0]}-{YEARS[-1]} …（依 repo tree 精確列舉）", flush=True)
    rows, missing = fetch_city(CITY, YEARS)
    print(f"取得 {len(rows):,} 筆收費明細列，涵蓋 {len({r.title for r in rows})} 所機構")
    if missing:
        print(f"⚠ {len(missing)} 檔在 tree 清單中但抓取 404（已記錄，未計入）")
        for year, title in missing[:5]:
            print(f"    slip{year}: {title[:50]}")

    OUT.mkdir(parents=True, exist_ok=True)
    fee_path = OUT / "fees_ntpc.csv"
    fields = [f.name for f in dataclasses.fields(FeeRow)]
    with fee_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(dataclasses.asdict(r))
    print(f"wrote {fee_path}")

    totals = semester_total(rows)
    tot_path = OUT / "fee_totals_ntpc.csv"
    with tot_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([
            "title", "year", "age_group", "semester", "class_type",
            "core_sum", "declared_total", "diff", "addons",
        ])
        for (title, year, age, sem, cls), g in sorted(totals.items()):
            w.writerow([
                title, year, age, sem, cls,
                round(g["core"], 2),
                "" if g["declared"] is None else round(g["declared"], 2),
                "" if g["diff"] is None else round(g["diff"], 2),
                round(g["addons"], 2),
            ])
    print(f"wrote {tot_path} ({len(totals):,} 組)")

    _write_institution_summary(totals)

    # --- checksum health ------------------------------------------------
    have = [g for g in totals.values() if g["diff"] is not None]
    exact = [g for g in have if abs(g["diff"]) < 1]
    print("\n=== 全學期總收費 checksum ===")
    print(f"可比對 {len(have):,} 組 / 共 {len(totals):,} 組")
    if have:
        print(f"  完全相符 {len(exact):,} 組 ({len(exact) / len(have) * 100:.1f}%)")
        off = sorted(have, key=lambda g: -abs(g["diff"]))[:5]
        print("  差異最大 5 組:")
        for g in off:
            print(
                f"    宣告 {g['declared']:>10,.0f}"
                f"  各項合計 {g['core']:>10,.0f}  差 {g['diff']:>+10,.0f}"
            )


def _write_institution_summary(totals: dict) -> None:
    """Compact per-institution × year table -- the artifact the model consumes.

    The tidy 464k-row file and the 59k-group file are both regenerable and too
    large to version, so this is what gets committed. 全日班/上學期 is used as the
    comparable basis because it is the most consistently filed combination across
    all six 學年度.
    """
    per: dict[tuple[str, int], list[float]] = {}
    for (title, year, _age, semester, class_type), g in totals.items():
        if class_type != "全日班" or semester != "上學期":
            continue
        if g["declared"] is None:
            continue
        per.setdefault((title, year), []).append(float(g["declared"]))

    by_title: dict[str, dict[int, float]] = {}
    for (title, year), vals in per.items():
        by_title.setdefault(title, {})[year] = sum(vals) / len(vals)

    path = OUT / "fee_summary_ntpc.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([
            "title", *[f"fee_{y}" for y in YEARS],
            "max_jump_pct", "max_jump_pre113_pct",
        ])
        for title, series in sorted(by_title.items()):
            row = [series.get(y) for y in YEARS]
            w.writerow([
                title,
                *[("" if v is None else round(v)) for v in row],
                _max_jump(series, YEARS),
                # Pre-113 only, for time-split modelling against 2024+ labels.
                _max_jump(series, (109, 110, 111, 112)),
            ])
    print(f"wrote {path} ({len(by_title)} 機構)")


def _max_jump(series: dict[int, float], years: tuple[int, ...]) -> str:
    jumps = [
        (series[b] - series[a]) / series[a] * 100
        for a, b in zip(years, years[1:])
        if series.get(a) and series.get(b)
    ]
    return "" if not jumps else str(round(max(jumps), 2))


if __name__ == "__main__":
    main()
