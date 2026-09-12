"""Re-survey the public-school 決算書 for *usable* text, not just any text.

Some pages embed a subset CIDFont with Identity-H encoding and no ToUnicode CMap.
Those pages still yield characters from ``get_text()``, but the characters are
Private Use Area glyph ids (U+E000-U+F8FF) whose mapping is arbitrary and differs
per page. They are unusable and must be routed to the OCR path instead.

Output: data/interim/text_quality.csv (one row per page of every 公校 volume)
"""

from __future__ import annotations

import csv
import pathlib

import fitz

RAW = pathlib.Path("data/raw/資料集/公校")
OUT = pathlib.Path("data/interim/text_quality.csv")

PUA_START, PUA_END = 0xE000, 0xF8FF


def pua_ratio(text: str) -> float:
    """Share of non-whitespace characters that fall in the Private Use Area."""
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    pua = sum(1 for c in chars if PUA_START <= ord(c) <= PUA_END)
    return pua / len(chars)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for pdf in sorted(RAW.rglob("*.pdf")):
        if "封面" in pdf.name:
            continue
        rel = pdf.relative_to(RAW)
        year = rel.parts[0].replace("年度決算書", "")
        doc = fitz.open(pdf)
        n_pages = doc.page_count
        for page in doc:
            text = page.get_text()
            stripped = "".join(text.split())
            rows.append(
                {
                    "year": year,
                    "volume": pdf.name,
                    "page_index": page.number,
                    "chars": len(stripped),
                    "pua_ratio": round(pua_ratio(text), 4),
                }
            )
        doc.close()
        print(f"{pdf.name}: {n_pages} pages", flush=True)

    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {OUT} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
