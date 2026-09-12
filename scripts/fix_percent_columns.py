"""Move paired 「金額 / %」 sub-columns out of ``period_labels`` in extracted pages.

The 1,672 pages extracted on 2026-09-12 predate the convention ``CLAUDE.md`` now
states: ``period_labels`` holds columns, and 資產負債表's 「%」 is a second
rendering of the 金額 column beside it rather than a period of its own. They came
back with four headers for two 基準日.

**The values were not shifted.** 現金及銀行存款 read ``[5803751, 39, 5348001, 41]``
against those four headers -- amount, its percentage, amount, its percentage --
so every figure sits under the header that describes it, which is why the
cell-by-cell agreement check against the earlier extraction came back 726/726.
That makes this a representation that diverges from the project's convention, not
corrupted data, and it can be repaired by rearranging rather than re-reading: no
model calls, no cost, no risk of a different answer coming back.

Re-extracting would also have been defensible. It was not chosen because a
deterministic rearrangement of known-correct numbers is safer than asking a model
the same question twice and hoping the second answer matches the first.

Idempotent: a page already in the new shape is left untouched and reported as
skipped, so this can be re-run after any future extraction without double-moving.

Usage
    PYTHONPATH=src .venv/Scripts/python scripts/fix_percent_columns.py --dry-run
    PYTHONPATH=src .venv/Scripts/python scripts/fix_percent_columns.py
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.console import use_utf8
from smart_watchdog.extract.pagewise import split_percent_columns, validate_page

PAGES = pathlib.Path("data/extracted/nonprofit_pages")


def main() -> None:
    ap = argparse.ArgumentParser(description="把成對的占比欄移進 percents")
    ap.add_argument("--dry-run", action="store_true", help="只報告，不寫檔")
    a = ap.parse_args()
    use_utf8()

    files = sorted(p for p in PAGES.glob("*/p*.json")
                   if not p.parent.name.startswith("_"))
    changed_pages = changed_tables = 0
    broke: list[str] = []
    kinds: collections.Counter = collections.Counter()

    for f in files:
        payload = json.loads(f.read_text(encoding="utf-8"))
        hits = 0
        for table in payload.get("tables") or []:
            if split_percent_columns(table):
                hits += 1
        if not hits:
            continue
        # The rearrangement must leave every row aggregatable; if it does not,
        # something about this table was not what the transform assumed.
        problems = validate_page(payload)
        if problems:
            broke.append(f"{f.parent.name}/{f.name}: {problems[0]}")
            continue
        changed_pages += 1
        changed_tables += hits
        kinds[payload.get("page_kind")] += hits
        if not a.dry_run:
            tmp = f.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(tmp, f)

    print(f"掃描 {len(files)} 頁")
    print(f"  需調整 {changed_pages} 頁、{changed_tables} 張表"
          f"{'（--dry-run，未寫檔）' if a.dry_run else '，已寫回'}")
    print(f"  其餘 {len(files) - changed_pages - len(broke)} 頁本來就符合慣例")
    if kinds:
        print("\n依頁面性質：")
        for k, n in kinds.most_common():
            print(f"  {k:<20}{n:>5} 張表")
    if broke:
        print(f"\n⚠ {len(broke)} 頁轉換後對不齊，已跳過不寫入：")
        for b in broke[:10]:
            print(f"    {b}")


if __name__ == "__main__":
    main()
