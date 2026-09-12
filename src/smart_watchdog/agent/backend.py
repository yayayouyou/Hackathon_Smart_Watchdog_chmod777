"""交付後端：Bedrock Converse 串流 + toolConfig。

搬自 `Eason20050201/hackathon@a0bdada` 的 `src/smart_watchdog/llm/agent_bedrock.py`。
兩處改動：`LLMError` → `AgentError`；模型 ID 與 region 的預設值改讀本 repo 的
`smart_watchdog.bedrock`，那裡是**唯一**定義處（`DEFAULT_MODEL`／`region()`）。

翻譯與事件解析拆成模組層級函式，因為它們是這個檔案裡唯一能在沒有 AWS 憑證時
被驗證的部分——「介面對不對得上」必須離線就能證明。

2026-09-12 已在競賽帳號（550561128629 / us-west-2）實測通過：
`converse_stream` + `toolConfig` 回 `stopReason: tool_use`，並正確吐出講解句
與一個 `list_institutions{"town":"板橋區","limit":10}` 呼叫。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any, Optional

from .protocol import (
    AgentBackend,
    AgentError,
    AgentEvent,
    TextDelta,
    ToolCall,
    ToolUse,
    TurnEnd,
)


def to_bedrock_messages(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        blocks: list[dict] = []
        for b in m["content"]:
            kind = b.get("type")
            if kind == "text":
                blocks.append({"text": b["text"]})
            elif kind == "tool_use":
                blocks.append(
                    {"toolUse": {"toolUseId": b["id"], "name": b["name"], "input": b["input"]}}
                )
            elif kind == "tool_result":
                blocks.append(
                    {
                        "toolResult": {
                            "toolUseId": b["tool_use_id"],
                            "content": [{"text": b["content"]}],
                        }
                    }
                )
        out.append({"role": m["role"], "content": blocks})
    return out


def to_tool_config(tools: list[dict]) -> dict:
    return {
        "tools": [
            {
                "toolSpec": {
                    "name": t["name"],
                    "description": t["description"],
                    "inputSchema": {"json": t["input_schema"]},
                }
            }
            for t in tools
        ]
    }


def parse_stream(raw: Iterable[dict]) -> Iterator[AgentEvent]:
    """把 Converse 串流事件翻成我們的事件。

    tool 的輸入是**分段**送來的 JSON 片段，要到 `contentBlockStop` 才拼得回來——
    在 delta 就想 json.loads 會拿到半截字串。
    """
    pending_id: Optional[str] = None
    pending_name: Optional[str] = None
    buf = ""
    for ev in raw:
        if "contentBlockStart" in ev:
            start = ev["contentBlockStart"].get("start", {}).get("toolUse")
            if start:
                pending_id = start["toolUseId"]
                pending_name = start["name"]
                buf = ""
        elif "contentBlockDelta" in ev:
            delta = ev["contentBlockDelta"]["delta"]
            if "text" in delta:
                yield TextDelta(delta["text"])
            elif "toolUse" in delta:
                buf += delta["toolUse"].get("input", "")
        elif "contentBlockStop" in ev and pending_id is not None:
            try:
                args = json.loads(buf) if buf.strip() else {}
            except json.JSONDecodeError as exc:
                raise AgentError(f"Bedrock 的 tool 輸入不是 JSON：{buf[:200]}") from exc
            yield ToolUse(ToolCall(id=pending_id, name=pending_name or "", arguments=args))
            pending_id = pending_name = None
            buf = ""
        elif "messageStop" in ev:
            yield TurnEnd(ev["messageStop"].get("stopReason", "end_turn"))


class BedrockAgentBackend(AgentBackend):
    name = "bedrock"

    def __init__(
        self,
        model_id: Optional[str] = None,
        region: Optional[str] = None,
        # 1024 不夠：一個回合可能載入 SOP、逐區取排序、再逐筆說明理由，
        # 實測「幫我排這週的稽查」在 1024 下會以 stop_reason=max_tokens 中斷，
        # 而且是斷在最後一個 tool 呼叫的參數上（參數缺一半，驗證直接擋下）。
        max_tokens: int = 4096,
    ) -> None:
        from .. import bedrock as _bedrock

        self.model_id = model_id or _bedrock.DEFAULT_MODEL
        self.region = region or _bedrock.region()
        self.max_tokens = max_tokens
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("bedrock-runtime", region_name=self.region)
        return self._client

    def stream(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
        timeout_s: int = 60,  # noqa: ARG002 - Bedrock 的逾時由 botocore 設定控制
    ) -> Iterator[AgentEvent]:
        try:
            resp = self._get_client().converse_stream(
                modelId=self.model_id,
                system=[{"text": system}],
                messages=to_bedrock_messages(messages),
                toolConfig=to_tool_config(tools),
                # temperature=0：同一個問句應該走同一條操作路徑，示範時才可複現。
                inferenceConfig={"maxTokens": self.max_tokens, "temperature": 0},
            )
        except Exception as exc:
            raise AgentError(f"Bedrock converse_stream 失敗：{exc}") from exc
        yield from parse_stream(resp["stream"])
