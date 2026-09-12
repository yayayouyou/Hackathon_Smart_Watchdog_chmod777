"""一輪對話。

搬自 `Eason20050201/hackathon@a0bdada` 的 `backend/app/agent/loop.py`。
改動：import 路徑、system prompt 改描述本 repo 的畫面（頁籤而非路由）。

每一步的形狀固定：講解句、tool 呼叫、結果。`step_id` 把三者綁在一起，前端靠它
決定「先打字、再動畫面、再說結果」的順序。

模型一步吐出多個 tool 呼叫時**全部執行**——協定要求每個 tool_use 都要有對應的
tool_result，只做第一個會讓下一輪的訊息串不合法。

**稽核分工**：`ToolRegistry.execute()` 負責把 `tool_call`／`tool_result` 兩種
`agent_message` 寫進資料庫——MCP server 也會呼叫 `execute()`，稽核放在這支迴圈
裡的話 MCP 那條路徑就完全沒有稽核。這支迴圈只寫 `text` 一種（使用者訊息與
agent 的講解句），不重複寫 tool 的稽核列。SSE 事件仍然完整送出。

**文字不邊吐邊送**：講解句要套禁用詞過濾，就不能邊吐邊送——字已經出去了才發現
要換掉。做法是把整步的文字收完、過濾完，一則 `text` 事件送出；打字動畫交給前端。

**`ToolDone`**：走 MCP 的後端把 tool 丟給 MCP server 執行，`registry.execute()`
已經在那邊跑過一次（稽核也寫過了），這支迴圈收到 `ToolDone` 時**跳過**
`execute()`，只把它已經帶著的 payload／ui_action 轉成 SSE 事件——再執行一次會讓
寫入型 tool 多寫一列。
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator

from ..db.models import AgentMessage
from .memory import load_history, user_block
from .narration import soften
from .protocol import AgentBackend, TextDelta, ToolDone, ToolUse, TurnEnd
from .registry import ToolContext, ToolDenied, ToolInvalid, ToolRegistry

MAX_STEPS = 8
STEP_TIMEOUT_S = 10

SYSTEM_PROMPT = """你是新北市教育局風險預警系統的操作助手。

你的工作是操作這個網站並解釋你在做什麼。你不做判斷、不下結論、不評價任何機構。

每一步的規矩：
- 動作前說一句你要做什麼，動作後說一句你看到什麼。每句不超過 30 個字。
- 你說出來的每個數字，都必須是剛才 tool 回傳的。沒查到的東西就說「資料不足」。
- 不要說某所園「違法」「造假」「有問題」。只能說「排序在前」「訊號指向」。
- 排序是稽查順序的建議，不是查核結論。被問到就這樣講。
- 主檔一列是一筆登記，不是一所實體園，一律說「筆」，不說「家」或「所」。
- 不要輸出 markdown（粗體、反引號、標題符號）；講解句是純文字，符號會原樣顯示。

畫面由六個頁籤組成：派工提案、查詢、助理、掃描、時間軸、建議書；
左側是全市地圖，點任一機構會展開它的卷宗。

地圖的顏色代表**事實**不是風險高低：預設依機構類別（公立／非營利／私立），
也可以用 set_map_view 改成依歷史裁罰件數。兩者都是公開事實，
**我們算出來的分數不上地圖**。被問到顏色就要講清楚這件事。

掃描會花錢，所以你只能用 scan_estimate 算錢，**不能發動掃描**。
要真的執行請人自己到掃描頁籤按。

你只能透過 tools 操作網站。沒有對應 tool 的事情就說你做不到。

可用的作業指引（需要時用 load_skill 載入，照它寫的步驟做）：
schedule_inspection 排稽查、read_memo 解讀建議書、explain_risk 解釋為何排序在前、
answer_challenge 回應對準確度的質疑、read_evidence 指出數字出自哪一頁。
"""


@dataclasses.dataclass(frozen=True)
class SSEEvent:
    event: str
    data: dict


def _summarise(payload: dict) -> str:
    if "count" in payload:
        return f"取得 {payload['count']} 筆"
    if "items" in payload:
        return f"取得 {len(payload['items'])} 筆"
    if "note" in payload:
        return payload["note"]
    return "完成"


def _persist_text(
    db, session_id: str, turn: int, step: int, role: str, text: str, softened: list[str]
) -> None:
    """只寫 text 這一種 agent_message。tool_call／tool_result 的稽核在
    `ToolRegistry.execute()` 裡寫，這裡不重複。"""
    db.add(
        AgentMessage(
            session_id=session_id, turn=turn, step_id=step, role=role, kind="text",
            content={"text": text, "softened": softened},
        )
    )
    db.commit()


def run_turn(
    *,
    db,
    user,
    session_id: str,
    turn: int,
    text: str,
    view: dict,
    backend: AgentBackend,
    registry: ToolRegistry,
) -> Iterator[SSEEvent]:
    system = (
        SYSTEM_PROMPT
        + "\n\n"
        + user_block(user)
        + f"\n\n目前畫面：{json.dumps(view, ensure_ascii=False)}"
    )
    messages = load_history(db, session_id)
    messages.append({"role": "user", "content": [{"type": "text", "text": text}]})
    _persist_text(db, session_id, turn, 0, "user", text, [])

    yield SSEEvent("session", {"session_id": session_id, "turn": turn})
    # turn 整輪固定，step_id 逐步更新——registry.execute() 用這兩個欄位把它自己
    # 寫的稽核列與這一步的講解句對起來。ToolContext 是可變的 dataclass，
    # 同一個 ctx 物件跨步驟重複使用。
    ctx = ToolContext(db=db, user=user, view=view, session_id=session_id, turn=turn)
    tools = registry.schemas()

    for step in range(1, MAX_STEPS + 1):
        ctx.step_id = step
        buf: list[str] = []
        calls: list = []
        stop = "end_turn"
        for ev in backend.stream(
            system=system, messages=messages, tools=tools, timeout_s=STEP_TIMEOUT_S
        ):
            if isinstance(ev, TextDelta):
                buf.append(ev.text)
            elif isinstance(ev, (ToolUse, ToolDone)):
                calls.append(ev)
            elif isinstance(ev, TurnEnd):
                stop = ev.stop_reason

        said, hits = soften("".join(buf))
        if said:
            yield SSEEvent("text", {"step_id": step, "text": said, "softened": hits})
            _persist_text(db, session_id, turn, step, "assistant", said, hits)

        if not calls:
            yield SSEEvent("done", {"steps": step, "stop_reason": stop})
            return

        assistant_content: list[dict] = []
        if said:
            assistant_content.append({"type": "text", "text": said})
        results: list[dict] = []
        for ev in calls:
            call = ev.call
            assistant_content.append(
                {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
            )
            yield SSEEvent("tool_call", {
                "step_id": step, "id": call.id,
                "name": call.name, "arguments": call.arguments,
            })
            if isinstance(ev, ToolDone):
                # 已經在 MCP server 裡跑過 execute()（稽核也寫過了），這裡不能
                # 再執行一次，只把結果轉成 SSE 事件推給前端。
                content = json.dumps(ev.payload, ensure_ascii=False, default=str)
                yield SSEEvent("tool_result", {
                    "step_id": step, "id": call.id, "ok": True,
                    "summary": _summarise(ev.payload),
                })
                if ev.ui_action:
                    yield SSEEvent("ui_action", {"step_id": step, **ev.ui_action})
                results.append(
                    {"type": "tool_result", "tool_use_id": call.id, "content": content}
                )
                continue
            try:
                outcome = registry.execute(ctx, call.name, call.arguments, call_id=call.id)
            except (ToolDenied, ToolInvalid) as exc:
                content = json.dumps({"error": str(exc)}, ensure_ascii=False)
                yield SSEEvent("tool_result", {
                    "step_id": step, "id": call.id, "ok": False, "summary": str(exc),
                })
            else:
                content = json.dumps(outcome.payload, ensure_ascii=False, default=str)
                yield SSEEvent("tool_result", {
                    "step_id": step, "id": call.id, "ok": True,
                    "summary": _summarise(outcome.payload),
                })
                if outcome.ui_action:
                    yield SSEEvent("ui_action", {"step_id": step, **outcome.ui_action})
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": content})

        messages.append({"role": "assistant", "content": assistant_content})
        messages.append({"role": "user", "content": results})

        # calls 非空只代表「這一步有 tool 呼叫」，不代表「backend 還要繼續」——
        # 走 MCP 的後端在自己的 subprocess 內就把所有 tool 呼叫做完了，
        # stop_reason 幾乎必然是 end_turn，這時不該再呼叫一次 backend.stream()。
        # Bedrock 後端在 calls 非空時 stop 恆為 tool_use，這個判斷對它是 no-op。
        if stop != "tool_use":
            yield SSEEvent("done", {"steps": step, "stop_reason": stop})
            return

    yield SSEEvent("done", {"steps": MAX_STEPS, "stop_reason": "max_steps"})
