"""行政區派工優先序：型態校正、曝險閘門、用詞界線。

這張榜最容易出的錯不是算錯，是**算對了但排的是別的東西**。不做型態校正的
區級排名實質上是在排「哪一區私立園比較多」（實測 corr(私立占比, 區平均風險
分數) = 0.93），而且看起來完全正常——沒有例外、沒有紅字，只是結論是錯的。
所以這裡釘的主要是那些「壞掉也不會報錯」的性質。
"""

from __future__ import annotations

import json
import pathlib
from collections import Counter

import pytest

from smart_watchdog.api.payload import LEVELS, MIN_EXPECTED, _byar, district_board

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAYLOAD = ROOT / "dist/data/payload.json"


def _pt(i, d, t, r):
    return {"i": i, "d": d, "t": t, "r": r, "np": 0, "fin": 0}


def test_type_composition_cannot_decide_the_ranking() -> None:
    """組成不同、密度相同的兩區，名次不該有先後。

    這是整個型態校正存在的理由。造一個全私立的區與一個全公立的區，讓兩邊
    **相對於自己型態的基準率**一樣高——沒有校正的排法（絕對家數或粗比率）
    會把私立那區排前面，校正過的不會。
    """
    # 全市基準：私立 10/100 = 10%、公立 5/500 = 1%
    # 公立區自己 100 家、1 家進榜（1%＝公立基準率）；另外 400 家公立散在別處，
    # 其中 4 家進榜，湊出 5/500 = 1% 的全市公立基準率。
    pts = [_pt(f"p{n}", "私立區", 2, n + 1 if n < 10 else 9999) for n in range(100)]
    pts += [_pt(f"u{n}", "公立區", 0, 11 if n == 0 else 9999) for n in range(100)]
    pts += [_pt(f"v{n}", "其他區", 0, 12 + n if n < 4 else 9999) for n in range(400)]
    b = district_board(pts, k=100)
    by = {r["d"]: r for r in b["rows"]}
    # 兩區都剛好落在自己型態的基準率上 → SIR 都是 1.0
    assert by["私立區"]["sir"] == pytest.approx(1.0, abs=0.01)
    assert by["公立區"]["sir"] == pytest.approx(1.0, abs=0.01)
    assert by["私立區"]["band"] == by["公立區"]["band"] == "與全市相當"


def test_a_tiny_district_cannot_top_the_board_on_one_case() -> None:
    """2 園的區出現 1 家進榜，點估是 50 倍——不給名次，而不是排第一。

    ⚠️ 只靠信賴下界擋不住這件事：坪林（2 園、全公立、exp=0.0136）多 1 家，
    Byar 下界仍有 12.6。必須有 exp<1 的曝險閘門。
    """
    pts = [_pt(f"b{n}", "大區", 2, n + 1 if n < 10 else 9999) for n in range(200)]
    # 公立母體要夠大，基準率才不會被小區自己的 2 家決定（那會讓 exp 剛好是 1）。
    pts += [_pt(f"c{n}", "中區", 0, 20 + n if n < 2 else 9999) for n in range(400)]
    pts += [_pt("t0", "小區", 0, 5), _pt("t1", "小區", 0, 9999)]
    b = district_board(pts, k=100)
    tiny = next(r for r in b["rows"] if r["d"] == "小區")
    assert tiny["exp"] < MIN_EXPECTED
    assert tiny["band"] == "資料不足"
    assert tiny["rank"] is None, "小區拿到名次了——曝險閘門沒擋住"
    assert tiny["sir"] is None
    # ⚠️ 不給名次不等於把案件藏起來：obs 必須照常回報。
    assert tiny["obs"] == 1


def test_insufficient_rows_are_returned_not_dropped() -> None:
    """資料不足的區要留在 rows 裡。

    全市 41% 的行政區落在這一帶。從回傳裡拿掉它們，前端就再也畫不出
    「我們選擇說不知道」這件事——而那正是這個閘門唯一的意義。
    """
    d = json.loads(PAYLOAD.read_text(encoding="utf-8"))
    b = district_board(d["points"], k=100)
    assert b["insufficient"] > 0
    na = [r for r in b["rows"] if r["band"] == "資料不足"]
    assert len(na) == b["insufficient"]
    assert b["ranked"] + b["insufficient"] == len(b["rows"])
    # 排序：資料不足永遠在最後，但在同一個陣列裡。
    assert all(r["band"] == "資料不足" for r in b["rows"][-len(na):])


def test_the_biggest_district_does_not_win_by_being_big() -> None:
    """板橋進榜家數全市最多，名次不該是第一。

    這是「這張榜有沒有在排規模」最直接的檢查。板橋 162 園、13 家進榜都是
    全市最多，型態校正後 SIR 0.87——它應該排在中段。
    """
    d = json.loads(PAYLOAD.read_text(encoding="utf-8"))
    b = district_board(d["points"], k=100)
    rows = [r for r in b["rows"] if r["band"] != "資料不足"]
    most = max(rows, key=lambda r: r["obs"])
    assert most["d"] == "板橋區", "資料換了，這個測試的前提要重看"
    assert most["rank"] > 3, (
        f"進榜家數最多的 {most['d']} 排到第 {most['rank']} 名——"
        "型態校正可能沒生效，這張榜變成在排規模"
    )


def test_expected_parts_add_up_to_the_expected_count() -> None:
    """exp 的分項要加得回 exp 本身。

    下鑽面板把算式攤開給人心算驗證（「公立 10 家 × 0.68% = 0.07 ＋ …」）。
    分項與總數對不起來的話，那個面板就是在說謊，而且沒有人會發現。
    """
    d = json.loads(PAYLOAD.read_text(encoding="utf-8"))
    b = district_board(d["points"], k=100)
    for r in b["rows"]:
        parts = sum(x["exp"] for x in r["exp_parts"])
        assert parts == pytest.approx(r["exp"], abs=0.02), r["d"]
        assert sum(x["n"] for x in r["exp_parts"]) == r["n"], r["d"]


def test_byar_bounds_bracket_the_point_estimate() -> None:
    """下界 ≤ 點估 ≤ 上界，而且 obs=0 時下界是 0 不是負數。

    常態近似在 obs<10 時下界會掉到負的，而多數區的 obs 是個位數
    （鶯歌 6、五股 5）——這就是用 Byar 而不是常態近似的原因。
    """
    for obs in range(0, 30):
        for exp in (0.5, 1.0, 3.7, 14.9):
            lo, hi = _byar(obs, exp)
            assert lo >= 0, (obs, exp)
            assert lo <= obs / exp <= hi, (obs, exp, lo, hi)


def test_capacity_changes_the_board_and_is_reported() -> None:
    """k 是政策數字，換了就該換一張榜，而且回傳裡要說是哪個 k。

    排序對 k 敏感（Spearman 相對 k=100：k=50 是 0.972、k=200 掉到 0.589），
    滑桿把這個敏感度攤開來看。回傳沒帶 k 的話，畫面就沒辦法誠實標示。
    """
    d = json.loads(PAYLOAD.read_text(encoding="utf-8"))
    a = district_board(d["points"], k=50)
    c = district_board(d["points"], k=200)
    assert a["k"] == 50 and c["k"] == 200
    assert a["flagged"] == 50 and c["flagged"] == 200
    assert [r["d"] for r in a["rows"]] != [r["d"] for r in c["rows"]]


def test_the_board_never_calls_a_district_risky() -> None:
    """用詞界線：這是對「我們的派工名單」的陳述，不是對一個地方的陳述。

    29 個行政區是有居民、有園所、有名譽的真實地點，資料完全不支持「某區的
    孩子比較不安全」。`aws-architecture.md` §6 的輸出定位在區級同樣適用。
    """
    d = json.loads(PAYLOAD.read_text(encoding="utf-8"))
    b = district_board(d["points"], k=100)
    blob = json.dumps(b, ensure_ascii=False)
    for bad in ("高風險", "風險最高", "危險", "違規率", "不合格", "低風險"):
        assert bad not in blob, f"回傳裡出現「{bad}」——越過了稽查優先序的用詞界線"
    assert "不是違法認定" in b["caveat"]
    # 「回顧不是預測」這件事要主動講：前 100 名有 94% 帶既有裁罰紀錄。
    assert "預測" in b["caveat"]


def test_levels_refine_the_band_and_never_contradict_it() -> None:
    """五級只能把「與全市相當」拆成略高／略低，不可以改寫 band 本身。

    「明顯」必須對應 95% 區間整段在 1 的同一側；「略」只看點估計。反過來的話
    （例如點估 1.3 但區間跨過 1 卻標成明顯偏高），就是把一個還不能確定的差異
    塗成最深的顏色。
    """
    d = json.loads(PAYLOAD.read_text(encoding="utf-8"))
    b = district_board(d["points"], k=100)
    expect = {"資料不足": {0}, "高於全市": {4}, "低於全市": {1}, "與全市相當": {2, 3}}
    for r in b["rows"]:
        assert r["level"] in expect[r["band"]], (r["d"], r["band"], r["level"])
        assert r["level_label"] == LEVELS[r["level"]]
        if r["level"] == 3:
            assert r["sir"] >= 0.995, r["d"]
        if r["level"] == 2:
            assert r["sir"] <= 1.005, r["d"]
    assert sum(x["n"] for x in b["levels"]) == len(b["rows"])


def test_each_row_lists_exactly_its_flagged_institutions() -> None:
    """每區的 ids 就是地圖清單那一組要列的園，不多不少、依名次排。

    地圖清單直接拿這個分組、不在前端再 filter 一次。兩份 filter 的話，遲早
    會出現「區頭寫 6 家、底下列了 5 家」。
    """
    d = json.loads(PAYLOAD.read_text(encoding="utf-8"))
    b = district_board(d["points"], k=100)
    by_id = {p["i"]: p for p in d["points"]}
    every = [i for r in b["rows"] for i in r["ids"]]
    assert len(every) == len(set(every)) == b["flagged"] == 100
    for r in b["rows"]:
        assert len(r["ids"]) == r["obs"], r["d"]
        ranks = [by_id[i]["r"] for i in r["ids"]]
        assert ranks == sorted(ranks), r["d"]
        assert all(by_id[i]["d"] == r["d"] for i in r["ids"]), r["d"]


def test_the_middle_band_is_split_so_the_map_is_not_one_colour() -> None:
    """k=100 時 17 個有名次的區有 13 個是「與全市相當」。

    只畫三帶的話，地圖底色與清單區頭幾乎是同一個顏色——使用者要的「顏色顯眼、
    看得出嚴重程度」就做不到。拆成五級之後，最多的一級不可以超過六成。
    """
    d = json.loads(PAYLOAD.read_text(encoding="utf-8"))
    b = district_board(d["points"], k=100)
    ranked = [r for r in b["rows"] if r["level"]]
    assert len({r["level"] for r in ranked}) >= 3
    assert max(Counter(r["level"] for r in ranked).values()) <= len(ranked) * 0.6
