"""資料室：原始資料、依表單類型分類的數字、以及一份原件的入庫。

這一室回答的是「**這份文件上印的是什麼**」。它不做判讀——同儕比較、風險
分數、法遵結論都在別的房間，刻意不放進來。

所有查詢都走 ``dataroom.store``，與 agent 的工具**同一份**。兩邊各寫一份
查詢的那天，就是畫面上的數字與助理講的數字開始不一致的那天。

## 為什麼上傳不寫進 data/raw 或 data/extracted

``data/raw/`` 是主辦方資料集（不得轉散布），而 ``setup_raw_data.py`` 會檢查
它剛好 162 份；``data/extracted/nonprofit_pages/`` 是交付語料，
``build_pagewise_facts.py`` 的 glob 會把任何新資料夾併進下一次的
144,320 筆事實。兩個都不能碰。上傳一律落在 ``data/runtime/``（已 gitignore）。
"""

from __future__ import annotations

import hashlib
import pathlib
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from ..dataroom import store
from ..db.models import User
from .auth import get_current_user

router = APIRouter(prefix="/api/dataroom", tags=["dataroom"])

ROOT = pathlib.Path(__file__).resolve().parents[3]
CACHE = ROOT / "data/interim/dataroom_pages"
DPI = 150

#: 上傳上限。一份非營利園財報 37–45 頁、2 MB 上下，50 MB 綽綽有餘。
MAX_BYTES = 50 * 1024 * 1024


def _need_slice() -> None:
    if not store.available():
        raise HTTPException(
            503, "資料室切片不存在，請先執行 "
                 "`PYTHONPATH=src python scripts/build_dataroom_slice.py`")


# ── 瀏覽 ──────────────────────────────────────────────────────────────
@router.get("/overview")
def overview() -> dict:
    """總目：已載入的報告、表單類型與各自張數。

    統計**只加總已載入的報告**。待載入的報告仍列在 ``reports`` 裡並標
    ``state: "pending"``，因為「我們知道有這份、還沒進來」與「沒有這份」
    是兩件事。
    """
    _need_slice()
    return store.overview()


@router.get("/report/{report_id}")
def report(report_id: str) -> dict:
    """一份報告的逐頁與逐表。尚未載入的回 404——它不在庫裡就是不在。"""
    _need_slice()
    r = store.report(report_id)
    if r is None:
        raise HTTPException(404, f"{report_id} 尚未載入或不存在")
    return r


@router.get("/tables")
def tables(section: Optional[str] = None, institution: Optional[str] = None,
           year: Optional[int] = None,
           limit: int = Query(50, ge=1, le=400)) -> dict:
    """符合條件的表頭清單。列要用 /table 另取。"""
    _need_slice()
    total, rows = store.count_tables(section=section, institution=institution,
                                     year=year, limit=limit)
    # `count` 是這一次回傳幾張，`total` 是符合條件的總數。兩個一樣時畫面不必
    # 多說一句；不一樣時就必須說，否則「顯示 12 張」會被讀成「只有 12 張」。
    return {"count": len(rows), "total": total, "tables": rows}


@router.get("/table")
def table(uid: str = Query(description="表的代號，例如 N01/113/p05/t1")) -> dict:
    """一張表的全部內容，**含空白格**。

    ``values`` 裡的 null 代表原件那一格是空白（未編列），不是 0。
    """
    _need_slice()
    t = store.get_table(uid)
    if t is None:
        raise HTTPException(404, f"找不到 {uid}（或該報告尚未載入）")
    return t


@router.get("/notes")
def notes(institution: Optional[str] = None, year: Optional[int] = None,
          limit: int = Query(20, ge=1, le=200)) -> dict:
    """抽取過程自報的疑點。**不是機構的稽查發現。**"""
    _need_slice()
    rows = store.extraction_notes(institution=institution, year=year,
                                  limit=limit)
    return {"count": len(rows), "notes": rows,
            "note": "這些是抽取時「這一格看不清楚／自相矛盾」的自報疑點，"
                    "不是對機構的稽查發現。"}


@router.get("/compare")
def compare(institution: str, section: str,
            max_rows: int = Query(30, ge=1, le=200)) -> dict:
    """同一種表跨學年度對齊。期間逐年照抄，不改寫。"""
    _need_slice()
    return store.compare_years(institution, section, max_rows=max_rows)


# ── 入庫 ──────────────────────────────────────────────────────────────
@router.post("/upload")
async def upload(file: UploadFile = File(...),
                 user: User = Depends(get_current_user)) -> dict:
    """收一份原件，把它的抽取結果登錄進來。

    回傳的每一項都是這個檔案的實際屬性與登錄後真正多出來的東西，
    不含任何估計值。
    """
    del user
    _need_slice()
    name = file.filename or ""
    if not name.lower().endswith(".pdf"):
        raise HTTPException(400, "只接受 PDF")

    blob = await file.read()
    if len(blob) > MAX_BYTES:
        raise HTTPException(413, f"檔案超過 {MAX_BYTES // 1024 // 1024} MB")
    # 副檔名可以隨便改，檔頭不行。
    if not blob.startswith(b"%PDF-"):
        raise HTTPException(400, "檔頭不是 %PDF-，不是有效的 PDF")

    result = store.ingest(name, blob)
    if not result.get("ok"):
        raise HTTPException(422, result.get("detail", "無法登錄這份文件"))
    return result


@router.post("/reset")
def reset(user: User = Depends(get_current_user)) -> dict:
    """回到上傳前的狀態。"""
    del user
    _need_slice()
    return store.reset()


# ── 證據頁 ────────────────────────────────────────────────────────────
@router.get("/page")
def page(report_id: str = Query(alias="report"),
         page: int = Query(ge=1),
         user: User = Depends(get_current_user)) -> FileResponse:
    """把原件的某一頁渲染成 PNG。

    優先用**這次上傳的那一份**，沒有才退回 ``data/raw`` 裡的同一份——
    示範機器上不一定有 ``data/raw``（1.8 GB，不進版控）。

    ⚠️ 路徑包含判斷用 ``is_relative_to``，不用 ``str.startswith``：
    後者會讓 ``data/raw_x/`` 這種同前綴的旁支通過。
    """
    del user
    _need_slice()
    meta = next((r for r in store.index().get("reports", [])
                 if r["id"] == report_id), None)
    if meta is None:
        raise HTTPException(404, f"沒有 {report_id} 這份報告")

    pdf = store.uploaded_pdf(report_id)
    if pdf is None:
        if not meta.get("pdf"):
            raise HTTPException(404, "這份報告的原件不在這台機器上")
        candidate = (ROOT / meta["pdf"]).resolve()
        raw = (ROOT / "data/raw").resolve()
        if not candidate.is_relative_to(raw) or not candidate.exists():
            raise HTTPException(404, "原件不在 data/raw 內或已不存在")
        pdf = candidate

    key = hashlib.sha256(f"{report_id}:{page}:{DPI}".encode()).hexdigest()[:16]
    out = CACHE / f"{key}.png"
    if not out.exists():
        try:
            import pymupdf
        except ImportError as exc:  # pragma: no cover - 相依缺失才會走到
            raise HTTPException(503, "伺服器缺 pymupdf，無法渲染頁面") from exc
        with pymupdf.open(pdf) as doc:
            if page > doc.page_count:
                raise HTTPException(404, f"這份只有 {doc.page_count} 頁")
            CACHE.mkdir(parents=True, exist_ok=True)
            doc[page - 1].get_pixmap(dpi=DPI).save(out)

    return FileResponse(str(out), media_type="image/png",
                        headers={"Cache-Control": "private, max-age=3600"})
