"""算出 132 份非營利財報原件的 SHA-256，讓上傳入庫「看內容認檔」而不是看檔名。

入庫原本靠檔名判斷是哪一份，檔名一改就對不上。雜湊不受檔名影響，而且同一份
原件重傳時直接用既有的抽取結果，不必再花一次抽取費用。

``data/raw`` 不進版控也不進映像，所以雜湊要在有原件的機器上算好、存成 CSV
進版控，``build_dataroom_slice.py`` 再把它併進索引。雜湊不是原件內容，存它
不構成轉散布。

Usage
    PYTHONPATH=src .venv/Scripts/python scripts/hash_nonprofit_pdfs.py
"""

from __future__ import annotations

import csv
import hashlib
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
RAW = ROOT / "data/raw/資料集/非營利園財報"
OUT = ROOT / "data/extracted/nonprofit_pdf_sha256.csv"

# 只有這裡看檔名：主辦方資料集的命名是可信的，上傳者取的檔名不是。
FILENAME = re.compile(r"^(N\d\d)(.+?)_(\d{3})學年度")


def main() -> None:
    rows = []
    for pdf in sorted(RAW.glob("*學年度/*.pdf")):
        m = FILENAME.match(pdf.name)
        if not m:
            sys.exit(f"檔名不符預期：{pdf.name}")
        blob = pdf.read_bytes()
        rows.append({"report": f"{m.group(1)}_{m.group(2)}_{int(m.group(3))}",
                     "sha256": hashlib.sha256(blob).hexdigest(),
                     "bytes": len(blob)})
    if not rows:
        sys.exit(f"找不到原件：{RAW}")
    with OUT.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["report", "sha256", "bytes"])
        w.writeheader()
        w.writerows(rows)
    print(f"寫出 {OUT.relative_to(ROOT)}：{len(rows)} 份")


if __name__ == "__main__":
    main()
