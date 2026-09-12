"""Verify every extraction belongs to the institution its filename claims.

Why this exists: an extraction agent reported that a shared scratchpad directory
was being overwritten by other concurrent agents mid-task, and that one of its
page crops showed a *different* kindergarten (中園 instead of 中平). An earlier
agent on a different report said the same thing in different words ("p09 shows a
different kindergarten than its header crop suggested").

Attributing one institution's figures to another is the worst error this pipeline
can make -- worse than a wrong digit, because every downstream compliance finding
would name the wrong 園. This script is the independent check that it did not
happen, run against the registry rather than against the extraction itself.

Three cross-checks per file, all from data the extraction did not choose:

1. **Operator** -- the 受託法人 named in 附註一 must match the operator parsed from
   the institution's registered title in the official registry.
2. **Contract period** -- 受託辦理期間 must contain the report's own 學年度.
3. **Magnitude** -- total assets must be within a plausible band for the 園's
   approved capacity; a 60-child 園 carrying a 484-child 園's figures shows up here
   even when the operator happens to coincide.

Run:  PYTHONPATH=src .venv/bin/python scripts/verify_extraction_identity.py
"""

from __future__ import annotations

import csv
import json
import pathlib
import re
import sys

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.features.compliance import contract_covers_year, is_opening_year
from smart_watchdog.scrape import registry

EXTRACT_DIR = pathlib.Path("data/extracted/nonprofit")
INSTITUTIONS = pathlib.Path("data/processed/institutions_ntpc.csv")
OUT = pathlib.Path("data/processed/extraction_identity_audit.csv")
FILENAME_RE = re.compile(r"^(N\d\d)_(.+?)_(\d{3})$")

# Total assets per approved place, from the reports that pass every other check.
# Used only to catch gross mis-attribution, so the band is deliberately wide.
ASSETS_PER_PLACE_LOW, ASSETS_PER_PLACE_HIGH = 20_000, 500_000

# Date parsing and the two 學年度-boundary questions below (does a contract cover
# this year? does it start in it?) live in compliance.py, shared with the
# opening-year exception in check_report(). They used to be duplicated here with
# a subtly different bug: reading the *last* date found as the contract's end
# instead of the second one, which breaks on agents' helpful
# "（109年2月1日開園）" annotations -- see compliance.py's docstring for the
# concrete false-positive that caused.
_period_covers = contract_covers_year
_is_opening_year = is_opening_year


# Shared with the permanent report-to-registry crosswalk builder.
normalise = registry.normalise_operator


def main() -> None:
    inst = pd.read_csv(INSTITUTIONS)
    nonprofit = inst[inst["type"] == "非營利"]

    rows: list[dict] = []
    for path in sorted(EXTRACT_DIR.glob("*.json")):
        m = FILENAME_RE.match(path.stem)
        if not m:
            continue
        _code, short, year = m.groups()
        payload = json.loads(path.read_text(encoding="utf-8"))
        note = payload.get("note_1") or {}
        bs = payload.get("balance_sheet") or {}

        matches = nonprofit[nonprofit["title"].str.contains(short, regex=False)]
        registry_ops = {normalise(o) for o in matches["operator"].dropna() if o}
        capacity = matches["count_approved"].max() if not matches.empty else None

        extracted_op = normalise(note.get("operator"))
        # A 園 whose contract was re-tendered has several registry operators; the
        # report only needs to match one of them.
        op_ok: bool | None
        if not extracted_op or not registry_ops:
            op_ok = None
        else:
            op_ok = any(
                extracted_op in r or r in extracted_op for r in registry_ops
            )

        # The period reads "108年8月1日至112年7月31日", so a substring test against
        # "111" fails even though 111 學年度 sits inside it. Parse the endpoints.
        period = "".join(str(note.get("contract_period") or "").split())
        period_ok = _period_covers(period, int(year))
        opening_year = _is_opening_year(period, int(year))

        assets = bs.get("total_assets")
        if opening_year:
            # Newly opened 園 has not accumulated a typical asset base; the band
            # below is calibrated on mature 園 and does not apply to it.
            per_place = (
                round(float(assets) / float(capacity))
                if assets and capacity else None
            )
            magnitude_ok = None
        elif assets and capacity and capacity > 0:
            per_place = float(assets) / float(capacity)
            magnitude_ok = ASSETS_PER_PLACE_LOW <= per_place <= ASSETS_PER_PLACE_HIGH
        else:
            per_place, magnitude_ok = None, None

        rows.append(
            {
                "file": path.name, "short_name": short, "academic_year": year,
                "extracted_operator": note.get("operator") or "",
                "registry_operators": " | ".join(sorted(registry_ops)),
                "operator_match": op_ok,
                "contract_period": note.get("contract_period") or "",
                "period_contains_year": period_ok,
                "opening_year": int(opening_year),
                "total_assets": assets,
                "registry_capacity": capacity,
                "assets_per_place": (
                    per_place if opening_year else
                    (round(per_place) if per_place else None)
                ),
                "magnitude_plausible": magnitude_ok,
            }
        )

    if not rows:
        sys.exit("沒有可稽核的抽取結果")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {OUT}  ({len(rows)} 份抽取)\n")

    op_bad = [r for r in rows if r["operator_match"] is False]
    period_bad = [r for r in rows if r["period_contains_year"] is False]
    mag_bad = [r for r in rows if r["magnitude_plausible"] is False]

    print("=== 身分稽核 ===")
    print(f"  受託法人與主檔相符      {sum(1 for r in rows if r['operator_match']):>3}"
          f" / 不符 {len(op_bad)} / 無法比對"
          f" {sum(1 for r in rows if r['operator_match'] is None)}")
    print(f"  契約期間包含該學年度    {sum(1 for r in rows if r['period_contains_year']):>3}"
          f" / 不含 {len(period_bad)}")
    opening = sum(r["opening_year"] for r in rows)
    print(f"  資產規模合理            {sum(1 for r in rows if r['magnitude_plausible']):>3}"
          f" / 不合理 {len(mag_bad)} / 開辦首年不列入判定 {opening}")

    if op_bad:
        print("\n⚠️ 受託法人與主檔不符（可能為頁面錯置或契約更替）：")
        for r in op_bad:
            print(f"  {r['file']}")
            print(f"    抽取：{r['extracted_operator']}")
            print(f"    主檔：{r['registry_operators']}")

    if period_bad:
        print("\n⚠️ 契約期間未涵蓋該學年度：")
        for r in period_bad:
            print(f"  {r['file']}｜{r['contract_period']}")

    if mag_bad:
        print("\n⚠️ 資產規模與核定人數不成比例：")
        for r in mag_bad:
            print(
                f"  {r['file']}｜資產 {r['total_assets']:,.0f}"
                f" / 核定 {r['registry_capacity']} 人"
                f" = 每人 {r['assets_per_place']:,} 元"
            )

    if not (op_bad or period_bad or mag_bad):
        print("\n✓ 未發現任何身分錯置徵兆")


if __name__ == "__main__":
    main()
