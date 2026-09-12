"""Turn each report's printed table of contents into a list of pages worth reading.

Reading all 5,162 pages costs about seven hours at this account's token ceiling,
and most of those pages are covers, contents and auditor prose that carry no
figure any downstream check can use. But the pages that *do* matter are not at
predictable positions: 附表一 sits on printed page 23 in N01 安溪 110 and on 24 in
N01 安溪 113, and ``EXTRACTION_GUIDE.md`` records earlier attempts to guess a
fixed range that missed sections entirely.

Each report answers the question itself. Page 2 is its 目錄, and it lists every
section against a printed page number:

    淨值變動表 …………………………………………………… 8
    附表二：經費流用及勻支檢查表 ……………………… 24

So: read one page per report, and let that page say which others to fetch. The
pages we then request are the ones that report claims carry those sections --
not an extrapolation from a different report that happened to be laid out the
same way.

**The plan is a claim, and the extraction checks it.** Every extracted page
reports the ``<代號>-<頁碼>`` printed in its own footer. ``verify_plan`` compares
that against what the 目錄 promised, so a misread contents line shows up as a
mismatch rather than as quietly wrong data filed under the right name.

Usage
    # 1. read the contents page of every report (one page each)
    python run.py extract-pages --only-page 2
    # 2. build the plan
    PYTHONPATH=src .venv/Scripts/python scripts/plan_from_toc.py
    # 3. fetch only what the plan names
    python run.py extract-pages --pages-from data/runtime/targeted_pages.json
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import fitz

from smart_watchdog.console import use_utf8

PAGES = pathlib.Path("data/extracted/nonprofit_pages")
REPORT_DIR = pathlib.Path("data/raw/資料集/非營利園財報")
OUT = pathlib.Path("data/runtime/targeted_pages.json")
TOC_PAGE = 2

#: Sections worth fetching, and why. Matching is by substring against the 目錄
#: entry, because the printed wording varies ("附表二：經費流用及勻支檢查表" in
#: 113, "經費流用及勻支檢查表" alone in some 110 reports).
WANTED = {
    "淨值變動表": "餘絀總額的第二來源，交叉驗證資產負債表",
    "現金流量表": "期末現金應等於資產負債表的現金及銀行存款",
    "重要會計項目說明": "附註三：代收代付／代收補助／專案補助——判定「收入不得以淨額入帳」",
    "收支餘絀表-功能別": "支出的功能別拆解",
    "經費流用": "附表二：經費流用及勻支檢查表——判定「人事費不得流出」",
    "各學年收支預決算比較": "單表內的跨年度比較，省去跨檔對齊誤差",
}

#: "附表五：到園檢查表（不適用）" and similar are listed but carry nothing.
SKIP_IF_CONTAINS = ("不適用",)

#: 目錄 line: a name, a run of leaders, then a page number.
#: The leader glyph is not consistent across reports -- N01 uses `…` (U+2026)
#: while N11 新林 113 uses `⋯` (U+22EF). A class that covers only the first
#: silently yields zero entries for the other, which is why the fallback below
#: exists as well: a contents page we cannot read must not mean "skip this
#: report", it must mean "read all of it".
ENTRY = re.compile(r"^[\s　]*(.+?)[.．·‧⋯…。、\s　]{3,}(\d{1,3})\s*$")


def parse_toc(payload: dict) -> list[tuple[str, int]]:
    """Extract (section name, printed page) pairs from a 目錄 page."""
    rows: list[tuple[str, int]] = []
    for section in payload.get("text_sections") or []:
        for line in (section.get("text") or "").splitlines():
            m = ENTRY.match(line.strip())
            if m:
                rows.append((m.group(1).strip("　 "), int(m.group(2))))
    return rows


def wanted_ranges(rows: list[tuple[str, int]], last_page: int) -> dict[int, str]:
    """Map each page we want to the 目錄 entry that justified fetching it.

    A section runs until the next entry starts, so its span comes from its
    neighbour rather than from a fixed assumption about section length -- that is
    what makes 附註三's 7-or-8 page body come out right in both years.
    """
    pages: dict[int, str] = {}
    for i, (name, start) in enumerate(rows):
        if any(s in name for s in SKIP_IF_CONTAINS):
            continue
        why = next((w for w in WANTED if w in name), None)
        if why is None:
            continue
        nxt = rows[i + 1][1] if i + 1 < len(rows) else start + 1
        end = max(start, min(nxt - 1, last_page))
        for p in range(start, end + 1):
            pages.setdefault(p, name)
    return pages


def page_count(pdf: pathlib.Path) -> int:
    doc = fitz.open(pdf)
    try:
        return doc.page_count
    finally:
        doc.close()


def find_pdf(report: str) -> pathlib.Path | None:
    """`N01_安溪_113` -> the 113學年度 PDF whose filename starts with N01."""
    code, _short, year = report.rsplit("_", 2)
    hits = sorted((REPORT_DIR / f"{year}學年度").glob(f"{code}*.pdf"))
    return hits[0] if hits else None


def main() -> None:
    ap = argparse.ArgumentParser(description="用每份報告自己的目錄決定要抽哪些頁")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()
    use_utf8()

    plan: dict[str, dict] = {}
    no_toc: list[str] = []
    unparsed: list[str] = []
    fellback: list[str] = []
    offsets: collections.Counter = collections.Counter()
    reasons: collections.Counter = collections.Counter()

    reports = sorted(d.name for d in PAGES.iterdir()
                     if d.is_dir() and not d.name.startswith("_"))
    for report in reports:
        toc_file = PAGES / report / f"p{TOC_PAGE:02d}.json"
        if not toc_file.exists():
            no_toc.append(report)
            continue
        payload = json.loads(toc_file.read_text(encoding="utf-8"))
        if payload.get("page_kind") != "toc":
            unparsed.append(f"{report}（p{TOC_PAGE} 不是目錄，是 {payload.get('page_kind')}）")
            continue

        # The 目錄 cites printed page numbers. Every page also prints its own
        # number in the footer, so the offset between the two is measurable
        # rather than assumed -- and so far it has been 0 on all 710 pages read.
        printed = payload.get("printed_page")
        offset = (payload.get("pdf_page", TOC_PAGE) - printed) if printed else 0
        offsets[offset] += 1

        pdf = find_pdf(report)
        if pdf is None:
            unparsed.append(f"{report}（找不到對應 PDF）")
            continue
        total = page_count(pdf)

        rows = parse_toc(payload)
        wanted = wanted_ranges(rows, total - offset) if rows else {}
        if not wanted:
            # Falling back to the whole report is deliberate. A contents page we
            # cannot parse is an unknown, and the cost of reading one report in
            # full is ~40 pages; the cost of silently skipping it is a 園 missing
            # from every finding downstream, with nothing to show it is missing.
            why = "目錄無法解析" if not rows else "目錄無白名單章節"
            fellback.append(f"{report}（{why}，改為全份抽取）")
            wanted = dict.fromkeys(range(1, total + 1), "目錄無法解析，全份抽取")

        for name in wanted.values():
            key = next((w for w in WANTED if w in name), "（全份抽取）")
            reasons[key] += 1
        plan[report] = {
            "pdf": str(pdf),
            "offset": offset,
            "pages": sorted(p + offset for p in wanted),
            "why": {str(p + offset): n for p, n in sorted(wanted.items())},
        }

    pathlib.Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(a.out).write_text(
        json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")

    total_pages = sum(len(v["pages"]) for v in plan.values())
    done = sum(1 for r, v in plan.items() for p in v["pages"]
               if (PAGES / r / f"p{p:02d}.json").exists())
    print(f"目錄可解析 {len(plan)}/{len(reports)} 份報告")
    print(f"鎖定 {total_pages} 頁（平均 {total_pages / max(len(plan), 1):.1f} 頁/份）"
          f"，其中 {done} 頁已抽過，待抽 {total_pages - done} 頁")
    print(f"印刷頁碼與 PDF 頁碼的位移分布：{dict(offsets)}")
    print()
    print("各章節命中份數：")
    for key, n in reasons.most_common():
        print(f"  {key:<14}{n:>4} 份　{WANTED[key]}")
    if no_toc:
        print(f"\n⚠ {len(no_toc)} 份尚未抽目錄頁"
              f"（先跑 `python run.py extract-pages --only-page 2`）")
    if fellback:
        print(f"\n⚠ {len(fellback)} 份改為全份抽取（保底，不會漏掉整份報告）：")
        for f in fellback[:10]:
            print(f"    {f}")
    if unparsed:
        print(f"\n⚠ {len(unparsed)} 份目錄無法使用：")
        for u in unparsed[:10]:
            print(f"    {u}")
    print(f"\n寫入 {a.out}")


if __name__ == "__main__":
    main()
