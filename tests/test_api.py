"""API 契約：動態版與靜態版必須讀同一份 payload，且輸出定位不能在此處遺失。

聊天介面是「哪間比較危險」這種問題會被問出來的地方，所以這裡的測試重點不是
端點會不會 200，而是**它有沒有在回答一個它無權回答的問題**。
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from smart_watchdog.api.chat import KeywordPlanner, answer
from smart_watchdog.api.server import app

PAYLOAD = pathlib.Path("dist/data/payload.json")
needs_payload = pytest.mark.skipif(
    not PAYLOAD.exists(), reason="尚未執行 scripts/build_frontend.py")

FAKE = {
    "schema_version": 1,
    "points": [
        {"i": "a1", "n": "甲", "full": "新北市私立甲幼兒園", "t": 2, "d": "板橋區",
         "x": 121.4, "y": 25.0, "s": 0.5, "r": 1, "why": "分數前100",
         "fin": 0, "cf": 0, "ch": 0, "ct": "", "e90": 0, "e365": 0, "np": 3,
         "cap": 60, "fee": 12000, "ev": "", "evd": "", "er": "", "erd": "", "ep": 0},
        {"i": "b2", "n": "乙", "full": "新北市乙非營利幼兒園", "t": 1, "d": "三重區",
         "x": 121.5, "y": 25.1, "s": 0.2, "r": 900, "why": "財報法遵未通過(高)",
         "fin": 1, "cf": 2, "ch": 1, "ct": "…", "e90": 0, "e365": 0, "np": 0,
         "cap": 90, "fee": 3000, "ev": "", "evd": "", "er": "", "erd": "", "ep": 0},
    ],
    "districts": [], "dossier": {}, "bench": {}, "boundary": [],
    "realtime": {"swept_at": "2026-09-08 11:27", "channels_live": 2,
                 "channels_total": 5, "channels": [],
                 "by_institution": {"a1": [{"ch": "news_rss", "h": "標題", "u": "u",
                                            "d": "2026-09-01", "p": "報社",
                                            "k": "incident"}]}},
}


@needs_payload
def test_health_reports_what_loaded():
    h = TestClient(app).get("/api/health").json()
    assert h["payload_loaded"] is True
    assert h["institutions"] > 1000
    # 面板靠這兩個數字說「幾個管道在看」；缺了就會被讀成「很安靜」
    assert h["channels_total"] >= h["channels_live"] >= 0


def test_land_outline_excludes_new_taipei():
    """反灰層畫的是**新北以外**的陸地。

    這份資料一旦混進新北市自己的輪廓，地圖會把新北連同鄰縣市一起灰掉——
    畫面看起來只是「顏色怪怪的」，但整個視覺論述（這 20 家在新北的哪裡）就沒了。
    端點允許回空陣列（檔案還沒建），前端會自己退回舊的遮罩做法。
    """
    land = TestClient(app).get("/api/land").json()["land"]
    assert isinstance(land, list)
    names = {c["c"] for c in land}
    assert "新北市" not in names
    if land:                       # 有建檔的話，臺北市一定要在裡面：它是飛地
        assert "臺北市" in names
        for county in land:
            for ring in county["poly"]:
                assert len(ring) >= 3
                assert all(len(pt) == 2 for pt in ring)


@needs_payload
def test_proposal_tiers_are_ordered_findings_before_score():
    """財報法遵未通過必須排在分數之前，否則升級管道形同虛設。"""
    proposal = TestClient(app).get("/api/proposal?n=40").json()["proposal"]
    tiers = [p["tier"] for p in proposal]
    if "財報法遵未通過（高）" in tiers and "分數排序" in tiers:
        assert tiers.index("財報法遵未通過（高）") < tiers.index("分數排序")


@needs_payload
def test_dossier_always_carries_the_disclaimer():
    """只讀這個端點的呼叫端，也必須被告知這是建議查核而非違法認定。"""
    c = TestClient(app)
    first = c.get("/api/proposal?n=1").json()["proposal"][0]["i"]
    d = c.get(f"/api/institutions/{first}").json()
    assert "非違法認定" in d["disclaimer"]
    assert "資料不足，非低風險" in d["disclaimer"]


@needs_payload
def test_unknown_institution_is_404_not_an_empty_dossier():
    assert TestClient(app).get("/api/institutions/deadbeef").status_code == 404


def test_chat_never_answers_a_danger_question_directly():
    """「哪幾間比較危險」只能得到排序與定位聲明，不能得到危險判定。"""
    r = answer("三重區哪幾間幼兒園比較危險？", FAKE)
    for word in ("危險", "違法", "不法"):
        assert f"比較{word}" not in r["summary"]
    assert "非危險或違法認定" in r["caveat"]
    assert "資料不足，非低風險" in r["caveat"]


def test_chat_plan_is_retrieval_only():
    """planner 的輸出必須是檢索條件，不含任何判斷欄位。"""
    plan = KeywordPlanner().plan("板橋區私立幼兒園有幾間有裁罰紀錄？")
    assert set(plan) == {"filters", "sort", "limit", "intent"}
    assert plan["filters"]["town"] == "板橋區"
    assert plan["filters"]["type"] == 2
    assert plan["filters"]["has_penalty"] is True
    assert plan["intent"] == "count"


def test_chat_filters_actually_filter():
    r = answer("三重區有哪些幼兒園財報法遵沒通過？", FAKE)
    assert [x["id"] for x in r["results"]] == ["b2"]
    # 「沒有公開財報」曾被判成「法遵未通過」——意思相反，是 planner 的真 bug
    for q in ("哪些園沒有公開財報？", "哪些園無財報？", "查不到財報的有哪些？"):
        assert [x["id"] for x in answer(q, FAKE)["results"]] == ["a1"], q


def test_chat_reports_the_planner_it_used():
    """決賽換成 BedrockPlanner 時，答案必須說明是誰做的計畫。"""
    assert answer("全市概況", FAKE)["planner"] == "keyword"
