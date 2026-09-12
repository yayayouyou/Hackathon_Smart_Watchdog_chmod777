"""agent 迴圈的契約，離線測。

`AgentBackend` 是抽象的，所以整個迴圈可以用腳本化的假後端驗完——**不需要 AWS
憑證，也不會送出任何 Bedrock 請求**。真後端的翻譯層（`backend.to_bedrock_messages`
等）另外單獨測，那些也是純函式。

這裡測的是四件會壞掉但不容易發現的事：
1. 事件順序與 `step_id` 的綁定（前端靠它決定「先打字、再動畫面」）
2. 稽核列由 `registry.execute()` 寫，迴圈不重複寫
3. 禁用詞被替換掉，且替換有被記錄
4. 未註冊的 tool 被白名單擋下，且擋下來不會讓整輪崩掉
5. 一輪不管怎麼收尾，還原出來的歷史都接得上下一輪（見 `_replayable`）
6. tool 自己回的錯誤不會在畫面上顯示成成功
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
from smart_watchdog.agent.memory import load_history
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


def test_sse_frames_use_crlf_which_the_frontend_must_normalise() -> None:
    """釘住前端依賴的線上格式。

    sse-starlette 送的是 CRLF：`event: …\\r\\ndata: …\\r\\n\\r\\n`。
    而 `\\r\\n\\r\\n` 裡**沒有**相鄰的 `\\n\\n`——`webapp/agent.js` 若直接找
    `"\\n\\n"` 分幀就永遠切不出東西，畫面上的表徵是「送出後完全沒反應」，
    而後端一切正常（curl 看得到完整事件）。這個 bug 真的發生過。

    所以 agent.js 每次都先把緩衝區 `replace(/\\r\\n/g, "\\n")` 再切。
    這條測試是在說：**哪天這個格式變了，前端那行正規化要跟著檢查。**
    """
    sse = pytest.importorskip("sse_starlette.sse")
    raw = sse.ServerSentEvent(data='{"a":1}', event="text").encode()
    assert raw.endswith(b"\r\n\r\n")
    assert b"\n\n" not in raw, "若哪天變成 LF，agent.js 的正規化就不再是必要的"


# ── 還原出來的歷史必須接得上下一輪 ──────────────────────────────────


def _replayable(s) -> list[dict]:
    """還原歷史，並檢查它接得上下一則使用者訊息。

    `run_turn` 下一輪做的第一件事就是 `load_history()` 之後 append 一則 user
    訊息，所以這串必須以 assistant 結尾，而且不能有懸空的 tool_use。任一條
    不成立，Converse 就會整串拒收——症狀是這個 session **之後每一輪都失敗**，
    使用者只能重新整理頁面。
    """
    messages = load_history(s, SESSION_ID)
    for i, m in enumerate(messages):
        if any(b["type"] == "tool_use" for b in m["content"]):
            nxt = messages[i + 1] if i + 1 < len(messages) else None
            assert nxt and any(b["type"] == "tool_result" for b in nxt["content"]), (
                f"第 {i} 則帶著沒有對應 tool_result 的 tool_use"
            )
    roles = [m["role"] for m in messages] + ["user"]
    assert all(a != b for a, b in zip(roles, roles[1:])), f"角色沒有交替：{roles}"
    return messages


def _boom(_ctx: ToolContext, _a: NoArgs) -> ToolOutcome:
    raise RuntimeError("payload 讀不到／資料庫鎖住／handler 自己的 bug")


def test_a_crashing_tool_does_not_poison_the_session(db) -> None:
    """handler 丟例外之後，這個 session 還要能繼續用。

    `execute()` 先寫 tool_call 稽核才跑 handler，所以 handler 炸掉時曾經只留下
    tool_call 那一列，軌跡上是「叫了但沒有下文」，而下一輪還原出來是一個沒有
    對應 tool_result 的 tool_use。
    """
    reg = ToolRegistry()
    reg.register(ToolSpec(
        name="do_thing", description="做一件事", params=NoArgs, handler=_boom,
    ))
    s, user = db
    with pytest.raises(RuntimeError):
        list(run_turn(
            db=s, user=user, session_id=SESSION_ID, turn=1, text="做一下", view={},
            backend=ScriptedBackend([[
                TextDelta("我先查一下。"),
                ToolUse(ToolCall("c1", "do_thing", {})),
                TurnEnd("tool_use"),
            ]]),
            registry=reg,
        ))
    kinds = [r.kind for r in s.query(AgentMessage).order_by(AgentMessage.id).all()]
    assert kinds == ["text", "text", "tool_call", "tool_result"], (
        "失敗的 tool 也要留下 tool_result，否則稽核軌跡缺一半"
    )
    _replayable(s)


@pytest.mark.parametrize(
    "script",
    [
        # 不帶講解句：8 步 × 2 列 + 使用者那一句 = 17 列，還在 HISTORY_LIMIT
        # 的視窗內。每步都配一句講解就是 25 列，使用者那一句會被切到視窗外，
        # `load_history` 找不到安全起點而回傳空清單——那條路徑測不到這裡要測的事。
        pytest.param(
            [[ToolUse(ToolCall(f"c{i}", "do_thing", {})), TurnEnd("tool_use")]
             for i in range(20)],
            id="撞到-MAX_STEPS",
        ),
        pytest.param(
            [[TextDelta("我先查一下。"), ToolUse(ToolCall("c1", "do_thing", {})),
              TurnEnd("tool_use")],
             [TurnEnd("end_turn")]],
            id="最後一步沒吐講解句",
        ),
    ],
)
def test_history_always_ends_where_the_next_turn_can_continue(db, registry, script) -> None:
    """這兩種收尾都**不是錯誤**，卻一樣會讓下一輪的訊息串不合法。

    兩者的最後一列都是 tool_result，而 tool_result 的 role 是 user——下一輪在
    後面接上使用者訊息就成了連續兩則 user，角色沒有交替。這條路徑不必出任何
    差錯就會走到。
    """
    _run(db, registry, script)
    _replayable(db[0])


def test_a_dead_backend_on_the_first_step_does_not_poison_the_session(db, registry) -> None:
    """Bedrock 在第一步就丟例外（限流、憑證過期）時，資料庫只留下使用者那一句。

    留著它，下一輪接上新的使用者訊息就是連續兩則 user。寧可整串不還原。
    """

    class DeadBackend(AgentBackend):
        name = "dead"

        def stream(self, *, system, messages, tools, timeout_s=60):  # noqa: ARG002
            raise RuntimeError("Bedrock throttled")
            yield  # pragma: no cover - 讓它是 generator

    s, user = db
    with pytest.raises(RuntimeError):
        list(run_turn(
            db=s, user=user, session_id=SESSION_ID, turn=1, text="做一下", view={},
            backend=DeadBackend(), registry=registry,
        ))
    assert _replayable(s) == [], "只剩一句使用者訊息時，寧可少還原也不要送出不合法的串"


# ── tool 自己回的錯誤 ────────────────────────────────────────────────


def _not_found(_ctx: ToolContext, _a: NoArgs) -> ToolOutcome:
    return ToolOutcome(payload={
        "error": "查無機構 deadbeef",
        "note": "機構 id 是 8 碼十六進位，可先用 list_institutions 取得。",
    })


def test_a_tool_that_returns_an_error_is_not_shown_as_success(db) -> None:
    """tool 自己回 error 時，步驟軌道不可以顯示成綠色打勾。

    這類回傳（查無機構、類別名稱不合法、export_schedule 少給參數）不是例外，
    所以曾經一律送 `ok: True`，軌道上顯示「✓ 完成」，而模型下一句講的是
    「查無這筆」——畫面與說法互相矛盾。
    """
    reg = ToolRegistry()
    reg.register(ToolSpec(
        name="do_thing", description="做一件事", params=NoArgs, handler=_not_found,
    ))
    events = _run(db, reg, [
        [ToolUse(ToolCall("c1", "do_thing", {})), TurnEnd("tool_use")],
        [TextDelta("查無這筆。"), TurnEnd("end_turn")],
    ])
    result = next(e for e in events if e.event == "tool_result")
    assert result.data["ok"] is False
    assert result.data["summary"] == "查無機構 deadbeef", "摘要要講出錯在哪，不是「完成」"


def test_bedrock_client_is_built_with_a_real_timeout() -> None:
    """逾時必須真的生效，不是只在簽名裡出現。

    botocore 的預設是 60 秒讀取逾時加上預設重試——實測一次沒回應的呼叫會卡
    約三分鐘，而畫面上只停在「整理中」，使用者看到的是系統當掉。
    先前 `stream()` 收下 timeout_s 卻標成 noqa 直接忽略，註解寫「由 botocore
    設定控制」，但建 client 時根本沒設 Config。
    """
    pytest.importorskip("boto3")
    from smart_watchdog.agent.backend import BedrockAgentBackend

    b = BedrockAgentBackend(timeout_s=7, region="us-west-2")
    cfg = b._get_client().meta.config
    assert cfg.read_timeout == 7, "讀取逾時沒有套用"
    assert cfg.connect_timeout <= 15
    # 串流重試沒有意義：前面吐過的字已經送出去了，而多次重試正是卡住的來源。
    # botocore 會把 max_attempts 正規化成 total_max_attempts（＝重試次數 + 1）。
    assert cfg.retries.get("total_max_attempts", 99) <= 2


def test_the_loop_and_the_backend_agree_on_the_timeout() -> None:
    """兩邊各寫一份就會出現「迴圈以為會中止、實際上還在等」。"""
    pytest.importorskip("boto3")
    from smart_watchdog.agent.loop import STEP_TIMEOUT_S
    from smart_watchdog.api.agent import _backend

    assert _backend().timeout_s == STEP_TIMEOUT_S
