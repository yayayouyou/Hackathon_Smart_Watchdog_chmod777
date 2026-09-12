"""tool 白名單與參數驗證。

搬自 `Eason20050201/hackathon@a0bdada` 的 `backend/app/agent/registry.py`，
只改 import 路徑。

**安全邊界在這裡，不在前端。** 註冊表裡沒有能改分數、改建議書、對外送資料的
tool，所以即使 LLM 被說服要做那些事，也沒有工具可用。前端的分派表只是把六種
已知動作對應到函式，不是安全機制。

稽核寫在 `execute()`，不留給呼叫端（SSE 迴圈）自己記：`execute()` 是迴圈與
MCP server 兩條路徑**唯一的交會點**，放在任何一條路徑各自記，另一條就會漏掉。
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Callable
from typing import Any, Optional

from pydantic import BaseModel, ValidationError

from ..db.models import AgentMessage

UI_ACTION_TYPES = frozenset(
    # open_table：資料室用。未列在這裡的型別會讓 ToolOutcome.__post_init__ 直接
    # ValueError，而 loop.py 只 catch ToolDenied/ToolInvalid——漏加的話第一次
    # 呼叫就是整輪 error，不是降級。
    {"navigate", "set_filters", "open_drawer", "close_drawer", "highlight",
     "download", "open_table"}
)


class ToolDenied(RuntimeError):
    """要求的 tool 不在白名單，或這個身分不得使用。"""


class ToolInvalid(ValueError):
    """參數不符 schema。"""


@dataclasses.dataclass
class ToolContext:
    db: Any
    user: Any
    view: dict
    session_id: str
    # 迴圈逐步呼叫時會填真正的回合／步驟號；預設 0 讓尚未接上迴圈的呼叫端
    # （測試、MCP）不必跟著改。
    turn: int = 0
    step_id: int = 0


@dataclasses.dataclass(frozen=True)
class ToolOutcome:
    payload: dict
    ui_action: Optional[dict] = None

    def __post_init__(self) -> None:
        if self.ui_action is not None and self.ui_action.get("type") not in UI_ACTION_TYPES:
            raise ValueError(f"未知的 ui_action 型別：{self.ui_action.get('type')!r}")


@dataclasses.dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    params: type[BaseModel]
    handler: Callable[[ToolContext, Any], ToolOutcome]
    writes: bool = False
    anonymous_ok: bool = False


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"tool 重複註冊：{spec.name}")
        self._specs[spec.name] = spec

    def names(self) -> list[str]:
        return sorted(self._specs)

    def schemas(self, *, anonymous: bool = False) -> list[dict]:
        specs = [s for s in self._specs.values() if s.anonymous_ok or not anonymous]
        return [
            {
                "name": s.name,
                "description": s.description,
                "input_schema": s.params.model_json_schema(),
            }
            for s in sorted(specs, key=lambda s: s.name)
        ]

    def execute(
        self, ctx: ToolContext, name: str, arguments: dict, *, call_id: Optional[str] = None
    ) -> ToolOutcome:
        """執行一個 tool 並寫稽核。

        稽核列的鍵名必須與 `memory.load_history()` 讀取時用的鍵一致，否則同一個
        session 的第二輪對話會在還原歷史時 KeyError——那個接縫在來源專案曾經
        真的斷過。MCP 那條路徑沒有模型給的 id，自己補一個，稽核仍然完整。
        """
        if call_id is None:
            call_id = f"call_{uuid.uuid4().hex[:12]}"
        try:
            spec = self._specs.get(name)
        except TypeError as exc:
            # name 不可雜湊（例如傳了 list／dict）：待驗證的輸入沒有理由讓
            # dict.get() 的 TypeError 直接炸出去，一律歸類為「未註冊」。
            raise ToolDenied(f"未註冊的 tool：{name!r}") from exc
        if spec is None:
            raise ToolDenied(f"未註冊的 tool：{name}")
        if ctx.user is None and not spec.anonymous_ok:
            raise ToolDenied(f"未登入不得使用 {name}")
        try:
            parsed = spec.params.model_validate(arguments)
        except ValidationError as exc:
            raise ToolInvalid(f"{name} 參數不符：{exc.errors()[:3]}") from exc
        self._audit(
            ctx, "assistant", "tool_call",
            {"id": call_id, "name": name, "arguments": arguments},
        )
        try:
            outcome = spec.handler(ctx, parsed)
        except Exception as exc:
            # handler 炸掉時仍要補一列 tool_result。理由有二：軌跡上「叫了但沒有
            # 下文」是稽核缺口；而且 `memory.load_history()` 還原時會留下一個
            # 沒有對應 tool_result 的 tool_use，那會讓這個 session 之後每一輪
            # 都被拒收。⚠️ 這一列自己也可能寫不進去（壞掉的就是資料庫的時候），
            # 所以 `load_history` 另有一道尾端修剪兜底。
            if ctx.db is not None:
                # handler 可能留下未提交的寫入，先清掉才寫得了這列稽核。
                ctx.db.rollback()
            self._audit(
                ctx, "tool", "tool_result",
                {
                    "id": call_id,
                    "name": name,
                    # 截到 200 字，與 `api/agent.py` 送 error 事件時同一個長度：
                    # 完整的 traceback 字串會塞爆之後每一輪重送的歷史。
                    "content": json.dumps(
                        {"error": f"{type(exc).__name__}: {exc}"[:200]},
                        ensure_ascii=False,
                    ),
                    "ui_action": None,
                },
            )
            raise
        self._audit(
            ctx, "tool", "tool_result",
            {
                "id": call_id,
                "name": name,
                # load_history 會把這個字串原樣放回模型的 tool_result 區塊，
                # 所以這裡就要是最終形狀，不是結構化的 payload。
                "content": json.dumps(outcome.payload, ensure_ascii=False, default=str),
                "ui_action": outcome.ui_action,
            },
        )
        return outcome

    def _audit(self, ctx: ToolContext, role: str, kind: str, content: dict) -> None:
        if ctx.db is None:
            return
        ctx.db.add(
            AgentMessage(
                session_id=ctx.session_id, turn=ctx.turn, step_id=ctx.step_id,
                role=role, kind=kind, content=content,
            )
        )
        ctx.db.commit()
