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


def _dos():
    from ..api import dossier

    return dossier

ROOT = pathlib.Path(__file__).resolve().parents[3]
SKILLS_DIR = pathlib.Path(__file__).resolve().parent / "skills"
SKILL_NAMES = (
    # 情境：使用者會怎麼開口 → 該照什麼步驟做。講話的通則在 system prompt，
    # 不重複寫在每一份裡——寫兩份就會有兩套標準。
    "survey_district", "schedule_inspection", "prepare_visit",
    "compare_institutions", "track_changes", "follow_mentions",
    "read_memo", "explain_risk", "answer_challenge", "read_evidence",
)

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
        "note": "機構 id 是 8 碼十六進位。只知道名字的話，"
                "用 list_institutions 的 name 參數換 id。",
    })


# ── 1. list_institutions ─────────────────────────────────────────────


class ListInstitutionsArgs(BaseModel):
    name: Optional[str] = Field(
        default=None,
        description="機構名稱的一部分，例如「安溪」。**使用者只給名字時用這個**"
                    "把它換成 id，不要自己猜 id。"
                    "預設**不要**同時加 town：他問的那一所常常不在畫面現在這一區，"
                    "加了就會撈到 0 筆。真的撈出兩所同名時才用 town 縮小。",
    )
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
    evaluation_partial: Optional[bool] = Field(
        default=None, description="只要近兩年評鑑部分指標未通過的登記"
    )
    has_mentions: Optional[bool] = Field(
        default=None, description="只要近期有可歸屬公開報導的登記"
    )
    no_financial: Optional[bool] = Field(
        default=None,
        description="只要**沒有**公開財務報告的登記（全市 94.8% 屬此，是涵蓋範圍限制）",
    )
    sort: str = Field(
        default="rank",
        description="排序：rank 交付順序（預設）、penalties 裁罰件數多的在前、"
                    "recent 最近有官方事件的在前",
    )
    limit: int = Field(default=20, ge=1, le=MAX_LIMIT, description="取幾筆，上限 50")


def _list_institutions(_ctx: ToolContext, a: ListInstitutionsArgs) -> ToolOutcome:
    payload = _srv().payload()
    mentions = payload.get("realtime", {}).get("by_institution", {})

    filters: dict = {}
    if a.name:
        filters["name"] = a.name
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
    if a.evaluation_partial:
        filters["evaluation_partial"] = True
    if a.has_mentions:
        filters["has_mentions"] = True
    if a.no_financial:
        filters["no_financial"] = True

    hits = [p for p in payload.get("points", []) if _ch()._matches(p, filters, mentions)]
    # 與查詢頁籤同一組排序鍵。rank 是交付順序（越小越前面），另外兩個是
    # 「最多」「最近」，所以要反向。
    if a.sort == "penalties":
        hits.sort(key=lambda p: -p.get("np", 0))
    elif a.sort == "recent":
        hits.sort(key=lambda p: p.get("evd", ""), reverse=True)
    else:
        hits.sort(key=lambda p: p["r"])
    rows = [{
        "id": p["i"], "title": p["full"], "type": _ch().TYPE_NAMES.get(p["t"], "?"),
        "town": p["d"], "priority_rank": p["r"], "reason": p.get("why", ""),
        "penalties": p.get("np", 0), "compliance_failed": p.get("cf", 0),
        "has_financial_report": bool(p.get("fin")),
        "last_event": p.get("ev", ""), "last_event_date": p.get("evd", ""),
        "mentions": len(mentions.get(p["i"], [])),
    } for p in hits[:a.limit]]

    return ToolOutcome(
        payload={"count": len(rows), "matched": len(hits), "items": rows, "note": CAVEAT},
        ui_action={
            "type": "set_filters", "tab": "list",
            "filters": {k: v for k, v in {
                "town": a.town, "type": a.type, "has_penalty": a.has_penalty,
                "has_compliance_failure": a.has_compliance_failure,
                "evaluation_partial": a.evaluation_partial,
                "has_mentions": a.has_mentions, "no_financial": a.no_financial,
            }.items() if v is not None},
            # 指定行政區時把地圖真的飛過去——「調閱蘆洲區」應該看起來像有人
            # 把地圖放大到蘆洲，而不是只有標記變少。
            "focus_town": a.town,
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
        ui_action={"type": "navigate", "tab": "list", "focus_town": a.town,
                   "ids": [r["id"] for r in rows]},
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
    # 與卷宗畫面走同一支函式——各讀一次 CSV 的那天，就是 agent 講的數字與
    # 畫面不符的那天。
    out = _dos().penalties_of(a.institution_id)
    return ToolOutcome(
        payload={"institution": p["full"], **out},
        ui_action={"type": "open_drawer", "institution_id": a.institution_id},
    )


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
    """取現行的稽核建議書。與卷宗畫面走同一支函式。"""
    p = _point(a.institution_id)
    if not p:
        return _not_found(a.institution_id)
    out = _dos().memo_of(a.institution_id)
    if not out.get("exists"):
        return ToolOutcome(payload={"institution": p["full"], **out})
    return ToolOutcome(
        payload={"institution": p["full"], **out},
        ui_action={"type": "open_drawer", "institution_id": a.institution_id,
                   "memo": out["file"]},
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


# ── 13. get_rank_track ───────────────────────────────────────────────


def _get_rank_track(_ctx: ToolContext, a: InstitutionArgs) -> ToolOutcome:
    """單一機構在各時點的名次軌跡。

    `set_time_machine` 是全市視角（某一格的前 N 名），這個是單園視角
    （這一所在每一格排第幾）。兩個都需要——被問「這家一直都排這麼前面嗎」
    時要的是後者。
    """
    p = _point(a.institution_id)
    if not p:
        return _not_found(a.institution_id)
    out = _dos().rank_track_of(a.institution_id)
    return ToolOutcome(
        payload={"institution": p["full"], **out},
        ui_action={"type": "open_drawer", "institution_id": a.institution_id},
    )


# ── 14. get_staffing ─────────────────────────────────────────────────


def _get_staffing(_ctx: ToolContext, a: InstitutionArgs) -> ToolOutcome:
    """員工數、教保人數、每人人事費、師生比，並附全體同儕基準。

    ⚠️ 非營利園採成本分攤制、薪給結構一致，所以這一段是**查證對照**不是風險
    訊號——全體 94 份非開辦年報告無一落在四分位距外。講的時候不要說成異常。
    """
    p = _point(a.institution_id)
    if not p:
        return _not_found(a.institution_id)
    _code, dossier = _dossier_for(a.institution_id)
    if dossier is None or not dossier.get("staff"):
        return ToolOutcome(payload={
            "institution": p["full"], "count": 0, "items": [],
            "note": "本園無公開財務報告，沒有員工與人事費資料。資料不足，不是低風險。",
        })
    bench = _srv().payload().get("bench", {})
    items = []
    for s in dossier["staff"]:
        per_head = round(s["cost"] / s["st"]) if s.get("st") else None
        ratio = round(s["en"] / s["ed"], 1) if s.get("ed") and s.get("en") else None
        items.append({
            "academic_year": s.get("y"), "staff": s.get("st"),
            "educators": s.get("ed"), "approved": s.get("cap"),
            "enrolled": s.get("en"), "personnel_cost": s.get("cost"),
            "cost_per_head": per_head, "child_per_educator": ratio,
        })
    return ToolOutcome(
        payload={
            "institution": p["full"], "count": len(items), "items": items,
            "peer_median_cost_per_head": bench.get("ph_med"),
            "peer_iqr": [bench.get("ph_q1"), bench.get("ph_q3")],
            "peer_median_child_per_educator": bench.get("ratio_med"),
            "peer_reports": bench.get("n_norm"),
            "note": ("非營利園採成本分攤制、薪給結構一致，這一段是查證對照不是"
                     "風險訊號——全體無一落在四分位距外。不要講成異常。"),
        },
        ui_action={"type": "open_drawer", "institution_id": a.institution_id},
    )


# ── 15. get_realtime ─────────────────────────────────────────────────


def _get_realtime(_ctx: ToolContext, a: InstitutionArgs) -> ToolOutcome:
    """這一所的公開提及（新聞／PTT／Threads）與 Google 評論。

    **這些不影響排序，也不是預測。** 新聞講的是已經發生的裁罰，提前量是 0；
    定位是即時監看。Google 評分只即時顯示、不入庫、不進特徵（Places ToS）。
    """
    p = _point(a.institution_id)
    if not p:
        return _not_found(a.institution_id)
    data = _srv().payload()
    rt = data.get("realtime", {})
    mentions = rt.get("by_institution", {}).get(a.institution_id, [])
    return ToolOutcome(
        payload={
            "institution": p["full"],
            "swept_at": rt.get("swept_at", ""),
            "channels_live": rt.get("channels_live", 0),
            "channels_total": rt.get("channels_total", 0),
            "count": len(mentions),
            "items": [{
                "channel": m.get("channel"), "headline": m.get("headline"),
                "publisher": m.get("publisher"), "published": m.get("published"),
                "url": m.get("url"),
                # 為什麼歸給這所園。名稱能對應 ≥2 所就會拒絕歸屬。
                "attribution_basis": m.get("attribution_basis"),
            } for m in mentions],
            "note": ("提及不影響排序，也不是預測——新聞消費的是已經發生的官方"
                     "裁罰紀錄，提前量為 0。查無提及不代表低風險。"),
        },
        ui_action={"type": "open_drawer", "institution_id": a.institution_id},
    )


# ── 16. list_memos ───────────────────────────────────────────────────


class ListMemosArgs(BaseModel):
    q: Optional[str] = Field(default=None, description="比對園名或行政區，例如「三重」")
    limit: int = Field(default=20, ge=1, le=MAX_LIMIT, description="取幾筆")


def _list_memos(_ctx: ToolContext, a: ListMemosArgs) -> ToolOutcome:
    """本批建議書清單。`open_memo` 是開一份，這個是瀏覽整批。"""
    out = _dos().list_memos(q=a.q, limit=a.limit)
    return ToolOutcome(
        payload=out,
        # 查詢字串要一起送。只切室不帶條件的話，助理講「提到三重的那幾份」
        # 而畫面列出全部 130 份——它講的跟畫面上的不是同一批。
        ui_action={"type": "navigate", "tab": "memos", "memo_query": a.q},
    )


# ── 17. set_map_view ─────────────────────────────────────────────────


class SetMapViewArgs(BaseModel):
    """地圖的顯示設定。每個欄位都可省略，只送要改的那幾個。"""

    colour_by: Optional[str] = Field(
        default=None, description="標記著色依據：type（設立別）或 penalty（裁罰件數）"
    )
    cluster: Optional[bool] = Field(default=None, description="標記群集")
    districts: Optional[bool] = Field(default=None, description="行政區界線")
    district_names: Optional[bool] = Field(default=None, description="行政區名稱")
    mask: Optional[bool] = Field(default=None, description="新北以外反灰")
    choropleth: Optional[bool] = Field(default=None, description="行政區底色")
    flagged_only: Optional[bool] = Field(default=None, description="只顯示本批建議查核")
    focus_town: Optional[str] = Field(
        default=None, description="把地圖飛到這個行政區，例如「蘆洲區」"
    )
    # ⚠️ 範圍必須等於畫面上 #cap 的 min/max（app.js::setCap 會夾到 20–300）。
    # 原本是 1–200：模型送 10，畫面夾成 20，工具卻回報「已套用 10」，助理講的
    # 條件與實際套用的不一致。tests/test_agent_tools.py 釘住兩邊相等。
    capacity: Optional[int] = Field(
        default=None, ge=20, le=300,
        description="本月可稽查家數（20–300），會改變本批提案的筆數與行政區密度",
    )


def _set_map_view(_ctx: ToolContext, a: SetMapViewArgs) -> ToolOutcome:
    """改地圖顯示。**只改畫面，不改任何分數或名次。**

    `colour_by` 兩個值都是中性事實（設立別、公開裁罰件數），**不是我們算出來的
    分數**——分數不上地圖，見 aws-architecture.md §6.5。
    """
    if a.colour_by is not None and a.colour_by not in ("type", "penalty"):
        return ToolOutcome(payload={
            "error": f"colour_by 只能是 type 或 penalty，收到「{a.colour_by}」",
        })
    view = {k: v for k, v in a.model_dump().items() if v is not None}
    if not view:
        return ToolOutcome(payload={"error": "沒有指定任何要改的項目"})
    return ToolOutcome(
        payload={
            "applied": view,
            "note": "只改畫面顯示，不影響排序或分數。地圖顏色代表事實，不是風險高低。",
        },
        ui_action={"type": "set_filters", "map": view},
    )


# ── 18. scan_estimate ────────────────────────────────────────────────


class ScanEstimateArgs(BaseModel):
    channels: list[str] = Field(
        min_length=1, max_length=6,
        description="管道：news_rss、ptt、apify_threads、places_reviews",
    )
    scope: str = Field(
        default="proposal",
        description="範圍：city 全市、proposal 本批提案、compliance_fail 法遵未通過、"
                    "evaluation 評鑑、top_risk 前段班、district 指定行政區",
    )
    town: Optional[str] = Field(default=None, description="scope=district 時的行政區")


def _scan_estimate(_ctx: ToolContext, a: ScanEstimateArgs) -> ToolOutcome:
    """**算錢，不花錢。** 回傳金額上界與會被哪一道上限擋住。

    刻意只給估算、不給發動：讓對話能直接產生支出，風險太高。要真的掃描，
    請人到「掃描」頁籤自己按——那裡會把畫面上的金額回押給伺服器重驗（TOCTOU）。

    直接呼叫 `scan.estimate()` 端點函式，不自己組 plan——估算結果必須與畫面上
    看到的那個數字完全一樣，否則 agent 講的金額會跟使用者要確認的金額不同。
    """
    from ..api.scan import ScanRequest, estimate

    req = ScanRequest(scope=a.scope, district=a.town or "", channels=list(a.channels))
    try:
        out = estimate(req)
    except Exception as exc:  # noqa: BLE001 - 估算失敗要變成可讀訊息，不是 500
        return ToolOutcome(payload={
            "error": f"估算失敗：{exc}",
            "note": "管道或範圍可能不正確；掃描頁籤的選項清單是唯一的來源。",
        })
    return ToolOutcome(
        payload={
            "scope": out.get("label"), "usd_max": out.get("usd_max"),
            "lines": out.get("lines"), "gate": out.get("gate"),
            "expected_leads": out.get("expected_leads"),
            "note": ("這是金額上界，不是實際花費；實際以供應商自報結算。"
                     "本 tool 不會發動掃描——要執行請到掃描頁籤操作。"),
        },
        ui_action={"type": "navigate", "tab": "scan"},
    )


# ── 19. get_peer_comparison ──────────────────────────────────────────


class PeerArgs(BaseModel):
    institution_id: str = Field(description="機構 id，8 碼十六進位")
    year: Optional[int] = Field(default=None, description="學年度，例如 113。不給就是最新")


def _get_peer_comparison(_ctx: ToolContext, a: PeerArgs) -> ToolOutcome:
    """同儕財務比較：這一所在同年度、同類型的非營利園裡看起來多不一樣，以及為什麼。

    **這不是分類器，回傳的百分位不是「違規機率」。** 整個面板只有 10 個正樣本，
    低於 CLAUDE.md 設的監督式門檻——在那個數量上擬合出來的分數是「沒有內容的
    數字被包裝成發現」。

    `contributions` 與 `anomalies` **不是同一件事**，講的時候不可以混：
    - `contributions` 是「這一所的分數由哪幾項拉高」，**本身不是發現**。
      132 園年裡有 97 個沒有任何一項越過門檻，那是正常狀態。
    - `anomalies` 才是越過 robust z = 3.5（Iglewicz-Hoaglin）的項目，
      而那也只是「值得看一下的科目」，不是認定。

    比較基礎是**同學年度、同類型、只用比率**：CLAUDE.md 記了兩個特徵分層後
    效果腰斬，以及一個全市系統性下滑會誤標整個世代。只跟自己那一年的非營利
    同儕比，這兩個問題都不必建模就消掉。
    """
    p = _point(a.institution_id)
    if not p:
        return _not_found(a.institution_id)
    _code, dossier = _dossier_for(a.institution_id)
    peer = (dossier or {}).get("peer")
    if not peer:
        return ToolOutcome(payload={
            "institution": p["full"], "available": False,
            "note": ("這一所沒有同儕財務比較——只有申報公開財報的非營利園才有。"
                     "那是涵蓋範圍限制，不是合規證明。"),
        })
    hist = peer.get("history", [])
    if a.year is not None:
        hit = next((h for h in hist if h.get("y") == a.year), None)
        if hit is None:
            return ToolOutcome(payload={
                "institution": p["full"],
                "error": f"沒有 {a.year} 學年度的比較",
                "available_years": [h.get("y") for h in hist],
            })
        peer = {**peer, **hit}
    return ToolOutcome(
        payload={
            "institution": p["full"],
            "available": True,
            "academic_year": peer.get("y"),
            "percentile": peer.get("pct"),
            "rank_among_peers": peer.get("rank"),
            "peer_count": peer.get("peers"),
            "features_compared": peer.get("nfeat"),
            # 先給貢獻、再給越門檻的，名稱不同、意義不同。
            "contributions": peer.get("contributions", []),
            "anomalies": peer.get("anomalies", []),
            "history": hist,
            "note": ("這是同年度、同類型非營利園之間的相對位置，只用比率不用金額，"
                     "所以大園不會因為規模就顯得異常。**不是分類器、不是違規機率**——"
                     "面板只有 10 個正樣本，撐不起監督式模型。contributions 是分數的"
                     "組成，不是發現；anomalies 才是越過門檻的科目，而那也只代表"
                     "值得看一下。法遵檢核與裁罰紀錄刻意不放進這個計算——那兩者是"
                     "事後用來檢驗這個排序的，訓練過的檢查不算檢查。"),
        },
        ui_action={"type": "open_drawer", "institution_id": a.institution_id},
    )


# ── 20–24. 資料室 ────────────────────────────────────────────────────
#
# 這五個 tool 一律走 `dataroom.store`，與 `/api/dataroom/*` **同一份查詢**。
# 兩邊各寫一份的那天，就是助理講的數字與畫面上的數字開始不一致的那天。
#
# 它們只回答「這份文件上印的是什麼」。同儕比較、風險分數、法遵結論都不在
# 這裡——那些是判讀，屬於卷宗與派工提案，混進來會讓「原件轉錄」這個定位失效。


def _dr():
    from ..dataroom import store

    return store


def _intake():
    from ..dataroom import intake

    return intake


class DocListArgs(BaseModel):
    institution: Optional[str] = Field(
        default=None, description="園名簡稱或代號，例如「安溪」或 N01")
    year: Optional[int] = Field(default=None, description="學年度，例如 113")
    limit: int = Field(default=30, ge=1, le=100)


def _list_documents(_ctx: ToolContext, a: DocListArgs) -> ToolOutcome:
    """列出資料室裡有哪些文件。

    既有的 `search_documents` 必須先有查詢字串，所以在它之前沒有任何 tool
    能回答「我們手上有什麼」。
    """
    rows = _dr().loaded_reports(institution=a.institution, year=a.year)
    items = [{
        "report": r["id"], "code": r["code"], "institution": r["short_name"],
        "academic_year": r["academic_year"], "year_kind": "學年度",
        "extracted_pages": r["pages"], "tables": r["tables"],
        "n_issues": r.get("n_issues"), "identity_ok": r.get("identity_ok"),
        "models": r.get("models"), "dpi": r.get("dpi"),
    } for r in rows[:a.limit]]
    pend = _dr().pending()
    return ToolOutcome(
        payload={
            "count": len(items), "items": items,
            "pending": sorted(pend),
            # 上傳後自動抽取的進度。使用者之後問「好了沒」要查得到。
            "jobs": _intake().recent_jobs(),
            "note": "非營利園財報為純掃描影像，內容出自視覺抽取而非 PDF 文字層。"
                    "公校決算書用年度制、一冊含多園，沒有頁級抽取，不在此清單。",
        },
        ui_action={"type": "navigate", "tab": "data"},
    )


def _prepare_upload(_ctx: ToolContext, _a: NoArgs) -> ToolOutcome:
    """使用者說要上傳、但還沒附檔時：說明怎麼附加，並帶到上傳按鈕前。

    少了它，助理手上沒有任何與上傳有關的東西，被問到就回答「系統沒有上傳功能」。
    助理不能替人選檔（瀏覽器只讓使用者親手打開檔案選擇視窗），所以檔案一定是
    使用者用「+」或「選擇 PDF」自己選的。
    """
    pend = [r for r in _dr().overview()["reports"] if r["state"] == "pending"]
    return ToolOutcome(
        payload={
            "count": len(pend),
            "pending": [{"report": r["id"], "institution": r["short_name"],
                         "academic_year": r["academic_year"], "year_kind": "學年度"}
                        for r in pend],
            "how": ["輸入框上方的「+ 上傳檔案」附加 PDF，再說要放進文件控管室",
                    "或在文件控管室「原始資料」層按「選擇 PDF」"],
            "rules": ["只接受 PDF，檔名不限",
                      "庫裡已有的原件會認出來直接入庫；新的報告會自動抽取，一份約 2–3 分鐘"],
            "note": "助理無法替使用者選檔或按下按鈕（瀏覽器限制）。",
        },
        ui_action={"type": "open_table", "layer": "raw", "upload": True},
    )


class AttachmentArgs(BaseModel):
    attachment_id: str = Field(description="使用者訊息裡「附件代號」後面那串")


def _add_to_dataroom(ctx: ToolContext, a: AttachmentArgs) -> ToolOutcome:
    """把使用者用「+」附加的檔案放進文件控管室。

    入庫一律走 `intake.submit`，與文件控管室自己的「選擇 PDF」同一條路——
    兩邊各寫一份，就會出現「畫面上傳得進去、叫助理放卻不行」。
    """
    from . import attachments

    got = attachments.load(a.attachment_id, getattr(ctx.user, "id", None))
    if got is None:
        return ToolOutcome(payload={"error": "查無此附件（代號不對，或不是這位使用者附加的）"})
    meta, blob = got
    res = _intake().submit(meta["filename"], blob)
    if not res.get("ok"):
        return ToolOutcome(payload={"error": res.get("detail", "無法入庫")})

    # 狀態給中文：模型曾把 "loaded" 原樣講給使用者聽。
    status_zh = {"loaded": "已入庫", "already_loaded": "庫中已有，未重複入庫",
                 "extracting": "背景抽取中"}
    payload = {k: res[k] for k in ("report", "institution", "academic_year", "detail")
               if k in res}
    payload["status"] = status_zh[res["status"]]
    if res["status"] == "extracting":
        job = res["job"]
        payload.update(
            job=job["id"], pages=job["pages_total"],
            # note 會出現在畫面的步驟小字上（loop._summarise），只放給人看的短句；
            # 給模型的說明放 guide——曾把「回覆時不要提工具名稱」直接秀給使用者。
            note=f"背景抽取中，共 {job['pages_total']} 頁",
            # 認不得的是「這個檔案」，不代表是新的報告——抽完也可能是庫裡已有的那份。
            guide=("這個檔案與庫裡的原件都不相同，已在背景自動抽取（一份約 2–3 分鐘，"
                  "會產生 Bedrock 費用）。抽完才知道是哪一所園、哪一學年度，也可能是"
                  "庫裡已有的報告；認不出來會標「無法辨識」，不會猜。進度顯示在文件控管室，使用者之後問起再查一次"
                  "文件清單即可；回覆時不要提工具名稱。"))
        ui = {"type": "open_table", "layer": "raw", "job": job["id"]}
    else:
        if res["status"] == "loaded":
            payload.update(tables=res["added"]["tables"],
                           note="依檔案內容認出這份原件，直接用既有的抽取結果入庫，沒有重新抽取。")
        ui = {"type": "open_table", "layer": "raw", "refresh": True, "report": res["report"],
              "pages": (res["upload"]["pdf_pages"] or res["added"]["extracted_pages"])
              if res["status"] == "loaded" else None}
    return ToolOutcome(payload=payload, ui_action=ui)


class TableTypesArgs(BaseModel):
    institution: Optional[str] = Field(default=None, description="園名簡稱或代號")
    year: Optional[int] = Field(default=None, description="學年度")


def _list_table_types(_ctx: ToolContext, a: TableTypesArgs) -> ToolOutcome:
    """有哪幾種表單，各幾張。這是資料室選單本身。"""
    store = _dr()
    if not a.institution and a.year is None:
        secs = store.overview()["sections"]
        items = [{"section": s["key"], "section_zh": s["zh"],
                  "tables": s["tables"], "reports": s["reports"]} for s in secs]
    else:
        rows = store.find_tables(institution=a.institution, year=a.year,
                                 limit=400)
        agg: dict[str, dict] = {}
        for t in rows:
            e = agg.setdefault(t["section"], {
                "section": t["section"], "section_zh": t["section_zh"],
                "tables": 0, "reports": set()})
            e["tables"] += 1
            e["reports"].add(t["report"])
        items = sorted(({**v, "reports": len(v["reports"])} for v in agg.values()),
                       key=lambda e: -e["tables"])
    return ToolOutcome(
        payload={
            "count": len(items), "items": items,
            "note": "「未分類明細」是標題無法歸類的表，刻意保留成一項而不藏起來。",
        },
        # 帶上園所與學年度：只給 section 的話，畫面會列出全 132 園的同一種表，
        # 跟助理剛才講的那一所對不起來。
        ui_action={"type": "open_table",
                   "sections": [i["section"] for i in items[:8]],
                   "institution": a.institution, "year": a.year},
    )


class GetTableArgs(BaseModel):
    uid: Optional[str] = Field(default=None, description="表的代號，例如 N01/113/p05/t1")
    institution: Optional[str] = Field(default=None, description="園名簡稱或代號")
    year: Optional[int] = Field(default=None, description="學年度")
    section: Optional[str] = Field(
        default=None, description="表單類型鍵，例如 personnel_detail")
    nth: int = Field(default=1, ge=1, le=50, description="同類多張時取第幾張")


def _get_table(_ctx: ToolContext, a: GetTableArgs) -> ToolOutcome:
    """取一張表的全部內容，**含空白格**。

    空白格是這個 tool 存在的理由之一：`nonprofit_pagewise_facts.csv` 對空白
    是整列跳過，而「業務發展費預算欄空白」＝未編列預算，是一項稽查發現。
    """
    store = _dr()
    uid = a.uid
    if not uid:
        rows = store.find_tables(section=a.section, institution=a.institution,
                                 year=a.year, limit=a.nth)
        if len(rows) < a.nth:
            return ToolOutcome(payload={
                "found": False,
                "note": "找不到符合條件的表。這代表這份報告沒有抽到這一種表，"
                        "不代表機構沒有編列——資料不足，不是低風險。",
            })
        uid = rows[a.nth - 1]["uid"]

    t = store.get_table(uid)
    if t is None:
        return ToolOutcome(payload={"found": False, "uid": uid,
                                    "note": "這張表不在已載入的資料裡。"})
    rows_out = [{
        "item_label": r.get("label"), "note_ref": r.get("note_ref"),
        "values": r.get("values") or [],
        # 前端與模型都要能分辨「空白」與「0」，所以另給一條布林陣列，
        # 不要求讀者自己去判斷 null。
        "blanks": [v is None for v in (r.get("values") or [])],
        "percents": r.get("percents"),
    } for r in t["rows"]]
    return ToolOutcome(
        payload={
            "found": True, "uid": t["uid"], "report": t["report"],
            "institution": t["institution"], "academic_year": t["academic_year"],
            "section": t["section"], "section_zh": t["section_zh"],
            "section_inherited": t["section_inherited"],
            "title": t["title"], "context_heading": t["context_heading"],
            "unit": t["unit"], "aligned": t["aligned"],
            "citation": f'{t["report"]} p.{t["printed_page"]}',
            "pdf_page": t["pdf_page"], "printed_page": t["printed_page"],
            "period_labels": t["period_labels"], "rows": rows_out,
            "page_issues": t["issues"],
            "note": "數值為 null 代表原件那一格空白（未編列），不是 0。"
                    "本表為原件轉錄，不含任何判讀。",
        },
        # uid 前端用不到——資料室是按「園所＋學年度＋表單類型」在瀏覽的，
        # 沒有「只顯示這一張」的畫面。所以把那三個維度一起送過去。
        ui_action={"type": "open_table", "uid": t["uid"],
                   "section": t["section"], "institution": t["institution"],
                   "year": t["academic_year"]},
    )


class CompareYearsArgs(BaseModel):
    institution: str = Field(description="園名簡稱或代號，例如「安溪」")
    section: str = Field(description="表單類型鍵，例如 personnel_detail")
    max_rows: int = Field(default=30, ge=1, le=100)


def _compare_table_across_years(_ctx: ToolContext, a: CompareYearsArgs) -> ToolOutcome:
    """同一種表跨學年度對齊。

    ⚠️ 這件事交給模型自己用多次 `get_table` 做一定會錯：學年度 N 的資產負債表
    基準日是 (N+1)/7/31，而同一份報告裡兩張同名表期間不同是常態。所以這裡
    **逐年回傳該年自己的 `period_labels` 原文**，不用 `academic_year` 代稱期間。
    """
    res = _dr().compare_years(a.institution, a.section, max_rows=a.max_rows)
    return ToolOutcome(payload={
        **res,
        "note": res.get("note", "")
        + " 期間請以各年度的 period_labels 原文為準，不要用學年度代稱。"
          " unmatched_items 是只出現在部分年度的科目，不是消失。",
    })


class ExtractionNotesArgs(BaseModel):
    institution: Optional[str] = Field(default=None, description="園名簡稱或代號")
    year: Optional[int] = Field(default=None, description="學年度")
    limit: int = Field(default=20, ge=1, le=50)


def _get_extraction_notes(_ctx: ToolContext, a: ExtractionNotesArgs) -> ToolOutcome:
    """抽取過程自報的疑點。

    「完整抽取」這四個字唯一撐得住的方式，是系統講得出自己哪裡不完整。
    """
    rows = _dr().extraction_notes(institution=a.institution, year=a.year,
                                  limit=a.limit)
    return ToolOutcome(payload={
        "count": len(rows), "items": rows,
        "unresolved": sum(1 for r in rows if r["unresolved"]),
        "note": "這些是抽取時「這一格看不清楚／自相矛盾」的自報疑點，"
                "**不是機構的稽查發現**。標 unresolved 的是模型自己寫明"
                "需要人工確認的，不可當成已確認的事實引用。",
    })


# ── 註冊 ─────────────────────────────────────────────────────────────

# ── 25–28. 輿情蒐集 ──────────────────────────────────────────────────
#
# 這一室有兩塊，性質完全不同，講的時候不可以混：
#   上半「社群聲音」讀的是**已經收進來的**東西——民眾在 Threads 上 @標註官方
#   帳號的通報、新聞與 PTT 的提及。讀庫不花錢。
#   下半「掃描主控台」是**去外面抓新的**，每一次執行都計費。
#
# 這四個 tool 全部屬於上半與帳本，都是唯讀、都不花錢。發動掃描與採用結果
# 刻意不給，理由見 `scan_estimate` 與 test_agent_tools 的那兩支測試。


def _soc():
    from ..api import social

    return social


class SocialListArgs(BaseModel):
    town: Optional[str] = Field(default=None, description="行政區全名，例如「板橋區」")
    channel: Optional[str] = Field(
        default=None,
        description="管道：apify_threads、news_rss、ptt、vendor_feed。不給就是全部",
    )
    since: Optional[str] = Field(
        default=None, description="只看這個日期之後的，格式 YYYY-MM-DD")
    limit: int = Field(default=20, ge=1, le=MAX_LIMIT)


def _list_social(_ctx: ToolContext, a: SocialListArgs) -> ToolOutcome:
    """目前有公開社群訊號的機構。

    ⚠️ 這不是聲量排行榜，排序依據是時間不是分數；社群聲量刻意不併入風險分數。

    ⚠️ 這裡查無不代表沒事。`coverage` 會講清楚只列出「本系統已取得公開社群
    內容」的機構——1,213 所裡目前只有個位數有訊號，其餘是**沒抓到**，不是
    **沒問題**。回傳一定帶著 coverage 與 disclaimer，講的時候要一起講。
    """
    out = _soc().browse(limit=a.limit, town=a.town, channel=a.channel,
                        since=a.since, db=_ctx.db)
    return ToolOutcome(
        payload={
            "count": out.get("count"), "matched": out.get("matched"),
            "items": out.get("items"), "coverage": out.get("coverage"),
            "swept_at": out.get("snapshot_swept_at"),
            "note": out.get("disclaimer"),
        },
        ui_action={"type": "open_voice"},
    )


def _get_social(_ctx: ToolContext, a: InstitutionArgs) -> ToolOutcome:
    """一所機構的社群串：主貼文、底下的回覆、新聞與 PTT 提及、Google 評論。

    回覆要與主貼文分開講。縮排會讓人把回覆讀成「也是在講這一園」，而指名別家
    的那一則後端已經標了出去向——講的時候不可以把它算進這一所。

    Google 評分**不是風險訊號**：裁罰 ≥5 件的園評分中位 4.20、無裁罰者 4.60
    （p=0.061，不顯著），個案更完全不具鑑別力（16 件裁罰的園 4.7 星）。
    它是稽查員到場前值得看一眼的家長觀感，不入庫、不進特徵、不影響排序。

    ⚠️ 這一支會即時查一次 Google 評論，**那是計費的**（`realtime/ledger.py`
    有額度閘門，超支會被擋下而不是靜默多花）。走的是與使用者自己點開那一列
    完全相同的路徑——兩邊看到同一份資料，才不會出現「助理說的跟畫面不一樣」。
    """
    p = _point(a.institution_id)
    if not p:
        return _not_found(a.institution_id)
    out = _soc().institution_social(a.institution_id, live=True, db=_ctx.db)
    return ToolOutcome(
        payload={
            "institution": p["full"],
            "has_signal": out.get("has_signal"),
            "reason": out.get("reason"),
            "counts": out.get("counts"),
            "threads": out.get("threads"), "mentions": out.get("mentions"),
            "reviews": out.get("reviews"),
            "note": out.get("disclaimer"),
        },
        ui_action={"type": "open_voice", "institution_id": a.institution_id},
    )


class ScanJobsArgs(BaseModel):
    job_id: Optional[str] = Field(
        default=None, description="指定一次掃描的代號；不給就是列出最近幾次")
    limit: int = Field(default=10, ge=1, le=30)


def _list_scan_jobs(_ctx: ToolContext, a: ScanJobsArgs) -> ToolOutcome:
    """過去掃描的紀錄與抽到了什麼。**唯讀，不會發動新的掃描。**

    「上次掃到什麼」是這一室最常被問的問題，而在這之前助理只能算錢——
    它講得出一次掃描要多少錢，卻講不出上一次花的錢換到了什麼。
    """
    from ..api import scan as scan_api

    if a.job_id:
        try:
            job = scan_api.get_job(a.job_id)
        except Exception as exc:  # noqa: BLE001 - 查無要變成可讀訊息，不是 500
            return ToolOutcome(payload={"error": f"查無這次掃描：{exc}"})
        return ToolOutcome(payload=job, ui_action={"type": "navigate", "tab": "scan"})
    out = scan_api.list_jobs(limit=a.limit)
    jobs = out.get("jobs", [])
    return ToolOutcome(
        payload={
            "count": len(jobs), "items": jobs,
            "note": "這是已經執行過的掃描紀錄。要知道再掃一次要多少錢用 "
                    "scan_estimate；本 tool 與那一支都不會真的發動掃描。",
        },
        ui_action={"type": "navigate", "tab": "scan"},
    )


def _get_scan_budget(_ctx: ToolContext, _a: NoArgs) -> ToolOutcome:
    """掃描的預算與已花費。唯讀。

    被問「你們這樣要花多少錢」時要答得出實際數字，而不是只答單次估價。
    """
    from ..api import scan as scan_api

    out = scan_api.budget()
    return ToolOutcome(
        payload={**out, "note": "金額以本機帳本為準，供應商自報結算可能有出入。"},
        ui_action={"type": "navigate", "tab": "scan"},
    )


class StartScanArgs(BaseModel):
    channels: list[str] = Field(
        default_factory=lambda: ["news_rss", "ptt"],
        min_length=1, max_length=6,
        description="管道。**只能用不花錢的**：news_rss 新聞、ptt。"
                    "apify_threads 與 places_reviews 要付費，本 tool 會拒絕，"
                    "那兩條請估價後請使用者自己到畫面上按執行。",
    )
    scope: str = Field(
        default="proposal",
        description="範圍：city 全市、proposal 本批提案、compliance_fail 法遵未通過、"
                    "evaluation 評鑑、top_risk 前段班、district 指定行政區",
    )
    town: Optional[str] = Field(default=None, description="scope=district 時的行政區")


def _start_scan(_ctx: ToolContext, a: StartScanArgs) -> ToolOutcome:
    """真的發動一次掃描——**但只限不花錢的管道**。

    為什麼不是寫死「news_rss 與 ptt 可以」：定價會變，而寫死的名單不會。
    這裡先跑一次真正的 `estimate()`，只有**每一條管道都估出剛好 0 元**才放行。
    哪天 RSS 開始收費，這道閘門會自己開始擋，不必有人記得回來改。

    `usd_max` 是 `None` 時一律擋下。那代表「價格未知」，不是「免費」——
    `pricing.unpriced_meter` 的註解已經講過：顯示編造的數字比留白更糟，
    而拿不確定的價格去花錢比兩者都糟。

    要付費的管道請用 `scan_estimate` 報價，然後請使用者自己按。那顆鈕會把畫面
    上的金額原樣回押給伺服器重驗（`confirm_ceiling_usd`），是一道 TOCTOU 保護
    ——由對話代按就繞過了它。
    """
    from ..api.scan import ScanRequest, estimate
    from ..api.scan import start as _start

    req = ScanRequest(scope=a.scope, district=a.town or "", channels=list(a.channels))
    try:
        plan = estimate(req)
    except Exception as exc:  # noqa: BLE001 - 估算失敗要變成可讀訊息，不是 500
        return ToolOutcome(payload={"error": f"估算失敗：{exc}"})

    charged = []
    for line in plan.get("lines", []):
        meter = line.get("meter") or {}
        usd = meter.get("usd_max")
        if usd is None:
            charged.append(f"{line.get('label')}（價格未知）")
        elif usd > 0:
            charged.append(f"{line.get('label')}（US${usd}）")
        elif meter.get("free_remaining") is not None:
            # ⚠️ 「現在算出 0」與「結構上不花錢」是兩回事。
            # Google Places 有每月免費額度，額度內 usd_max 確實是 0——但那個
            # 額度是**本機計數**算的，而它自己的註記寫著「同一把金鑰若被其他
            # 程式使用，本機計數會低估」。也就是說它可能其實已經超額而不自知，
            # 這時放行就是在用一個承認自己可能算錯的數字決定要不要花錢。
            # 只有 `free_meter`（新聞、PTT）不帶 free_remaining，那才是真的
            # 沒有金錢成本。
            charged.append(f"{line.get('label')}（靠免費額度，額度用完就開始計費）")
    if charged:
        return ToolOutcome(payload={
            "error": "這些管道要付費，我不會替你按下去：" + "、".join(charged),
            "note": "免費的是新聞（news_rss）與 PTT。要掃付費管道，我可以先用 "
                    "scan_estimate 報價，再請你自己到掃描主控台按執行——"
                    "那顆鈕會把畫面上的金額回押給伺服器重驗，由我代按會繞過它。",
        }, ui_action={"type": "navigate", "tab": "scan"})

    try:
        job = _start(req)
    except Exception as exc:  # noqa: BLE001 - 被預算或前置條件擋下都要說人話
        detail = getattr(exc, "detail", None)
        return ToolOutcome(payload={
            "error": f"發動失敗：{detail or exc}",
            "note": "常見原因是管道前置條件未備妥，或撞到單次／每日／本期上限。",
        }, ui_action={"type": "navigate", "tab": "scan"})

    return ToolOutcome(
        payload={
            "job_id": job.get("id") or job.get("job_id"),
            "status": job.get("status"),
            "deduplicated": job.get("deduplicated", False),
            "scope": plan.get("scope_label"),
            "channels": list(a.channels),
            "usd_max": plan.get("usd_max"),
            "note": "已發動，這是不花錢的管道。掃描是背景工作，結果用 "
                    "list_scan_jobs 查。抓到的東西是**未經查證的公開內容**，"
                    "供研判參考，不是違法認定，也不計入風險分數。",
        },
        ui_action={"type": "navigate", "tab": "scan"},
    )


_SPECS = [
    ("list_institutions",
     "列出機構：可依行政區、類別、有無前科、財報法遵未通過、評鑑部分未通過、"
     "近期有無公開報導、有無公開財報篩選，並可依交付順序／裁罰件數／最近事件排序。"
     "會把畫面帶到名單頁並移動地圖",
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
    ("get_rank_track", "取單一機構在各時點的名次軌跡（set_time_machine 是全市視角）",
     InstitutionArgs, _get_rank_track, False),
    ("get_staffing", "取一所機構的員工數、每人人事費、師生比，附全體同儕基準",
     InstitutionArgs, _get_staffing, False),
    ("get_realtime", "取一所機構的公開提及（新聞／PTT／Threads）與歸屬依據",
     InstitutionArgs, _get_realtime, False),
    ("list_memos", "瀏覽或搜尋本批建議書清單（open_memo 是開其中一份）",
     ListMemosArgs, _list_memos, False),
    ("set_map_view", "改地圖顯示：著色依據、群集、區界、區名、反灰、底色、"
                     "只看複查名單、派工容量。只改畫面，不改分數",
     SetMapViewArgs, _set_map_view, False),
    ("scan_estimate", "估算一次輿情掃描要花多少錢（算錢不花錢，不會發動掃描）",
     ScanEstimateArgs, _scan_estimate, False),
    ("get_peer_comparison",
     "同儕財務比較：這一所在同年度同類型非營利園中的相對位置與逐項原因"
     "（相對位置，不是違規機率）",
     PeerArgs, _get_peer_comparison, False),
    ("list_documents", "列出資料室裡有哪些已抽取的財務報告（不需先給查詢字串）",
     DocListArgs, _list_documents, False),
    ("prepare_upload",
     "使用者說要上傳檔案、但訊息裡還沒有附件時用：說明怎麼附加，並帶到文件控管室的"
     "上傳按鈕。助理無法替人選檔",
     NoArgs, _prepare_upload, False),
    ("add_to_dataroom",
     "把使用者用「+」附加的 PDF 放進文件控管室入庫（訊息裡有「附件代號」時用）。"
     "庫裡已有的原件直接入庫；新的報告會自動在背景抽取、會花費用。"
     "使用者沒說要放哪裡就先問",
     AttachmentArgs, _add_to_dataroom, True),
    ("list_table_types", "有哪幾種表單、各幾張，並把資料室的類型選單帶到對應位置",
     TableTypesArgs, _list_table_types, False),
    ("get_table", "取一張表的全部內容，含空白格（null＝未編列，不是 0）",
     GetTableArgs, _get_table, False),
    ("compare_table_across_years", "同一種表跨學年度對齊，期間逐年照抄不改寫",
     CompareYearsArgs, _compare_table_across_years, False),
    ("get_extraction_notes", "取抽取過程自報的疑點（不是機構的稽查發現）",
     ExtractionNotesArgs, _get_extraction_notes, False),
    ("list_social_mentions",
     "列出目前有公開社群訊號的機構（民眾 Threads 通報、新聞、PTT）。"
     "唯讀，不發動掃描；查無代表未取得公開內容，不代表無異常",
     SocialListArgs, _list_social, False),
    ("get_social_mentions",
     "取一所機構的社群全貌：Threads 串與回覆、新聞／PTT 提及、Google 評論"
     "（評分不是風險訊號，且會即時查詢一次、計費）",
     InstitutionArgs, _get_social, False),
    ("list_scan_jobs", "查過去執行過的輿情掃描與抽到了什麼（唯讀，不會發動掃描）",
     ScanJobsArgs, _list_scan_jobs, False),
    ("get_scan_budget", "查掃描的預算與已花費（唯讀）",
     NoArgs, _get_scan_budget, False),
    ("start_scan",
     "真的發動一次輿情掃描，**但只限不花錢的管道**（新聞、PTT）。"
     "要付費的管道會被拒絕——那些請用 scan_estimate 報價後請使用者自己按",
     StartScanArgs, _start_scan, True),
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
