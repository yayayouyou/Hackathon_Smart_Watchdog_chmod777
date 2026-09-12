"""Measure the page-level extraction against the extraction we already trusted.

Until now this project had no number for how accurate its financial extraction is.
``score_extraction.py`` compares against hand-marked ground truth, which exists for
five statements. ``validate_extraction.py`` checks internal arithmetic, which
catches a mis-read digit only when it breaks a sum.

This does something cheaper and broader: the two extractions **overlap**. The
earlier pipeline read each report's 資產負債表 and 收支餘絀表 into named fields;
the page-level pipeline re-read those same printed pages, months later, with a
different prompt, a different schema, and a different model. Where they agree on a
figure, two independent readings of the same scan produced the same number. Where
they disagree, exactly one of them is wrong and the page is worth a human's time.

This is not ground truth and it is not presented as accuracy: two extractions can
agree and both be wrong, most plausibly on a figure that is genuinely hard to read.
What it bounds is *disagreement* -- and a disagreement rate is the thing we can act
on, because every mismatch is a specific page and a specific line item.

Matching is by printed label, normalised only for whitespace, because both sides
were told to transcribe labels verbatim. A label that exists on one side and not
the other is reported separately from a value mismatch: those are different
problems, one about coverage and one about correctness.

Usage
    PYTHONPATH=src .venv/Scripts/python scripts/check_pagewise_agreement.py
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

OLD = pathlib.Path("data/extracted/nonprofit")
PAGES = pathlib.Path("data/extracted/nonprofit_pages")
OUT = pathlib.Path("data/processed/pagewise_agreement.csv")

#: Balance-sheet fields whose printed label is unambiguous. Deliberately excludes
#: reserve_asset/reserve_liability: 業務發展準備金 (asset) and 業務發展準備
#: (liability) differ by one character and appear in both halves of the same
#: table, so a label match cannot tell them apart and a wrong pairing would
#: manufacture a disagreement that is really an artefact of this script.
BALANCE_LABELS = {
    "cash": "現金及銀行存款",
    "prepaid_receipts": "預收款項",
    "current_assets_total": "流動資產合計",
    "current_liabilities_total": "流動負債合計",
    "accumulated_surplus": "累積餘絀",
    "current_surplus": "本期餘絀",
    "equity_total": "餘絀總額",
    "total_assets": "資產總計",
    "total_liabilities": "負債總額",
}


def flat(text: object) -> str:
    return "".join(str(text or "").split())


def page_tables(report: str, page: int) -> list[dict]:
    f = PAGES / report / f"p{page:02d}.json"
    if not f.exists():
        return []
    d = json.loads(f.read_text(encoding="utf-8"))
    return [t for t in (d.get("tables") or []) if t.get("aligned") is not False]


def first_numeric_column(table: dict) -> int:
    """Which column holds the current period? Column 0 unless it is all blank."""
    labels = table.get("period_labels") or []
    for i in range(len(labels)):
        if any((r.get("values") or [None] * (i + 1))[i] is not None
               for r in (table.get("items") or []) if len(r.get("values") or []) > i):
            return i
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="比對新舊兩套抽取在重疊頁上的一致性")
    ap.add_argument("--tol", type=float, default=0.5, help="容許誤差（元）")
    a = ap.parse_args()
    use_utf8()

    rows: list[dict] = []
    stats: collections.Counter = collections.Counter()
    reports_compared: set = set()

    for old_file in sorted(OLD.glob("*.json")):
        d = json.loads(old_file.read_text(encoding="utf-8"))
        report = f"{d['code']}_{d['short_name']}_{d['academic_year']}"

        # ── 收支餘絀表：逐列比對，label 明確，是主要證據 ──────────────
        inc = d.get("income_statement") or {}
        if inc.get("page"):
            tables = page_tables(report, inc["page"])
            new_rows: dict[str, list] = {}
            for t in tables:
                for r in t.get("items") or []:
                    new_rows.setdefault(flat(r.get("label")), r.get("values") or [])
            if new_rows:
                reports_compared.add(report)
                for line in inc.get("lines") or []:
                    key = flat(line.get("label"))
                    got = new_rows.get(key)
                    if got is None:
                        stats["收支餘絀表_舊有新無"] += 1
                        rows.append({"report": report, "statement": "收支餘絀表",
                                     "item": line.get("label"), "field": "—",
                                     "old": "", "new": "", "verdict": "舊有新無"})
                        continue
                    # 舊格式的欄位順序是 預算數, 決算數
                    for idx, field in ((0, "budget"), (1, "actual")):
                        ov = line.get(field)
                        nv = got[idx] if idx < len(got) else None
                        if ov is None and nv is None:
                            continue
                        if ov is not None and nv is not None and abs(ov - nv) <= a.tol:
                            stats["收支餘絀表_相符"] += 1
                            continue
                        verdict = ("兩邊皆有但不同" if ov is not None and nv is not None
                                   else ("舊有新無" if ov is not None else "新有舊無"))
                        stats[f"收支餘絀表_{verdict}"] += 1
                        rows.append({"report": report, "statement": "收支餘絀表",
                                     "item": line.get("label"), "field": field,
                                     "old": ov, "new": nv, "verdict": verdict})

        # ── 資產負債表：只比對標籤明確的欄位 ──────────────────────────
        bal = d.get("balance_sheet") or {}
        if bal.get("page"):
            tables = page_tables(report, bal["page"])
            lookup: dict[str, float | None] = {}
            for t in tables:
                col = first_numeric_column(t)
                for r in t.get("items") or []:
                    vals = r.get("values") or []
                    lookup.setdefault(flat(r.get("label")),
                                      vals[col] if col < len(vals) else None)
            if lookup:
                reports_compared.add(report)
                for field, label in BALANCE_LABELS.items():
                    ov, nv = bal.get(field), lookup.get(label)
                    if ov is None and nv is None:
                        continue
                    if ov is not None and nv is not None and abs(ov - nv) <= a.tol:
                        stats["資產負債表_相符"] += 1
                        continue
                    verdict = ("兩邊皆有但不同" if ov is not None and nv is not None
                               else ("舊有新無" if ov is not None else "新有舊無"))
                    stats[f"資產負債表_{verdict}"] += 1
                    rows.append({"report": report, "statement": "資產負債表",
                                 "item": label, "field": field,
                                 "old": ov, "new": nv, "verdict": verdict})

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["report", "statement", "item", "field",
                                           "old", "new", "verdict"])
        w.writeheader()
        w.writerows(rows)

    if not reports_compared:
        print("沒有可比對的重疊頁——頁級抽取尚未涵蓋任何一份報告的資產負債表或收支餘絀表。")
        return

    print(f"可比對 {len(reports_compared)} 個園-學年度")
    for statement in ("收支餘絀表", "資產負債表"):
        same = stats[f"{statement}_相符"]
        diff = stats[f"{statement}_兩邊皆有但不同"]
        old_only = stats[f"{statement}_舊有新無"]
        new_only = stats[f"{statement}_新有舊無"]
        total = same + diff
        if not total and not old_only and not new_only:
            continue
        rate = same / total * 100 if total else 0.0
        print(f"\n{statement}")
        print(f"  兩邊都有數字的格：{total:,}　相符 {same:,}（{rate:.2f}%）　不同 {diff}")
        print(f"  舊有新無 {old_only}　新有舊無 {new_only}")

    if rows:
        bad = [r for r in rows if r["verdict"] == "兩邊皆有但不同"]
        if bad:
            print(f"\n⚠ {len(bad)} 個格子兩邊都有數字卻不同，前 12 筆：")
            for r in bad[:12]:
                print(f"    {r['report']:<18}{str(r['item'])[:14]:<16}{r['field']:<8}"
                      f"舊 {r['old']}　新 {r['new']}")
    print(f"\n明細寫入 {OUT}")


if __name__ == "__main__":
    main()
