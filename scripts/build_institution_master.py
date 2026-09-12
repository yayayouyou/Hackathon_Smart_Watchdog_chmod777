"""Build the 新北市 institution master table + penalty label table.

This is the zero-OCR baseline: it produces a usable risk dataset for all ~1,200
新北市 preschools straight from the public registry, before any PDF is touched.

Outputs
    data/processed/institutions_ntpc.csv   one row per registry entry (incl. 分班)
    data/processed/penalties_ntpc.csv      one row per penalty record
    data/processed/operators_ntpc.csv       one row per 受託法人 (非營利園 only)
"""

from __future__ import annotations

import collections
import csv
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.scrape import registry

CITY = "新北市"
OUT = pathlib.Path("data/processed")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    insts = registry.build_institutions(city=CITY)
    print(f"{CITY}: {len(insts)} registry entries")

    # --- institutions -----------------------------------------------------
    inst_path = OUT / "institutions_ntpc.csv"
    fields = [
        "id", "title", "parent", "entity", "type", "town", "owner", "operator",
        "count_approved", "monthly", "reg_date", "is_active",
        "size", "size_in", "size_out", "indoor_area_per_child",
        "shuttle", "after_care", "pre_public",
        "penalty_count", "total_fine", "nonmonetary_sanction_count",
    ]
    with inst_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        for i in insts:
            area_per_child = i.indoor_area_per_child
            w.writerow(
                {
                    "id": i.id, "title": i.title, "parent": i.parent,
                    "entity": i.entity, "type": i.type,
                    "town": i.town, "owner": i.owner, "operator": i.operator or "",
                    "count_approved": i.count_approved, "monthly": i.monthly,
                    "reg_date": i.reg_date, "is_active": i.is_active,
                    "size": i.size, "size_in": i.size_in, "size_out": i.size_out,
                    "indoor_area_per_child": (
                        round(area_per_child, 2) if area_per_child else ""
                    ),
                    "shuttle": int(i.shuttle), "after_care": int(i.after_care),
                    "pre_public": i.pre_public,
                    "penalty_count": i.penalty_count, "total_fine": i.total_fine,
                    "nonmonetary_sanction_count": i.nonmonetary_sanction_count,
                }
            )
    print(f"wrote {inst_path}")

    # --- penalties --------------------------------------------------------
    pen_path = OUT / "penalties_ntpc.csv"
    with pen_path.open("w", newline="", encoding="utf-8") as fh:
        penalty_fields = [
            "id", "title", "type", "date", "article", "law", "fine", "actor",
            "punishment", "sanction_type", "is_monetary", "actor_role",
            "actor_name", "penalty_group_id", "group_record_index",
            "group_record_count", "source_row_count", "law_variants",
        ]
        w = csv.DictWriter(fh, fieldnames=penalty_fields, lineterminator="\n")
        w.writeheader()
        n = 0
        source_rows = 0
        for i in insts:
            for p in i.penalties:
                fine = p.get("fine")
                source_row_count = int(p.get("source_row_count") or 1)
                w.writerow(
                    {
                        "id": i.id, "title": i.title, "type": i.type,
                        "date": p.get("date", ""),
                        "article": registry.law_article(p.get("law")) or "",
                        "law": (p.get("law") or "").strip(),
                        "fine": fine,
                        "actor": p.get("actor", ""),
                        "punishment": p.get("punishment", ""),
                        "sanction_type": p["sanction_type"],
                        "is_monetary": int(fine is not None),
                        "actor_role": p["actor_role"],
                        "actor_name": p.get("actor_name", ""),
                        "penalty_group_id": p["penalty_group_id"],
                        "group_record_index": p["group_record_index"],
                        "group_record_count": p["group_record_count"],
                        "source_row_count": source_row_count,
                        "law_variants": json.dumps(
                            p.get("law_variants") or [],
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    }
                )
                n += 1
                source_rows += source_row_count
    print(
        f"wrote {pen_path} ({n} canonical records; "
        f"{source_rows - n} duplicate source rows collapsed)"
    )

    # --- operators (非營利園) ----------------------------------------------
    ops: dict[str, dict] = collections.defaultdict(
        lambda: {
            "parks": 0,
            "penalised": 0,
            "records": 0,
            "fines": 0,
            "nonmonetary": 0,
            "names": [],
        }
    )
    for i in insts:
        if i.type != "非營利" or not i.operator:
            continue
        o = ops[i.operator]
        o["parks"] += 1
        o["records"] += i.penalty_count
        o["fines"] += i.total_fine
        o["nonmonetary"] += i.nonmonetary_sanction_count
        o["penalised"] += 1 if i.penalty_count else 0
        o["names"].append(i.title.split("非營利")[0].replace(CITY, ""))

    op_path = OUT / "operators_ntpc.csv"
    with op_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "operator", "parks", "penalised_parks", "penalty_records",
                "total_fine", "nonmonetary_sanctions", "penalised_share",
                "parks_list",
            ],
            lineterminator="\n",
        )
        w.writeheader()
        for name, o in sorted(ops.items(), key=lambda kv: -kv[1]["parks"]):
            w.writerow(
                {
                    "operator": name, "parks": o["parks"],
                    "penalised_parks": o["penalised"], "penalty_records": o["records"],
                    "total_fine": o["fines"],
                    "nonmonetary_sanctions": o["nonmonetary"],
                    "penalised_share": round(o["penalised"] / o["parks"], 3),
                    "parks_list": ",".join(o["names"]),
                }
            )
    print(f"wrote {op_path} ({len(ops)} operators)")

    # --- console summary --------------------------------------------------
    # Report per *physical* 園, not per registry entry: contract re-tendering and
    # 分班 both create extra rows for the same institution, which inflates the
    # denominator and can hide penalties filed against a sibling entry.
    ent: dict[tuple[str, str], bool] = {}
    for i in insts:
        key = (i.type, i.entity)
        ent[key] = ent.get(key, False) or bool(i.penalty_count)
    by_type: dict[str, list[int]] = {}
    for (t, _e), pen in ent.items():
        acc = by_type.setdefault(t, [0, 0])
        acc[0] += 1
        acc[1] += int(pen)
    print("\nby type（按實體園計，非登記筆數）:")
    for t, (n, p_) in sorted(by_type.items(), key=lambda kv: -kv[1][0]):
        rows = sum(1 for i in insts if i.type == t)
        print(f"  {t:<6} 實體 {n:>5}（登記 {rows:>5} 筆）  受罰 {p_:>4} ({p_ / n * 100:.1f}%)")


if __name__ == "__main__":
    main()
