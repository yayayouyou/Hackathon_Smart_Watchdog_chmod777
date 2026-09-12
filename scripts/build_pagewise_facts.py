"""Normalise the page-level extraction into one long table of facts.

The page JSON is shaped like a scanned page -- some tables, some prose. Nothing
downstream wants that shape. What the compliance checks and the feature builders
want is "for this 園, this 學年度, this section, this line item, this period: this
number", one row at a time, with enough provenance attached to walk back to the
page it came from.

Three decisions worth stating, because each one is a place where a convenient
shortcut would quietly corrupt the output:

**Unaligned tables are refused, not repaired.** ``validate_page`` marks a table
``aligned: false`` when its rows carry a different number of values than it has
column headers. There is no safe way to guess which column a stray number belongs
to, and guessing wrong files a figure under the wrong year -- the exact failure
that made the page schema get rewritten. Those tables are counted and named in
the run summary so the number is visible, never silently dropped.

**A section is identified by what the page prints, not by where it sits.** Routing
uses the table's own ``title`` and, when the table has no title, the
``context_heading`` above it. Roughly a fifth of the detail tables print no title
at all, so without the heading a 代收代付 table and a 專案補助 table are
indistinguishable -- and they mean opposite things for 附註二(八)'s ban on
recording revenue net of expenditure.

**A blank cell stays ``None``.** It travels all the way to the CSV as an empty
field, never as 0, for the reason ``CLAUDE.md`` gives: an unbudgeted 業務發展費 is
an audit finding, and a zero is a false statement about a real institution.

Usage
    PYTHONPATH=src .venv/Scripts/python scripts/build_pagewise_facts.py
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.console import use_utf8

PAGES = pathlib.Path("data/extracted/nonprofit_pages")
FACTS = pathlib.Path("data/processed/nonprofit_pagewise_facts.csv")
SECTIONS = pathlib.Path("data/processed/nonprofit_pagewise_sections.csv")
REFUSED = pathlib.Path("data/processed/nonprofit_pagewise_refused.csv")

#: Canonical section keys, matched against the table's title first and its
#: context_heading second. Order matters: the first hit wins, so the more
#: specific patterns are listed before the general ones ("代收代付" before
#: "重要會計項目說明", which is the note that contains it).
ROUTES: list[tuple[str, tuple[str, ...]]] = [
    ("cash_flow", ("現金流量表",)),
    ("equity_change", ("淨值變動表",)),
    ("budget_transfer", ("經費流用", "勻支")),
    ("multiyear_compare", ("各學年收支預決算比較", "收支預決算比較")),
    ("income_by_function", ("功能別",)),
    ("agency_passthrough", ("代收代付",)),
    ("agency_subsidy", ("代收補助",)),
    ("project_subsidy", ("專案補助",)),
    ("net_difference", ("淨額差異",)),
    # 提列準備的明細。這兩條直接對應「資遣費準備金 資產=負債」與
    # 「業務發展準備提列上限 20%」兩項法遵檢核的判定依據。
    ("severance_reserve", ("資遣費準備",)),
    ("development_reserve", ("業務發展準備",)),
    # 附註二(八) 禁止收支相抵後淨額入帳，而多份報告的「其他收入」與
    # 「其他支出」金額完全相同——是否總額入帳只有這張明細答得出來。
    ("other_income_expense", ("其他收入及其他支出", "其他收入及支出")),
    ("admin_fee", ("行政管理費",)),
    ("payables", ("其他應付款", "應付款項")),
    ("cash_detail", ("現金及銀行存款",)),
    ("performance_review", ("績效考評",)),
    ("auditor_checklist", ("會計師查核附表", "查核項目")),
    ("personnel_detail", ("人事費",)),
    ("operating_detail", ("業務費",)),
    ("material_detail", ("材料費",)),
    ("maintenance_detail", ("維護費", "修繕購置")),
    ("surplus_execution", ("賸餘款",)),
    ("related_party", ("關係人",)),
    ("property", ("財產",)),
    ("balance_sheet", ("資產負債表",)),
    ("income_statement", ("收支餘絀表",)),
    ("note_items", ("重要會計項目說明",)),
]


def route(title: str | None, heading: str | None) -> str:
    """Which canonical section is this table? Title wins; heading is the fallback."""
    for text in (title, heading):
        if not text:
            continue
        flat = "".join(str(text).split())
        for key, needles in ROUTES:
            if any(n in flat for n in needles):
                return key
    return "unrouted"


def main() -> None:
    ap = argparse.ArgumentParser(description="把頁級抽取正規化成長表")
    a = ap.parse_args()
    use_utf8()
    del a

    facts: list[dict] = []
    sections: list[dict] = []
    refused: list[dict] = []
    seen_pages = 0
    kinds: collections.Counter = collections.Counter()
    routed: collections.Counter = collections.Counter()
    reports: set = set()

    for page_file in sorted(PAGES.glob("*/p*.json")):
        if page_file.parent.name.startswith("_"):
            continue
        d = json.loads(page_file.read_text(encoding="utf-8"))
        seen_pages += 1
        kinds[d.get("page_kind")] += 1
        code, short, year = d["code"], d["short_name"], d["academic_year"]
        reports.add((code, year))
        base = {"code": code, "short_name": short, "academic_year": year,
                "pdf_page": d["pdf_page"], "page_kind": d.get("page_kind")}

        for t_i, table in enumerate(d.get("tables") or [], start=1):
            title = table.get("title")
            heading = table.get("context_heading")
            section = route(title, heading)
            labels = table.get("period_labels") or []
            rows = table.get("items") or []
            n_valued = sum(1 for r in rows if any(
                v is not None for v in (r.get("values") or [])))

            sections.append({**base, "table_index": t_i, "section": section,
                             "title": title, "context_heading": heading,
                             "n_columns": len(labels), "n_rows": len(rows),
                             "n_valued_rows": n_valued,
                             "aligned": table.get("aligned", True)})
            routed[section] += 1

            if table.get("aligned") is False:
                refused.append({**base, "table_index": t_i, "section": section,
                                "title": title, "context_heading": heading,
                                "n_columns": len(labels), "n_rows": len(rows),
                                "reason": "values 長度與 period_labels 不符"})
                continue

            for row in rows:
                values = row.get("values") or []
                percents = row.get("percents") or []
                for p_i, label in enumerate(labels):
                    value = values[p_i] if p_i < len(values) else None
                    pct = percents[p_i] if p_i < len(percents) else None
                    if value is None and pct is None:
                        continue
                    facts.append({
                        **base, "table_index": t_i, "section": section,
                        "table_title": title, "context_heading": heading,
                        "period_index": p_i, "period_label": label,
                        "item_label": row.get("label"),
                        "note_ref": row.get("note_ref"),
                        "value": value, "percent": pct,
                    })

    def write(path: pathlib.Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as fh:
            if not rows:
                fh.write("")
                return
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)

    write(FACTS, facts)
    write(SECTIONS, sections)
    write(REFUSED, refused)

    print(f"讀入 {seen_pages} 頁，涵蓋 {len(reports)} 個園-學年度")
    print(f"  {FACTS}　{len(facts):,} 筆事實")
    print(f"  {SECTIONS}　{len(sections):,} 張表")
    print(f"  {REFUSED}　{len(refused)} 張表被拒收")
    print()
    print("章節路由結果（表數）：")
    for key, n in routed.most_common():
        mark = "  ⚠" if key == "unrouted" else "   "
        print(f"{mark} {key:<20}{n:>5}")
    if refused:
        print()
        print(f"⚠ {len(refused)} 張表因欄位對不齊被拒收（明細見 {REFUSED.name}）：")
        by_sec = collections.Counter(r["section"] for r in refused)
        for key, n in by_sec.most_common(6):
            print(f"    {key:<20}{n:>4} 張")
    unrouted = routed.get("unrouted", 0)
    if unrouted:
        pct = unrouted / max(sum(routed.values()), 1) * 100
        print(f"\n⚠ {unrouted} 張表（{pct:.1f}%）路由不到已知章節，"
              f"在 {SECTIONS.name} 裡 section=unrouted")


if __name__ == "__main__":
    main()
