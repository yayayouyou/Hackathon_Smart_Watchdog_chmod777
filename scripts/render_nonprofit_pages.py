"""Render the statement pages of 非營利園 reports for visual extraction.

Free (no model calls) and idempotent, so it can be re-run per batch. Output goes
to data/interim/ which is gitignored -- the images are 115 MB per 18 reports and
are always regenerable from data/raw/.

Usage
    # one 學年度
    PYTHONPATH=src .venv/bin/python scripts/render_nonprofit_pages.py 113
    # only reports not yet extracted
    PYTHONPATH=src .venv/bin/python scripts/render_nonprofit_pages.py 113 --pending
"""

from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.ingest.nonprofit_locator import locate_pages, render_page

REPORT_DIR = pathlib.Path("data/raw/資料集/非營利園財報")
OUT_ROOT = pathlib.Path("data/interim/pages")
EXTRACTED = pathlib.Path("data/extracted/nonprofit")

# Render from p4 (the first statement page) to the end of the document. Two notes
# carry findings the statements alone cannot show:
#   附註三  composition of 其他收入 / 其他支出, which are exactly equal in many
#           reports (10-24% of income) -- 附註二(八) forbids netting, so whether
#           these are recorded gross is only answerable here.
#   附註五  關係人交易. 中園 113 discloses 203,350 of 行政管理費 to its operator
#           while the income statement shows 311,956 -- a 108,606 gap inside one
#           report. That class of internal contradiction has a naturally low base
#           rate, unlike the corpus-wide behaviours we mistook for red flags.
# Their position is not predictable: a p4-p16 range held for 113 學年度 but missed
# both in N01 110, where they sit on printed pages 19 and 21 -- and 110-era reports
# do not even use standalone 附註三/五 headings, folding the same content into
# 三、重要會計項目說明 and 五、關係人交易. Rather than guess a second fixed bound,
# render everything; data/interim/ is gitignored and regenerable.
FIRST_PAGE = 4
DPI = 170

FILENAME_RE = re.compile(r"^(N\d\d)(.+?)_(\d{3})學年度")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("用法：render_nonprofit_pages.py <學年度> [--pending]")
    year = sys.argv[1]
    pending_only = "--pending" in sys.argv

    src = REPORT_DIR / f"{year}學年度"
    if not src.is_dir():
        sys.exit(f"找不到 {src}（data/raw/ 是否已用 scripts/setup_raw_data.py 還原？）")

    done = {p.stem for p in EXTRACTED.glob("*.json")}
    rendered = skipped = 0
    for pdf in sorted(src.glob("*.pdf")):
        m = FILENAME_RE.match(pdf.name)
        if not m:
            print(f"  ⚠ 檔名不符預期，略過：{pdf.name}")
            continue
        code, short, _ = m.groups()
        key = f"{code}_{short}"
        if pending_only and f"{key}_{year}" in done:
            skipped += 1
            continue

        layout = locate_pages(pdf)
        dest = OUT_ROOT / year / key
        dest.mkdir(parents=True, exist_ok=True)
        last = layout.page_count
        for page_no in range(FIRST_PAGE, last + 1):
            target = dest / f"p{page_no:02d}.png"
            if target.exists():
                continue
            target.write_bytes(render_page(pdf, page_no - 1, dpi=DPI))
        rendered += 1
        print(f"  {key:<14} 頁數={layout.page_count:>3}  p{FIRST_PAGE}-p{last}")

    print(f"\n渲染 {rendered} 份，略過 {skipped} 份（已抽取）")
    print(f"影像位置：{OUT_ROOT / year}/")


if __name__ == "__main__":
    main()
