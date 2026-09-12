"""Restore data/raw/ from the organiser's zip files.

Run this once after cloning. The 1.8 GB dataset is not in the repository -- every
team member has the organiser's download link, and we have no right to
redistribute it.

The zips need special handling: their filenames are Big5-encoded with the UTF-8
flag unset, so ``unzip`` fails with "Illegal byte sequence" on every Chinese
filename. This script decodes them explicitly.

Usage
    # place the two zips anywhere, then:
    .venv/bin/python scripts/setup_raw_data.py ~/Downloads
"""

from __future__ import annotations

import pathlib
import sys
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.console import use_utf8

use_utf8()

DATASET_ZIP = "E_教育局-資料集.zip"
BRIEF_ZIP = "E_教育局-命題文件.zip"
RAW_DIR = pathlib.Path("data/raw")
DOCS_DIR = pathlib.Path("docs/competition")

EXPECTED_PDFS = 162


def decode_name(info: zipfile.ZipInfo) -> str:
    """Recover the original filename.

    Entries written by Windows tooling set neither the UTF-8 flag nor a usable
    encoding, so zipfile decodes the raw bytes as cp437. Re-encoding to cp437 and
    decoding as Big5 recovers the Chinese names.
    """
    if info.flag_bits & 0x800:
        return info.filename
    try:
        raw = info.orig_filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename
    for encoding in ("big5", "cp950", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:  # noqa: PERF203 - three encodings, tried once per file
            continue
    return info.filename


def extract(zip_path: pathlib.Path, dest: pathlib.Path, flatten: bool = False) -> int:
    n = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = decode_name(info)
            target = dest / (pathlib.Path(name).name if flatten else name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))
            n += 1
    return n


def main() -> None:
    search = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(".")
    if not search.is_dir():
        sys.exit(f"找不到目錄：{search}")

    dataset = search / DATASET_ZIP
    brief = search / BRIEF_ZIP
    if not dataset.exists():
        sys.exit(
            f"找不到 {DATASET_ZIP}（在 {search}）\n"
            "請先向隊長索取主辦方的資料集下載連結，"
            "把 zip 放到任一目錄後把該目錄路徑傳給本腳本。"
        )

    print(f"解壓 {DATASET_ZIP} → {RAW_DIR}/ …")
    n = extract(dataset, RAW_DIR)
    print(f"  {n} 檔")

    if brief.exists():
        print(f"解壓 {BRIEF_ZIP} → {DOCS_DIR}/ …")
        extract(brief, DOCS_DIR, flatten=True)

    pdfs = list(RAW_DIR.rglob("*.pdf"))
    print(f"\ndata/raw/ 共 {len(pdfs)} 份 PDF")
    if len(pdfs) != EXPECTED_PDFS:
        print(f"⚠ 預期 {EXPECTED_PDFS} 份，實得 {len(pdfs)} 份——請確認 zip 完整")
    else:
        print("✓ 檔數符合預期")
    print(
        "\n接著可跑：\n"
        "  .venv/bin/python scripts/survey_pdfs.py\n"
        "  PYTHONPATH=src .venv/bin/python scripts/extract_public_kindergartens.py"
    )


if __name__ == "__main__":
    main()
