"""逐園裁罰檔：處分書文號補上了 punish_all.json 缺的事件識別碼。

這一檔測的是**正規化與聚合的邊界**，不是「有沒有讀到檔案」。理由是這份資料
唯一的用途就是把「條款列」收斂成「處分書」，而那個收斂一旦做錯，錯的方向
是把兩張處分書講成一張——也就是把某園的被罰次數講少。寧可漏合併，不可錯合併。

測試不連網路：快照已進版控，`fetch_*` 那條路不在這裡驗。
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.scrape import mirror

ROOT = pathlib.Path(__file__).resolve().parents[1]
SNAPSHOT = mirror.DEFAULT_PENALTIES
needs_snapshot = pytest.mark.skipif(
    not SNAPSHOT.exists(),
    reason="尚未抓取逐園裁罰快照，跑：python run.py mirror-extras")


# ── 欄位長度 ────────────────────────────────────────────────────────
def test_seven_column_rows_do_not_shift_the_other_fields():
    """1,406 列 6 欄、102 列 7 欄。第 7 欄是上游記的原始機構名。

    直接 `zip(FIELDS, row)` 在 7 欄時不會錯，但 `row[5]` 之類的位置存取會；
    這裡釘住「多出來的那一欄不會污染前六欄」。
    """
    six = ["2024/12/11", "新北府教幼字第1132420187號", "幼照法第52條第6款",
           "第43條第3項-收費備查", "負責人：某某", "罰鍰：60,000元"]
    a = mirror.parse_rows("甲園", [six])[0]
    b = mirror.parse_rows("甲園", [[*six, "新北市私立舊名幼兒園"]])[0]
    for field in mirror.FIELDS:
        assert a[field] == b[field], field
    assert a["source_title"] == ""
    assert b["source_title"] == "新北市私立舊名幼兒園"


def test_short_rows_are_dropped_not_padded():
    """欄位不足的列寧可丟掉。補 null 會讓處分內容欄落到法源欄上。"""
    assert mirror.parse_rows("甲園", [["2024/12/11", "文號"]]) == []


# ── 文號正規化 ──────────────────────────────────────────────────────
def test_duplicated_character_in_doc_no_is_normalised():
    """上游打字變體：『教幼字字第』重複一個「字」，實測 25 列。

    不正規化會把同一張處分書拆成兩件，也就是把被罰次數講多。
    """
    assert (mirror.normalise_doc_no("新北府教幼字字第1130034721號")
            == mirror.normalise_doc_no("新北府教幼字第1130034721號"))


def test_doc_no_normalisation_does_not_merge_different_numbers():
    """正規化只處理已實測的打字變體，不做模糊比對。

    文號是拿來合併紀錄的鍵，合錯就是把兩張處分書講成一張。
    """
    assert (mirror.normalise_doc_no("新北府教幼字第1130034721號")
            != mirror.normalise_doc_no("新北府教幼字第1130034722號"))


def test_event_key_includes_the_school():
    """⚠️ 文號**不是全域唯一**。

    實測 `新北府教幼字第1130236665號` 同時出現在安興非營利與私立智盛兩個
    不相干的園。只用文號當鍵會把兩園的處分書併成一件。
    """
    doc = "新北府教幼字第1130236665號"
    assert mirror.event_key("安興非營利", doc) != mirror.event_key("私立智盛", doc)


# ── 重複列的判定 ────────────────────────────────────────────────────
def _row(**kw):
    base = {"date": "2024/12/11", "doc_no": "新北府教幼字第1號",
            "statute": "幼照法第52條第2款", "law": "第16條第1項",
            "actor": "負責人：某某", "punishment": "罰鍰：60,000元"}
    base.update(kw)
    return base


def test_same_document_same_statute_same_wording_is_a_duplicate():
    """同文號＋同法源＋處分內容逐字相同 → 是同一筆罰鍰被記成兩列。

    最典型是「第16條第1項-年齡規定」與「第16條第1項-混齡規定」各記一列
    60,000 元，實為一張處分書一筆罰鍰。
    """
    rows = [_row(law="第16條第1項-年齡規定"), _row(law="第16條第1項-混齡規定")]
    dupes = mirror.duplicate_rows({"甲園": mirror.parse_rows("甲園", [
        [r["date"], r["doc_no"], r["statute"], r["law"], r["actor"], r["punishment"]]
        for r in rows])})
    assert len(dupes) == 1


def test_different_amounts_under_one_document_are_not_duplicates():
    """⚠️ 這是最重要的一條：**不可只用文號聚合罰鍰**。

    實測有 34 組同文號同法源但金額不同的真實多筆罰鍰（佳學 60,000＋150,000、
    實栽童心 3,000＋15,000）。把它們併成一筆，等於把該園的罰鍰總額講少。
    """
    rows = [_row(punishment="罰鍰：60,000元"), _row(punishment="罰鍰：150,000元")]
    dupes = mirror.duplicate_rows({"甲園": mirror.parse_rows("甲園", [
        [r["date"], r["doc_no"], r["statute"], r["law"], r["actor"], r["punishment"]]
        for r in rows])})
    assert dupes == []


# ── 前瞻驗證集 ──────────────────────────────────────────────────────
def test_new_since_is_strictly_after():
    """邊界要嚴格大於：等於 as_of 的那一天已經在快照裡，不是新資料。"""
    by_school = {"甲園": mirror.parse_rows("甲園", [
        ["2026/08/05", "d1", "s", "l", "a", "罰鍰：1,000元"],
        ["2026/08/06", "d2", "s", "l", "a", "罰鍰：1,000元"],
    ])}
    fresh = mirror.new_since(by_school, "2026/08/05")
    assert [r["date"] for r in fresh] == ["2026/08/06"]


# ── 已釘住的快照 ────────────────────────────────────────────────────
@needs_snapshot
def test_snapshot_matches_its_manifest():
    """快照與 manifest 的 sha256 必須相符，否則「釘住」是一句空話。"""
    import hashlib

    manifest = json.loads(
        (mirror.DEFAULT_DIR / "manifest.json").read_text(encoding="utf-8"))
    want = manifest["files"][SNAPSHOT.name]
    assert hashlib.sha256(SNAPSHOT.read_bytes()).hexdigest() == want["sha256"]
    assert SNAPSHOT.stat().st_size == want["byte_length"]


@needs_snapshot
def test_snapshot_carries_its_attribution():
    """CC-BY 的標註要跟著資料走，不是只寫在 README 裡。

    這份 JSON 可能被單獨複製出去，屆時手上只有它。
    """
    payload = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    att = payload["attribution"]
    assert att["original_source"] == \
        "https://ap.ece.moe.edu.tw/webecems/pubSearch.aspx"
    assert "CC-BY" in att["licence"]


@needs_snapshot
def test_documents_are_fewer_than_rows():
    """這份資料存在的理由：條款列數 ≠ 被抓次數。

    若兩者相等，代表文號沒有被正確讀到或聚合，這份資料就沒有帶來任何
    punish_all.json 沒有的東西。
    """
    by_school = mirror.load_penalties_by_school(SNAPSHOT)
    rows = sum(len(v) for v in by_school.values())
    docs = len(mirror.documents(by_school))
    assert docs < rows
    assert rows > 1000, "新北應有一千多列，數量級不對代表快照抓錯縣市"
