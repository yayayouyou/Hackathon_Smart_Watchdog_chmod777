"""價目表：唯一寫著「一次掃描要花多少錢」的地方。

估算與實際執行共用這一份，所以主控台顯示的數字與排程腳本扣的錢不可能不一致。

**單價經帳務查證，不是照抄文件。** Apify 於 2026-07-02 起改為 PAY_PER_EVENT：

    啟動費 $0.02/GB（FREE）或 $0.005/GB（BRONZE 以上）× ceil(記憶體GB)
  + 每筆寫進 dataset 的資料 $0.0025

actor 的 build 把 ``minMemoryMbytes`` 與 ``maxMemoryMbytes`` 都寫死 4096，
`memory` 參數沒有下修空間，因此 FREE 方案的啟動費固定 4 × $0.02 = $0.08。
對帳（本專案帳號實際三次執行）：

    回傳   0 筆 → $0.08     = 0.08 + 0 × 0.0025
    回傳  10 筆 → $0.105    = 0.08 + 10 × 0.0025
    回傳 100 筆 → $0.33     = 0.08 + 100 × 0.0025

先前 ``apify.COST_PER_RUN_USD = 0.08`` 只是地板價，對一次 100 筆的掃描少報 4.1 倍。

Google Places 依 FieldMask 落在最高級距計價（不是逐級加總）。含 ``reviews``
即 Enterprise + Atmosphere，$25/千次，**每月前 1,000 次免費**；只要星等不要
評論則是 Enterprise $20/千次，走另一個免費桶。

免費管道回傳的是請求次數與預估秒數而不是金額。它們的成本不是錢，是被限流——
而 news.google.com 與 PTT 都沒有公告配額，被擋的表徵是空結果不是錯誤，
所以節流值標明是我方自訂、無官方依據可循。
"""

from __future__ import annotations

import dataclasses
import math

#: Apify 啟動事件單價（每 GB）。方案代號取自 GET /v2/users/me 的 plan.id。
APIFY_START_USD_PER_GB = {"FREE": 0.02}
APIFY_START_USD_PER_GB_PAID = 0.005
APIFY_ITEM_USD = 0.0025
#: actor build 寫死 min=max=4096MB，無法下修——這條省錢路已確認封死。
APIFY_MEMORY_MB = 4096
#: actor 單次執行的筆數上限。估算若不夾在這個範圍內，會算出永遠不可能發生的
#: 金額——max_posts=9999 估成 US$25，實際執行只抓 100 篇、花 US$0.33，
#: 然後被單次上限擋下，使用者被告知不能做一件其實做得到的事。
APIFY_MAX_POSTS = 100

#: Places 各 FieldMask 落點的單價與每月免費次數（免費額度各級距獨立，不共用）。
PLACES_WITH_REVIEWS = ("places_reviews", 0.025, 1000)
PLACES_RATING_ONLY = ("places_rating", 0.020, 1000)

#: 我方自訂的請求節流與每日上限。這兩個端點都沒有公告配額。
THROTTLE_SECONDS = {"news_rss": 1.5, "ptt": 1.0}
REQUESTS_PER_INSTITUTION = {"news_rss": 1, "ptt": 5}
DAILY_REQUEST_CAP = {"news_rss": 200, "ptt": 300}

#: 逐園模式的硬上限。免費管道的成本是被限流的風險，那個風險隨家數線性上升。
PER_INSTITUTION_CAP = 25

#: 三道我方上限。月上限刻意低於 Apify 的 $5，留餘裕給開卷宗的 Google 呼叫——
#: 撞自己的牆是「不給跑」，撞供應商的牆是「跑到一半被砍、錢照付、沒結果」。
CAP_PER_RUN_USD = 0.50
CAP_PER_DAY_USD = 1.00
CAP_PER_MONTH_USD = 4.00


@dataclasses.dataclass(frozen=True)
class Meter:
    """一個管道在一次掃描裡的預估用量。

    ``usd_max`` 是**上界**不是預測值：Apify 按實際寫進 dataset 的筆數計費，
    回傳少於要求的筆數就付得比較少。實付由 ``settle()`` 以供應商自報金額校正。
    """

    channel: str
    label: str
    usd_max: float | None
    requests: int
    est_seconds: float
    free_remaining: int | None = None
    price_known: bool = True
    formula: str = ""
    note: str = ""

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def clamp_posts(n: int) -> int:
    """夾到 actor 真的做得到的範圍。估算必須是可能發生的金額。

    下限是 0 而不是 1：對帳事實裡「回傳 0 筆 → US$0.08」是啟動費本身，
    夾成 1 會讓那個基準點變成 US$0.0825，與帳單對不上。
    """
    return max(0, min(int(n), APIFY_MAX_POSTS))


def apify_start_usd(plan: str | None = None) -> float:
    """啟動費。FREE 方案 $0.02/GB，付費方案 $0.005/GB。"""
    gb = math.ceil(APIFY_MEMORY_MB / 1024)
    per_gb = APIFY_START_USD_PER_GB.get(
        (plan or "FREE").upper(), APIFY_START_USD_PER_GB_PAID)
    return round(gb * per_gb, 4)


def apify_meter(max_posts: int, *, plan: str | None = None,
                keywords: int = 1) -> Meter:
    """一次 Apify 執行的成本上界。

    刻意**不乘關鍵字數**：`max_posts` 是否為每關鍵字獨立計算並未經查證，
    乘上去只是把猜測放大成畫面上的數字。真正的防線是把同一個金額送進
    Apify 的 ``maxTotalChargeUsd``，由供應商自己擋——那是硬的，猜測不是。
    """
    start = apify_start_usd(plan)
    posts = clamp_posts(max_posts)
    usd = round(start + APIFY_ITEM_USD * posts, 4)
    return Meter(
        channel="apify_threads", label="Threads（Apify）", usd_max=usd,
        requests=1, est_seconds=45.0 + 0.6 * posts,
        formula=f"{start} 啟動 + {APIFY_ITEM_USD} × {posts} 筆",
        note=f"{keywords} 組關鍵字共用一次執行；實付按實際回傳筆數，"
             f"必然 ≤ US${usd}",
    )


def places_meter(n: int, *, used_this_month: int = 0,
                 with_reviews: bool = True) -> Meter:
    """逐園查 Google 地圖評論的成本上界，扣掉當月剩餘免費額度。"""
    _key, unit, free = PLACES_WITH_REVIEWS if with_reviews else PLACES_RATING_ONLY
    remaining_free = max(0, free - used_this_month)
    billable = max(0, n - remaining_free)
    return Meter(
        channel="places_reviews",
        label="Google 地圖評論" + ("（含評論）" if with_reviews else "（僅星等）"),
        usd_max=round(unit * billable, 4), requests=n,
        est_seconds=0.35 * n, free_remaining=remaining_free,
        formula=f"{unit} × max(0, {n} − 剩餘免費 {remaining_free}) = {billable} 次計費",
        note="免費額度為本機計數，非 Google 帳單。同一把金鑰若被其他程式使用，"
             "本機計數會低估。",
    )


def free_meter(channel: str, label: str, n_institutions: int,
               *, sweep: bool) -> Meter:
    """免費管道：回請求次數與預估秒數，不回金額。"""
    if sweep:
        requests = 5
        seconds = 8.0 if channel == "news_rss" else 0.5
        note = "廣掃：固定幾次查詢，成本與範圍無關"
    else:
        per = REQUESTS_PER_INSTITUTION.get(channel, 1)
        requests = per * n_institutions
        seconds = requests * THROTTLE_SECONDS.get(channel, 1.0)
        note = (f"逐園：{per} 次/園 × {n_institutions} 園，"
                f"節流 {THROTTLE_SECONDS.get(channel, 1.0)} 秒/次"
                "（我方自訂，該端點無公告配額可依據）")
    return Meter(channel=channel, label=label, usd_max=0.0, requests=requests,
                 est_seconds=seconds, formula="無金錢成本", note=note)


def unpriced_meter(channel: str, label: str, reason: str) -> Meter:
    """價格未知的管道。``usd_max=None`` 不是 0——顯示編造的數字比留白更糟。"""
    return Meter(channel=channel, label=label, usd_max=None, requests=0,
                 est_seconds=0.0, price_known=False, note=reason)
