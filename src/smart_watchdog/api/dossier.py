"""卷宗的三段補充：裁罰明細、排名軌跡、稽核建議書。

這三件事**agent 本來就叫得到**（`get_penalties`／`set_time_machine`／`open_memo`），
但人用滑鼠點卷宗時看不到——合併時只搬了 agent 的能力，漏了畫面。這支檔案把
同一批資料開成端點，讓兩條路徑看到的東西一致。

`agent/tools.py` 的對應 handler 改成呼叫這裡的函式，不各自讀一次檔：各寫一份的
那天，就是 agent 講的數字與畫面不符的那天。

**裁罰明細不在 payload 裡**（payload 只有計數 `np`），要讀
`data/processed/penalties_ntpc.csv`；機構 id 是該檔 `id` 欄的前 8 碼。
"""

from __future__ import annotations

import functools
import pathlib
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(tags=["dossier"])

ROOT = pathlib.Path(__file__).resolve().parents[3]
LETTERS = ROOT / "data/processed/audit_letters"


def _isna(v: Any) -> bool:
    return v is None or v != v  # NaN != NaN


@functools.lru_cache(maxsize=1)
def _penalty_table():
    import pandas as pd

    df = pd.read_csv(ROOT / "data/processed/penalties_ntpc.csv")
    df["short_id"] = df["id"].astype(str).str[:8]
    return df


@functools.lru_cache(maxsize=1)
def _letter_index() -> dict:
    """建議書索引，以 8 碼 id 為鍵。沒有索引檔就回空的，不讓卷宗因此壞掉。"""
    import csv

    path = LETTERS / "index.csv"
    if not path.exists():
        return {}
    out = {}
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[str(row["id"])[:8]] = row
    return out


def penalties_of(institution_id: str) -> dict:
    """一所機構的裁罰明細，新到舊。

    **受處分角色不可合併。** 負責人與行為人即使同園、同日、同條、同金額也可能是
    不同處分，所以每一列都帶 `actor_role`，計數也不去重。
    """
    rows = _penalty_table()
    hits = rows[rows["short_id"] == institution_id]
    items = [{
        "date": r.date,
        "article": None if _isna(r.article) else int(r.article),
        "law": None if _isna(r.law) else r.law,
        # 非金錢處分的 fine 是空值而不是 0——填 0 等於謊稱罰了零元。
        "fine": None if _isna(r.fine) else int(r.fine),
        "sanction_type": None if _isna(r.sanction_type) else r.sanction_type,
        "actor_role": None if _isna(r.actor_role) else r.actor_role,
        "punishment": None if _isna(r.punishment) else r.punishment,
    } for r in hits.itertuples()]
    items.sort(key=lambda x: str(x["date"]), reverse=True)
    return {
        "count": len(items),
        "items": items,
        "note": "受處分角色（負責人／行為人）不同即為不同處分，不可合併計數。",
    }


def rank_track_of(institution_id: str) -> dict:
    """這一所在各時點的名次軌跡。

    每一格都是當時重新訓練的結果，所以名次會動——那正是要給人看的東西。
    `hit` 三態：1 後來受罰、0 後來未受罰、**null 代表觀察期還沒過完**，
    不是「沒事」。
    """
    from . import explore as _explore

    tl = _explore.load_timeline()
    points = []
    for p in tl.get("points", []):
        entry = next((r for r in p.get("ranking", []) if r["i"] == institution_id), None)
        points.append({
            "as_of": p["as_of"],
            "rank": entry["rank"] if entry else None,
            "score": entry["score"] if entry else None,
            "hit": entry.get("hit") if entry else None,
            "had_prior": bool(entry.get("prior")) if entry else None,
            "total": len(p.get("ranking", [])),
            "label_complete": p.get("label_complete"),
        })
    return {
        "count": len(points),
        "points": points,
        "note": ("每一格都只用當時看得到的資料重新訓練（時序切分）。"
                 "hit 為空值代表後續觀察期尚未結束，不是未受罰。"),
    }


def memo_of(institution_id: str) -> dict:
    """現行建議書。未列入本批名單的園沒有信，那不是錯誤。"""
    meta = _letter_index().get(institution_id)
    hits = sorted(LETTERS.glob(f"{institution_id}_*.txt"))
    if not hits:
        return {
            "exists": False,
            "note": "這一所沒有現行建議書。未列入本批建議查核名單的園不會產生建議書。",
        }
    return {
        "exists": True,
        "file": hits[0].name,
        "content": hits[0].read_text(encoding="utf-8"),
        # backend=template 是確定性組裝，bedrock 是生成後通過 verify.py 才寫出。
        "backend": (meta or {}).get("backend"),
        "verified": (meta or {}).get("verified"),
        "review_reason": (meta or {}).get("review_reason"),
        "note": "本文是請求說明，不是違法認定。涵蓋範圍那一段要一併讀。",
    }


# ── 端點 ─────────────────────────────────────────────────────────────


@router.get("/api/institutions/{institution_id}/penalties")
def get_penalties(institution_id: str) -> dict:
    return penalties_of(institution_id)


@router.get("/api/institutions/{institution_id}/ranking")
def get_rank_track(institution_id: str) -> dict:
    return rank_track_of(institution_id)


@router.get("/api/institutions/{institution_id}/memo")
def get_memo(institution_id: str) -> dict:
    return memo_of(institution_id)


def list_memos(q: Optional[str] = None, limit: int = 200) -> dict:
    """本批建議書清單。`q` 比對園名與行政區。

    與另外三個一樣拆成純函式 + 薄端點：端點的預設值是 FastAPI 的 `Query` 物件，
    直接當函式呼叫（測試、agent）時它不是 int，`rows[:limit]` 會 TypeError。
    """
    rows = []
    for short_id, r in sorted(_letter_index().items(),
                              key=lambda kv: int(kv[1].get("priority_rank") or 0)):
        if q and q not in r.get("title", "") and q not in r.get("town", ""):
            continue
        rows.append({
            "id": short_id,
            "title": r.get("title", ""),
            "type": r.get("type", ""),
            "town": r.get("town", ""),
            "priority_rank": int(r["priority_rank"]) if r.get("priority_rank") else None,
            "findings": int(r["findings"]) if r.get("findings") else 0,
            "review_reason": r.get("review_reason", ""),
            "backend": r.get("backend", ""),
        })
    if not rows and not _letter_index():
        raise HTTPException(503, "建議書索引不存在，請先執行 python run.py letters")
    return {
        "count": len(rows), "items": rows[:limit],
        "note": "本批建議查核名單；未列入者不會有信，不代表該園無虞。",
    }


@router.get("/api/memos")
def get_memo_list(q: Optional[str] = None,
                  limit: int = Query(200, ge=1, le=500)) -> dict:
    return list_memos(q=q, limit=limit)
