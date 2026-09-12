"""社群面板的端點契約：測的重點是**空的時候有沒有把話講清楚**。

這三支端點餵的是一個還沒做好的介面，所以壞掉的方式不會是 500，而是回了一個
形狀正確、意思卻相反的東西：

* 一個空陣列被前端畫成綠燈（「這一園很平靜」），而事實是沒有任何管道在看。
* `mention` 與 `reply` 被加在一起，於是一串十則「+1」變成「十個人向教育局
  反映」——`06-plan` §6 不准的重複加權。
* 歸屬拒配的通報從清單上消失，畫面上就變成從來沒有人通報過。

所以這裡幾乎每一條測試都在問同一件事：**沒有資料的時候，回應說的是「查了，
沒有」還是「沒事」。**

離線保證：payload 與機構主檔就地造、`monitor.watch` 換成假的（不打新聞與
PTT）、`GOOGLE_MAPS_API_KEY` 清空（不打 Places API）、資料庫是 in-memory
SQLite（不碰 `data/runtime/watchdog.sqlite`）。
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from smart_watchdog import config
from smart_watchdog.api import social
from smart_watchdog.api.server import app
from smart_watchdog.db.models import Base
from smart_watchdog.db.session import get_db
from smart_watchdog.realtime import (
    classify,
    mention_store,
    monitor,
    news_classify,
)
from smart_watchdog.report import reply as reply_mod
from smart_watchdog.scrape import threads

WENDE = "00957c83-0061-4581-a587-97629968f371"
GINEER = "a1b2c3d4-0000-0000-0000-000000000001"
#: 一所完全沒有社群訊號的園。整份測試最重要的那一所。
QUIET = "c0ffee00-0000-0000-0000-000000000002"

INSTITUTIONS = [
    {"id": WENDE, "title": "新北市私立文德幼兒園", "town": "蘆洲區"},
    {"id": GINEER, "title": "新北市私立吉尼爾幼兒園", "town": "新莊區"},
    {"id": QUIET, "title": "新北市私立安靜幼兒園", "town": "板橋區"},
]
MASTER = {i["id"][:8]: dict(i) for i in INSTITUTIONS}

PAYLOAD = {
    "points": [{"i": i["id"][:8], "full": i["title"], "d": i["town"]}
               for i in INSTITUTIONS],
    "realtime": {
        "swept_at": "2026-09-08 17:24",
        "channels_live": 2, "channels_total": 7, "channels": [],
        # 建置時那次全市掃描留下的：吉尼爾有一則舊新聞，其他兩園沒有。
        "by_institution": {GINEER[:8]: [{
            "ch": "news_rss", "h": "違反幼照法遭罰 新莊吉尼爾幼兒園罰 30 萬",
            "u": "https://news.example/gineer-1", "d": "2026-04-24",
            "p": "自由時報", "k": "unclear"}]},
    },
}

#: 假的即時結果：只有文德查得到一則新聞。其餘園回空的，那才是常態。
FAKE_LIVE = {WENDE[:8]: [{
    "channel": "news_rss", "institution_id": WENDE[:8],
    "headline": "蘆洲文德幼兒園收費爭議 家長投訴", "url": "https://news.example/wende-1",
    "published": "2026-09-11", "publisher": "測試報", "stored": True,
    "attribution_basis": "標題含機構名稱"}]}

NO_CREDENTIALS = {"places_api_key": None, "threads_token": None,
                  "apify_token": None, "vendor_feed_path": None}


def _page(*rows) -> dict:
    return {"data": list(rows), "paging": {"cursors": {"after": ""}}}


def _row(post_id: str, text: str, stamp: str, *, is_reply: bool = False) -> dict:
    handle = f"user_{post_id.lower()}"
    return {"id": post_id, "text": text, "username": handle, "timestamp": stamp,
            "is_reply": is_reply,
            "permalink": f"https://www.threads.net/@{handle}/post/{post_id}"}


def _seed(db) -> None:
    """走真正的寫入路徑，不手刻資料列。

    `attribution_source` 是 `record_replies()` 算出來的，不是我們填的——
    手刻的話，測到的就只是「我們填了什麼」，而這幾個值正是端點的契約重點。
    """
    mention_store.record(db, threads.parse(_page(
        _row("M1", "@ntpc_watchdog\n文德幼兒園的收費單有問題", "2026-09-08T11:24:51+0000"),
        # 認不出是哪一園：這一則要活著進到 /api/social/unattributed。
        _row("M9", "@ntpc_watchdog\n聽說有一間幼兒園很誇張，老師會罵小孩",
             "2026-09-10T13:47:02+0000"),
    )), INSTITUTIONS)
    mention_store.record_replies(db, "M1", threads.parse(_page(
        _row("R1", "我也遇過 +1", "2026-09-08T12:03:22+0000", is_reply=True),
        # 自身指名了另一家：串是討論的容器，不是主體的容器。
        _row("R2", "新莊的吉尼爾幼兒園也這樣", "2026-09-09T21:30:04+0000", is_reply=True),
        _row("R3", "同感，收據一直要不到", "2026-09-11T09:15:00+0000", is_reply=True),
    )), INSTITUTIONS)


def _fake_watch(institution, channels=None, limit=20):
    """替 `monitor.watch` 的離線替身。回傳形狀與真的一樣，但不連外。"""
    del channels, limit
    return {"mentions": FAKE_LIVE.get(institution.get("id"), []), "errors": []}


@pytest.fixture
def db():
    """全新的 in-memory 資料庫。不碰 data/runtime/watchdog.sqlite。

    `StaticPool` + `check_same_thread=False` 不是裝飾：TestClient 把同步端點丟到
    threadpool 跑，而 SQLite 的連線有 thread affinity，預設的 in-memory 池還會
    每個 thread 給一個**各自空白**的資料庫。少了這兩個設定，端點會在另一個
    thread 上看到一張空表，測出來的「沒有訊號」是假的。
    """
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True,
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    _seed(session)
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def offline(db, monkeypatch, tmp_path):
    """把所有會連外或會讀到真實檔案的東西導開。"""
    monkeypatch.setattr(social, "_payload", lambda: PAYLOAD)
    monkeypatch.setattr(social, "_master", lambda: MASTER)
    monkeypatch.setattr(monitor, "watch", _fake_watch)
    # 新聞標籤的 sidecar 也就地決定，不讀 data/runtime/：讀真的那一份的話，
    # 同一條測試在「跑過分類」與「沒跑過」的機器上會得到相反的結論。
    monkeypatch.setattr(news_classify, "DIR", tmp_path)
    monkeypatch.setattr(news_classify, "PATH", tmp_path / "news_labels.jsonl")
    monkeypatch.setattr(news_classify, "LOCK", tmp_path / "news_labels.lock")
    news_classify.reload()
    # 憑證就地決定，不讀 .env：否則同一條測試在有金鑰與沒金鑰的機器上結論相反。
    monkeypatch.setattr(config, "credentials", lambda: dict(NO_CREDENTIALS))
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "")
    monkeypatch.setattr(social, "PLACE_IDS", pathlib.Path("no-such-place-ids.csv"))
    # 回覆草稿的後端就地決定，不看 .env：否則這幾條測試會在有 AWS 憑證的機器上
    # 真的去打 Bedrock，測出來的東西就不是離線那條路了。
    monkeypatch.setattr(reply_mod, "available", lambda: False)
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield db
    finally:
        app.dependency_overrides.pop(get_db, None)
        news_classify.reload()


@pytest.fixture
def client(offline):
    """不用 context manager：lifespan 會建真的資料表並載入真的 payload。"""
    del offline
    return TestClient(app)


def _one(client, institution_id, **params) -> dict:
    return client.get(f"/api/social/{institution_id}", params=params).json()


# ── 三支端點的基本形狀 ────────────────────────────────────────────────


def test_one_institution_returns_every_block_the_panel_needs(client):
    body = _one(client, WENDE[:8])
    assert set(body) >= {"institution", "sources", "threads", "mentions",
                         "reviews", "counts", "disclaimer"}
    assert body["institution"] == {
        "id": WENDE[:8], "full_id": WENDE,
        "title": "新北市私立文德幼兒園", "town": "蘆洲區",
        # 真實機構的旗標是 false，而且**一定要在**：缺欄位時前端寫的
        # `it.is_demo` 是 undefined，那與 false 在畫面上長得一樣，於是
        # 哪天真的漏掉示範標記也不會有人發現。
        "is_demo": False}
    assert body["disclaimer"]
    for block in ("threads", "mentions", "reviews"):
        # 每一段都要能單獨回答「有沒有訊號」與「為什麼沒有」。
        assert "has_signal" in body[block], block
        assert "reason" in body[block], block


def test_either_id_form_resolves_to_the_same_institution(client):
    """地圖給 8 碼、Threads 通報給完整 UUID，兩種都要認得。

    要前端自己截字串的話，截錯一碼的結果是 404，而 404 在畫面上看起來像
    「這一園沒有資料」。
    """
    assert _one(client, WENDE)["institution"] == _one(client, WENDE[:8])["institution"]


def test_a_demo_institution_opens_a_panel_instead_of_404ing_on_the_reviews_block(
        client, monkeypatch):
    """示範機構不在 payload 上（那是刻意的），面板照樣要開得起來。

    `reviews_of()` 對不在 payload 上的 id 丟 404，而 `_reviews_block()` 原本
    照原樣往外丟——於是第三個區塊會讓**整個面板**變成 404，看起來像這一園
    不存在，而不是「這一段不適用」。示範機構也不對外查新聞與 PTT：拿一個
    虛構園名去查，回來若不是空的，就是把一篇關於真實機構的報導掛到假園名下。
    """
    from smart_watchdog.realtime import demo_data

    inst = demo_data.institutions()[0]
    monkeypatch.setattr(social, "_master",
                        lambda: {**MASTER, inst["id"][:8]: dict(inst)})

    body = _one(client, inst["id"][:8])
    assert body["institution"]["is_demo"] is True
    assert body["demo_note"]
    assert body["reviews"]["available"] is False
    assert "示範機構" in body["reviews"]["reason"]
    assert body["mentions"]["ran"] == []
    assert all("示範機構" in s["reason"] for s in body["mentions"]["skipped"])


def test_an_unknown_institution_is_404_not_an_empty_panel(client):
    """查無此園與這一園沒有聲音是兩件事，不可以長成同一個畫面。"""
    assert client.get("/api/social/zzzzzzzz").status_code == 404


def test_browse_lists_recent_institutions_newest_first(client):
    body = client.get("/api/social").json()
    assert set(body) >= {"count", "items", "sources", "coverage", "disclaimer"}
    ids = [r["institution_id"] for r in body["items"]]
    # 文德最後一則是 R3（09-11），吉尼爾是 R2（09-09）。安靜幼兒園沒有訊號，
    # 不在清單上——這一點由 coverage.note 負責說明。
    assert ids == [WENDE[:8], GINEER[:8]]
    assert body["items"][0]["last_activity"] > body["items"][1]["last_activity"]
    for row in body["items"]:
        assert row["latest"]["permalink"]        # 每一則都要能點回原文
        assert row["title"] and row["town"]


def test_browse_filters_by_town_channel_and_since(client):
    by_town = client.get("/api/social", params={"town": "新莊區"}).json()
    assert [r["institution_id"] for r in by_town["items"]] == [GINEER[:8]]
    # news_rss 只有吉尼爾有（來自建置時的快照）。
    by_channel = client.get("/api/social", params={"channel": "news_rss"}).json()
    assert [r["institution_id"] for r in by_channel["items"]] == [GINEER[:8]]
    since = client.get("/api/social", params={"since": "2026-09-10"}).json()
    assert [r["institution_id"] for r in since["items"]] == [WENDE[:8]]


def test_an_unknown_channel_is_rejected_not_answered_with_an_empty_list(client):
    """打錯管道名稱回空清單的話，看起來會像「這個管道什麼都沒有」。"""
    assert client.get("/api/social", params={"channel": "nope"}).status_code == 400
    assert client.get("/api/social", params={"since": "2026/09/01"}).status_code == 400


# ── 硬規則一：不回分數 ────────────────────────────────────────────────


def test_no_endpoint_returns_a_score_or_a_risk_level(client):
    """社群內容是未查證線索，`06-plan` §1 明訂不併入永久風險分數。

    端點回的是可點回原文的清單，判斷留給人。任何看起來像「幾分」「幾級」的
    欄位都會被前端畫成紅黃綠燈，而那就是違法認定的語氣。
    """
    forbidden = {"score", "risk", "risk_level", "level", "priority", "sentiment",
                 "rank", "grade"}

    def walk(node):
        if isinstance(node, dict):
            assert not (forbidden & set(node)), sorted(forbidden & set(node))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(_one(client, WENDE[:8]))
    walk(client.get("/api/social").json())
    walk(client.get("/api/social/unattributed").json())


def test_counts_carry_the_do_not_add_them_up_note(client):
    body = _one(client, WENDE[:8])
    assert body["counts"]["note"]
    # 沒有跨來源總計欄位：那個數字一旦存在就會被當成「聲量」。
    assert "total" not in body["counts"]


# ── 硬規則二：mention 與 reply 分開 ──────────────────────────────────


def test_mention_and_reply_counts_are_reported_separately(client):
    """一串十則「+1」不是十個人向教育局反映。

    文德這一串：主貼文 M1（有人 @我們）＋ R1／R3 兩則沒指名的附和（繼承歸屬）。
    R2 自身指名了吉尼爾，所以不算文德的。
    """
    counts = _one(client, WENDE[:8])["counts"]["threads"]
    assert counts == {"threads": 1, "mentions": 1, "replies": 2}
    # 合併成一個數字的話就是 3，而「3 個人向教育局反映文德」是假的。
    assert counts["mentions"] != counts["mentions"] + counts["replies"]


def test_browse_counts_threads_not_posts(client):
    """跨機構列表顯示的數量是串數。轉貼同一事件不得重複加權（06-plan §6）。"""
    row = next(r for r in client.get("/api/social").json()["items"]
               if r["institution_id"] == WENDE[:8])
    assert row["counts"]["threads"] == {"threads": 1, "mentions": 1, "replies": 2}


# ── Threads 串狀結構 ─────────────────────────────────────────────────


def test_replies_hang_under_their_root_post(client):
    """回覆必須掛在主貼文底下，不是攤平成三則獨立通報。"""
    items = _one(client, WENDE[:8])["threads"]["items"]
    assert len(items) == 1
    thread = items[0]
    assert thread["root"]["threads_id"] == "M1"
    assert thread["root"]["kind"] == "mention"
    assert thread["root"]["attribution_source"] == "own"
    assert [r["threads_id"] for r in thread["replies"]] == ["R1", "R2", "R3"]
    assert all(r["kind"] == "reply" for r in thread["replies"])
    assert thread["counts"]["posts_in_thread"] == 4
    for post in [thread["root"], *thread["replies"]]:
        assert post["permalink"].startswith("https://")   # 每一則都點得回原文


def test_a_reply_naming_another_institution_is_marked_not_silently_merged(client):
    """串是討論的容器，不是主體的容器。

    在文德那串底下說「吉尼爾也這樣」的那一則，講的就是吉尼爾——把它壓成文德
    的附和，就是把一條新線索改寫成附和。
    """
    thread = _one(client, WENDE[:8])["threads"]["items"][0]
    other = next(r for r in thread["replies"] if r["threads_id"] == "R2")
    assert other["attribution_source"] == "own"
    assert other["institution_id"] == GINEER
    assert other["is_this_institution"] is False
    assert thread["other_institutions"] == [
        {"institution_id": GINEER, "title": "新北市私立吉尼爾幼兒園",
         "is_demo": False, "posts": 1}]
    # 繼承下來的那些照樣標明是繼承的，不是自己掙來的。
    inherited = next(r for r in thread["replies"] if r["threads_id"] == "R1")
    assert inherited["attribution_source"] == "inherited"
    assert inherited["is_this_institution"] is True


def test_the_named_institution_sees_the_thread_it_was_named_in(client):
    """吉尼爾沒有人 @過我們，但有人在別人的串裡指名了它。

    那一串要看得到（複查的人需要上下文），而計數必須是 0 則主貼文、1 則回覆。
    """
    body = _one(client, GINEER[:8])
    assert body["counts"]["threads"] == {"threads": 1, "mentions": 0, "replies": 1}
    thread = body["threads"]["items"][0]
    assert thread["root"]["threads_id"] == "M1"
    assert thread["root"]["is_this_institution"] is False


# ── 硬規則三：零聲量不等於低風險 ─────────────────────────────────────


def test_an_institution_with_no_social_content_says_why_not_just_an_empty_list(client):
    """沒有任何社群內容時，回的必須是「查了，沒有」而不是一個空物件。

    前端拿到空陣列會畫成綠燈或「正常」，而 CLAUDE.md 的界線是：
    不確定時標「資料不足」，不是「低風險」。
    """
    body = _one(client, QUIET[:8])
    assert body["has_signal"] is False
    assert body["reason"]
    for block in ("threads", "mentions", "reviews"):
        assert body[block]["has_signal"] is False, block
        assert body[block]["reason"], block
        assert "無訊號不等於無異常" in body[block]["reason"], block
    assert body["counts"]["threads"] == {"threads": 0, "mentions": 0, "replies": 0}
    # 零要看得見：管道 key 不能因為沒結果就消失，否則前端讀到 undefined。
    assert body["counts"]["mentions"]["news_rss"] == 0
    assert body["counts"]["mentions"]["ptt"] == 0


def test_browse_says_absence_from_the_list_is_not_absence_of_risk(client):
    coverage = client.get("/api/social").json()["coverage"]
    assert coverage["listed"] == 2
    assert coverage["institutions_total"] == len(INSTITUTIONS)
    assert "不代表無異常" in coverage["note"]


def test_a_channel_that_is_not_switched_on_says_so_instead_of_returning_nothing(client):
    """缺的是授權不是資料。管道沒開要說「缺金鑰」，不是靜靜回空陣列。

    `sources` 沿用 `Channel.describe()`，所以畫面上列的就是系統真正在看的那些。
    """
    sources = _one(client, WENDE[:8])["sources"]
    keys = {s["key"] for s in sources}
    assert {"news_rss", "ptt", "threads_mentions", "places_reviews",
            "apify_threads", "threads", "vendor_feed"} <= keys
    for source in sources:
        assert set(source) >= {"key", "label", "status", "legal_basis",
                               "may_store", "available", "reason", "feeds",
                               "how_to_enable"}
        assert source["available"] == (source["status"] == "live")
        # 沒開通的一定要說出為什麼；開通的不需要理由。
        assert bool(source["reason"]) != bool(source["available"])
    pending = next(s for s in sources if s["key"] == "threads_mentions")
    assert pending["available"] is False
    assert "缺金鑰" in pending["reason"]
    assert pending["how_to_enable"]


def test_stored_reports_survive_the_channel_being_switched_off(client):
    """管道沒開，不代表庫裡沒有先前同步進來的通報。

    `available` 講的是「現在還在不在收」，`has_signal` 講的是「手上有沒有東西」。
    兩者混成一個欄位，就會有一邊被誤讀。
    """
    block = _one(client, WENDE[:8])["threads"]
    assert block["available"] is False
    assert block["has_signal"] is True
    assert block["reason"] == ""


# ── 硬規則四：Google 評論 ────────────────────────────────────────────


def test_missing_google_key_is_a_reason_not_a_500(client):
    body = _one(client, WENDE[:8])["reviews"]
    assert body["available"] is False
    assert "GOOGLE_MAPS_API_KEY" in body["reason"]
    assert body["reviews"] == []
    assert body["has_signal"] is False
    # 定位那句話即使在取不到的時候也要在：它是這個來源的意義，不是成功路徑的裝飾。
    assert "非法遵指標" in body["note"]
    assert body["may_store"] is False


def test_reviews_endpoint_and_panel_share_one_implementation(client):
    """各寫一份的那天，就是卷宗與面板對同一家園講出不同星等的那天。"""
    from smart_watchdog.api import server

    direct = client.get(f"/api/reviews/{WENDE[:8]}").json()
    panel = _one(client, WENDE[:8])["reviews"]
    assert direct["available"] is False and panel["available"] is False
    # 面板在後面補了一句「無訊號不等於無異常」，其餘原樣沿用。
    assert panel["reason"].startswith(direct["reason"])
    assert server.reviews.__doc__ and "social" in server.reviews.__doc__


# ── 歸屬拒配佇列 ─────────────────────────────────────────────────────


def test_unattributed_reports_are_a_queue_not_a_bin(client):
    """認不出是哪一園的通報要留著，而且要帶原文——認園只能由讀過原文的人做。"""
    body = client.get("/api/social/unattributed").json()
    assert body["count"] == 1
    item = body["items"][0]
    assert item["threads_id"] == "M9"
    assert "老師會罵小孩" in item["text"]          # 原文，不是摘要
    assert item["permalink"].startswith("https://")
    assert item["attribution_basis"]               # 為什麼拒配
    assert item["kind"] == "mention"
    assert "不是「與機構無關」" in body["note"]


def test_attributed_reports_do_not_leak_into_the_unattributed_queue(client):
    ids = {i["threads_id"] for i in client.get("/api/social/unattributed").json()["items"]}
    assert "M1" not in ids and "R1" not in ids


def test_the_unattributed_filter_runs_in_sql_not_after_the_limit(db):
    """先 LIMIT 再過濾的話，佇列會無聲地少掉一截。

    庫裡最新的那幾列都是已歸屬的，所以 limit=1 時「取一列再過濾」會回空的，
    而正確答案是那一則拒配的通報。
    """
    rows = mention_store.recent(db, limit=1, unattributed=True)
    assert [r["threads_id"] for r in rows] == ["M9"]


# ── 即時與快照 ───────────────────────────────────────────────────────


def test_live_and_snapshot_mentions_are_distinguishable(client):
    """「今天查到的」與「上次掃描查到的」是兩件事。"""
    wende = _one(client, WENDE[:8])["mentions"]
    assert wende["has_signal"] is True
    assert [i["source"] for i in wende["items"]] == ["live"]
    assert wende["counts"]["news_rss"] == 1
    assert wende["snapshot_swept_at"] == "2026-09-08 17:24"

    gineer = _one(client, GINEER[:8])["mentions"]
    assert [i["source"] for i in gineer["items"]] == ["snapshot"]
    assert gineer["items"][0]["url"] == "https://news.example/gineer-1"
    # 快照沒有保留歸屬依據；null 是「當時沒記下來」，不是「沒有依據」。
    assert gineer["items"][0]["attribution_basis"] is None


def test_live_false_skips_the_outbound_query_and_says_it_did(client):
    body = _one(client, WENDE[:8], live="false")["mentions"]
    assert body["ran"] == []
    assert body["available"] is False
    assert all(s["reason"] for s in body["skipped"])


# ── 語氣分類：上色在貼文，不在機構 ────────────────────────────────────


def _classify_seeded(db, mapping) -> None:
    """替已入庫的幾則貼上分類。走真正的寫入路徑，不手刻資料列。"""
    from smart_watchdog.db.models import ThreadsMention

    for threads_id, payload in mapping.items():
        row = db.query(ThreadsMention).filter_by(threads_id=threads_id).one()
        classify.apply(row, classify.from_payload(payload, backend="test"))
    db.commit()


NEGATIVE = {"event_category": "財務收費", "tone": "negative",
            "specificity": "specific", "stance": "first_hand",
            "contains_minor_identifiers": False, "issues": []}
QUESTION = {**NEGATIVE, "tone": "question", "specificity": "vague"}
NEUTRAL = {**NEGATIVE, "tone": "neutral"}


def test_each_post_carries_its_own_closed_labels(client, db):
    """分類跟著**貼文**走。機構那一層拿不到任何一個語氣欄位。"""
    _classify_seeded(db, {"M1": NEGATIVE})
    body = _one(client, WENDE[:8])["threads"]
    root = body["items"][0]["root"]
    assert root["tone"] == "negative"
    assert root["tone_bucket"] == "negative"
    assert root["tone_label"] == "語氣負面"
    assert root["event_category"] == "財務收費"
    assert root["classified_at"]
    # 模型寫的自由文字不出現在 API 上。
    assert "issues" not in root


def test_an_unclassified_post_says_so_instead_of_looking_neutral(client, db):
    """`tone` 為 null 的那些要回 `unclassified`，不是 `neutral`。

    前端若自己寫 `tone || "neutral"`，一批沒有人看過的貼文會一次變成中性。
    所以這個判斷在後端做一次。
    """
    body = _one(client, WENDE[:8])["threads"]
    root = body["items"][0]["root"]
    assert root["tone"] is None and root["classified_at"] is None
    assert root["tone_bucket"] == "unclassified"
    assert root["tone_label"] == "未分類"
    del db


def test_the_institution_row_gets_a_composition_not_one_colour(client, db):
    """「3 則語氣負面 · 1 則中性」——機構那一層沒有單一語氣欄位，也不會有。"""
    _classify_seeded(db, {"M1": NEGATIVE, "R1": NEGATIVE, "R2": QUESTION,
                          "R3": NEUTRAL})
    row = next(i for i in client.get("/api/social").json()["items"]
               if i["full_id"] == WENDE)
    tone = row["tone"]
    assert tone["counts"]["negative"] == 2
    assert tone["counts"]["neutral"] == 1
    assert tone["counts"]["unclassified"] == 0
    assert tone["labels"]["negative"] == "語氣負面"
    # 沒有任何一個「這一園的語氣是什麼」的欄位，counts 也還是純計數。
    assert row["counts"]["threads"] == {"threads": 1, "mentions": 1, "replies": 2}
    assert not {"sentiment", "dominant_tone", "risk", "level"} & set(row)
    assert "dominant" not in tone and "score" not in tone


def test_the_composition_keeps_unclassified_visible(client, db):
    """未分類自成一項。把它藏起來，分母就少了一截。"""
    _classify_seeded(db, {"M1": NEGATIVE})
    tone = _one(client, WENDE[:8])["threads"]["tone"]
    assert tone["counts"]["negative"] == 1
    assert tone["counts"]["unclassified"] >= 1
    assert tone["unclassified"] == tone["counts"]["unclassified"]
    assert "未分類" in tone["note"]


def test_the_tone_note_forbids_colouring_the_whole_institution(client):
    body = _one(client, WENDE[:8])
    note = body["threads"]["tone_note"]
    assert "不是對機構的評價" in note
    assert client.get("/api/social").json()["tone_note"] == note


# ── 擬定回覆 ──────────────────────────────────────────────────────────


def _draft(client, institution_id, root, **body):
    return client.post(f"/api/social/{institution_id}/draft-reply",
                       json={"root_threads_id": root, **body})


def test_a_draft_reply_is_text_only_and_never_sent(client):
    r = _draft(client, WENDE[:8], "M1")
    assert r.status_code == 200
    body = r.json()
    assert body["auto_send"] is False
    assert body["verified"] is True, body["problems"]
    assert body["backend"] == "template"      # 離線沒有憑證時的地板
    assert "不會自動送出" in body["draft"]
    assert body["posts_used"] == 4            # 主貼文 + 三則回覆


def test_the_draft_carries_the_permalink_and_the_unverified_declaration(client):
    body = _draft(client, WENDE[:8], "M1").json()
    assert body["permalink"]
    assert body["permalink"] in body["draft"]
    assert "未經查證" in body["draft"]
    assert "非違法認定" in body["draft"]
    assert body["checklist"]


def test_the_sendable_half_is_handed_over_separately(client):
    """前端不自己切字串：切錯會把園名連同「這是未查證的通報」一起貼出去。"""
    body = _draft(client, WENDE[:8], "M1").json()
    assert body["sendable"]
    assert body["sendable_mark"] in body["draft"]
    assert body["draft"].endswith(body["sendable"])
    assert "文德幼兒園" in body["draft"]
    assert "文德幼兒園" not in body["sendable"]


def test_drafting_for_a_thread_that_belongs_to_another_institution_is_refused(client):
    """從 A 園的面板去回 B 園的串，會讓草稿宣稱一個沒有人做出來的歸屬。"""
    r = _draft(client, QUIET[:8], "M1")
    assert r.status_code == 409
    assert "文德" in r.json()["detail"]


def test_drafting_for_a_thread_that_is_not_in_the_store_is_404(client):
    assert _draft(client, WENDE[:8], "NOPE").status_code == 404


def test_an_unknown_institution_cannot_be_drafted_for(client):
    assert _draft(client, "zzzzzzzz", "M1").status_code == 404


# ── 新聞／PTT 的標籤：與 Threads 的語氣**不共用、不合併** ────────────


def _label(url: str, headline: str, kind: str, category: str,
           channel: str = "news_rss") -> None:
    """把一筆標籤寫進（被導到 tmp_path 的）sidecar，走真正的寫入路徑。"""
    news_classify.record(
        {"channel": channel, "url": url, "headline": headline},
        news_classify.NewsLabel(report_kind=kind, event_category=category,
                                backend="fake"))


def test_news_items_carry_their_own_labels_not_a_tone(client):
    """新聞那一則帶的是 `report_kind`，不是 `tone`。

    「教育局開罰 39 萬」是一件**已經作成的官方行動**被報導出來，不是語氣負面。
    共用同一個欄位的那天，就是稽查員分不出「有人抱怨」與「已經起訴」的那天。
    """
    _label("https://news.example/wende-1", "蘆洲文德幼兒園收費爭議 家長投訴",
           "爭議未定", "財務收費")
    item = _one(client, WENDE[:8])["mentions"]["items"][0]
    assert item["report_kind"] == "爭議未定"
    assert item["event_category"] == "財務收費"
    assert item["report_bucket"] == "爭議未定"
    assert item["report_label"] == "爭議未定"
    assert item["news_classified_at"]
    # 語氣那一族的欄位不得出現在新聞項目上。
    assert "tone" not in item and "tone_bucket" not in item


def test_the_keyword_verdict_is_kept_next_to_the_model_verdict(client):
    """`kind`（關鍵字表）與 `report_kind`（模型）是兩個欄位，不互相覆蓋。

    兩者不一致的那幾則正是最該由人看一眼的——合併成一欄就再也分不出來。
    """
    _label("https://news.example/gineer-1",
           "違反幼照法遭罰 新莊吉尼爾幼兒園罰 30 萬", "事件報導", "兒少安全")
    item = _one(client, GINEER[:8])["mentions"]["items"][0]
    assert item["kind"] == "unclear"        # 快照當時關鍵字表判的
    assert item["report_kind"] == "事件報導"  # 模型判的


def test_an_unlabelled_news_item_reads_as_unclassified_not_as_routine(client):
    """sidecar 沒有這一則時，畫面上要說「未分類」，不是「例行報導」。

    這是 §7.7 的第二個形態：我們沒看過的東西，畫面上不可以長得像我們看過而且
    沒事。沒有 Bedrock 憑證的機器上，整份 sidecar 就是空的。
    """
    body = _one(client, WENDE[:8])
    item = body["mentions"]["items"][0]
    assert item["report_kind"] is None
    assert item["report_bucket"] == "unclassified"
    assert item["report_label"] == "未分類"
    labels = body["mentions"]["labels"]
    assert labels["classified"] == 0 and labels["unclassified"] == 1
    assert labels["counts"]["例行報導"] == 0


def test_the_news_composition_is_separate_from_the_tone_composition(client):
    """機構那一列的兩個組成是兩個欄位，計數永遠不合併。

    那邊數的是未查證的民眾陳述，這邊數的是已經見報的報導。加起來就是把兩者
    數成同一類——`06-plan` §6 不准的重複加權的另一個形態。
    """
    _label("https://news.example/gineer-1",
           "違反幼照法遭罰 新莊吉尼爾幼兒園罰 30 萬", "事件報導", "兒少安全")
    rows = {r["institution_id"]: r for r in client.get("/api/social").json()["items"]}
    gineer = rows[GINEER[:8]]
    assert gineer["news"]["counts"]["事件報導"] == 1
    assert gineer["tone"]["counts"]["negative"] == 0
    # 兩份 counts 的 key 只在「不知道」那兩個上重疊，其餘完全不同。
    shared = set(gineer["tone"]["counts"]) & set(gineer["news"]["counts"])
    assert shared == {"unclear", "unclassified"}
    assert "事件報導" not in gineer["tone"]["counts"]


def test_both_endpoints_say_the_two_label_families_do_not_merge(client):
    """話寫在後端，前端照印。彙總時被換掉一次，這條界線就沒了。"""
    for note in (_one(client, WENDE[:8])["mentions"]["label_note"],
                 client.get("/api/social").json()["label_note"]):
        assert "不共用" in note
        assert "合併計數" in note or "不合併" in note


def test_the_news_labels_add_no_score_and_no_tone_field(client):
    """新增欄位不得把一個分數或一個語氣偷渡進來。

    `test_no_endpoint_returns_a_score_or_a_risk_level` 擋的是名字；這一條擋的
    是值域：新聞的標籤裡不得出現 `negative` 這種語氣值。
    """
    _label("https://news.example/gineer-1",
           "違反幼照法遭罰 新莊吉尼爾幼兒園罰 30 萬", "事件報導", "兒少安全")
    body = _one(client, GINEER[:8])
    # 只看資料本身，不看 `label_note`——那句話正是在解釋兩族不共用，它**必須**
    # 提到「語氣」才講得清楚這件事。
    blob = str(body["mentions"]["items"]) + str(body["mentions"]["labels"]["counts"])
    for banned in ("negative", "sentiment", "語氣", "情緒"):
        assert banned not in blob, f"新聞那一段的資料不得出現 {banned}"
