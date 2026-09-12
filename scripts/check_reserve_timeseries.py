"""Track each 園's reserve funding gap across 學年度 to separate timing from shortfall.

Why a single year cannot answer this. 附註二(六)(七) require the 業務發展準備 and
資遣費準備 recognised as a liability to be matched by cash in a designated account
on the asset side. When the two sides differ, one year of data gives no way to tell
these apart:

The classification itself lives in
:func:`smart_watchdog.features.compliance.classify_reserve_gap` so it can be tested
against constructed cases; this script only assembles the year pairs and reports.

* **Timing.** The 園 books the reserve at year end and transfers the cash in the
  following year. The gap appears every year but is the same size each time, and
  this year's asset balance equals last year's liability balance.
* **Shortfall.** The money never arrives. The gap compounds -- year N's gap is
  year N-1's gap plus this year's unfunded appropriation.

三多 113 學年度 is what prompted this: liability exceeded asset by 2,200,000 while
the comparative column showed 1,200,000, and the current asset balance equalled the
prior liability balance. That reads as a one-year lag, which is a very different
conversation from 2,200,000 of missing cash -- and getting it wrong in either
direction is a real cost. Calling a timing difference a shortfall accuses a
compliant 園; calling a compounding shortfall a timing difference misses the case
we exist to find.

So the classification here is deliberately conservative: a gap is only called
**擴大（缺口累積）** when it grows across consecutive years by roughly the amount
newly appropriated. Anything consistent with a lag is reported as 疑似撥付落後 and
asks for the designated-account statement rather than asserting a breach.

This needs adjacent 學年度 for the same 園, so it only reports on 園 with two or
more extracted reports and stays silent about the rest.

Run:  PYTHONPATH=src .venv/bin/python scripts/check_reserve_timeseries.py
"""

from __future__ import annotations

import csv
import json
import pathlib
import re
import sys
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.features.compliance import classify_reserve_gap

EXTRACT_DIR = pathlib.Path("data/extracted/nonprofit")
OUT = pathlib.Path("data/processed/reserve_timeseries.csv")
FILENAME_RE = re.compile(r"^(N\d\d)_(.+?)_(\d{3})$")

# Two reserves carry the 專戶 requirement, each with its own asset/liability pair.
RESERVES = {
    "業務發展準備": ("reserve_asset", "reserve_liability"),
    "資遣費準備": ("severance_asset", "severance_liability"),
}



def main() -> None:
    by_school: dict[tuple[str, str], dict[int, dict]] = defaultdict(dict)
    for path in sorted(EXTRACT_DIR.glob("*.json")):
        m = FILENAME_RE.match(path.stem)
        if not m:
            continue
        code, short, year = m.groups()
        payload = json.loads(path.read_text(encoding="utf-8"))
        by_school[(code, short)][int(year)] = payload

    rows: list[dict] = []
    for (code, short), years in sorted(by_school.items()):
        ordered = sorted(years)
        for prev_y, y in zip(ordered, ordered[1:]):
            if y - prev_y != 1:
                continue  # a missing intervening year makes the delta unreadable
            cur_bs = years[y].get("balance_sheet") or {}
            prev_bs = years[prev_y].get("balance_sheet") or {}

            for name, (a_key, l_key) in RESERVES.items():
                verdict_pair = classify_reserve_gap(
                    prev_bs.get(a_key), prev_bs.get(l_key),
                    cur_bs.get(a_key), cur_bs.get(l_key),
                )
                if verdict_pair is None:
                    continue
                verdict, lag_match = verdict_pair

                prev_gap = float(prev_bs[l_key]) - float(prev_bs[a_key])
                gap = float(cur_bs[l_key]) - float(cur_bs[a_key])
                delta = gap - prev_gap
                cur_asset, prev_liability = cur_bs[a_key], prev_bs[l_key]

                if verdict == "缺口穩定":
                    note = (
                        f"缺口連續兩年約 {abs(gap):,.0f} 元且未變動，"
                        "與固定時間差一致，亦與長期短撥一致，須調閱專戶明細"
                    )
                elif verdict == "缺口縮小":
                    note = f"缺口自 {prev_gap:,.0f} 降至 {gap:,.0f} 元，與補撥一致"
                elif verdict == "疑似撥付落後一年":
                    note = (
                        f"本年度資產面 {float(cur_asset):,.0f} ≈ 前一年度負債面"
                        f" {float(prev_liability):,.0f}，缺口自 {prev_gap:,.0f}"
                        f" 增至 {gap:,.0f} 元，符合當年提列次年撥入之型態"
                    )
                else:
                    note = (
                        f"缺口自 {prev_gap:,.0f} 增至 {gap:,.0f} 元"
                        f"（增加 {delta:,.0f}），且資產面與前一年度負債面不相當，"
                        "建議查核專戶存入憑證"
                    )

                rows.append(
                    {
                        "code": code, "short_name": short,
                        "reserve": name,
                        "prev_year": prev_y, "year": y,
                        "prev_asset": prev_bs.get(a_key),
                        "prev_liability": prev_bs.get(l_key),
                        "asset": cur_bs.get(a_key),
                        "liability": cur_bs.get(l_key),
                        "prev_gap": round(prev_gap), "gap": round(gap),
                        "gap_delta": round(delta),
                        "asset_matches_prev_liability": int(bool(lag_match)),
                        "verdict": verdict,
                        "note": note,
                    }
                )

    paired = sum(1 for years in by_school.values() if len(years) > 1)
    print(
        f"可作跨年度比較的園：{paired} / {len(by_school)} 所"
        f"（需同園連續兩個學年度皆已抽取）"
    )
    if not rows:
        print("目前尚無同園連續年度的準備金缺口可比較。")
        return

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {OUT}  ({len(rows)} 筆年度間比較)\n")

    order = ["缺口擴大", "疑似撥付落後一年", "缺口穩定", "缺口縮小"]
    for verdict in order:
        group = [r for r in rows if r["verdict"] == verdict]
        if not group:
            continue
        print(f"=== {verdict}（{len(group)} 筆）===")
        for r in group:
            print(
                f"  {r['code']} {r['short_name']}｜{r['reserve']}"
                f"｜{r['prev_year']}→{r['year']} 學年度"
            )
            print(f"    {r['note']}")
        print()

    print(
        "⚠️ 「缺口擴大」是最值得追的一類，但仍是**請機構說明**的依據，"
        "不是短撥的認定。\n"
        "   「疑似撥付落後」與「缺口穩定」在報表上無法區分時間差與長期短撥，"
        "須以專戶存款餘額證明書判讀。"
    )


if __name__ == "__main__":
    main()
