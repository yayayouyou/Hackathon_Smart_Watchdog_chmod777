"""agent 後端契約：串流 + tool use 迴圈。

搬自 `Eason20050201/hackathon@a0bdada` 的 `src/smart_watchdog/llm/agent.py`，
原樣保留，只把 `LLMError` 換成本檔的 `AgentError`（本 repo 沒有 `llm/base.py`）。

與既有三個落點（抽取、建議書、查詢）的呼叫方式**刻意不共用**：那三個是一次性
補完，永遠不需要 tool use；把 tool use 加進它們的契約只會逼它們實作用不到的方法。
那三個走 `anthropic` 的 `AnthropicBedrock`，這裡走 boto3 的 `converse_stream`。

訊息用 Anthropic 的形狀當正規化格式，Bedrock 後端負責翻譯。
"""

from __future__ import annotations

import abc
import dataclasses
from collections.abc import Iterator
from typing import Optional, Union


class AgentError(RuntimeError):
    """agent 後端的傳輸或解析失敗。"""


@dataclasses.dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclasses.dataclass(frozen=True)
class TextDelta:
    """一小段講解文字。前端收到就打字。"""

    text: str


@dataclasses.dataclass(frozen=True)
class ToolUse:
    call: ToolCall


@dataclasses.dataclass(frozen=True)
class ToolDone:
    """一個 tool 已經在後端之外執行完畢，這是結果——不是「請執行這個」。

    給走 MCP 的後端用：tool 在 MCP server 內已經呼叫過 `registry.execute()`
    （稽核也寫了），迴圈收到這個事件**不能再呼叫一次**，否則稽核列與副作用
    都會重複一次。
    """

    call: ToolCall
    payload: dict
    ui_action: Optional[dict]


@dataclasses.dataclass(frozen=True)
class TurnEnd:
    stop_reason: str  # tool_use | end_turn | max_tokens


AgentEvent = Union[TextDelta, ToolUse, ToolDone, TurnEnd]


class AgentBackend(abc.ABC):
    name: str

    @abc.abstractmethod
    def stream(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
        timeout_s: int = 60,
    ) -> Iterator[AgentEvent]:
        """跑一步：吐出文字增量與 tool 呼叫，最後一定吐 TurnEnd。"""
