"""Survey every PDF in data/raw: page count, text-layer coverage, image-only ratio.

Output: data/interim/pdf_survey.csv  (one row per PDF)
Purpose: decide which files need OCR vs. native text extraction.
"""
import csv
import pathlib
import sys

import fitz

RAW = pathlib.Path("data/raw/資料集")
OUT = pathlib.Path("data/interim/pdf_survey.csv")


def survey(path: pathlib.Path) -> dict:
    doc = fitz.open(path)
    n = doc.page_count
    text_pages = 0
    image_pages = 0
    total_chars = 0
    for page in doc:
        chars = len(page.get_text().strip())
        total_chars += chars
        if chars > 50:
            text_pages += 1
        if page.get_images():
            image_pages += 1
    doc.close()
    rel = path.relative_to(RAW)
    return {
        "path": str(rel),
        "category": rel.parts[0],
        "period": rel.parts[1] if len(rel.parts) > 1 else "",
        "filename": path.name,
        "size_mb": round(path.stat().st_size / 1e6, 2),
        "pages": n,
        "text_pages": text_pages,
        "image_pages": image_pages,
        "chars": total_chars,
        "text_ratio": round(text_pages / n, 3) if n else 0.0,
        "needs_ocr": text_pages / n < 0.5 if n else True,
    }


def main() -> None:
    pdfs = sorted(RAW.rglob("*.pdf"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for i, p in enumerate(pdfs, 1):
        try:
            rows.append(survey(p))
        except Exception as exc:  # noqa: BLE001 - one bad PDF must not abort the survey
            print(f"  FAIL {p.name}: {exc}", file=sys.stderr)
        print(f"[{i}/{len(pdfs)}] {p.name}", flush=True)
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {OUT} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
