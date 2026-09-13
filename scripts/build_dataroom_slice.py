"""把頁級抽取整理成資料室要的形狀。

輸入 ``data/extracted/nonprofit_pages/*/pNN.json``（2,245 頁，已進版控）。
輸出 ``data/interim/dataroom/``：一份總目 ``index.json`` 加上每份報告一個
``r/<id>.json``。

**為什麼不讀 nonprofit_pagewise_facts.csv**

那支 CSV 在 ``build_pagewise_facts.py`` 對空白格是 ``continue``——整列直接
不存在。全庫 177,953 格裡有 33,633 格（18.9%）是空白，只活在頁 JSON 裡。
而「業務發展費預算欄空白」＝未編列預算，是一項稽查發現；用 CSV 畫表會把它
變成「沒有這個項目」。要顯示表格就只能讀頁 JSON。

輸出寫進 ``data/interim/``（已 gitignore）而不是 ``webapp/`` 或
``data/processed/``：它完全由 ``data/extracted/`` 重生，與 ``data/interim/``
裡其他可重生中間產物同一個理由。``webapp/`` 是會部署出去的目錄，不放資料。

Usage
    PYTHONPATH=src .venv/Scripts/python scripts/build_dataroom_slice.py
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.dataroom import tabletypes as tt
from smart_watchdog.dataroom.build import build_report, index_entry

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGES = ROOT / "data/extracted/nonprofit_pages"
SURVEY = ROOT / "data/extracted/pdf_survey.csv"
RAW = ROOT / "data/raw/資料集/非營利園財報"
OUT = ROOT / "data/interim/dataroom"
HASHES = ROOT / "data/extracted/nonprofit_pdf_sha256.csv"


def load_report(d: pathlib.Path) -> dict:
    """一份報告的所有頁，照 pdf_page 排好。"""
    pages = [json.loads(f.read_text(encoding="utf-8"))
             for f in sorted(d.glob("p*.json"))]
    pages.sort(key=lambda p: p["pdf_page"])
    return {"id": d.name, "pages": pages}


def pdf_path(code: str, short_name: str, year: int) -> str | None:
    """原件在哪。找不到就 None——給一個開不起來的連結比不給更糟。"""
    hits = list(RAW.glob(f"{year}學年度/{code}{short_name}*.pdf"))
    return str(hits[0].relative_to(ROOT)).replace("\\", "/") if hits else None


def tally(built: list[dict]) -> tuple[dict, list[dict]]:
    """全庫統計與類別清單。空白格與有值格分開數——這是驗收的關鍵數字。"""
    n_tables = n_cells = n_valued = n_blank = refused = 0
    per_sec_tables: collections.Counter = collections.Counter()
    per_sec_cells: collections.Counter = collections.Counter()
    per_sec_reports: dict[str, set] = collections.defaultdict(set)

    for r in built:
        for t in r["tables"]:
            n_tables += 1
            sec = t["section"]
            per_sec_tables[sec] += 1
            per_sec_reports[sec].add(r["id"])
            if not t["aligned"]:
                # 欄位對不齊的表一格都不計入——它的數字沒有進任何下游。
                refused += 1
                continue
            for row in t["rows"]:
                for v in (row.get("values") or []):
                    n_cells += 1
                    if v is None:
                        n_blank += 1
                    else:
                        n_valued += 1
            per_sec_cells[sec] += len(t["rows"]) * max(len(t["period_labels"]), 1)

    sections = [{
        "key": k,
        "zh": tt.zh(k),
        "tables": n,
        "reports": len(per_sec_reports[k]),
        "cells": per_sec_cells[k],
    } for k, n in per_sec_tables.most_common()]

    totals = {
        "reports": len(built), "tables": n_tables, "refused": refused,
        "cells": n_cells, "valued": n_valued, "blank": n_blank,
    }
    return totals, sections


def public_documents() -> list[dict]:
    """公校決算書。**與非營利報告是兩種東西**，所以另立一張清單。

    三件事讓它不能與 132 份非營利報告並列成同一種列：用**年度**不是學年度、
    一冊含多個分基金（切割訊號是頁尾 ``<分基金代號>-<頁碼>``）、而且 30 個
    檔裡有 15 個是封面附件。頁級抽取一頁都沒有涵蓋到它們。
    """
    if not SURVEY.exists():
        return []
    out = []
    with SURVEY.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("category") != "公校":
                continue
            name = row["filename"]
            out.append({
                "filename": name,
                "path": row["path"],
                "period": row["period"],
                "pages": int(row["pages"]),
                "is_cover": name.startswith("附件"),
            })
    return sorted(out, key=lambda r: (r["period"], r["is_cover"], r["filename"]))


def main() -> None:
    ap = argparse.ArgumentParser(description="產生資料室切片")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()
    out = pathlib.Path(a.out)

    dirs = [d for d in sorted(PAGES.iterdir())
            if d.is_dir() and not d.name.startswith("_")]
    built = []
    for d in dirs:
        rep = load_report(d)
        p0 = rep["pages"][0]
        built.append(build_report(
            rep, pdf_path(p0["code"], p0["short_name"], p0["academic_year"])))
    totals, sections = tally(built)
    with HASHES.open(encoding="utf-8") as fh:
        hashes = {row["report"]: row["sha256"] for row in csv.DictReader(fh)}

    (out / "r").mkdir(parents=True, exist_ok=True)
    for r in built:
        (out / "r" / (r["id"] + ".json")).write_text(
            json.dumps(r, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8")

    index = {
        "source": "data/extracted/nonprofit_pages",
        "totals": totals,
        "sections": sections,
        # 原件雜湊讓上傳「看內容認檔」（見 scripts/hash_nonprofit_pdfs.py）。
        "reports": [{**index_entry(r), "sha256": hashes.get(r["id"])}
                    for r in built],
        "public": public_documents(),
    }
    (out / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")

    mb = sum(f.stat().st_size for f in out.rglob("*.json")) / 1e6
    print("報告 {}  表 {}（拒收 {}）  類別 {}".format(
        totals["reports"], totals["tables"], totals["refused"], len(sections)))
    print("格 {}：有值 {}、空白 {}（{:.1%}）".format(
        totals["cells"], totals["valued"], totals["blank"],
        totals["blank"] / totals["cells"]))
    print("公校文件 {} 份（其中封面 {}）".format(
        len(index["public"]),
        sum(1 for p in index["public"] if p["is_cover"])))
    print(f"寫出 {out}  共 {mb:.1f} MB")


if __name__ == "__main__":
    main()
