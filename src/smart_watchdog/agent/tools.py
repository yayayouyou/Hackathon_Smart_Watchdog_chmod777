"""tool 實作。**階段 1 只有 `list_institutions`**，其餘見 `docs/MERGE_PLAN.md` §3。

與來源專案（`Eason20050201/hackathon@a0bdada`）最大的差別：那邊的 handler 查
Postgres 的 `institution` 表，這裡**一律讀既有資料來源**——payload、
`api/chat.py` 的篩選引擎、`api/explore.py` 的端點。理由寫在 MERGE_PLAN §3：
兩套機構資料並存會產生兩個互相矛盾的清單與兩套名次。

`list_institutions` 重用 `chat._matches()`，不自己寫一套篩選。那支函式已經是
查詢頁在用的，agent 與使用者手動篩出來的結果因此保證一致——各寫一份的那天，
就是 agent 開始講與畫面不符的數字的那天。
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from ..api import chat as _chat
from ..api import server as _server
from .registry import ToolContext, ToolOutcome, ToolRegistry, ToolSpec

# payload 的 `t` 欄位：0 公立、1 非營利、2 私立。與 chat.py 的 TYPE_NAMES 同源。
TYPE_CODES = {"公立": 0, "非營利": 1, "私立": 2}
MAX_LIMIT = 50


class ListInstitutionsArgs(BaseModel):
    town: Optional[str] = Field(
        default=None, description="行政區全名，例如「板橋區」。不給就是全市。"
    )
    type: Optional[str] = Field(
        default=None, description="機構類別：公立／非營利／私立。不給就是全部。"
    )
    has_penalty: Optional[bool] = Field(
        default=None, description="只要有歷史裁罰紀錄的登記"
    )
    has_compliance_failure: Optional[bool] = Field(
        default=None, description="只要財報法遵檢核未通過的登記"
    )
    limit: int = Field(default=20, ge=1, le=MAX_LIMIT, description="取幾筆，上限 50")


def _list_institutions(_ctx: ToolContext, a: ListInstitutionsArgs) -> ToolOutcome:
    payload = _server.payload()
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

    points = payload.get("points", [])
    hits = [p for p in points if _chat._matches(p, filters, mentions)]
    hits.sort(key=lambda p: p["r"])          # r 是交付順序，不是模型分數
    rows = [{
        "id": p["i"],
        "title": p["full"],
        "type": _chat.TYPE_NAMES.get(p["t"], "?"),
        "town": p["d"],
        "priority_rank": p["r"],
        "reason": p.get("why", ""),
        "penalties": p.get("np", 0),
        "compliance_failed": p.get("cf", 0),
        "has_financial_report": bool(p.get("fin")),
    } for p in hits[:a.limit]]

    return ToolOutcome(
        payload={
            "count": len(rows),
            "matched": len(hits),
            "items": rows,
            # 每一次回覆都帶同一句界線。模型會照著講，而這正是我們要它講的。
            "note": ("這是建議查核的優先序，不是違法認定；"
                     "無公開財報者屬資料不足，不是低風險。"),
        },
        # 切到派工提案頁籤並套上同一組篩選，讓畫面與 agent 說的話一致。
        ui_action={
            "type": "set_filters",
            "tab": "list",
            "filters": {k: v for k, v in {
                "town": a.town, "type": a.type,
                "has_penalty": a.has_penalty,
                "has_compliance_failure": a.has_compliance_failure,
            }.items() if v is not None},
            "ids": [r["id"] for r in rows],
        },
    )


def build_registry() -> ToolRegistry:
    """模組層級呼叫一次，供 SSE 迴圈與 MCP server **共用同一份**。

    兩邊各建一份的那天，就是白名單失效的那天。
    """
    reg = ToolRegistry()
    reg.register(ToolSpec(
        name="list_institutions",
        description="依行政區、類別、有無前科或財報法遵未通過列出機構，"
                    "並把畫面帶到派工提案頁籤",
        params=ListInstitutionsArgs,
        handler=_list_institutions,
        writes=False,
    ))
    return reg
