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
        # ⚠️ 這個值是**真的**會生效的逾時，不是裝飾。botocore 的預設是 60 秒
        # 讀取逾時加上預設重試，實測會讓一次沒回應的呼叫卡住約三分鐘，而畫面
        # 上只會停在「整理中」——使用者看到的是系統當掉。
        timeout_s: int = 45,
        # 1024 不夠：一個回合可能載入 SOP、逐區取排序、再逐筆說明理由，
        # 實測「幫我排這週的稽查」在 1024 下會以 stop_reason=max_tokens 中斷，
        # 而且是斷在最後一個 tool 呼叫的參數上（參數缺一半，驗證直接擋下）。
        max_tokens: int = 4096,
    ) -> None:
        from .. import bedrock as _bedrock

        self.model_id = model_id or _bedrock.DEFAULT_MODEL
        self.region = region or _bedrock.region()
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self._client: Any = None

    def _get_client(self) -> Any:
        """建 client 時就把逾時與重試釘死。

        逾時只能在建 client 時設，不能逐次呼叫指定——所以 `stream()` 的
        `timeout_s` 參數只是介面相容，真正生效的是這裡。
        重試設 1：串流回應重試沒有意義（前面吐過的字已經送出去了），
        而預設的多次重試正是「卡三分鐘」的來源。
        """
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "bedrock-runtime", region_name=self.region,
                config=Config(
                    read_timeout=self.timeout_s, connect_timeout=10,
                    # total_max_attempts=1 就是「不重試」。串流重試沒有意義：
                    # 前面吐出去的字已經在畫面上了，重來會變成重複講一遍。
                    retries={"total_max_attempts": 1, "mode": "standard"},
                ),
            )
        return self._client

    def stream(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
        # 介面相容用。真正生效的是建 client 時的 Config，見 `_get_client()`。
        timeout_s: int = 60,  # noqa: ARG002
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
            # 逾時要講成人看得懂的話——「ReadTimeoutError」對使用者沒有意義，
            # 而這是最可能在示範中途發生的一種失敗。
            if "ReadTimeout" in type(exc).__name__ or "timeout" in str(exc).lower():
                raise AgentError(
                    f"模型超過 {self.timeout_s} 秒沒有回應，這一步中止了。請再說一次。"
                ) from exc
            # 憑證問題要講成「照著做就能修」的一句話。黑客松發的是臨時憑證，
            # 幾小時就過期，而 boto3 原文（ExpiredTokenException … reached max
            # retries: 0）會被原樣貼到助理的對話框裡——台上看到那一串，沒有人
            # 知道該做什麼。這是示範中途第二可能發生的失敗，僅次於逾時。
            if "ExpiredToken" in str(exc):
                raise AgentError(
                    "AWS 憑證已過期。請更新 .env 裡的 AWS_ACCESS_KEY_ID／"
                    "AWS_SECRET_ACCESS_KEY／AWS_SESSION_TOKEN，然後重啟伺服器。"
                ) from exc
            if "UnrecognizedClient" in str(exc) or "InvalidClientTokenId" in str(exc):
                raise AgentError(
                    "AWS 憑證無效——可能已被撤銷，或貼上時漏掉了一段。"
                    "請重新取得一組完整的憑證寫進 .env，然後重啟伺服器。"
                ) from exc
            raise AgentError(f"Bedrock converse_stream 失敗：{exc}") from exc
        yield from parse_stream(resp["stream"])
