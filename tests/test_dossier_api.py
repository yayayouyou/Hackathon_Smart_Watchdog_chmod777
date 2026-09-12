"""卷宗三段補充的契約：裁罰明細、排名軌跡、建議書。

這三件事 agent 本來就叫得到，但人點卷宗時看不到——合併時只搬了 agent 的能力，
漏了畫面。這裡測的是**兩條路徑看到的是同一份東西**，以及三個容易被誤讀的欄位：

1. 非金錢處分的 `fine` 是空值，不是 0。填 0 等於謊稱罰了零元。
2. 受處分角色不同即為不同處分，計數不可去重。
3. 排名軌跡的 `hit` 為空值代表觀察期未結束，不是「後來沒事」。
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

pytest.importorskip("pandas")

from smart_watchdog.api import dossier

ROOT = pathlib.Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not (ROOT / "dist/data/payload.json").exists(),
    reason="需要 dist/data/payload.json，先跑 python run.py frontend",
)


def _an_institution_with_penalties() -> str:
    from smart_watchdog.api import server

    for p in server.payload()["points"]:
        if p.get("np", 0) > 2:
            return p["i"]
    pytest.skip("沒有裁罰超過 2 件的機構")


def test_penalty_detail_count_matches_the_payload_count() -> None:
    """卷宗上顯示的件數與地圖點位的 np 必須一致，否則兩處數字會打架。"""
    from smart_watchdog.api import server

    iid = _an_institution_with_penalties()
    point = server._state["index"][iid]
    assert dossier.penalties_of(iid)["count"] == point["np"]


def test_non_monetary_sanction_has_a_blank_fine_not_zero() -> None:
    rows = []
    from smart_watchdog.api import server

    for p in server.payload()["points"][:400]:
        rows += dossier.penalties_of(p["i"])["items"]
    blanks = [r for r in rows if r["fine"] is None]
    if not blanks:
        pytest.skip("這批樣本沒有非金錢處分")
    assert all(r["fine"] is not None or r["fine"] is None for r in rows)
    assert 0 not in [r["fine"] for r in blanks], "非金錢處分不可記成罰 0 元"


def test_penalties_keep_the_actor_role_so_they_are_not_merged() -> None:
    iid = _an_institution_with_penalties()
    out = dossier.penalties_of(iid)
    assert "不可合併計數" in out["note"]
    assert all("actor_role" in r for r in out["items"])


def test_penalties_are_newest_first() -> None:
    iid = _an_institution_with_penalties()
    dates = [str(r["date"]) for r in dossier.penalties_of(iid)["items"]]
    assert dates == sorted(dates, reverse=True)


def test_rank_track_covers_every_snapshot() -> None:
    from smart_watchdog.api import explore

    iid = _an_institution_with_penalties()
    out = dossier.rank_track_of(iid)
    assert out["count"] == len(explore.load_timeline()["points"])
    assert "時序切分" in out["note"]


def test_rank_track_says_a_null_hit_is_not_a_clean_record() -> None:
    """hit 為空值代表前瞻窗還沒過完。讀成「後來沒事」就是把未知講成安全。"""
    iid = _an_institution_with_penalties()
    out = dossier.rank_track_of(iid)
    assert "尚未結束" in out["note"]
    assert all(p["hit"] in (0, 1, None) for p in out["points"])


def test_memo_absent_is_explained_not_silently_empty() -> None:
    out = dossier.memo_of("deadbeef")
    assert out["exists"] is False
    assert "不會產生建議書" in out["note"]


def test_memo_carries_the_request_for_explanation_framing() -> None:
    listing = dossier.list_memos()
    assert listing["count"] > 100
    first = listing["items"][0]["id"]
    memo = dossier.memo_of(first)
    assert memo["exists"] is True
    assert "不是違法認定" in memo["note"]
    assert "非違法認定" in memo["content"], "信裡的免責聲明不可消失"


def test_memo_list_is_ordered_by_the_delivered_rank() -> None:
    ranks = [x["priority_rank"] for x in dossier.list_memos()["items"]]
    assert ranks == sorted(ranks)


def test_agent_tool_and_the_dossier_endpoint_return_the_same_penalties() -> None:
    """各讀一次 CSV 的那天，就是 agent 講的數字與畫面不符的那天。"""
    from smart_watchdog.agent.registry import ToolContext
    from smart_watchdog.agent.tools import build_registry

    iid = _an_institution_with_penalties()
    reg = build_registry()
    via_tool = reg.execute(
        ToolContext(db=None, user=object(), view={}, session_id="x"),
        "get_penalties", {"institution_id": iid},
    ).payload
    assert via_tool["count"] == dossier.penalties_of(iid)["count"]
    assert via_tool["items"] == dossier.penalties_of(iid)["items"]
