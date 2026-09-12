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


def _with_penalties(reg) -> str:
    """挑一所有裁罰紀錄的機構。沒有就跳過——測試不該依賴特定某一所。"""
    for r in _run(reg, "list_institutions", {"limit": 50}).payload["items"]:
        if r["penalties"] > 2:
            return r["id"]
    pytest.skip("這批前 50 名沒有裁罰超過 2 件的機構")


# ── 白名單與身分 ──────────────────────────────────────────────────────


def test_every_screen_feature_has_a_tool(reg) -> None:
    """agent 要能操作網站上的每一件事（發動掃描除外，那會花錢）。

    少一個 tool 的表徵不是報錯，是 agent 說「我做不到」——而使用者會以為
    是模型不夠聰明，不會想到是我們沒給它工具。
    """
    names = set(reg.names())
    assert len(names) == 19
    for expected in (
        "list_institutions", "get_ranking",          # 派工提案
        "open_institution", "get_penalties", "get_findings",
        "get_staffing", "get_rank_track", "get_realtime",  # 卷宗各段
        "open_memo", "list_memos",                   # 建議書
        "set_time_machine", "get_model_card",        # 時間軸
        "search_documents",                          # 證據
        "set_map_view",                              # 地圖控制項
        "get_peer_comparison",                       # 同儕財務比較
        "scan_estimate",                             # 掃描（只估算）
        "export_schedule", "record_feedback", "load_skill",
    ):
        assert expected in names, f"少了 {expected}"


def test_scanning_can_be_priced_but_not_launched(reg) -> None:
    """讓對話能直接發動掃描，等於讓 agent 自己花錢。刻意只給估算。"""
    names = set(reg.names())
    assert "scan_estimate" in names
    for forbidden in ("scan_start", "scan", "run_scan", "scan_adopt"):
        assert forbidden not in names, f"{forbidden} 會讓 agent 花錢"


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


# ── 證據頁 ───────────────────────────────────────────────────────────


def test_search_results_link_to_a_page_image_when_the_pdf_is_present(reg) -> None:
    """有 data/raw 才掛 image_url——給一個必定 404 的連結比不給更糟。"""
    out = _run(reg, "search_documents", {"q": "資遣費準備金", "limit": 3})
    rows = out.payload["facts"] + out.payload["passages"]
    has_pdf = any((ROOT / r["path"]).exists() for r in rows if r.get("path"))
    if not has_pdf:
        pytest.skip("這台機器沒有 data/raw")
    linked = [r for r in rows if r.get("image_url")]
    assert linked, "有原始 PDF 卻沒掛上證據頁網址"
    # 路徑含中文與斜線，一定要編碼過：未編碼會被 uvicorn 以 400 擋掉。
    assert all("%2F" in r["image_url"] for r in linked)


def test_mcp_lifespan_is_wired_into_the_app() -> None:
    """MCP 的 session manager 靠自己的 lifespan 初始化 task group。

    只 `app.mount("/mcp", …)` 而不把它的 lifespan 接進父 app 的話：掛載會成功、
    `list_mcp_tools()` 也列得出 12 個，但**協定層的 initialize 會 500**
    （`Task group is not initialized`）——也就是「函式測得過、真的用 MCP 客戶端
    連就連不上」。這個組合實測踩過，所以這裡直接檢查接線本身。
    """
    pytest.importorskip("fastmcp")
    from smart_watchdog.api import server

    assert server._mcp_app is not None, "MCP 沒建起來，看啟動時的『MCP 未掛載』訊息"
    assert "/mcp" in [getattr(r, "path", "") for r in server.app.routes]
    # 自訂 lifespan 一旦傳進 FastAPI，on_event 的處理器就不再跑，所以啟動工作
    # （載 payload、bind 掃描、重啟對帳）必須在同一支 lifespan 裡。
    assert server.app.router.lifespan_context is not None
    assert server.app.router.on_startup == [], "on_event 與自訂 lifespan 不能並存"


# ── 新增的六個 tool ──────────────────────────────────────────────────


def test_rank_track_is_per_institution_not_city_wide(reg) -> None:
    """`set_time_machine` 回某一格的前 N 名，這個回某一所的逐格名次。兩者不同。"""
    iid = _with_penalties(reg)
    out = _run(reg, "get_rank_track", {"institution_id": iid})
    assert out.payload["count"] >= 3
    assert all("as_of" in p and "rank" in p for p in out.payload["points"])
    assert "尚未結束" in out.payload["note"], "hit 為空的三態要講明"


def test_staffing_says_it_is_a_cross_check_not_a_red_flag(reg) -> None:
    """非營利園採成本分攤制，薪給結構一致——這一段講成異常就是誤導。"""
    listed = _run(reg, "list_institutions",
                  {"has_compliance_failure": True, "limit": 3}).payload["items"]
    if not listed:
        pytest.skip("沒有財報法遵未通過的登記")
    out = _run(reg, "get_staffing", {"institution_id": listed[0]["id"]})
    assert out.payload["count"] > 0
    assert out.payload["peer_median_cost_per_head"], "沒有同儕基準就沒有對照意義"
    assert "不是風險訊號" in out.payload["note"]


def test_staffing_without_a_report_says_insufficient_data(reg) -> None:
    listed = _run(reg, "list_institutions", {"limit": 50}).payload["items"]
    target = next((r for r in listed if not r["has_financial_report"]), None)
    if target is None:
        pytest.skip("這批前 50 名都有財報")
    out = _run(reg, "get_staffing", {"institution_id": target["id"]})
    assert out.payload["count"] == 0
    assert "資料不足" in out.payload["note"]


def test_realtime_says_mentions_do_not_predict(reg) -> None:
    """新聞消費的是已經發生的裁罰，提前量 0。講成預警就是誇大。"""
    iid = _with_penalties(reg)
    out = _run(reg, "get_realtime", {"institution_id": iid})
    assert "不是預測" in out.payload["note"]
    assert "不代表低風險" in out.payload["note"]
    for m in out.payload["items"]:
        assert "attribution_basis" in m, "每則都要能說為什麼歸給這所園"


def test_list_memos_browses_and_filters(reg) -> None:
    everything = _run(reg, "list_memos", {"limit": 50}).payload
    assert everything["count"] > 100
    narrowed = _run(reg, "list_memos", {"q": "三重", "limit": 50}).payload
    assert 0 < narrowed["count"] < everything["count"]
    assert all("三重" in x["title"] or "三重" in x["town"] for x in narrowed["items"])


def test_map_view_only_changes_display(reg) -> None:
    out = _run(reg, "set_map_view", {"colour_by": "penalty", "cluster": False})
    assert out.ui_action["type"] == "set_filters"
    assert out.ui_action["map"] == {"colour_by": "penalty", "cluster": False}
    assert "不影響排序或分數" in out.payload["note"]


def test_map_view_rejects_an_unknown_colour_basis(reg) -> None:
    """只有 type 與 penalty 兩種，且兩者都是公開事實，不是我們算的分數。"""
    out = _run(reg, "set_map_view", {"colour_by": "risk"})
    assert "只能是 type 或 penalty" in out.payload["error"]


def test_map_view_with_no_fields_says_so(reg) -> None:
    assert "沒有指定" in _run(reg, "set_map_view", {}).payload["error"]


def test_scan_estimate_prices_without_spending(reg) -> None:
    out = _run(reg, "scan_estimate", {"channels": ["news_rss", "ptt"], "scope": "proposal"})
    assert "usd_max" in out.payload
    assert "不會發動掃描" in out.payload["note"]
    assert out.ui_action["tab"] == "scan"


def test_peer_comparison_never_reads_as_a_probability(reg) -> None:
    """百分位不是違規機率。面板只有 10 個正樣本，撐不起監督式模型。"""
    listed = _run(reg, "list_institutions",
                  {"has_compliance_failure": True, "limit": 5}).payload["items"]
    if not listed:
        pytest.skip("沒有財報法遵未通過的登記")
    out = None
    for r in listed:
        cand = _run(reg, "get_peer_comparison", {"institution_id": r["id"]}).payload
        if cand.get("available"):
            out = cand
            break
    if out is None:
        pytest.skip("這幾所都沒有同儕比較資料")
    assert "不是分類器" in out["note"]
    assert "不是違規機率" in out["note"]
    assert isinstance(out["percentile"], (int, float))
    assert out["peer_count"] > 0


def test_contributions_and_anomalies_are_kept_apart(reg) -> None:
    """contributions 是分數的組成，anomalies 才是越過門檻的科目。

    混講的後果是把「這一所的分數由這幾項拉高」說成「查出這幾項有問題」，
    而 132 園年裡有 97 個一項都沒越過門檻。
    """
    listed = _run(reg, "list_institutions", {"limit": 30}).payload["items"]
    for r in listed:
        out = _run(reg, "get_peer_comparison", {"institution_id": r["id"]}).payload
        if not out.get("available"):
            continue
        assert "contributions" in out and "anomalies" in out
        assert "不是發現" in out["note"]
        return
    pytest.skip("這批前 30 名都沒有同儕比較資料")


def test_peer_comparison_absent_says_coverage_not_compliance(reg) -> None:
    out = _run(reg, "get_peer_comparison", {"institution_id": "deadbeef"})
    assert "查無機構" in out.payload["error"]


def test_peer_scoring_excludes_the_things_it_is_checked_against(reg) -> None:
    """法遵與裁罰刻意不進這個計算——訓練過的檢查不算檢查。"""
    listed = _run(reg, "list_institutions", {"limit": 30}).payload["items"]
    for r in listed:
        out = _run(reg, "get_peer_comparison", {"institution_id": r["id"]}).payload
        if out.get("available"):
            assert "訓練過的檢查不算檢查" in out["note"]
            return
    pytest.skip("這批前 30 名都沒有同儕比較資料")


def test_agent_absorbed_everything_the_query_tab_could_do() -> None:
    """查詢頁籤移除前，它有三個篩選與兩種排序是助理沒有的。

    移除一個入口的前提是能力沒有跟著消失——這條測試就是那個前提，
    哪天有人把這些參數拿掉，這裡會先擋下來。
    """
    from smart_watchdog.agent.tools import ListInstitutionsArgs

    fields = set(ListInstitutionsArgs.model_fields)
    for f in ("town", "type", "has_penalty", "has_compliance_failure",
              "evaluation_partial", "has_mentions", "no_financial", "sort"):
        assert f in fields, f"少了 {f}，查詢頁籤原本做得到"


def test_no_financial_finds_the_uncovered_majority(reg) -> None:
    """全市 94.8% 沒有公開財報。查得出來才能誠實說「這些是資料不足」。"""
    out = _run(reg, "list_institutions", {"no_financial": True, "limit": 3})
    assert out.payload["matched"] > 1000
    assert all(not r["has_financial_report"] for r in out.payload["items"])


def test_sort_by_penalties_and_by_recency_differ_from_rank(reg) -> None:
    by_rank = _run(reg, "list_institutions", {"limit": 5}).payload["items"]
    by_pen = _run(reg, "list_institutions",
                  {"sort": "penalties", "limit": 5}).payload["items"]
    pens = [r["penalties"] for r in by_pen]
    assert pens == sorted(pens, reverse=True), "penalties 要由多到少"
    by_recent = _run(reg, "list_institutions",
                     {"sort": "recent", "limit": 5}).payload["items"]
    dates = [r["last_event_date"] or "" for r in by_recent]
    assert dates == sorted(dates, reverse=True), "recent 要由新到舊"
    assert [r["id"] for r in by_rank] != [r["id"] for r in by_pen]
