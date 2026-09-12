"""12 個 tool 與 MCP 的契約。離線測，不打 Bedrock。

測的重點不是「函式會回東西」，而是**四條界線有沒有守住**：
1. 白名單：未註冊的 tool 叫不到，MCP 這條路徑也一樣。
2. 身分：沒登入就叫不動任何 tool——擋在 registry，不是擋在 MCP。
3. 用詞：每個列表回傳都要帶「非違法認定」那句界線。
4. 三態：法遵檢核的空值是待判讀，不可被當成通過或未通過。

另外測一件實際踩過的事：`export_schedule` 必須能只用 `towns` 指定，
不能逼模型把 20 筆 id 抄回來（實測那會讓回合以 max_tokens 中斷）。
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("pandas")

from smart_watchdog.agent import mcp_server
from smart_watchdog.agent.registry import ToolContext, ToolDenied, ToolInvalid
from smart_watchdog.agent.tools import SKILL_NAMES, build_registry
from smart_watchdog.db import session as dbsession
from smart_watchdog.db.models import AuditFeedback, User

ROOT = pathlib.Path(__file__).resolve().parents[1]

# payload 是建前端時產生的；沒有它就沒有機構資料可查。
pytestmark = pytest.mark.skipif(
    not (ROOT / "dist/data/payload.json").exists(),
    reason="需要 dist/data/payload.json，先跑 python run.py frontend",
)


@pytest.fixture(scope="module")
def reg():
    return build_registry()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(dbsession, "_engine", None, raising=False)
    monkeypatch.setattr(dbsession, "_Session", None, raising=False)
    monkeypatch.setattr(
        dbsession, "url", lambda: f"sqlite+pysqlite:///{tmp_path / 't.sqlite'}"
    )
    dbsession.init_db()
    s = dbsession.session()
    s.add(User(email="t@x.gov", name="測試員", role="inspector", towns=["板橋區"],
               password_hash="x", is_active=True, agent_memory={}))
    s.commit()
    yield s
    s.close()
    monkeypatch.setattr(dbsession, "_engine", None, raising=False)
    monkeypatch.setattr(dbsession, "_Session", None, raising=False)


def _ctx(db=None, user=None):
    # db 為 None 時 registry 不寫稽核，正好用來測純讀取的 tool。
    return ToolContext(db=db, user=user or object(), view={}, session_id="s1")


def _run(reg, name, args, ctx=None):
    return reg.execute(ctx or _ctx(), name, args)


# ── 白名單與身分 ──────────────────────────────────────────────────────


def test_all_twelve_tools_are_registered(reg) -> None:
    assert len(reg.names()) == 12
    assert "list_institutions" in reg.names()
    assert "search_documents" in reg.names(), "本 repo 獨有的證據檢索 tool"


def test_unregistered_tool_is_denied(reg) -> None:
    with pytest.raises(ToolDenied):
        _run(reg, "delete_everything", {})


def test_no_tool_can_be_used_without_a_user(reg) -> None:
    """身分檢查在 registry，所以 SSE 與 MCP 兩條路徑不可能有不同的權限。"""
    anonymous = ToolContext(db=None, user=None, view={}, session_id="")
    for name in reg.names():
        with pytest.raises(ToolDenied):
            reg.execute(anonymous, name, {})


def test_bad_arguments_are_rejected_before_the_handler_runs(reg) -> None:
    with pytest.raises(ToolInvalid):
        _run(reg, "list_institutions", {"limit": 9999})


# ── 用詞界線 ─────────────────────────────────────────────────────────


def test_list_carries_the_caveat_on_every_response(reg) -> None:
    out = _run(reg, "list_institutions", {"town": "板橋區", "limit": 3})
    # 檢查語意而非精確措辭：措辭會被潤稿，但這兩件事不能消失。
    note = out.payload["note"]
    assert "違法認定" in note, "每一次回覆都要說這不是違法認定"
    assert "資料不足" in note, "查不到財報要說資料不足，不能讓人讀成低風險"
    assert out.ui_action["type"] == "set_filters"


def test_unknown_type_returns_a_readable_error_not_a_silent_full_scan(reg) -> None:
    out = _run(reg, "list_institutions", {"type": "公私立"})
    assert "未知的機構類別" in out.payload["error"]


def test_institution_without_a_report_says_insufficient_data_not_low_risk(reg) -> None:
    """全市多數園沒有公開財報。那是涵蓋範圍限制，不是合規證明。"""
    listed = _run(reg, "list_institutions", {"limit": 50}).payload["items"]
    target = next((r for r in listed if not r["has_financial_report"]), None)
    if target is None:
        pytest.skip("這批前 50 名都有財報")
    out = _run(reg, "get_findings", {"institution_id": target["id"]})
    assert "資料不足" in out.payload["note"]
    assert "低風險" in out.payload["note"]


def test_findings_explain_that_blank_status_means_pending(reg) -> None:
    listed = _run(reg, "list_institutions",
                  {"has_compliance_failure": True, "limit": 5}).payload["items"]
    if not listed:
        pytest.skip("沒有法遵未通過的登記")
    out = _run(reg, "get_findings", {"institution_id": listed[0]["id"]})
    assert out.payload["count"] > 0
    assert "待人工判讀" in out.payload["note"]


# ── 各 tool 能跑 ──────────────────────────────────────────────────────


def test_ranking_returns_tier_and_navigates(reg) -> None:
    out = _run(reg, "get_ranking", {"n": 5})
    assert out.payload["count"] == 5
    assert all(r["tier"] for r in out.payload["items"])
    assert out.ui_action["tab"] == "list"


def test_open_institution_and_penalties_agree_on_the_count(reg) -> None:
    listed = _run(reg, "list_institutions", {"limit": 20}).payload["items"]
    target = next(r for r in listed if r["penalties"] > 0)
    dossier = _run(reg, "open_institution", {"institution_id": target["id"]}).payload
    pens = _run(reg, "get_penalties", {"institution_id": target["id"]}).payload
    assert dossier["penalties"] == pens["count"], "卷宗的計數與明細筆數必須一致"
    assert "不可合併計數" in pens["note"]


def test_unknown_institution_id_is_a_message_not_a_crash(reg) -> None:
    out = _run(reg, "open_institution", {"institution_id": "deadbeef"})
    assert "查無機構" in out.payload["error"]


def test_model_card_reports_every_snapshot_including_the_bad_ones(reg) -> None:
    out = _run(reg, "get_model_card", {})
    assert out.payload["count"] >= 3
    assert any(r["auc"] is not None for r in out.payload["items"])
    assert "時序切分" in out.payload["note"]


def test_time_machine_lists_available_dates_when_asked_for_a_missing_one(reg) -> None:
    out = _run(reg, "set_time_machine", {"as_of": "1999-01-01"})
    assert out.payload["available"], "查錯時點要告訴模型有哪些時點可用"


def test_search_documents_returns_pages_that_can_be_cited(reg) -> None:
    out = _run(reg, "search_documents", {"q": "資遣費準備金", "limit": 3})
    assert out.payload["found"] > 0
    cited = out.payload["passages"] + out.payload["facts"]
    assert cited, "沒有可引述的段落或欄位就失去這個 tool 的意義"
    assert any("citation" in c for c in cited)


def test_every_skill_named_in_the_prompt_actually_loads(reg) -> None:
    for name in SKILL_NAMES:
        out = _run(reg, "load_skill", {"name": name})
        assert len(out.payload["content"]) > 200, f"{name} 內容太短，可能是空檔"


def test_load_skill_rejects_an_unknown_name(reg) -> None:
    out = _run(reg, "load_skill", {"name": "../../etc/passwd"})
    assert "沒有這份指引" in out.payload["error"]


# ── 實際踩過的事 ─────────────────────────────────────────────────────


def test_export_schedule_works_from_towns_without_echoing_ids(reg) -> None:
    """模型不必把 20 筆 id 抄回來——實測那會讓回合以 max_tokens 中斷。"""
    out = _run(reg, "export_schedule", {"towns": ["板橋區"], "n": 10})
    assert out.payload["rows"] == 10
    assert out.ui_action["type"] == "download"
    assert out.ui_action["content"].startswith("交付順序")


def test_export_schedule_with_neither_argument_says_what_it_needs(reg) -> None:
    out = _run(reg, "export_schedule", {})
    assert "institution_ids" in out.payload["error"]


def test_export_schedule_deduplicates_across_towns(reg) -> None:
    once = _run(reg, "export_schedule", {"towns": ["板橋區"], "n": 5}).payload["rows"]
    twice = _run(reg, "export_schedule",
                 {"towns": ["板橋區", "板橋區"], "n": 5}).payload["rows"]
    assert once == twice, "同一區給兩次不該讓排程出現重複列"


def test_record_feedback_appends_a_row_attributed_to_the_user(reg, db) -> None:
    user = db.query(User).one()
    listed = _run(reg, "list_institutions", {"limit": 1}).payload["items"]
    from smart_watchdog.db.models import AgentSession

    db.add(AgentSession(id="s1", user_id=user.id))
    db.commit()
    ctx = ToolContext(db=db, user=user, view={}, session_id="s1")
    out = reg.execute(ctx, "record_feedback", {
        "institution_id": listed[0]["id"], "item": "業務發展準備提列上限 20%",
        "agrees": False, "note": "現場已補件",
    })
    assert out.payload["recorded"] is True
    assert out.payload["by"] == "測試員"
    assert db.query(AuditFeedback).count() == 1


# ── MCP 是同一組 tool、同一套規則 ────────────────────────────────────


def test_mcp_exposes_exactly_the_registry(reg) -> None:
    """兩邊各維護一份清單的那天，就是白名單失效的那天。"""
    assert [t["name"] for t in mcp_server.list_mcp_tools()] == reg.names()


def test_mcp_without_a_token_is_denied_by_the_registry_not_by_mcp() -> None:
    out = mcp_server.dispatch("list_institutions", {"town": "板橋區"}, None)
    assert "未登入" in out["payload"]["note"]


def test_mcp_rejects_an_unregistered_tool() -> None:
    out = mcp_server.dispatch("drop_database", {}, "whatever")
    assert "未註冊" in out["payload"]["note"]
