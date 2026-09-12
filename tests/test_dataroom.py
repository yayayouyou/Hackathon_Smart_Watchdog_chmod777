"""資料室：表單分類、空白格、以及一份原件的入庫。

三件事值得釘住，因為它們各自對應一個會產生錯誤財務陳述的失敗：

1. **同頁不繼承分類。** 一頁印兩張表時，下一張通常是新的表而不是續表。
   N01 安溪 113 p17 的人事費與業務費期間差一個學年度——繼承過去就是
   CLAUDE.md 那條「一頁一組表頭造成無聲的一年偏移」的同型錯誤。
2. **空白格活著到畫面上。** `nonprofit_pagewise_facts.csv` 對空白是整列跳過，
   所以資料室只能讀頁 JSON。空白＝未編列，不是 0。
3. **待載入的報告是真的查不到。** 不是隱藏，是不在庫裡——上傳才進來。
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

from smart_watchdog.dataroom import store
from smart_watchdog.dataroom import tabletypes as tt

SLICE = pathlib.Path(__file__).resolve().parents[1] / "data/interim/dataroom"
needs_slice = pytest.mark.skipif(
    not (SLICE / "index.json").exists(),
    reason="需要 scripts/build_dataroom_slice.py 產生的切片",
)


# ── 分類規則 ──────────────────────────────────────────────────────────


def test_titles_route_to_their_section() -> None:
    assert tt.route("資產負債表", None) == "balance_sheet"
    assert tt.route(None, "2. 人事費") == "personnel_detail"
    assert tt.route("現金流量表", None) == "cash_flow"


def test_normalisation_absorbs_full_width_spaces() -> None:
    """原件會印「人　事　費」。比對前正規化，顯示仍用原字串。"""
    assert tt.route("人　事　費明細表", None) == "personnel_detail"


def test_added_routes_do_not_steal_from_existing_ones() -> None:
    """兩條新規則附加在尾端，不可插在既有條目之前。

    `route()` 第一個命中就贏，而「教保費」是短詞：插在 `income_by_function`
    之前，heading 同時寫到「功能別」與「教保費」的表會被搶走。
    """
    assert tt.route("收支餘絀表－功能別（含教保費）", None) == "income_by_function"
    assert tt.route("教保費收入明細表", None) == "tuition_income"


def test_original_typos_are_routed_not_left_unclassified() -> None:
    """原件印的是「資遺費準備」（遺，不是遣）。"""
    assert tt.route("1.資遺費準備", None) == "severance_reserve"
    assert tt.route("各學年收支預算決算比較表", None) == "multiyear_compare"


def test_a_continuation_on_the_next_page_inherits_the_section() -> None:
    tables = [
        {"pdf_page": 19, "title": "專案補助收支表", "context_heading": None},
        {"pdf_page": 20, "title": None, "context_heading": None},
    ]
    out = tt.route_tables(tables)
    assert out[0] == {"section": "project_subsidy", "section_inherited": False}
    assert out[1] == {"section": "project_subsidy", "section_inherited": True}


def test_a_second_table_on_the_same_page_never_inherits() -> None:
    """同頁的下一張表是新的表，不是續表。

    N01 安溪 113 p17 一頁印兩張明細，期間分別是 112.8.1~113.7.31 與
    113.8.1~114.7.31。把後者當成前者的續表就是一年偏移。
    """
    tables = [
        {"pdf_page": 17, "title": None, "context_heading": "2. 人事費"},
        {"pdf_page": 17, "title": None, "context_heading": None},
    ]
    out = tt.route_tables(tables)
    assert out[0]["section"] == "personnel_detail"
    assert out[1]["section"] == tt.UNROUTED, "同頁不得繼承"


def test_a_gap_of_more_than_one_page_does_not_inherit() -> None:
    tables = [
        {"pdf_page": 19, "title": "財產目錄", "context_heading": None},
        {"pdf_page": 25, "title": None, "context_heading": None},
    ]
    assert tt.route_tables(tables)[1]["section"] == tt.UNROUTED


def test_an_unknown_section_keeps_its_key_rather_than_inventing_a_name() -> None:
    assert tt.zh("no_such_section") == "no_such_section"
    assert tt.zh(tt.UNROUTED) == "未分類明細"


# ── 切片與查詢 ────────────────────────────────────────────────────────


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """把載入狀態指到 tmp_path，不碰 data/runtime 的真實狀態。"""
    monkeypatch.setattr(store, "STATE", tmp_path / "dataroom.json")
    monkeypatch.setattr(store, "UPLOADS", tmp_path / "uploads")
    store._cache["index"] = None
    store._cache["reports"] = {}
    yield
    store._cache["index"] = None
    store._cache["reports"] = {}


@needs_slice
def test_pending_reports_are_absent_from_the_totals(isolated) -> None:
    del isolated
    ov = store.overview()
    held = set(ov["pending"])
    assert held, "示範需要至少一份保留的報告"
    ids = {r["id"] for r in ov["reports"] if r["state"] != "pending"}
    assert not (ids & held)
    assert ov["totals"]["reports"] == len(ids)


@needs_slice
def test_a_pending_report_cannot_be_read(isolated) -> None:
    del isolated
    rid = store.overview()["pending"][0]
    assert store.report(rid) is None, "待載入不是隱藏，是不在庫裡"


@needs_slice
def test_blank_cells_survive_into_the_table(isolated) -> None:
    """空白格是資料室只能讀頁 JSON 的理由。"""
    del isolated
    t = store.get_table("N01/113/p05/t1")
    assert t is not None
    flat = [v for r in t["rows"] for v in (r["values"] or [])]
    assert None in flat, "空白格不可在途中被丟掉"
    assert 0 not in [v for v in flat if v is None], "空白絕不可變成 0"
    assert t["null_means"].startswith("空白")


@needs_slice
def test_section_totals_match_the_sum_of_loaded_reports(isolated) -> None:
    del isolated
    ov = store.overview()
    loaded = [r for r in ov["reports"] if r["state"] != "pending"]
    expect = sum(r["tables"] for r in loaded)
    assert ov["totals"]["tables"] == expect
    assert sum(s["tables"] for s in ov["sections"]) == expect


@needs_slice
def test_compare_years_keeps_each_year_s_own_period_labels(isolated) -> None:
    """學年度不可拿來代稱期間——那正是一年偏移的來源。"""
    del isolated
    res = store.compare_years("安溪", "personnel_detail", max_rows=5)
    assert len(res["years"]) >= 2
    for item in res["items"]:
        for year, cell in item["by_year"].items():
            if cell:
                assert cell["period_labels"], f"{year} 少了期間原文"
                assert "p." in cell["citation"]


# ── 入庫 ──────────────────────────────────────────────────────────────


@needs_slice
def test_an_unrelated_filename_is_refused(isolated) -> None:
    del isolated
    res = store.ingest("random.pdf", b"%PDF-1.4 ...")
    assert res["ok"] is False
    assert res["reason"] == "unknown_document"


@needs_slice
def test_uploading_the_held_back_report_adds_it(isolated) -> None:
    """上傳前後的差異就是示範本身：總數要真的變。"""
    del isolated
    rid = store.overview()["pending"][0]
    meta = next(r for r in store.index()["reports"] if r["id"] == rid)
    before = store.overview()["totals"]

    src = pathlib.Path(__file__).resolve().parents[1] / (meta["pdf"] or "")
    blob = src.read_bytes() if src.is_file() else b"%PDF-1.4 stand-in"
    res = store.ingest(pathlib.Path(meta["pdf"] or f"{rid}.pdf").name, blob)

    assert res["ok"] is True
    assert res["report"] == rid
    assert res["added"]["tables"] > 0
    assert res["added"]["sections"], "要說得出多了哪幾種表單"

    after = store.overview()["totals"]
    assert after["reports"] == before["reports"] + 1
    assert after["tables"] == before["tables"] + res["added"]["tables"]
    assert after["blank"] > before["blank"], "空白格也要一起進來"
    assert store.report(rid) is not None
    assert rid not in store.overview()["pending"]


@needs_slice
def test_reset_returns_to_the_pre_upload_state(isolated) -> None:
    del isolated
    rid = store.overview()["pending"][0]
    meta = next(r for r in store.index()["reports"] if r["id"] == rid)
    store.ingest(pathlib.Path(meta["pdf"] or f"{rid}.pdf").name, b"%PDF-1.4 x")
    assert store.report(rid) is not None
    store.reset()
    assert store.report(rid) is None


# ── 端點 ──────────────────────────────────────────────────────────────


@needs_slice
def test_the_slice_never_writes_into_delivered_corpora() -> None:
    """上傳落點不可碰主辦方資料集或交付語料。

    `data/raw/` 有 162 份的檢查、`data/extracted/nonprofit_pages/` 會被
    `build_pagewise_facts.py` 的 glob 掃進下一次的事實表。
    """
    assert "data/runtime" in str(store.UPLOADS).replace("\\", "/")
    assert "data/interim" in str(store.SLICE).replace("\\", "/")


@needs_slice
def test_the_index_records_which_sections_are_inherited() -> None:
    """繼承而來的分類是推論，UI 與 agent 都要看得到。"""
    rid = next(p.stem for p in (SLICE / "r").glob("*.json"))
    data = json.loads((SLICE / "r" / f"{rid}.json").read_text(encoding="utf-8"))
    assert all("section_inherited" in t for t in data["tables"])
