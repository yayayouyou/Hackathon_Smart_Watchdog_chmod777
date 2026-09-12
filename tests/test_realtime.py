"""Real-time channels: permission is a first-class property, not a detail.

The risk in this layer is not a wrong number, it is a system that quietly
breaches a platform's terms on a city government's behalf, or one that shows an
empty panel and lets an operator read it as silence.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.realtime.monitor import coverage_report, watch
from smart_watchdog.realtime.sources import (
    LIVE,
    NEEDS_APPROVAL,
    NEEDS_KEY,
    NEEDS_PROCUREMENT,
    Channel,
    Mention,
    PlacesReviewChannel,
    ThreadsKeywordChannel,
    VendorFeedChannel,
    default_channels,
)

INST = {"id": "a1", "title": "新北市私立吉尼爾幼兒園", "town": "新莊區"}


class _Fake(Channel):
    key = "fake"
    label = "測試管道"
    status = LIVE
    may_store = True

    def __init__(self, mentions=None, boom=False):
        self._m = mentions or []
        self._boom = boom

    def search(self, institution, limit=20):
        del institution, limit
        if self._boom:
            raise RuntimeError("channel down")
        return self._m


def _mention(channel="fake"):
    return Mention(channel=channel, institution_id="a1", headline="測試",
                   url="https://example.invalid", published="2026-09-01",
                   publisher="測試", attribution_basis="測試")


def test_ungated_channels_return_nothing_rather_than_reaching_out():
    """沒有金鑰/核准/採購的管道必須安靜回空，不得嘗試連線。"""
    for ch in (PlacesReviewChannel(), ThreadsKeywordChannel(), VendorFeedChannel()):
        assert ch.search(INST) == []
        assert ch.status != LIVE


def test_supplying_a_credential_flips_status_to_live():
    assert PlacesReviewChannel(api_key="k").status == LIVE
    assert ThreadsKeywordChannel(access_token="t").status == LIVE
    assert VendorFeedChannel(feed_path="/tmp/f.json").status == LIVE


def test_google_reviews_are_display_only():
    """ToS 3.2.3(b) 禁止快取；此管道的結果不得進入任何持久化儲存。"""
    ch = PlacesReviewChannel(api_key="k")
    assert ch.may_store is False


def test_storable_excludes_display_only_channels():
    """即使管道回了資料，display-only 的內容也不得出現在可入庫清單。"""
    class _NoStore(_Fake):
        key = "places_reviews"
        may_store = False

    out = watch(INST, channels=[_NoStore([_mention("places_reviews")])])
    assert len(out["mentions"]) == 1
    assert out["storable"] == []


def test_one_dead_channel_does_not_take_the_panel_down():
    out = watch(INST, channels=[_Fake([_mention()]), _Fake(boom=True)])
    assert len(out["mentions"]) == 1
    assert out["errors"] and "channel down" in out["errors"][0]["error"]


def test_panel_always_reports_how_many_channels_are_watching():
    """空清單若沒說明只有 1/4 管道在看，會被讀成「這所園很安靜」。"""
    channels = default_channels(use_env=False)
    live = [c for c in channels if c.status == LIVE]
    # 不硬編數量：管道會增加，但「面板必須報出幾個在看」這件事不能變
    assert len(channels) > len(live), "應同時存在已啟用與待啟用管道"
    out = watch(INST, channels=[c for c in channels if c.key != "news_rss"
                                and c.key != "ptt"] )
    assert out["channels_total"] == len(channels) - 2
    assert out["channels_live"] == 0
    assert out["mentions"] == []


def test_every_pending_channel_says_how_to_enable_it():
    """待啟用不是死路，是待辦事項——必須寫明由誰去做什麼。"""
    report = coverage_report(default_channels(use_env=False))
    assert report["pending"], "應有待啟用管道"
    for ch in report["pending"]:
        assert ch["how_to_enable"].strip(), f"{ch['key']} 未說明如何啟用"
        assert ch["legal_basis"].strip(), f"{ch['key']} 未說明法律依據"
    assert {c["status"] for c in report["pending"]} <= {
        NEEDS_KEY, NEEDS_APPROVAL, NEEDS_PROCUREMENT}


def test_mentions_carry_no_verdict():
    """未經查證的公開內容不得帶有判定，處置一律為待人工研判。"""
    out = watch(INST, channels=[_Fake([_mention()])])
    assert out["disposition"] == "待人工研判"
    assert "risk" not in out and "score" not in out


def test_credentials_from_env_switch_channels_on():
    """給一把金鑰就是開通管道的唯一步驟——不需要改任何程式。"""
    from smart_watchdog.realtime.sources import LIVE as _LIVE

    # 明確不讀 .env，測試才不會因為開發機上剛好有金鑰而時好時壞
    before = {c.key: c.status for c in default_channels(use_env=False)}
    assert before["places_reviews"] != _LIVE
    after = {c.key: c.status
             for c in default_channels(use_env=False, places_api_key="test-key")}
    assert after["places_reviews"] == _LIVE


def test_every_credential_says_what_it_enables_and_where_to_get_it():
    """缺的是授權不是資料；畫面上必須看得到去哪裡申請。"""
    from smart_watchdog import config

    for c in config.status():
        assert c["enables"].strip(), f"{c['key']} 未說明開啟什麼"
        assert c["how_to_get"].strip(), f"{c['key']} 未說明如何取得"


def test_system_runs_with_no_credentials_at_all():
    """一項憑證都沒有時，仍須有可用的管道——否則就是硬相依。"""
    from smart_watchdog.realtime.sources import LIVE as _LIVE

    live = [c for c in default_channels(use_env=False) if c.status == _LIVE]
    assert len(live) >= 2, "無憑證時應仍有新聞與 PTT 兩個管道"


def test_vendor_feed_reads_the_common_payload_shapes():
    """供應商欄位命名互異；接線要能吃常見形態，而不是要求對方配合我們。"""
    import json
    import pathlib
    import tempfile

    from smart_watchdog.realtime.sources import VendorFeedChannel

    inst = [{"id": "a1", "title": "新北市私立吉尼爾幼兒園", "town": "新莊區"}]
    cases = [
        [{"text": "新莊吉尼爾幼兒園又出事", "url": "u", "timestamp": "2026-09-01"}],
        {"data": [{"content": "新莊吉尼爾幼兒園又出事", "link": "u",
                   "publishedAt": "2026-09-01"}]},
        {"items": [{"title": "新莊吉尼爾幼兒園又出事", "permalink": "u",
                    "created_at": "2026-09-01"}]},
    ]
    for i, payload in enumerate(cases):
        p = pathlib.Path(tempfile.gettempdir()) / f"vendor_case_{i}.json"
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        ch = VendorFeedChannel(feed_path=str(p))
        assert ch.status == LIVE
        mentions = ch.sweep(inst)
        assert len(mentions) == 1, f"case {i}"
        assert mentions[0].institution_id == "a1"


def test_vendor_feed_skips_rows_it_cannot_read_rather_than_guessing():
    """認不出文字欄位就跳過該筆，不要拿其他欄位硬湊。"""
    import json
    import pathlib
    import tempfile

    from smart_watchdog.realtime.sources import VendorFeedChannel

    p = pathlib.Path(tempfile.gettempdir()) / "vendor_unknown.json"
    p.write_text(json.dumps([{"weird_field": "新莊吉尼爾幼兒園"}]), encoding="utf-8")
    ch = VendorFeedChannel(feed_path=str(p))
    assert ch.sweep([{"id": "a1", "title": "新北市私立吉尼爾幼兒園",
                      "town": "新莊區"}]) == []


def test_apify_channel_is_off_without_a_token():
    from smart_watchdog.realtime.apify import ApifyThreadsChannel

    ch = ApifyThreadsChannel()
    assert ch.status != LIVE
    assert ch.search(INST) == []
    assert ch.sweep([INST]) == []
    assert ch.runs_used == 0, "沒有 token 時不得發出任何計費請求"


def test_per_institution_search_is_off_by_default():
    """每次單園查詢都是一次計費執行；介面上隨手點不該燒掉額度。

    FREE 方案每月 US$5、每次執行約 US$0.08，逐園查 1,213 所要 US$97——
    一個月的額度連一輪都跑不完，所以主要用法是 sweep。
    """
    from smart_watchdog.realtime.apify import ApifyThreadsChannel

    ch = ApifyThreadsChannel(token="fake")
    assert ch.status == LIVE
    assert ch.allow_per_institution is False
    assert ch.search(INST) == []
    assert ch.runs_used == 0

    opted_in = ApifyThreadsChannel(token="fake", allow_per_institution=True)
    assert opted_in.allow_per_institution is True


def test_apify_rows_map_onto_mentions_with_strict_attribution():
    """actor 的欄位名稱（text_content／post_url／created_at）要對得上，
    且歸屬仍走 alerts.attribute()——沒點名機構的貼文不得歸屬。"""
    from smart_watchdog.realtime.apify import ApifyThreadsChannel

    ch = ApifyThreadsChannel(token="fake")
    inst = [{"id": "a1", "title": "新北市私立吉尼爾幼兒園", "town": "新莊區"}]
    rows = [
        {"text_content": "新莊吉尼爾幼兒園又上新聞了", "post_url": "https://t/1",
         "created_at": "2026-09-01T10:00:00+00:00", "username": "someone"},
        {"text_content": "今天幼兒園放假", "post_url": "https://t/2",
         "created_at": "2026-09-02T10:00:00+00:00", "username": "other"},
    ]
    mentions = ch._to_mentions(rows, inst, 20)
    assert len(mentions) == 1
    assert mentions[0].institution_id == "a1"
    assert mentions[0].published == "2026-09-01"
    assert mentions[0].publisher == "Threads @someone"


def test_apify_estimated_cost_tracks_runs():
    from smart_watchdog.realtime.apify import COST_PER_RUN_USD, ApifyThreadsChannel

    ch = ApifyThreadsChannel(token="fake")
    assert ch.estimated_cost_usd == 0
    ch.runs_used = 3
    assert ch.estimated_cost_usd == round(3 * COST_PER_RUN_USD, 3)
