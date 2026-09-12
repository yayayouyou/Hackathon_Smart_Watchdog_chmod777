"""記憶三層：session、使用者、機構。

搬自 `Eason20050201/hackathon@a0bdada` 的 `backend/app/agent/memory.py`，
只改 import 路徑。

session 層（最近 N 則）在這裡用 `load_history` 還原；機構層（回饋）不預載，
要用 tool 讀。使用者層由 `remember` 寫，**呼叫點在路由，不在 tool**——模型能
寫進自己下一輪脈絡的東西，就是一條能自我強化的管道。
"""

from __future__ import annotations

from sqlalchemy import select

from ..db.models import AgentMessage, User

HISTORY_LIMIT = 20


def load_history(db, session_id: str, limit: int = HISTORY_LIMIT) -> list[dict]:
    """把 agent_message 還原成正規化訊息串。只取最近 N 則，軌跡另外完整保留。

    Converse／Messages API 要求角色**嚴格交替**：一則 assistant 訊息可以同時帶
    `text` 與多個 `tool_use` 區塊，一則 user 訊息可以帶多個 `tool_result` 區塊，
    但不能連續兩則同角色訊息。`agent_message` 一列只存一個區塊，所以這裡要把
    連續同角色的列合併回一則多區塊訊息，否則第二輪就會因為角色沒有交替被拒絕。

    只取最近 `limit` 列時，視窗可能切在某一步的中間（例如切在 tool_use 與它的
    tool_result 之間），這裡一律往前修剪到下一個安全起點——一輪的起始使用者訊息
    （`kind="text"` 且 `role="user"`）。那是唯一保證前面沒有懸空 tool_use／
    tool_result 的位置；往前找不到就回傳空清單，**寧可少還原幾則，也不要送出
    不合法的訊息串**。
    """
    rows = (
        db.execute(
            select(AgentMessage)
            .where(AgentMessage.session_id == session_id)
            .order_by(AgentMessage.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    rows = list(reversed(rows))

    start = next(
        (i for i, r in enumerate(rows) if r.kind == "text" and r.role == "user"), None
    )
    if start is None:
        return []
    rows = rows[start:]

    messages: list[dict] = []
    for row in rows:
        if row.kind == "text":
            role = row.role
            block = {"type": "text", "text": row.content["text"]}
        elif row.kind == "tool_call":
            role = "assistant"
            block = {
                "type": "tool_use",
                "id": row.content["id"],
                "name": row.content["name"],
                "input": row.content["arguments"],
            }
        elif row.kind == "tool_result":
            role = "user"
            block = {
                "type": "tool_result",
                "tool_use_id": row.content["id"],
                "content": row.content["content"],
            }
        else:
            continue
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].append(block)
        else:
            messages.append({"role": role, "content": [block]})
    return messages


def user_block(user: User) -> str:
    towns = "、".join(user.towns) if user.towns else "未指定"
    lines = [f"使用者：{user.name}（{user.role}）", f"負責行政區：{towns}"]
    for key, value in sorted((user.agent_memory or {}).items()):
        lines.append(f"{key}：{value}")
    return "\n".join(lines)


def remember(db, user: User, key: str, value) -> None:
    """程式端更新使用者記憶。呼叫點在路由，不在 tool。"""
    memory = dict(user.agent_memory or {})
    memory[key] = value
    user.agent_memory = memory
    db.commit()
