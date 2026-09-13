"""一份報告的頁級抽取 → 資料室要的形狀。

原本寫在 ``scripts/build_dataroom_slice.py``。上傳後自動抽取（``intake.py``）
要產生同樣形狀的報告，兩邊各寫一份就會分岔，所以搬到這裡共用。
"""

from __future__ import annotations

import collections

from . import tabletypes as tt


def build_report(rep: dict, pdf: str | None = None) -> dict:
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
        "pdf": pdf,
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


def index_entry(r: dict) -> dict:
    """總目裡的一列。切片與上傳後抽取的報告共用，兩邊的欄位才不會分岔。"""
    return {
        "id": r["id"], "code": r["code"], "short_name": r["short_name"],
        "academic_year": r["academic_year"],
        "pages": len(r["pages"]),
        "models": r["models"], "dpi": r["dpi"], "pdf": r["pdf"],
        "n_issues": sum(len(p["issues"]) for p in r["pages"]),
        "identity_ok": all(p["identity_ok"] for p in r["pages"]),
        **report_stats(r),
    }
