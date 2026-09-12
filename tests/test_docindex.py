"""文件索引：檢索必須可引用，而且引用必須是對的。

這裡鎖住的兩件事，各自對應一個真實的錯誤：

1. **頁碼不可偏移。** 抽取結果記的是文件自己印在頁尾的頁碼（1-based）。
   曾經在 `_page_label()` 多加 1，結果每一筆引用都差一頁——對稽查而言，
   **給錯頁碼比答不出來更糟**，因為它會讓人翻到別的表然後認定系統在亂講。
2. **空白不可變成 0。** `facts.value` 為 NULL 的意思是「未編列預算」，
   那本身是一項稽查發現；落成 0 等於對真實機構謊稱編列了零元。
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.docindex import build as build_mod
from smart_watchdog.docindex import schema, search

INDEX = pathlib.Path("data/processed/document_index.sqlite")
needs_index = pytest.mark.skipif(
    not INDEX.exists(),
    reason="尚未建索引；先執行 python run.py doc-index")


# ── 不需要索引檔的純函式 ───────────────────────────────────────────
def test_page_label_does_not_shift_the_page_number():
    """傳進來的已經是印刷頁碼，這裡不做任何加減。"""
    assert build_mod._page_label(5) == "p.5"
    assert build_mod._page_label(11, 14) == "p.11-14"
    assert build_mod._page_label(7, 7) == "p.7"


def test_blank_stays_null_and_never_becomes_zero():
    for blank in (None, ""):
        assert build_mod._num(blank) is None, f"{blank!r} 必須維持 None"
    assert build_mod._num(0) == 0.0            # 真正的 0 仍然是 0
    assert build_mod._num("1,234") == 1234.0
    assert build_mod._num("—") is None         # 破折號是空白，不是數字


def test_clause_splitting_keeps_the_sub_clause_number():
    """條號要留著，法遵檢核引用的正是「附註二(四)」這個粒度。"""
    text = "二、重大會計政策\n(一) 會計基礎\n內容甲\n(四) 資遣費準備金\n內容乙"
    out = build_mod._split_clauses(text)
    labels = [c for c, _ in out]
    assert "(一)" in labels and "(四)" in labels
    body = dict(out)["(四)"]
    assert "資遣費準備金" in body


def test_clause_splitting_handles_full_width_brackets():
    """全形與半形括號都出現過，兩種都要吃。"""
    out = build_mod._split_clauses("（一）甲\n（二）乙")
    assert [c for c, _ in out] == ["(一)", "(二)"]


# ── 需要已建好的索引 ───────────────────────────────────────────────
@needs_index
def test_citation_matches_the_page_recorded_by_the_extraction():
    """索引給出的頁碼必須等於抽取結果記的頁碼，一頁都不能差。"""
    src = pathlib.Path("data/extracted/nonprofit/N01_安溪_113.json")
    if not src.exists():
        pytest.skip("缺少抽取結果")
    d = json.loads(src.read_text(encoding="utf-8"))

    conn = schema.connect(INDEX)
    try:
        rows = search.locate(conn, institution="安溪", year=113)
        by_kind = {r.kind: r for r in rows}
        assert by_kind["資產負債表"].page_label == f"p.{d['balance_sheet']['page']}"
        n2 = d["note_2"]
        assert by_kind["附註二 重大會計政策"].page_label == (
            f"p.{n2['page']}-{n2['page_end']}")
    finally:
        conn.close()


@needs_index
def test_facts_report_blank_as_blank_not_zero():
    conn = schema.connect(INDEX)
    try:
        blanks = conn.execute(
            "SELECT count(*) FROM facts WHERE value IS NULL").fetchone()[0]
        assert blanks > 0, "整份語料不可能沒有任何空白格——NULL 疑似被寫成 0"
        rows = search.find_facts(conn, institution="安溪", year=113, limit=500)
        for r in rows:
            if r["value"] is None:
                assert r["is_blank"] is True
                assert r["blank_means"] and "不是 0" in r["blank_means"]
    finally:
        conn.close()


@needs_index
def test_every_result_carries_a_citation():
    """沒有出處的結果不該回傳——稽查員要能指著那一頁。"""
    conn = schema.connect(INDEX)
    try:
        for hit in search.find_passages(conn, "資遣費準備金", limit=10):
            assert hit.citation(), f"{hit.label} 沒有出處"
    finally:
        conn.close()


@needs_index
def test_zero_hits_report_insufficient_data_not_low_risk():
    """查不到不等於沒問題。零命中必須講涵蓋範圍，不可暗示低風險。"""
    conn = schema.connect(INDEX)
    try:
        r = search.retrieve(conn, "這串字不可能出現在任何財報裡的隨機文字")
        assert r["found"] == 0
        assert "資料不足" in r["note"]
        assert "低風險" in r["note"]        # 明說「不代表低風險」
        assert r["coverage"], "零命中時要附上涵蓋範圍說明"
    finally:
        conn.close()


@needs_index
def test_coverage_states_what_is_not_covered():
    conn = schema.connect(INDEX)
    try:
        scopes = {c["scope"]: c for c in search.summarise_coverage(conn)}
        vis = scopes.get("全市教保機構財務可見度")
        assert vis, "索引必須聲明它涵蓋不到的部分"
        assert vis["covered"] < vis["total"]
        assert "不是合規證明" in vis["note"]
    finally:
        conn.close()


@needs_index
def test_compliance_coverage_keeps_three_states_apart():
    """通過／未通過／待判讀三態不可被壓成兩態。"""
    conn = schema.connect(INDEX)
    try:
        scopes = {c["scope"]: c for c in search.summarise_coverage(conn)}
        note = scopes["法遵檢核"]["note"]
        for word in ("通過", "未通過", "待判讀"):
            assert word in note, f"涵蓋說明沒有提到「{word}」"
    finally:
        conn.close()


@needs_index
def test_short_query_returns_nothing_rather_than_everything():
    """trigram 分詞器對 3 字以下無意義；要回空，不是回全部。"""
    conn = schema.connect(INDEX)
    try:
        assert search.find_passages(conn, "金") == []
        assert search.find_passages(conn, "") == []
    finally:
        conn.close()
