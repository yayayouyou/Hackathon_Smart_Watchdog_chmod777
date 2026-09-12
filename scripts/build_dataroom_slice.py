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

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGES = ROOT / "data/extracted/nonprofit_pages"
SURVEY = ROOT / "data/extracted/pdf_survey.csv"
RAW = ROOT / "data/raw/資料集/非營利園財報"
OUT = ROOT / "data/interim/dataroom"


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


def build_report(rep: dict) -> dict:
    """一份報告 → 可直接渲染的形狀。表的順序即原件順序。"""
    pages = rep["pages"]
    first = pages[0]

    flat: list[dict] = []
    for pg in pages:
        for i, t in enumerate(pg.get("tables") or [], start=1):
            flat.append({
                # table_index 從 1 起算，與 nonprofit_pagewise_sections.csv
                # 同慣例，兩邊的 uid 才對得起來。
                "table_index": i,
                "pdf_page": pg["pdf_page"],
                "printed_page": pg.get("printed_page"),
                "page_kind": pg.get("page_kind"),
                "title": t.get("title"),
                "context_heading": t.get("context_heading"),
                "unit": t.get("unit"),
                "aligned": bool(t.get("aligned", True)),
                "period_labels": t.get("period_labels") or [],
                "rows": t.get("items") or [],
                "issues": pg.get("issues") or [],
            })
    flat.sort(key=lambda t: (t["pdf_page"], t["table_index"]))

    for t, r in zip(flat, tt.route_tables(flat)):
        t["section"] = r["section"]
        t["section_zh"] = tt.zh(r["section"])
        # 分類是推論而非原件所寫時要標出來，UI 與 agent 都得看得到。
        t["section_inherited"] = r["section_inherited"]
        t["uid"] = "{}/{}/p{}/t{}".format(
            first["code"], first["academic_year"],
            t["pdf_page"], t["table_index"])

    return {
        "id": rep["id"],
        "code": first["code"],
        "short_name": first["short_name"],
        "academic_year": first["academic_year"],
        # provenance 逐頁都記著，這裡取整份的實際值域——混過模型的那次
        # （6 頁 sonnet-4-5）會在這裡顯示成兩個值，那是事實不是瑕疵。
        "models": sorted({p.get("model") for p in pages if p.get("model")}),
        "dpi": sorted({p.get("dpi") for p in pages if p.get("dpi")}),
        "pdf": pdf_path(first["code"], first["short_name"],
                        first["academic_year"]),
        "pages": [{
            "pdf_page": p["pdf_page"],
            "printed_page": p.get("printed_page"),
            "page_kind": p.get("page_kind"),
            "identity_ok": p.get("identity_ok"),
            "footer_code": p.get("footer_code"),
            "n_tables": len(p.get("tables") or []),
            "n_text": len(p.get("text_sections") or []),
            "issues": p.get("issues") or [],
        } for p in pages],
        "tables": flat,
    }


def report_stats(r: dict) -> dict:
    """一份報告自己的統計。總目要能只加總「已載入」的報告，所以逐份存。"""
    sec: collections.Counter = collections.Counter()
    cells = valued = blank = refused = 0
    for t in r["tables"]:
        sec[t["section"]] += 1
        if not t["aligned"]:
            refused += 1
            continue
        for row in t["rows"]:
            for v in (row.get("values") or []):
                cells += 1
                if v is None:
                    blank += 1
                else:
                    valued += 1
    return {"sec_tables": dict(sec), "tables": len(r["tables"]),
            "refused": refused, "cells": cells, "valued": valued, "blank": blank}


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
    built = [build_report(load_report(d)) for d in dirs]
    totals, sections = tally(built)

    (out / "r").mkdir(parents=True, exist_ok=True)
    for r in built:
        (out / "r" / (r["id"] + ".json")).write_text(
            json.dumps(r, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8")

    index = {
        "source": "data/extracted/nonprofit_pages",
        "totals": totals,
        "sections": sections,
        "reports": [{
            "id": r["id"], "code": r["code"], "short_name": r["short_name"],
            "academic_year": r["academic_year"],
            "pages": len(r["pages"]),
            "models": r["models"], "dpi": r["dpi"], "pdf": r["pdf"],
            "n_issues": sum(len(p["issues"]) for p in r["pages"]),
            "identity_ok": all(p["identity_ok"] for p in r["pages"]),
            **report_stats(r),
        } for r in built],
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
