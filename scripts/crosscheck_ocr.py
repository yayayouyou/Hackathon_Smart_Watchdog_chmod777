"""Cross-check every extracted figure against OCR of the same pages.

Why OCR is a checker here and never a source. Tesseract without a Chinese model
still reads the digits on these scans almost perfectly -- all 13 balance-sheet
values of N01 110 appear verbatim in its output -- but the labels come out as
noise, so OCR cannot say *which* figure it found. Financial extraction errors
live in exactly that gap: 現金 vs 預收款項, the 111/7/31 column vs the 110/7/31
comparative, 預算數 vs 決算數. Feeding a flattened OCR text layer to the pipeline
would reintroduce the column-interleaving problem 公校 決算書 already taught us.

Used as a checker the asymmetry works in our favour: a figure the vision model
reported that appears nowhere in the OCR digits is *evidence of a problem*, while
a figure that does appear is merely not-contradicted. This can only falsify, never
confirm, which is the right shape for a second opinion (see
docs/architecture/aws-architecture.md §OCR 備援).

Misses are candidates for human review, not proven errors: OCR drops a digit of
its own often enough that a single miss usually means "look at this page", not
"the extraction is wrong".

Run:  .venv/bin/python scripts/crosscheck_ocr.py [學年度]
"""

from __future__ import annotations

import csv
import json
import pathlib
import re
import subprocess
import sys

EXTRACT_DIR = pathlib.Path("data/extracted/nonprofit")
PAGES_ROOT = pathlib.Path("data/interim/pages")
OCR_ROOT = pathlib.Path("data/interim/ocr")
OUT = pathlib.Path("data/processed/ocr_crosscheck.csv")
FILENAME_RE = re.compile(r"^(N\d\d)_(.+?)_(\d{3})$")

# Four digits and up. Below that, a match against the page's digit soup says
# nothing -- an 87 will collide with a page number or an execution percentage.
NUMBER_RE = re.compile(r"\d[\d,]{3,}")
MIN_ABS = 1000


def ocr_page(png: pathlib.Path, cache: pathlib.Path) -> str:
    """OCR one page, caching the text so re-runs cost nothing."""
    if cache.exists():
        return cache.read_text(encoding="utf-8")
    proc = subprocess.run(
        ["tesseract", str(png), "stdout", "-l", "eng"],
        capture_output=True, text=True, check=False,
    )
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(proc.stdout, encoding="utf-8")
    return proc.stdout


def page_numbers(pages_dir: pathlib.Path, ocr_dir: pathlib.Path) -> set[int]:
    found: set[int] = set()
    for png in sorted(pages_dir.glob("p*.png")):
        text = ocr_page(png, ocr_dir / f"{png.stem}.txt")
        for token in NUMBER_RE.findall(text):
            found.add(int(token.replace(",", "")))
    return found


def extracted_figures(payload: dict) -> list[tuple[str, int]]:
    """Every material figure the model reported, as (where, value)."""
    out: list[tuple[str, int]] = []

    def take(where: str, value: object) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        if abs(value) >= MIN_ABS:
            out.append((where, int(value)))

    for field, value in (payload.get("balance_sheet") or {}).items():
        if field != "page":
            take(f"balance_sheet.{field}", value)

    for line in (payload.get("income_statement") or {}).get("lines") or []:
        label = line.get("label", "?")
        take(f"income.{label}.budget", line.get("budget"))
        take(f"income.{label}.actual", line.get("actual"))

    for note in ("note_3", "note_5"):
        block = payload.get(note) or {}
        for field, value in block.items():
            if isinstance(value, list):
                for item in value:
                    take(f"{note}.{item.get('label', '?')}", item.get("amount"))
            elif field != "page":
                take(f"{note}.{field}", value)

    return out


def main() -> None:
    year_filter = sys.argv[1] if len(sys.argv) > 1 else None

    rows: list[dict] = []
    skipped: list[str] = []
    for path in sorted(EXTRACT_DIR.glob("*.json")):
        m = FILENAME_RE.match(path.stem)
        if not m:
            continue
        code, short, year = m.groups()
        if year_filter and year != year_filter:
            continue

        pages_dir = PAGES_ROOT / year / f"{code}_{short}"
        if not pages_dir.is_dir():
            skipped.append(f"{path.stem}（未渲染，跑 render_nonprofit_pages.py {year}）")
            continue

        payload = json.loads(path.read_text(encoding="utf-8"))
        figures = extracted_figures(payload)
        seen = page_numbers(pages_dir, OCR_ROOT / year / f"{code}_{short}")
        missing = [(w, v) for w, v in figures if abs(v) not in seen]

        rows.append(
            {
                "file": path.name,
                "code": code,
                "short_name": short,
                "academic_year": year,
                "figures_checked": len(figures),
                "confirmed": len(figures) - len(missing),
                "missing": len(missing),
                "missing_detail": "; ".join(f"{w}={v:,}" for w, v in missing),
            }
        )
        flag = "✓" if not missing else f"⚠ {len(missing)}"
        print(f"  {path.stem:<24} {len(figures) - len(missing):>3}/{len(figures):<3} {flag}")

    if not rows:
        extra = ("\n略過：\n  " + "\n  ".join(skipped)) if skipped else ""
        sys.exit("沒有可交叉驗證的抽取結果" + extra)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    checked = sum(r["figures_checked"] for r in rows)
    missing = sum(r["missing"] for r in rows)
    print(f"\nwrote {OUT}  ({len(rows)} 份抽取)")
    print("\n=== OCR 交叉驗證 ===")
    print(f"  數值 {checked} 個，OCR 未見 {missing} 個（{missing / checked:.1%}）")
    if skipped:
        print(f"\n略過 {len(skipped)} 份：")
        for s in skipped:
            print(f"  {s}")
    if missing:
        print("\n⚠ OCR 未見的數值需人工覆核（OCR 自己也會掉字，未見不等於抽錯）：")
        for r in rows:
            if r["missing"]:
                print(f"  {r['file']}: {r['missing_detail']}")
    else:
        print("\n✓ 每個數值都在該頁 OCR 結果中出現")


if __name__ == "__main__":
    main()
