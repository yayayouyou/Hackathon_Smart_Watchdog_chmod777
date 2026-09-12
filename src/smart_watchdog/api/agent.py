"""agent 的 SSE 端點。

搬自 `Eason20050201/hackathon@a0bdada` 的 `backend/app/routers/agent.py`。
改動：前綴對齊本專案（`/api/agent`，原本是 `/api/v1/agent`）、後端選擇改讀
`.env` 的 `AGENT_BACKEND`、去掉那邊的 `settings` 模組。

⚠️ **速率上限是記憶體字典。** 重啟 uvicorn 會遺失狀態，多開一個 worker 這個
上限就形同虛設。這與 `realtime/jobs.py` 的 `_JOBS` 是同一個取捨——本機單一
程序可以接受，正式環境要換成資料庫或 Redis。
"""

from __future__ import annotations

import datetime as dt
import json
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import config
from ..agent.backend import BedrockAgentBackend
from ..agent.loop import run_turn
from ..agent.memory import remember
from ..db.models import AgentMessage, AgentSession, User
from ..db.session import get_db
from .auth import get_current_user

router = APIRouter(prefix="/api/agent", tags=["agent"])

_REGISTRY: Any = None
_RATE: dict[int, list[float]] = {}
RATE_LIMIT_PER_MIN = 6


def _registry() -> Any:
    """延後建立 registry，因為 `agent/tools.py` 會匯入 `api.server`，而
    `api.server` 又匯入這支模組——模組層級匯入時哪邊先進來就會決定成敗
    （實測：先匯入 `agent.tools` 會 ImportError）。建一次後快取。
    """
    global _REGISTRY
    if _REGISTRY is None:
        from ..agent.tools import build_registry

        _REGISTRY = build_registry()
    return _REGISTRY


class MessageIn(BaseModel):
    text: str
    session_id: Optional[str] = None
    # 前端目前在看什麼。進 system prompt，讓 agent 知道「這一頁」是哪一頁。
    view: dict = {}


def _backend() -> Any:
    """目前只有 bedrock 一個正式後端。

    來源專案另有一個走 Claude Code CLI 的開發後端，那需要本機已登入的 CLI，
    不搬——決賽規定只能用 Bedrock，留一條會在容器裡消失的路徑只會誤導。
    **刻意沒有離線退路後端**：在 AI 競賽上展示一個假裝聽懂的 agent，比誠實說
    「這部分接不上」更糟。
    """
    kind = (config.get("AGENT_BACKEND") or "bedrock").lower()
    if kind not in ("bedrock", "claude_code"):
        raise HTTPException(500, f"未知的 AGENT_BACKEND：{kind}")
    return BedrockAgentBackend()


def _check_rate(now: float, user_id: int) -> None:
    hits = [t for t in _RATE.get(user_id, []) if now - t < 60]
    if len(hits) >= RATE_LIMIT_PER_MIN:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "請稍候再試")
    hits.append(now)
    _RATE[user_id] = hits


def _next_turn(db: Session, session_id: str) -> int:
    """這一輪是第幾輪：session 目前已有的最大 turn 加一，第一輪是 1。"""
    max_turn = db.execute(
        select(func.max(AgentMessage.turn)).where(AgentMessage.session_id == session_id)
    ).scalar()
    return (max_turn or 0) + 1


@router.post("/messages")
def post_message(
    body: MessageIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from sse_starlette.sse import EventSourceResponse

    _check_rate(time.monotonic(), user.id)

    session_id = body.session_id
    if session_id is None:
        session_id = str(uuid.uuid4())
        db.add(AgentSession(id=session_id, user_id=user.id))
        db.commit()
    else:
        # 一併檢查「存在」與「屬於這個使用者」。只檢查存在的話，猜到別人的
        # session_id 就能讀到他的對話歷史並往裡面追加訊息——那是物件層級的
        # 授權漏洞。不屬於自己時回 404 而非 403，避免洩漏「這個 id 存在」。
        sess = db.get(AgentSession, session_id)
        if sess is None or sess.user_id != user.id:
            raise HTTPException(404, "查無此對話")

    turn = _next_turn(db, session_id)
    backend = _backend()

    def gen():
        try:
            for ev in run_turn(
                db=db, user=user, session_id=session_id, turn=turn,
                text=body.text, view=body.view,
                backend=backend, registry=_registry(),
            ):
                yield {"event": ev.event, "data": json.dumps(ev.data, ensure_ascii=False)}
        except Exception as exc:  # noqa: BLE001 - 任何失敗都要變成一則 error 事件
            yield {"event": "error",
                   "data": json.dumps({"message": str(exc)[:200]}, ensure_ascii=False)}
            return
        # 使用者記憶由程式更新，不讓 LLM 寫——呼叫點在路由，不在 tool。
        remember(db, user, "last_turn_at", dt.datetime.now(dt.UTC).isoformat())

    return EventSourceResponse(gen())


@router.get("/tools")
def list_tools(user: User = Depends(get_current_user)) -> dict:
    """agent 目前能用的 tool。讓前端與稽查員看得見「它能做什麼」。"""
    del user
    reg = _registry()
    return {"count": len(reg.names()), "tools": reg.schemas()}


MCP_TOKEN_TTL_HOURS = 12


@router.post("/mcp-token")
def mint_mcp_token(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """發一個給 MCP 客戶端用的 bearer token。

    刻意**另發一列** `user_session` 而不是回傳瀏覽器那一顆：撤掉 MCP 的存取
    （刪掉這一列）就不會把人踢出登入狀態，兩者的生命週期分開。

    這是唯一會把可用憑證交出去的端點，所以它要求已登入，且只回傳一次——
    token 沒有保存在任何可再讀取的地方。
    """
    from ..db.models import UserSession
    from ..security import new_token

    token = new_token()
    db.add(UserSession(
        token=token, user_id=user.id,
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=MCP_TOKEN_TTL_HOURS),
    ))
    db.commit()
    return {
        "token": token,
        "expires_in_hours": MCP_TOKEN_TTL_HOURS,
        "usage": "在 MCP 客戶端設定 Authorization: Bearer <token>",
        "mcp_url": "/mcp",
        "note": "這顆權杖等同你的身分，不要外流；它與瀏覽器的登入各自獨立。",
    }
