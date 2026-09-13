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

import csv
import hashlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

from smart_watchdog.dataroom import intake, store
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
    monkeypatch.setattr(store, "EXTRACTED", tmp_path / "extracted")
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


def _stand_in(rid: str, monkeypatch) -> bytes:
    """讓一份假 PDF 的雜湊等於某份已知原件。測「看內容認檔」不必帶著 data/raw。"""
    blob = b"%PDF-1.4 stand-in for " + rid.encode()
    meta = next(r for r in store.index()["reports"] if r["id"] == rid)
    monkeypatch.setitem(meta, "sha256", hashlib.sha256(blob).hexdigest())
    return blob


@needs_slice
def test_a_known_original_is_recognised_by_content_whatever_its_name(
        isolated, monkeypatch) -> None:
    """上傳前後的差異就是示範本身：總數要真的變。檔名亂取也一樣認得。"""
    del isolated
    rid = store.overview()["pending"][0]
    blob = _stand_in(rid, monkeypatch)
    before = store.overview()["totals"]

    res = intake.submit("掃描檔(1).pdf", blob)

    assert res["ok"] is True and res["status"] == "loaded"
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
def test_an_original_already_in_the_library_is_not_added_twice(
        isolated, monkeypatch) -> None:
    del isolated
    rid = next(r["id"] for r in store.overview()["reports"] if r["state"] == "loaded")
    blob = _stand_in(rid, monkeypatch)
    before = store.overview()["totals"]
    assert intake.submit("x.pdf", blob)["status"] == "already_loaded"
    assert store.overview()["totals"] == before


@needs_slice
def test_reset_returns_to_the_pre_upload_state(isolated, monkeypatch) -> None:
    del isolated
    rid = store.overview()["pending"][0]
    intake.submit("a.pdf", _stand_in(rid, monkeypatch))
    assert store.report(rid) is not None
    store.reset()
    assert store.report(rid) is None


# ── 認不得的 PDF：自動抽取 ────────────────────────────────────────────


def _pdf(n: int) -> bytes:
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    for _ in range(n):
        doc.new_page()
    try:
        return doc.tobytes()
    finally:
        doc.close()


def _page(code, periods=()) -> dict:
    tables = [{"title": "收支餘絀表", "context_heading": None, "unit": "元",
               "period_labels": list(periods),
               "items": [{"label": "教保費收入", "values": [1000] * len(periods)},
                         {"label": "業務發展費", "values": [None] * len(periods)}]}
              ] if periods else []
    return {"footer_code": code, "printed_page": 1, "page_kind": "other",
            "tables": tables, "text_sections": [], "issues": []}


_Y114 = ["114.8.1~115.7.31", "113.8.1~114.7.31"]


@pytest.fixture
def extraction(isolated, monkeypatch):
    """換掉 Bedrock 與背景執行緒：替身照頁碼回答，工作同步跑完。"""
    del isolated
    calls: list[int] = []

    def use(pages: dict) -> None:
        def fake(image, page):
            del image
            calls.append(page)
            return json.loads(json.dumps(pages[page])), 6000, 1000, "fake-model"
        monkeypatch.setattr(intake, "EXTRACT", fake)

    monkeypatch.setattr(intake, "SPAWN", lambda fn: fn())
    return use, calls


@needs_slice
def test_a_new_report_is_extracted_and_identified_from_its_own_pages(extraction) -> None:
    """庫裡沒有的報告：抽完才知道是哪一所、哪一年，而且只看頁面內容。

    N01 安溪在庫裡只有 110–113，所以 114 學年度是一份「新的」。
    """
    use, _ = extraction
    use({1: _page("N01"), 2: _page("N01"), 3: _page(None),
         4: _page("N01", _Y114), 5: _page("N01")})
    before = store.overview()["totals"]

    job = intake.job(intake.submit("新的掃描.pdf", _pdf(5))["job"]["id"])

    assert job["status"] == "done", job
    assert job["report"] == "N01_安溪_114"
    assert job["quarantined"] == 1, "讀不到頁尾的那一頁不可以用推定的名義進來"
    rep = store.report("N01_安溪_114")
    assert rep["tables"][0]["rows"][1]["values"] == [None, None], "空白要留 null，不是 0"
    assert store.overview()["totals"]["reports"] == before["reports"] + 1
    assert store.loaded_reports("安溪", 114)


@needs_slice
def test_pages_naming_different_institutions_are_not_guessed(extraction) -> None:
    use, _ = extraction
    use({1: _page("N01"), 2: _page("N02", _Y114), 3: _page("N01")})
    before = store.overview()["totals"]
    job = intake.job(intake.submit("a.pdf", _pdf(3))["job"]["id"])
    assert job["status"] == "unidentified", job
    assert store.overview()["totals"] == before


@needs_slice
def test_a_pdf_that_is_not_a_report_stops_before_paying_for_every_page(extraction) -> None:
    use, calls = extraction
    use({i: _page(None) for i in range(1, 11)})
    job = intake.job(intake.submit("競賽規則.pdf", _pdf(10))["job"]["id"])
    assert job["status"] == "unidentified", job
    assert sorted(calls) == [1, 2, 3], "讀不到機構代號還繼續抽，是在替一份不是財報的檔案付錢"


@needs_slice
def test_a_different_copy_of_a_report_in_the_library_does_not_overwrite_it(extraction) -> None:
    use, _ = extraction
    use({1: _page("N01"), 2: _page("N01", ["113.8.1~114.7.31"])})
    n = len(store.report("N01_安溪_113")["tables"])
    job = intake.job(intake.submit("a.pdf", _pdf(2))["job"]["id"])
    assert job["status"] == "duplicate", job
    assert len(store.report("N01_安溪_113")["tables"]) == n


@needs_slice
def test_too_many_pages_is_refused_before_any_extraction(extraction) -> None:
    use, calls = extraction
    use({})
    res = intake.submit("book.pdf", _pdf(intake.MAX_PAGES + 1))
    assert res["ok"] is False and res["reason"] == "too_many_pages"
    assert calls == []


def test_the_year_rule_recovers_every_known_report() -> None:
    """學年度只看期間欄。132 份已知答案的報告逐一驗證：一份都不能錯、不能推不出。"""
    root = pathlib.Path(__file__).resolve().parents[1] / "data/extracted/nonprofit_pages"
    wrong, n = [], 0
    for d in sorted(x for x in root.iterdir() if x.is_dir() and not x.name.startswith("_")):
        pages = [json.loads(f.read_text(encoding="utf-8")) for f in d.glob("p*.json")]
        n += 1
        if intake.academic_year(pages) != pages[0]["academic_year"]:
            wrong.append(d.name)
    assert n > 100
    assert not wrong, wrong


def test_every_extracted_report_has_its_original_hash() -> None:
    root = pathlib.Path(__file__).resolve().parents[1] / "data/extracted"
    with (root / "nonprofit_pdf_sha256.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    extracted = {x.name for x in (root / "nonprofit_pages").iterdir()
                 if x.is_dir() and not x.name.startswith("_")}
    assert {r["report"] for r in rows} == extracted
    assert len({r["sha256"] for r in rows}) == len(rows), "兩份雜湊相同就分不出上傳的是哪一份"


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
