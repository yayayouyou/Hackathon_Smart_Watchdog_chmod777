"""FastMCP server：把 registry 的 tool 原封不動暴露給任何 MCP 客戶端。

搬自 `Eason20050201/hackathon@a0bdada` 的 `backend/app/mcp_server.py`，但**換掉
身分機制**。

原本是一組記憶體裡的短效權杖（`llm/mcp_tokens.py`），存在的理由是服務那邊的
`ClaudeCodeAgentBackend`——它每次 spawn `claude -p` 都要臨時帶一個身分進去。
我們沒搬那個後端，而那份實作自己就註明「⚠️ 重啟遺失、多起一個 worker 就各自
為政；正式環境要換成 Redis 或資料庫表，不能就這樣上線」。

這裡改用**既有的 `user_session` 表**：它本來就是 token → 使用者 + 到期時間的
映射，那正是 bearer token 需要的東西，而且是持久化的。`POST /api/agent/mcp-token`
發一個獨立的 session 列給 MCP 用——與瀏覽器那一個分開，所以撤掉 MCP 的存取
不會把人踢出登入狀態。

**工具清單直接從 `build_registry().schemas()` 產生，不另外寫一份**——兩份會漂移，
漂移的那天就是白名單失效的那天。每個 tool 的 handler 只做一件事：把請求解出
身分、組 `ToolContext`、交給 `registry.execute()`。**不在這裡重寫任何驗證**：
沒有權杖／權杖過期／查無使用者都會讓 `ctx.user` 是 `None`，`execute()` 自己會
用 `ToolDenied` 擋下（這 12 個 tool 都要求登入）。

稽核落在每個使用者自己的一列 `agent_session`（id 為 `mcp-<user_id>`），所以
MCP 這條路徑的 tool 呼叫與瀏覽器那條一樣有完整軌跡，且兩者分得開。
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Optional

from ..db.models import AgentSession, User, UserSession
from ..db.session import session as db_session
from .registry import ToolContext, ToolDenied, ToolInvalid

_REGISTRY: Any = None


def _registry() -> Any:
    """延後建立。`tools.py` 會匯入 `api.server`，而 `api.server` 又匯入本模組，
    所以模組層級建 registry 會讓「先匯入哪一邊」決定成敗（實測：先匯入
    `agent.mcp_server` 會 ImportError，MCP 就靜默不掛載）。建一次後快取。
    """
    global _REGISTRY
    if _REGISTRY is None:
        from .tools import build_registry

        _REGISTRY = build_registry()
    return _REGISTRY


def list_mcp_tools(anonymous: bool = False) -> list[dict]:
    """MCP 的工具清單。直接轉發 `registry.schemas()`——不要另外維護一份。"""
    return _registry().schemas(anonymous=anonymous)


def _resolve(db, token: Optional[str]) -> Optional[User]:
    """權杖換身分。與 `api/auth.py::get_current_user` 同一張表、同一套規則。"""
    if not token:
        return None
    row = db.get(UserSession, token)
    if row is None:
        return None
    expires = row.expires_at
    # SQLite 讀回來的 datetime 沒有 tzinfo，直接比較會 TypeError。
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=dt.UTC)
    if expires < dt.datetime.now(dt.UTC) or not row.user.is_active:
        return None
    return row.user


def _mcp_agent_session(db, user: User) -> str:
    """MCP 這條路徑的稽核容器，每個使用者一列，不存在就建。

    與瀏覽器那條路徑的 session 分開，這樣事後看軌跡分得出「這是誰從哪裡叫的」。
    """
    sid = f"mcp-{user.id}"
    if db.get(AgentSession, sid) is None:
        db.add(AgentSession(id=sid, user_id=user.id))
        db.commit()
    return sid


def dispatch(name: str, arguments: dict, token: Optional[str]) -> dict:
    """給 tool handler 用，也給測試直接呼叫（不必真的起一個 HTTP request）。

    `ToolDenied`／`ToolInvalid` 一律轉成溫和的 `{"note": ...}`——與 `tools.py`
    裡「查無」「格式不正確」的既有慣例一致，不讓協定層的錯誤把整個呼叫炸掉，
    模型看得到這句話就能自己反應。
    """
    db = db_session()
    try:
        user = _resolve(db, token)
        session_id = _mcp_agent_session(db, user) if user else ""
        ctx = ToolContext(db=db if user else None, user=user, view={},
                          session_id=session_id)
        outcome = _registry().execute(ctx, name, arguments)
    except (ToolDenied, ToolInvalid) as exc:
        return {"payload": {"note": str(exc)}, "ui_action": None}
    finally:
        db.close()
    return {"payload": outcome.payload, "ui_action": outcome.ui_action}


def build_mcp() -> Any:
    """建 FastMCP app。延後匯入 fastmcp，讓沒裝 web extra 的機器仍能匯入本模組。"""
    from fastmcp import FastMCP
    from fastmcp.server.dependencies import get_http_headers
    from fastmcp.tools import Tool, ToolResult

    def bearer() -> Optional[str]:
        auth = get_http_headers(include={"authorization"}).get("authorization")
        if not auth:
            return None
        prefix = "bearer "
        if auth.lower().startswith(prefix):
            return auth[len(prefix):].strip()
        return None

    class _RegistryTool(Tool):
        """薄薄一層：name／schema 來自 registry，執行也直接轉給 registry。"""

        async def run(self, arguments: dict) -> Any:
            result = dispatch(self.name, arguments, bearer())
            return ToolResult(content=json.dumps(result, ensure_ascii=False, default=str))

    mcp = FastMCP(name="smart-watchdog")
    for schema in list_mcp_tools(anonymous=False):
        mcp.add_tool(_RegistryTool(
            name=schema["name"],
            description=schema["description"],
            parameters=schema["input_schema"],
        ))
    return mcp
