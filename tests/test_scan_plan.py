"""掃描計畫：範圍縮小可以省錢，但有一樣東西絕對不能跟著縮。

`build_plan` 是純函式，所以這一檔不碰網路也不碰檔案，用一份自造的假 points。
不讀 dist/data/payload.json 是刻意的：這些不變式不該因為某次重建改變了資料
分布就時好時壞。

最重要的一條在 `test_attribution_pool_never_shrinks_with_scope`。其餘幾條擋的
是同一類錯誤——「範圍」這個控制項在廣掃與逐園兩種管道下意義相反，而空清單與
被擋下來的清單在畫面上長得一模一樣。
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

from smart_watchdog.realtime import plan, pricing

DISTRICTS = ("板橋區", "新莊區", "三重區")


def _points(n: int = 30) -> list:
    """假 points，欄位比照 payload。t=2 私立／t=1 非營利，r 為分數排名。"""
    return [
        {"i": f"id{k:04d}", "full": f"新北市私立第{k}幼兒園",
         "d": DISTRICTS[k % len(DISTRICTS)], "t": 2 if k % 2 else 1,
         "r": k + 1,
         "cf": 1 if k % 5 == 0 else 0, "ch": 1 if k % 10 == 0 else 0,
         "ep": 1 if k % 7 == 0 else 0, "fin": 1 if k % 3 else 0}
        for k in range(n)
    ]


def _every_scope(points: list) -> list:
    """每一個範圍代號，配上足以讓它選出東西的參數。"""
    ids = [p["i"] for p in points[:4]]
    return [
        ("city", {}),
        ("proposal", {"proposal_ids": ids}),
        ("compliance_fail", {}),
        ("evaluation", {}),
        ("top_risk", {"top_n": 5}),
        ("district", {"district": DISTRICTS[0]}),
        ("picked", {"ids": ids}),
    ]


def test_attribution_pool_never_shrinks_with_scope():
    """歸屬池恆為全部機構，不隨掃描範圍縮小。這是正確性前提，不是效能取捨。

    `alerts.attribute()` 的「名稱可對應到 ≥2 所機構就拒絕歸屬」只有在看得見
    全部機構時才成立。只餵子集不是少看見幾筆，是製造誤判——把「板橋幼兒園」
    歸給名單內那一家，而真正被談論的是名單外的另一家。
    """
    points = _points()
    everyone = [p["i"] for p in points]
    for scope, kwargs in _every_scope(points):
        p = plan.build_plan(points, scope=scope,
                            channels=["apify_threads", "news_rss"], **kwargs)
        assert p.attribution_ids == everyone, f"{scope} 縮小了歸屬池"
        assert p.as_dict()["attribution_pool"] == len(points)


def test_narrow_scope_still_narrows_the_targets():
    """歸屬池不縮，但「要去查誰」該縮還是要縮，否則上一條就變成沒有代價的。"""
    points = _points()
    p = plan.build_plan(points, scope="picked", ids=[points[0]["i"]],
                        channels=["news_rss"])
    line = next(ln for ln in p.lines if ln.channel == "news_rss")
    assert line.mode == "per_institution"
    assert line.target_ids == [points[0]["i"]]
    assert len(p.attribution_ids) == len(points)


def test_threads_is_always_a_sweep():
    """逐園 Threads 在型別上就無法表達——不是預設關閉，是沒有那條路徑。

    50 家 × US$0.205 = US$10.25，一次耗盡整個月額度還不夠。所以無論選了多窄的
    範圍，這條線的 mode 恆為 sweep 且 target_ids 恆為空。
    """
    points = _points()
    for scope, kwargs in _every_scope(points):
        p = plan.build_plan(points, scope=scope, channels=["apify_threads"],
                            **kwargs)
        line = next(ln for ln in p.lines if ln.channel == "apify_threads")
        assert line.mode == "sweep", f"{scope} 讓 Threads 變成逐園"
        assert line.target_ids == []
        assert line.as_dict()["targets"] == 0


def test_threads_cost_is_independent_of_scope():
    """廣掃的成本與範圍無關；把它跟逐園混在一起算會讓 UI 講錯話。"""
    points = _points()
    city = plan.build_plan(points, scope="city", channels=["apify_threads"])
    one = plan.build_plan(points, scope="picked", ids=[points[0]["i"]],
                          channels=["apify_threads"])
    assert city.usd_max == one.usd_max == pricing.apify_meter(50).usd_max


def test_per_institution_cap_blocks_instead_of_silently_charging():
    """逐園查詢超過 25 家要擋，而且被擋的那條線目標數必須是 0。

    成本與範圍成正比的管道，一個手滑的範圍就是幾十倍的帳單。擋下來時 target
    必須真的清空，否則 blocker 只是畫面上的一句話，執行路徑照跑。
    """
    points = _points(30)
    ids = {p["i"] for p in points}
    p = plan.build_plan(points, scope="city", channels=["places_reviews"],
                        has_place_id=ids, places_used_this_month=1000)
    line = next(ln for ln in p.lines if ln.channel == "places_reviews")
    assert line.blocker, "30 家超過上限卻沒有 blocker"
    assert str(pricing.PER_INSTITUTION_CAP) in line.blocker
    assert line.target_ids == [] and line.as_dict()["targets"] == 0
    assert p.blockers == [line.blocker]


def test_exactly_at_the_cap_is_allowed():
    """上限是 25 家「以內」可跑。邊界差一家就等於整條功能形同虛設。"""
    points = _points(pricing.PER_INSTITUTION_CAP)
    ids = {p["i"] for p in points}
    p = plan.build_plan(points, scope="city", channels=["places_reviews"],
                        has_place_id=ids, places_used_this_month=1000)
    line = next(ln for ln in p.lines if ln.channel == "places_reviews")
    assert line.blocker == ""
    assert len(line.target_ids) == pricing.PER_INSTITUTION_CAP
    assert p.usd_max == pytest.approx(0.025 * pricing.PER_INSTITUTION_CAP)


def test_wrong_place_id_key_is_a_blocker_not_an_empty_result():
    """鍵對錯時全數落空，看起來會像「這些園都沒有 place_id」——那是假結論。

    payload 的 i 是 UUID 前 8 碼，place_ids_ntpc.csv 存完整 UUID。傳錯的表徵是
    一條靜靜回 0 家的線；必須說出「可能是鍵不一致」，而不是讓空清單被讀成查過了。
    """
    points = _points(10)
    full_uuids = {p["i"] + "-0000-0000-0000-000000000000" for p in points}
    p = plan.build_plan(points, scope="city", channels=["places_reviews"],
                        has_place_id=full_uuids)
    line = next(ln for ln in p.lines if ln.channel == "places_reviews")
    assert line.blocker, "全數落空卻沒有 blocker"
    assert "has_place_id" in line.blocker and "UUID" in line.blocker
    assert line.target_ids == []


def test_no_targets_at_all_is_not_a_place_id_blocker():
    """範圍本來就選不到任何一家時，不該報成鍵不一致——那會把人送去查錯的東西。"""
    points = _points(10)
    p = plan.build_plan(points, scope="picked", ids=["不存在"],
                        channels=["places_reviews"], has_place_id=set())
    line = next(ln for ln in p.lines if ln.channel == "places_reviews")
    assert line.blocker == ""
    assert line.target_ids == []


def test_every_scope_label_states_how_many_institutions():
    """使用者授權的是「掃這幾家」。標籤沒有家數，等於沒有給他確認的依據。"""
    points = _points()
    for scope, kwargs in _every_scope(points):
        targets, label = plan.resolve_scope(points, scope, **kwargs)
        assert str(len(targets)) in label, f"{scope} 的標籤沒有家數：{label}"
        p = plan.build_plan(points, scope=scope, channels=["news_rss"], **kwargs)
        assert p.scope_label == label


def test_blocked_lines_do_not_count_towards_the_ceiling():
    """被擋下的線不會執行，就不能出現在使用者要授權的金額裡。"""
    points = _points(30)
    ids = {p["i"] for p in points}
    blocked = plan.build_plan(points, scope="city",
                              channels=["apify_threads", "places_reviews"],
                              has_place_id=ids, places_used_this_month=1000)
    assert blocked.blockers
    assert blocked.usd_max == pricing.apify_meter(50).usd_max

    # 同樣兩條線，範圍縮到上限以內就會計入——證明上面那個相等不是因為線不見了。
    allowed = plan.build_plan(points, scope="picked",
                              ids=[p["i"] for p in points[:25]],
                              channels=["apify_threads", "places_reviews"],
                              has_place_id=ids, places_used_this_month=1000)
    assert not allowed.blockers
    assert allowed.usd_max > blocked.usd_max
    assert allowed.usd_max == pytest.approx(
        pricing.apify_meter(50).usd_max + 0.025 * 25)


def test_free_channels_switch_to_sweep_when_the_scope_is_wide():
    """逐園的免費管道成本是被限流的風險，那個風險隨家數線性上升。"""
    points = _points(30)
    wide = plan.build_plan(points, scope="city", channels=["news_rss", "ptt"])
    for line in wide.lines:
        assert line.mode == "sweep"
        assert line.target_ids == []

    narrow = plan.build_plan(points, scope="picked",
                             ids=[p["i"] for p in points[:5]],
                             channels=["news_rss", "ptt"])
    for line in narrow.lines:
        assert line.mode == "per_institution"
        assert len(line.target_ids) == 5
    assert narrow.usd_max == 0.0, "免費管道不得產生金額"


def test_keywords_default_and_are_cleaned():
    """空白關鍵字送進 Apify 會變成一次撈全站的查詢，錢照算。"""
    points = _points(3)
    assert plan.build_plan(points, channels=["apify_threads"]).keywords == list(
        plan.DEFAULT_KEYWORDS)
    p = plan.build_plan(points, channels=["apify_threads"],
                        keywords=[" 幼兒園 ", "", "  "])
    assert p.keywords == ["幼兒園"]


def test_a_plan_with_only_free_channels_costs_nothing():
    """免費管道不得產生金額，也不得產生 blocker——估算頁的初始狀態就是這個。"""
    p = plan.build_plan(_points(5), channels=["news_rss", "ptt"])
    assert p.usd_max == 0.0
    assert p.blockers == [] and p.unpriced == []


def test_deselecting_every_channel_plans_nothing():
    """把所有管道都取消勾選，計畫就該是空的。

    前端全部取消勾選時送出的是 `channels: []`；`channels or [...]` 讓空清單
    落回預設值，於是規劃出兩條使用者剛剛明確關掉的線。兩條都免費所以不會多花錢，
    但它們仍會對 news.google.com 與 PTT 送出請求，並產生使用者沒有要的 mentions。
    """
    p = plan.build_plan(_points(5), channels=[])
    assert p.lines == []
    assert p.as_dict()["est_seconds"] == 0.0


def test_every_preset_is_a_usable_keyword_list():
    """關鍵字組是給使用者按的按鈕；空的或沒說明的選項只會製造誤按。"""
    for key, (words, note) in plan.KEYWORD_PRESETS.items():
        assert words and all(w.strip() for w in words), f"{key} 有空關鍵字"
        assert note.strip(), f"{key} 沒有說明"
