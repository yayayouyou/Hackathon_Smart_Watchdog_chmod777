"""tool 實作：8 個唯讀 tool。寫入型 tool 見階段 5。

與來源專案（`Eason20050201/hackathon@a0bdada`）最大的差別：那邊的 handler 查
Postgres 的 `institution` 表，這裡**一律讀既有資料來源**——payload、
`api/chat.py` 的篩選引擎、`api/explore.py` 的時間軸與文件索引、以及
`data/processed/` 的 CSV。理由寫在 `docs/MERGE_PLAN.md` §3：兩套機構資料並存
會產生兩個互相矛盾的清單與兩套名次。

`list_institutions` 重用 `chat._matches()`，`get_ranking` 重用
`server.get_proposal()`，不自己寫第二套——agent 與使用者手動操作出來的結果
因此保證一致。各寫一份的那天，就是 agent 開始講與畫面不符的數字的那天。

**機構 id 是 registry UUID 的前 8 碼。** payload 的 `i`（例如 `00ac631e`）對應
`data/processed/*.csv` 的 `id`（`00ac631e-b98e-...`），建議書檔名也用同一個前綴。
"""

from __future__ import annotations

import functools
import pathlib
from typing import Any, Optional

from pydantic import BaseModel, Field

from .registry import ToolContext, ToolOutcome, ToolRegistry, ToolSpec


# `api.server` 在匯入時會掛載 MCP，而 MCP 要這支模組的 registry——模組層級匯入
# api.* 會形成 tools → api.server → agent.mcp_server → tools 的循環，且「先匯入
# 哪一邊」會決定成敗（實測 MCP 會靜默不掛載）。改成用到才匯入，永遠安全。
def _srv():
    from ..api import server

    return server


def _ch():
    from ..api import chat

    return chat


def _ex():
    from ..api import explore

    return explore

ROOT = pathlib.Path(__file__).resolve().parents[3]
SKILLS_DIR = pathlib.Path(__file__).resolve().parent / "skills"
SKILL_NAMES = ("schedule_inspection", "read_memo", "explain_risk",
               "answer_challenge", "read_evidence")

# payload 的 `t` 欄位：0 公立、1 非營利、2 私立。與 chat.py 的 TYPE_NAMES 同源。
TYPE_CODES = {"公立": 0, "非營利": 1, "私立": 2}
MAX_LIMIT = 50

# 每個回傳都帶同一句界線。模型會照著講，而這正是我們要它講的。
CAVEAT = "這是建議查核的優先序，不是違法認定；無公開財報者屬資料不足，不是低風險。"


# ── 共用讀取 ──────────────────────────────────────────────────────────


@functools.lru_cache(maxsize=1)
def _penalties():
    """裁罰明細。payload 只有計數（`np`），明細要讀 CSV。"""
    import pandas as pd

    df = pd.read_csv(ROOT / "data/processed/penalties_ntpc.csv")
    df["short_id"] = df["id"].astype(str).str[:8]
    return df


def _point(institution_id: str) -> Optional[dict]:
    _srv().payload()
    return _srv()._state["index"].get(institution_id)


def _dossier_for(institution_id: str) -> tuple[Optional[str], Optional[dict]]:
    data = _srv().payload()
    for code, v in data.get("dossier", {}).items():
        if institution_id in v.get("ids", []):
            return code, v
    return None, None


def _not_found(institution_id: str) -> ToolOutcome:
    return ToolOutcome(payload={
        "error": f"查無機構 {institution_id}",
        "note": "機構 id 是 8 碼十六進位，可先用 list_institutions 取得。",
    })


# ── 1. list_institutions ─────────────────────────────────────────────


class ListInstitutionsArgs(BaseModel):
    town: Optional[str] = Field(
        default=None, description="行政區全名，例如「板橋區」。不給就是全市。"
    )
    type: Optional[str] = Field(
        default=None, description="機構類別：公立／非營利／私立。不給就是全部。"
    )
    has_penalty: Optional[bool] = Field(default=None, description="只要有歷史裁罰紀錄的登記")
    has_compliance_failure: Optional[bool] = Field(
        default=None, description="只要財報法遵檢核未通過的登記"
    )
    limit: int = Field(default=20, ge=1, le=MAX_LIMIT, description="取幾筆，上限 50")


def _list_institutions(_ctx: ToolContext, a: ListInstitutionsArgs) -> ToolOutcome:
    payload = _srv().payload()
    mentions = payload.get("realtime", {}).get("by_institution", {})

    filters: dict = {}
    if a.town:
        filters["town"] = a.town
    if a.type:
        if a.type not in TYPE_CODES:
            # 不猜。回可讀的錯誤讓模型自己更正，比默默當成全部查更好。
            return ToolOutcome(payload={
                "error": f"未知的機構類別「{a.type}」，只能是：公立、非營利、私立",
            })
        filters["type"] = TYPE_CODES[a.type]
    if a.has_penalty:
        filters["has_penalty"] = True
    if a.has_compliance_failure:
        filters["has_compliance_failure"] = True

    hits = [p for p in payload.get("points", []) if _ch()._matches(p, filters, mentions)]
    hits.sort(key=lambda p: p["r"])
    rows = [{
        "id": p["i"], "title": p["full"], "type": _ch().TYPE_NAMES.get(p["t"], "?"),
        "town": p["d"], "priority_rank": p["r"], "reason": p.get("why", ""),
        "penalties": p.get("np", 0), "compliance_failed": p.get("cf", 0),
        "has_financial_report": bool(p.get("fin")),
    } for p in hits[:a.limit]]

    return ToolOutcome(
        payload={"count": len(rows), "matched": len(hits), "items": rows, "note": CAVEAT},
        ui_action={
            "type": "set_filters", "tab": "list",
            "filters": {k: v for k, v in {
                "town": a.town, "type": a.type, "has_penalty": a.has_penalty,
                "has_compliance_failure": a.has_compliance_failure,
            }.items() if v is not None},
            "ids": [r["id"] for r in rows],
        },
    )


# ── 2. get_ranking ───────────────────────────────────────────────────


class GetRankingArgs(BaseModel):
    n: int = Field(default=20, ge=1, le=MAX_LIMIT, description="取前幾名，上限 50")
    town: Optional[str] = Field(default=None, description="限定行政區")
    financial_only: bool = Field(default=False, description="只要有公開財報的登記")


def _get_ranking(_ctx: ToolContext, a: GetRankingArgs) -> ToolOutcome:
    # 直接呼叫端點函式，不重寫分層規則——tier 的順序是派工政策，只能有一份。
    res = _srv().get_proposal(n=a.n, town=a.town, financial_only=a.financial_only)
    rows = [{
        "id": p["i"], "title": p["full"], "type": _ch().TYPE_NAMES.get(p["t"], "?"),
        "town": p["d"], "priority_rank": p["r"], "tier": p.get("tier", ""),
        "reason": p.get("why", ""), "penalties": p.get("np", 0),
    } for p in res.get("proposal", [])]
    return ToolOutcome(
        payload={"count": len(rows), "items": rows, "note": CAVEAT},
        ui_action={"type": "navigate", "tab": "list", "ids": [r["id"] for r in rows]},
    )


# ── 3. open_institution ──────────────────────────────────────────────


class InstitutionArgs(BaseModel):
    institution_id: str = Field(description="機構 id，8 碼十六進位")


def _open_institution(_ctx: ToolContext, a: InstitutionArgs) -> ToolOutcome:
    p = _point(a.institution_id)
    if not p:
        return _not_found(a.institution_id)
    code, dossier = _dossier_for(a.institution_id)
    return ToolOutcome(
        payload={
            "id": p["i"], "title": p["full"],
            "type": _ch().TYPE_NAMES.get(p["t"], "?"), "town": p["d"],
            "priority_rank": p["r"], "reason": p.get("why", ""),
            "penalties": p.get("np", 0),
            "last_event": p.get("ev", ""), "last_event_date": p.get("evd", ""),
            "events_90d": p.get("e90", 0), "events_365d": p.get("e365", 0),
            "approved_capacity": p.get("cap"), "monthly_fee": p.get("fee"),
            "has_financial_report": bool(p.get("fin")),
            "compliance_failed": p.get("cf", 0),
            "report_code": code,
            "finding_count": len(dossier.get("findings", [])) if dossier else 0,
            "note": CAVEAT if p.get("fin") else (
                "本園無公開財務報告，未進行財務法遵檢核——那是涵蓋範圍限制，不是合規證明。"
            ),
        },
        ui_action={"type": "open_drawer", "institution_id": a.institution_id},
    )


# ── 4. get_penalties ─────────────────────────────────────────────────


def _get_penalties(_ctx: ToolContext, a: InstitutionArgs) -> ToolOutcome:
    p = _point(a.institution_id)
    if not p:
        return _not_found(a.institution_id)
    df = _penalties()
    rows = df[df["short_id"] == a.institution_id]
    items = [{
        "date": r.date, "article": r.article, "law": r.law,
        # 非金錢處分的 fine 是空值而不是 0——填 0 等於謊稱罰了零元。
        "fine": None if _isna(r.fine) else int(r.fine),
        "sanction_type": r.sanction_type,
        "actor_role": r.actor_role, "punishment": r.punishment,
    } for r in rows.itertuples()]
    items.sort(key=lambda x: str(x["date"]), reverse=True)
    return ToolOutcome(payload={
        "institution": p["full"], "count": len(items), "items": items,
        "note": "受處分角色（負責人／行為人）不同即為不同處分，不可合併計數。",
    })


def _isna(v: Any) -> bool:
    return v is None or v != v  # NaN != NaN


# ── 5. get_findings ──────────────────────────────────────────────────


class GetFindingsArgs(BaseModel):
    institution_id: str = Field(description="機構 id，8 碼十六進位")
    year: Optional[int] = Field(default=None, description="學年度，例如 113")


def _get_findings(_ctx: ToolContext, a: GetFindingsArgs) -> ToolOutcome:
    p = _point(a.institution_id)
    if not p:
        return _not_found(a.institution_id)
    _code, dossier = _dossier_for(a.institution_id)
    if dossier is None:
        return ToolOutcome(payload={
            "institution": p["full"], "count": 0, "items": [],
            "note": "本園無公開財務報告，沒有財報法遵檢核結果。資料不足，不是低風險。",
        })
    found = dossier.get("findings", [])
    if a.year is not None:
        found = [f for f in found if f.get("y") == a.year]
    items = [{
        "academic_year": f.get("y"), "rule": f.get("rule"),
        # st 有三態：pass／fail／空值＝待判讀。待判讀不等於通過，也不等於未通過。
        "status": f.get("st"), "severity": f.get("sev"),
        "detail": f.get("detail"), "cited_text": f.get("text"),
    } for f in found]
    return ToolOutcome(
        payload={
            "institution": p["full"], "count": len(items), "items": items,
            "gaps": dossier.get("gaps", []),
            "note": "status 為空值代表待人工判讀，既不是通過也不是未通過。",
        },
        ui_action={"type": "open_drawer", "institution_id": a.institution_id},
    )


# ── 6. set_time_machine ──────────────────────────────────────────────


class SetTimeMachineArgs(BaseModel):
    as_of: str = Field(description="時點，格式 YYYY-MM-DD，例如 2021-01-01")
    n: int = Field(default=20, ge=1, le=MAX_LIMIT, description="取當時的前幾名")


def _set_time_machine(_ctx: ToolContext, a: SetTimeMachineArgs) -> ToolOutcome:
    tl = _ex().load_timeline()
    point = next((p for p in tl["points"] if p["as_of"] == a.as_of), None)
    if point is None:
        return ToolOutcome(payload={
            "error": f"時間軸沒有 {a.as_of} 這一格",
            "available": [p["as_of"] for p in tl["points"]],
        })
    ranking = point.get("ranking", [])[:a.n]
    return ToolOutcome(
        payload={
            "as_of": a.as_of, "trained_on_data_up_to": point.get("train_as_of"),
            "auc": point.get("auc"), "base_rate": point.get("base_rate"),
            "label_complete": point.get("label_complete"),
            "count": len(ranking), "items": ranking,
            # hit 是 null 代表觀察期還沒過完，不是「沒事」。
            "note": "hit 為空值代表後續觀察期尚未結束，不是未受罰。",
        },
        ui_action={"type": "navigate", "tab": "timeline", "as_of": a.as_of},
    )


# ── 7. get_model_card ────────────────────────────────────────────────


class NoArgs(BaseModel):
    pass


def _get_model_card(_ctx: ToolContext, _a: NoArgs) -> ToolOutcome:
    """各時點的真實指標，含不好看的數字。

    被質疑準不準的時候要照實說——挑好看的那幾格講，整份模型卡的可信度會一起賠掉。
    """
    tl = _ex().load_timeline()
    rows = [{
        "as_of": p["as_of"], "auc": p.get("auc"),
        "base_rate": p.get("base_rate"),
        "precision_at": p.get("precision_at"),
        "n_institutions": p.get("n_institutions"), "n_positive": p.get("n_positive"),
        "label_complete": p.get("label_complete"),
        "observed_fraction": p.get("observed_fraction"),
    } for p in tl.get("points", [])]
    return ToolOutcome(
        payload={
            "protocol": tl.get("protocol"), "summary": tl.get("summary"),
            "count": len(rows), "items": rows,
            "note": ("時序切分，非隨機切分。label_complete 為 false 的時點觀察期"
                     "尚未結束，指標不可與完整時點並列比較。"),
        },
        ui_action={"type": "navigate", "tab": "timeline"},
    )


# ── 8. search_documents ──────────────────────────────────────────────


class SearchDocumentsArgs(BaseModel):
    q: str = Field(min_length=1, description="要找的字串，例如「資遣費準備金」")
    institution: Optional[str] = Field(default=None, description="園名簡稱，例如「安溪」")
    year: Optional[int] = Field(default=None, description="學年度，例如 113")
    limit: int = Field(default=8, ge=1, le=20, description="取幾筆")


def _search_documents(_ctx: ToolContext, a: SearchDocumentsArgs) -> ToolOutcome:
    """財報全文檢索。**每一筆都帶原始檔名與頁碼**，這是可引述的基礎。

    本 repo 獨有，來源專案沒有這個 tool——它的證據是渲染頁截圖，需要 data/raw；
    這裡的 4,293 段文字已進版控，不依賴原始 PDF。
    """
    conn = _ex()._connect()
    try:
        res = _ex().docsearch.retrieve(
            conn, a.q, institution=a.institution, year=a.year, limit=a.limit
        )
    finally:
        conn.close()

    passages = [dict(x) for x in res.get("passages", [])]
    facts = [dict(x) for x in res.get("facts", [])]
    # 每一筆補上「可以翻開的那一頁」。頁碼只有 facts 有單一值；passages 的
    # page_label 可能是 "p.11-14" 這種範圍，取第一頁即可（那是段落起始頁）。
    for f in facts:
        _attach_image(f, f.get("page"))
    for p in passages:
        _attach_image(p, _first_page(p.get("page_label")))

    return ToolOutcome(
        payload={
            "query": a.q, "found": res.get("found", 0),
            "passages": passages, "facts": facts,
            "coverage": res.get("coverage"),
            "note": res.get("note", "數值為空白代表未編列，不是 0。"),
        },
        ui_action={"type": "open_drawer", "evidence_query": a.q,
                   "passages": passages[:3], "facts": facts[:3]},
    )


def _first_page(label: Any) -> Optional[int]:
    """從 "p.11-14" 取 11。取不到就 None——猜錯頁碼比沒有圖更糟。"""
    import re

    if not label:
        return None
    m = re.search(r"\d+", str(label))
    return int(m.group()) if m else None


def _attach_image(row: dict, page: Optional[int]) -> None:
    """掛上證據頁的圖片網址。沒有 data/raw 的機器上這個網址會回 404，
    所以只在原始檔真的存在時才掛——不要給出一個一定開不起來的連結。"""
    from urllib.parse import quote

    path = row.get("path")
    if not path or not page:
        return
    if not (ROOT / path).exists():
        return
    # 路徑含中文與斜線，一定要編碼：未編碼的中文查詢字串會被 uvicorn 以
    # 400 Invalid HTTP request 擋掉（這個坑在 /api/docsearch 已經踩過一次）。
    row["image_url"] = f"/api/evidence/page?path={quote(path, safe='')}&page={page}"


# ── 9. load_skill ────────────────────────────────────────────────────


class LoadSkillArgs(BaseModel):
    name: str = Field(description="指引名稱：" + "、".join(SKILL_NAMES))


def _load_skill(_ctx: ToolContext, a: LoadSkillArgs) -> ToolOutcome:
    if a.name not in SKILL_NAMES:
        return ToolOutcome(payload={
            "error": f"沒有這份指引：{a.name}", "available": list(SKILL_NAMES),
        })
    path = SKILLS_DIR / f"{a.name}.md"
    if not path.exists():
        return ToolOutcome(payload={"error": f"指引檔不存在：{path.name}"})
    return ToolOutcome(payload={"name": a.name, "content": path.read_text("utf-8")})


# ── 10. open_memo ────────────────────────────────────────────────────


class OpenMemoArgs(BaseModel):
    institution_id: str = Field(description="機構 id，8 碼十六進位")


def _open_memo(_ctx: ToolContext, a: OpenMemoArgs) -> ToolOutcome:
    """取現行的稽核建議書。檔名是 `<id>_<園名>.txt`，id 就是 8 碼前綴。"""
    p = _point(a.institution_id)
    if not p:
        return _not_found(a.institution_id)
    hits = sorted((ROOT / "data/processed/audit_letters").glob(f"{a.institution_id}_*.txt"))
    if not hits:
        return ToolOutcome(payload={
            "institution": p["full"],
            "note": "這一所沒有現行建議書。未列入本批建議查核名單的園不會產生建議書。",
        })
    return ToolOutcome(
        payload={
            "institution": p["full"], "file": hits[0].name,
            "content": hits[0].read_text("utf-8"),
            "note": "本文是請求說明，不是違法認定。涵蓋範圍那一段要一併讀。",
        },
        ui_action={"type": "open_drawer", "institution_id": a.institution_id,
                   "memo": hits[0].name},
    )


# ── 11. export_schedule ──────────────────────────────────────────────


class ExportScheduleArgs(BaseModel):
    """兩種指定方式，給 id 或給條件，至少要有一種。

    一定要能用條件指定，否則模型得把二十筆 id 逐一抄回來——實測那會吃掉整個
    回合的 token 預算（`stop_reason` 變成 `max_tokens`），而且抄錯不會被發現。
    """

    institution_ids: Optional[list[str]] = Field(
        default=None, max_length=MAX_LIMIT, description="要排入的機構 id 清單"
    )
    towns: Optional[list[str]] = Field(
        default=None, max_length=10,
        description="改用條件指定：這些行政區各取前 n 名。例如 [\"板橋區\",\"三重區\"]",
    )
    n: int = Field(default=20, ge=1, le=MAX_LIMIT, description="用條件指定時每區取幾筆")


def _export_schedule(_ctx: ToolContext, a: ExportScheduleArgs) -> ToolOutcome:
    """組一份稽查排程 CSV 交給瀏覽器下載。**不寫任何檔案、不改任何分數。**"""
    import csv
    import datetime as dt
    import io

    ids: list[str] = list(a.institution_ids or [])
    if a.towns:
        # 與 get_ranking 走同一支函式，所以匯出的名單與畫面上看到的完全一致。
        for town in a.towns:
            res = _srv().get_proposal(n=a.n, town=town)
            ids += [p["i"] for p in res.get("proposal", [])]
    if not ids:
        return ToolOutcome(payload={
            "error": "要排入哪些機構？請給 institution_ids，或用 towns 指定行政區。",
        })

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["交付順序", "機構 id", "機構名稱", "類別", "行政區",
                "列入理由", "裁罰件數", "法遵未通過項數"])
    missing: list[str] = []
    rows = 0
    seen: set[str] = set()
    for iid in ids:
        if iid in seen:
            continue            # 多區合併時同一筆可能出現兩次
        seen.add(iid)
        p = _point(iid)
        if not p:
            missing.append(iid)
            continue
        w.writerow([p["r"], p["i"], p["full"], _ch().TYPE_NAMES.get(p["t"], "?"),
                    p["d"], p.get("why", ""), p.get("np", 0), p.get("cf", 0)])
        rows += 1
    stamp = dt.date.today().isoformat()
    return ToolOutcome(
        payload={
            "rows": rows, "filename": f"稽查排程_{stamp}.csv",
            "missing_ids": missing,
            "note": CAVEAT,
        },
        # download 是唯一會讓資料離開畫面的動作，所以它只送純文字，不含分數。
        ui_action={"type": "download", "filename": f"稽查排程_{stamp}.csv",
                   "mime": "text/csv", "content": buf.getvalue()},
    )


# ── 12. record_feedback ──────────────────────────────────────────────


class RecordFeedbackArgs(BaseModel):
    institution_id: str = Field(description="機構 id，8 碼十六進位")
    item: str = Field(min_length=1, max_length=64,
                      description="建議書或發現的項目識別，例如規則名稱")
    agrees: bool = Field(description="稽查員是否認同這一項")
    note: Optional[str] = Field(default=None, description="補充說明")


def _record_feedback(ctx: ToolContext, a: RecordFeedbackArgs) -> ToolOutcome:
    """記錄稽查員對單項的認同與否。**唯一的寫入型 tool。**

    追加式：不 UPDATE、不 DELETE。改變立場就再寫一列，因為「什麼時候改變了
    想法」本身就是這張人在迴圈資料集的一部分。
    """
    from ..db.models import AuditFeedback

    p = _point(a.institution_id)
    if not p:
        return _not_found(a.institution_id)
    if ctx.db is None or ctx.user is None:
        return ToolOutcome(payload={"error": "未登入，無法記錄回饋"})
    ctx.db.add(AuditFeedback(
        session_id=ctx.session_id, institution_id=a.institution_id,
        memo_item_id=a.item, agrees=a.agrees, note=a.note,
    ))
    ctx.db.commit()
    return ToolOutcome(payload={
        "recorded": True, "institution": p["full"], "item": a.item,
        "agrees": a.agrees, "by": ctx.user.name,
        "note": "已記錄。回饋是追加式的，先前的紀錄不會被覆寫。",
    })


# ── 註冊 ─────────────────────────────────────────────────────────────

_SPECS = [
    ("list_institutions",
     "依行政區、類別、有無前科或財報法遵未通過列出機構，並把畫面帶到派工提案頁籤",
     ListInstitutionsArgs, _list_institutions, False),
    ("get_ranking", "取現行派工提案的前 N 名，含分層理由（tier）",
     GetRankingArgs, _get_ranking, False),
    ("open_institution", "打開一所機構的卷宗", InstitutionArgs, _open_institution, False),
    ("get_penalties", "取一所機構的裁罰紀錄明細", InstitutionArgs, _get_penalties, False),
    ("get_findings", "取一所機構的財報法遵檢核發現與準備金缺口走勢",
     GetFindingsArgs, _get_findings, False),
    ("set_time_machine", "把時間軸切到指定時點，看當時的排序與後續是否受罰",
     SetTimeMachineArgs, _set_time_machine, False),
    ("get_model_card", "取各時點的模型指標（AUC、前 N 名命中率），含不好看的數字",
     NoArgs, _get_model_card, False),
    ("search_documents", "在 132 份非營利財報全文中檢索，回傳段落與原始頁碼",
     SearchDocumentsArgs, _search_documents, False),
    ("open_memo", "打開一所機構的現行稽核建議書", OpenMemoArgs, _open_memo, False),
    ("export_schedule",
     "把機構整理成稽查排程 CSV 供下載。可給 institution_ids，"
     "也可只給 towns 讓它自己取各區前 n 名（不必回抄 id）",
     ExportScheduleArgs, _export_schedule, False),
    ("record_feedback", "記錄稽查員對建議書單項的認同與否（追加式）",
     RecordFeedbackArgs, _record_feedback, True),
    ("load_skill", "載入一份作業指引", LoadSkillArgs, _load_skill, False),
]


def build_registry() -> ToolRegistry:
    """模組層級呼叫一次，供 SSE 迴圈與 MCP server **共用同一份**。

    兩邊各建一份的那天，就是白名單失效的那天。
    """
    reg = ToolRegistry()
    for name, desc, params, handler, writes in _SPECS:
        reg.register(ToolSpec(
            name=name, description=desc, params=params, handler=handler, writes=writes,
        ))
    return reg
