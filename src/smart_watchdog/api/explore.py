"""兩個「看得見證據」的端點：時間軸回測與文件索引。

兩者回答的是同一類質疑，而那是評審一定會問的兩個問題：

    「你怎麼知道這個模型有用？」        → /api/timeline    把時鐘倒回去，逐年驗
    「這個數字你從哪裡看到的？」        → /api/docsearch   給出檔案與頁碼

刻意分成獨立 router 而不是塞進 `server.py`：時間軸與索引各自有自己的資料檔，
缺檔時應該只讓自己的端點回 503，不該讓整個派工台起不來。
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from ..docindex import schema as docschema
from ..docindex import search as docsearch

ROOT = pathlib.Path(__file__).resolve().parents[3]
TIMELINE_PATH = ROOT / "data/processed/timeline.json"
SIGNAL_MAP_PATH = ROOT / "data/processed/signal_map.json"
INDEX_PATH = ROOT / "data/processed/document_index.sqlite"

router = APIRouter()

_state: dict[str, Any] = {"timeline": None}


def load_timeline(path: pathlib.Path = TIMELINE_PATH) -> dict:
    if _state["timeline"] is None:
        if not path.exists():
            raise HTTPException(
                503, f"{path.name} 不存在，請先執行 `python run.py timeline`")
        _state["timeline"] = json.loads(path.read_text(encoding="utf-8"))
    return _state["timeline"]


def _connect():
    if not INDEX_PATH.exists():
        raise HTTPException(
            503, f"{INDEX_PATH.name} 不存在，請先執行 `python run.py doc-index`")
    return docschema.connect(INDEX_PATH)


# ── 時間軸 ────────────────────────────────────────────────────────────
@router.get("/api/timeline")
def timeline_summary() -> dict:
    """時間軸的骨架：每一格的指標，不含逐園排序（那個很大，另外取）。

    `label_complete=false` 的格子前瞻窗還沒走完，命中率是**低估**而非真值，
    前端必須把它與完整觀察的格子在視覺上分開，不可以連成同一條趨勢線。
    """
    tl = load_timeline()
    return {
        "schema_version": tl["schema_version"],
        "protocol": tl["protocol"],
        "summary": tl["summary"],
        "points": [
            {k: v for k, v in p.items() if k != "ranking"}
            for p in tl["points"]
        ],
    }


@router.get("/api/timeline/{as_of}")
def timeline_point(as_of: str, n: int = Query(200, ge=1, le=2000)) -> dict:
    """某一格的逐園排序。`hit` 為 null 代表還沒發生，不是沒事。"""
    tl = load_timeline()
    for p in tl["points"]:
        if p["as_of"] == as_of:
            return {
                **{k: v for k, v in p.items() if k != "ranking"},
                "ranking": p["ranking"][:n],
                "returned": min(n, len(p["ranking"])),
                "total": len(p["ranking"]),
            }
    raise HTTPException(404, f"時間軸沒有 {as_of} 這一格")


@router.get("/api/signal-map")
def signal_map() -> dict:
    """我們有哪些資料、哪些真的進了模型、哪些沒有。

    這個端點回答的是「你憑什麼這樣排」——而誠實的答案包含**沒有用上的東西**：
    評鑑與交叉比對都還沒進計分，單文件法遵檢核在分層後提升只有 0.42（低於
    基準）。只回報有效訊號的圖會讓人以為我們什麼都用上了。

    數字由 `python run.py signal-map` 現算，不寫死在前端。
    """
    if not SIGNAL_MAP_PATH.exists():
        raise HTTPException(
            503, f"{SIGNAL_MAP_PATH.name} 不存在，請先執行 "
                 "`PYTHONPATH=src python scripts/build_signal_map.py`")
    return json.loads(SIGNAL_MAP_PATH.read_text(encoding="utf-8"))


# ── 文件索引 ──────────────────────────────────────────────────────────
@router.get("/api/docsearch")
def docsearch_endpoint(
    q: str = Query(..., min_length=1, description="要找的字串"),
    institution: Optional[str] = None,
    year: Optional[int] = None,
    limit: int = Query(12, ge=1, le=50),
) -> dict:
    """一個問題進來，可引用的證據出去。

    零命中時回傳的是涵蓋範圍說明而不是空陣列——全市 94.8% 的園沒有公開財報，
    「查不到」對它們而言是資料不足，不是合規證明。
    """
    conn = _connect()
    try:
        return docsearch.retrieve(conn, q, institution=institution,
                                  year=year, limit=limit)
    finally:
        conn.close()


@router.get("/api/docindex/locate")
def docindex_locate(
    institution: Optional[str] = None,
    year: Optional[int] = None,
    kind: Optional[str] = None,
    code: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    """某個區段在哪一份文件的哪幾頁。"""
    conn = _connect()
    try:
        rows = docsearch.locate(conn, institution=institution, year=year,
                                kind=kind, code=code, limit=limit)
        return {"count": len(rows), "sections": [r.as_dict() for r in rows]}
    finally:
        conn.close()


@router.get("/api/docindex/facts")
def docindex_facts(
    institution: Optional[str] = None,
    year: Optional[int] = None,
    field: Optional[str] = None,
    code: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    """某個數字是多少、出自哪裡。`value` 為 null 代表空白／未編列，不是 0。"""
    conn = _connect()
    try:
        rows = docsearch.find_facts(conn, institution=institution, year=year,
                                    field=field, code=code, limit=limit)
        return {"count": len(rows), "facts": rows,
                "null_means": "空白／未編列，不是 0"}
    finally:
        conn.close()


@router.get("/api/docindex/coverage")
def docindex_coverage() -> dict:
    """索引涵蓋到什麼程度，以及**沒有**涵蓋到什麼。"""
    conn = _connect()
    try:
        return {"stats": docsearch.stats(conn),
                "coverage": docsearch.summarise_coverage(conn)}
    finally:
        conn.close()
