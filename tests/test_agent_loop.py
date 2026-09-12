"""agent 迴圈的契約，離線測。

`AgentBackend` 是抽象的，所以整個迴圈可以用腳本化的假後端驗完——**不需要 AWS
憑證，也不會送出任何 Bedrock 請求**。真後端的翻譯層（`backend.to_bedrock_messages`
等）另外單獨測，那些也是純函式。

這裡測的是四件會壞掉但不容易發現的事：
1. 事件順序與 `step_id` 的綁定（前端靠它決定「先打字、再動畫面」）
2. 稽核列由 `registry.execute()` 寫，迴圈不重複寫
3. 禁用詞被替換掉，且替換有被記錄
4. 未註冊的 tool 被白名單擋下，且擋下來不會讓整輪崩掉
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("pydantic")

from pydantic import BaseModel

from smart_watchdog.agent.loop import run_turn
from smart_watchdog.agent.protocol import (
    AgentBackend,
    TextDelta,
    ToolCall,
    ToolUse,
    TurnEnd,
)
from smart_watchdog.agent.registry import (
    ToolContext,
    ToolOutcome,
    ToolRegistry,
    ToolSpec,
)
from smart_watchdog.db import session as dbsession
from smart_watchdog.db.models import AgentMessage, AgentSession, User

SESSION_ID = "11111111-2222-3333-4444-555555555555"


class NoArgs(BaseModel):
    pass


def _ok(_ctx: ToolContext, _a: NoArgs) -> ToolOutcome:
    return ToolOutcome(
        payload={"count": 3, "items": [1, 2, 3]},
        ui_action={"type": "navigate", "tab": "list"},
    )


class ScriptedBackend(AgentBackend):
    """照腳本吐事件。每次 `stream()` 消費腳本的下一段。"""

    name = "scripted"

    def __init__(self, script: list[list]) -> None:
        self.script = script
        self.calls = 0

    def stream(self, *, system, messages, tools, timeout_s=60):  # noqa: ARG002
        events = (self.script[self.calls] if self.calls < len(self.script)
                  else [TurnEnd("end_turn")])
        self.calls += 1
        yield from events


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(dbsession, "_engine", None, raising=False)
    monkeypatch.setattr(dbsession, "_Session", None, raising=False)
    monkeypatch.setattr(
        dbsession, "url", lambda: f"sqlite+pysqlite:///{tmp_path / 'agent.sqlite'}"
    )
    dbsession.init_db()
    s = dbsession.session()
    s.add(User(
        email="a@b.c", name="測試員", role="inspector", towns=["板橋區"],
        password_hash="x", is_active=True, agent_memory={},
    ))
    s.commit()
    user = s.query(User).one()
    s.add(AgentSession(id=SESSION_ID, user_id=user.id))
    s.commit()
    yield s, user
    s.close()
    monkeypatch.setattr(dbsession, "_engine", None, raising=False)
    monkeypatch.setattr(dbsession, "_Session", None, raising=False)


@pytest.fixture
def registry():
    reg = ToolRegistry()
    reg.register(ToolSpec(
        name="do_thing", description="做一件事", params=NoArgs, handler=_ok,
    ))
    return reg


def _run(db, registry, script, text="做一下"):
    s, user = db
    return list(run_turn(
        db=s, user=user, session_id=SESSION_ID, turn=1, text=text, view={},
        backend=ScriptedBackend(script), registry=registry,
    ))


def test_one_tool_step_emits_events_in_the_order_the_frontend_needs(db, registry) -> None:
    events = _run(db, registry, [
        [TextDelta("我先查一下。"), ToolUse(ToolCall("c1", "do_thing", {})),
         TurnEnd("tool_use")],
        [TextDelta("查到三筆。"), TurnEnd("end_turn")],
    ])
    assert [e.event for e in events] == [
        "session", "text", "tool_call", "tool_result", "ui_action", "text", "done",
    ]
    # 同一步的四個事件必須共用 step_id，否則前端無法把講解句與動作配對。
    step1 = [e for e in events if e.data.get("step_id") == 1]
    assert {e.event for e in step1} == {"text", "tool_call", "tool_result", "ui_action"}


def test_audit_is_written_once_per_tool_call_not_twice(db, registry) -> None:
    s, _ = db
    _run(db, registry, [
        [TextDelta("我先查一下。"), ToolUse(ToolCall("c1", "do_thing", {})),
         TurnEnd("tool_use")],
        [TextDelta("查到三筆。"), TurnEnd("end_turn")],
    ])
    kinds = [r.kind for r in s.query(AgentMessage).order_by(AgentMessage.id).all()]
    # user 問句、第一步講解句、tool_call、tool_result、第二步講解句
    assert kinds == ["text", "text", "tool_call", "tool_result", "text"]
    assert kinds.count("tool_call") == 1, "迴圈與 registry 各寫一次就會變成 2"


def test_verdict_language_is_replaced_and_the_hit_is_recorded(db, registry) -> None:
    events = _run(db, registry, [[TextDelta("這間園違法。"), TurnEnd("end_turn")]])
    said = next(e for e in events if e.event == "text")
    assert "違法" not in said.data["text"]
    assert said.data["softened"] == ["違法"], "替換要留紀錄，那是一個該被看見的指標"


def test_unregistered_tool_is_denied_without_killing_the_turn(db, registry) -> None:
    events = _run(db, registry, [
        [ToolUse(ToolCall("c1", "delete_everything", {})), TurnEnd("tool_use")],
        [TextDelta("我做不到。"), TurnEnd("end_turn")],
    ])
    result = next(e for e in events if e.event == "tool_result")
    assert result.data["ok"] is False
    assert "未註冊" in result.data["summary"]
    assert events[-1].event == "done", "被擋下來之後這一輪仍要正常收尾"


def test_max_steps_stops_the_loop(db, registry) -> None:
    """模型每步都要求 tool 時，迴圈必須自己停，不能無限打下去。"""
    forever = [[ToolUse(ToolCall(f"c{i}", "do_thing", {})), TurnEnd("tool_use")]
               for i in range(20)]
    events = _run(db, registry, forever)
    assert events[-1].data["stop_reason"] == "max_steps"
    assert events[-1].data["steps"] == 8
