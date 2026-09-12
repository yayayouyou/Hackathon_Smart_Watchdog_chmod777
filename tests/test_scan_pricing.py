"""價目表：主控台上那個金額是要拿去授權真的花錢的，所以它必須是對過帳的。

這一檔全部是離線算術，沒有網路、沒有檔案。它鎖住的不是「函式會不會跑」，
而是三件錯了就會直接虧錢或誤導使用者的事：

**單價是對過帳的硬事實，不是文件抄來的。** 三個 Apify 數字（$0.08 / $0.105 /
$0.33）來自本專案帳號的三次實際執行對帳。先前 `COST_PER_RUN_USD = 0.08` 只是
地板價，對一次 100 筆的掃描少報 4.1 倍——會少報的方向正是危險的方向。

**免費額度是額度，不是折扣。** Places 前 1,000 次免費，第 1,001 次起才計費。
把免費額度算進去或算漏，畫面上的數字會差兩個數量級（0 vs 23.975）。

**免費管道的成本不是零，是被限流。** `usd_max=0.0` 不代表可以無限制地掃；
requests 與 est_seconds 才是那條線的真實約束，所以它們必須隨家數變化。
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

from smart_watchdog.realtime import pricing


def test_apify_prices_match_the_three_reconciled_runs():
    """這三個數字是對過帳的事實。改動公式若讓它們變了，是公式錯，不是帳錯。"""
    assert pricing.apify_meter(0).usd_max == 0.08
    assert pricing.apify_meter(10).usd_max == 0.105
    assert pricing.apify_meter(100).usd_max == 0.33


def test_start_fee_is_four_gigabytes_at_the_plan_rate():
    """actor build 把記憶體寫死 4096MB，省錢只能靠換方案，不能靠調記憶體。"""
    assert pricing.apify_start_usd("FREE") == 0.08          # 4GB × $0.02
    assert pricing.apify_start_usd("BRONZE") == 0.02        # 4GB × $0.005
    assert pricing.apify_start_usd() == 0.08                # 未指定＝FREE
    assert pricing.apify_start_usd("bronze") == 0.02        # 方案代號不分大小寫


def test_apify_price_does_not_multiply_by_keyword_count():
    """刻意不乘關鍵字數：max_posts 是否每關鍵字獨立計算未經查證。

    乘上去只是把猜測放大成畫面上的數字。真正的防線是把同一個金額送進 Apify 的
    maxTotalChargeUsd 由供應商硬擋。這條測試防的是「看起來比較保守就乘一下」。
    """
    one = pricing.apify_meter(50, keywords=1)
    many = pricing.apify_meter(50, keywords=8)
    assert one.usd_max == many.usd_max == 0.205
    assert "8 組關鍵字" in many.note, "不乘金額，但必須在說明裡講清楚共用一次執行"


def test_apify_ceiling_is_an_upper_bound_not_a_prediction():
    """筆數越多越貴是線性的；上界的意義由 note 說明，不能被讀成預測值。"""
    assert pricing.apify_meter(100).usd_max - pricing.apify_meter(0).usd_max == pytest.approx(
        pricing.APIFY_ITEM_USD * 100)
    assert "必然 ≤" in pricing.apify_meter(100).note


def test_places_free_tier_is_a_quota_not_a_discount():
    """第 1,000 次以內為 0，額度用完後整批計費——差兩個數量級的分岔點。"""
    assert pricing.places_meter(959).usd_max == 0.0
    assert pricing.places_meter(959, used_this_month=0).free_remaining == 1000
    assert pricing.places_meter(959, used_this_month=1000).usd_max == 23.975
    assert pricing.places_meter(959, used_this_month=1000).free_remaining == 0


def test_places_free_tier_partially_consumed_bills_only_the_overflow():
    """本機計數是唯一知道免費額度用掉多少的東西，扣抵必須逐次生效。"""
    m = pricing.places_meter(100, used_this_month=950)
    assert m.free_remaining == 50
    assert m.usd_max == pytest.approx(0.025 * 50)


def test_rating_only_requests_fall_in_the_cheaper_tier():
    """FieldMask 決定級距：不要評論只要星等走 $0.020，不是 $0.025。"""
    assert pricing.places_meter(100, used_this_month=1000,
                                with_reviews=False).usd_max == 2.0
    assert pricing.places_meter(100, used_this_month=1000,
                               ).usd_max == 2.5
    assert "僅星等" in pricing.places_meter(1, with_reviews=False).label


def test_places_note_says_the_free_counter_is_ours_not_googles():
    """同一把金鑰被別的程式用過，本機計數會低估。UI 不得假裝那是帳單。"""
    assert "非 Google 帳單" in pricing.places_meter(10).note


def test_free_channels_cost_no_money_but_do_cost_requests():
    """usd_max=0.0 不是「隨便掃」。逐園模式下請求數與秒數必須隨家數上升。"""
    small = pricing.free_meter("news_rss", "新聞", 5, sweep=False)
    large = pricing.free_meter("news_rss", "新聞", 40, sweep=False)
    assert small.usd_max == large.usd_max == 0.0
    assert small.requests == 5 and large.requests == 40
    assert large.est_seconds > small.est_seconds

    # PTT 每園 5 次（多個看板），節流值不同——兩條線的成本結構不可共用一個常數。
    ptt = pricing.free_meter("ptt", "PTT", 5, sweep=False)
    assert ptt.requests == 25
    assert ptt.est_seconds == pytest.approx(25 * pricing.THROTTLE_SECONDS["ptt"])


def test_sweep_requests_do_not_grow_with_the_number_of_institutions():
    """廣掃是關鍵字查詢，成本與範圍無關——這正是全市掃描可行的原因。"""
    few = pricing.free_meter("news_rss", "新聞", 3, sweep=True)
    all_of_them = pricing.free_meter("news_rss", "新聞", 1213, sweep=True)
    assert few.requests == all_of_them.requests
    assert few.est_seconds == all_of_them.est_seconds
    assert "成本與範圍無關" in few.note


def test_throttle_values_declare_that_they_are_ours():
    """news 與 PTT 都沒有公告配額。節流值是我方自訂，不可寫成像是官方限制。"""
    note = pricing.free_meter("ptt", "PTT", 3, sweep=False).note
    assert "我方自訂" in note and "無公告配額" in note


def test_unknown_price_is_none_not_zero():
    """待採購管道顯示 US$0 會被讀成免費。留白比編一個數字誠實。"""
    m = pricing.unpriced_meter("vendor_feed", "輿情廠商", "需採購")
    assert m.usd_max is None
    assert m.price_known is False
    assert m.note == "需採購"


def test_caps_are_ordered_and_below_the_provider_ceiling():
    """撞自己的牆是不給跑；撞供應商的牆是跑到一半被砍、錢照付、沒結果。"""
    assert pricing.CAP_PER_RUN_USD < pricing.CAP_PER_DAY_USD < pricing.CAP_PER_MONTH_USD
    assert pricing.CAP_PER_MONTH_USD < 5.0, "月上限必須低於 Apify 的 $5，留餘裕"
