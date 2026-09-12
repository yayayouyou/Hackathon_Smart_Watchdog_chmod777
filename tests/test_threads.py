"""Threads @標註：來歷不明的就不收，認不出來的照樣留著。

兩件事在這裡被釘住，因為兩件事都會在無聲無息中出錯：

* **取用端**要能拒絕——沒有 id、沒有時間、時間看不懂、主機不對的，一律不進來。
  那個請求帶著 Authorization header，主機檢查錯一次就是把權杖送給別人。
* **入庫端**不能拒絕過頭——歸屬不上的通報必須留在庫裡，否則畫面上會變成
  從來沒有人通報過。

串下的回覆多釘兩件事：

* **讀不到**與**沒有人回覆**是兩種結果。兩者在呼叫端都長成「沒有回覆進來」，
  而只有後者是關於公眾的事實。
* **回覆的主體不一定是那一串的主體。** 沒指名的繼承，指名了另一家的就算另一
  家；哪一種算出來的記在 `attribution_source`，不是靠讀 basis 的字串猜。

`tests/fixtures/threads_mentions.json` 與 `threads_replies.json` 同時是決賽離線
demo 的資料來源，所以它們的行為也在這裡測：demo 當天跑出什麼，測試裡就先跑過
什麼。
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.db.models import Base, ThreadsMention
from smart_watchdog.realtime import mention_store
from smart_watchdog.scrape import threads

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "threads_mentions.json"
REPLIES = pathlib.Path(__file__).resolve().parent / "fixtures" / "threads_replies.json"
#: fixture 裡那則可歸屬的主貼文（蘆洲文德），與它底下那一串。
ROOT = "17849251066204813"
WENDE = "00957c83-0061-4581-a587-97629968f371"
GINEER = "a1"                           # 回覆裡指名的另一所園（新莊吉尼爾）
NO_NAME_ROOT = "17851990341270562"      # 跨縣市規則拒配的那則主貼文

# 歸屬用的機構清單就地造，不讀 CSV：測的是規則，不是那份檔案今天長什麼樣。
# 文德有兩所同名（私立文德在蘆洲、板橋區文德國小附幼），兩所都放進來，
# 才測得到「文德幼兒園」只會命中前者。
INSTITUTIONS = [
    {"id": "00957c83-0061-4581-a587-97629968f371",
     "title": "新北市私立文德幼兒園", "town": "蘆洲區"},
    {"id": "b7e480b2-8345-4775-b44f-32a3ec85fb80",
     "title": "新北市板橋區文德國民小學附設幼兒園", "town": "板橋區"},
    {"id": "a1", "title": "新北市私立吉尼爾幼兒園", "town": "新莊區"},
]


def _payload(*rows) -> dict:
    return {"data": list(rows), "paging": {"cursors": {"after": ""}}}


def _row(**kwargs) -> dict:
    row = {"id": "1", "text": "測試", "username": "u",
           "timestamp": "2026-09-08T11:24:51+0000",
           "permalink": "https://www.threads.net/@u/post/A", "is_reply": False}
    row.update(kwargs)
    return {k: v for k, v in row.items() if v is not None}


def _db():
    """全新的 in-memory 資料庫。不碰 data/runtime/watchdog.sqlite。"""
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)()


# ── 取用：拒絕的那一半 ──────────────────────────────────────────────


def test_a_mention_without_an_id_or_a_timestamp_is_dropped_not_repaired():
    """沒有 id 就無法去重、沒有時間就無法判斷新舊，補一個假的就看不出差別了。"""
    posts = threads.parse(_payload(
        _row(id="ok"),
        _row(id=None, text="沒有平台 id"),
        _row(id="no-time", timestamp=None),
    ))
    assert [p.threads_id for p in posts] == ["ok"]


def test_not_before_drops_the_mention_history_that_predates_the_token():
    """授權那一刻才開始收。不設 cutoff 的話首次同步會把歷年標註全當成新通報。"""
    import datetime as dt

    limit = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
    posts = threads.parse(_payload(
        _row(id="new", timestamp="2026-09-08T11:24:51+0000"),
        _row(id="old", timestamp="2024-03-02T05:00:00+0000"),
    ), not_before=limit)
    assert [p.threads_id for p in posts] == ["new"]


def test_an_unreadable_timestamp_counts_as_older_than_the_cutoff_never_newer():
    """看不清就不收，不猜。

    反過來（看不懂就當成新的）會讓一則年代不明的貼文以今天的身分進到收件匣，
    而且沒有任何欄位能事後把它認出來。與 CLAUDE.md「看不清就填 null，絕不猜」
    同一條。
    """
    import datetime as dt

    limit = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
    posts = threads.parse(
        _payload(_row(id="junk-time", timestamp="上週三")), not_before=limit)
    assert posts == []


def test_a_malformed_cutoff_raises_instead_of_quietly_importing_everything(monkeypatch):
    """設錯格式的 THREADS_MENTIONS_NOT_BEFORE 若被當成「沒設」，就等於沒有 cutoff。"""
    monkeypatch.setenv("THREADS_MENTIONS_NOT_BEFORE", "2026/09/01")
    with pytest.raises(threads.ThreadsError):
        threads.cutoff()


def test_content_hash_pins_the_version_we_saw_so_an_upstream_edit_shows_up():
    same = threads.parse(_payload(_row(id="a", text="超收"), _row(id="b", text="超收")))
    edited = threads.parse(_payload(_row(id="a", text="超收（已更正）")))
    assert same[0].content_hash == same[1].content_hash
    assert edited[0].content_hash != same[0].content_hash


def test_a_reply_that_happens_to_mention_us_is_not_addressed_to_us():
    """主貼文是寄給我們的通報；回覆是別人對話裡順手帶到我們的一句話。"""
    root, reply = threads.parse(_payload(
        _row(id="root", is_reply=False), _row(id="reply", is_reply=True)))
    assert root.addressed_to_us
    assert not reply.addressed_to_us


# ── 取用：連出去的那一半 ────────────────────────────────────────────


def test_the_request_url_never_carries_the_token_because_urls_get_logged():
    url = threads.mentions_url(after="CURSOR")
    assert url.startswith("https://graph.threads.net/me/mentions?")
    assert "access_token" not in url
    assert "Bearer" not in url


def test_fetch_refuses_any_host_but_the_api_because_the_request_carries_a_bearer():
    """主機檢查在這裡比在 ptt.fetch 重要一個量級：送錯就是把權杖送給對方。"""
    for url in ("https://graph.threads.net.attacker.example/me/mentions",
                "https://example.invalid/me/mentions",
                "http://graph.threads.net/me/mentions"):
        with pytest.raises(threads.ThreadsError):
            threads.fetch(url, "a-real-looking-token")


# ── 歸屬：只吃第一行 ────────────────────────────────────────────────


def test_headline_skips_the_mention_line_and_the_hashtag_line():
    """@標註與純 hashtag 不帶資訊，但會把 alerts.attribute() 要的那一行擠掉。"""
    body = "蘆洲的文德幼兒園多收教材費"
    assert mention_store._headline(f"@ntpc_watchdog\n#教保通報\n{body}") == body
    # 「@標註 後面就接正文」是常見寫法，那一行有內容，不能跳過。
    assert mention_store._headline(
        "@ntpc_watchdog 想問延托費用") == "@ntpc_watchdog 想問延托費用"


def test_the_demo_fixture_attributes_the_post_that_names_the_kindergarten():
    """決賽當天跑出來的第一筆就是這一則，所以它先在這裡跑一次。"""
    posts = {p.threads_id: p for p in threads.load_fixture(FIXTURE)}
    att = mention_store.attribute_post(posts["17849251066204813"], INSTITUTIONS)
    assert att.institution_id == "00957c83-0061-4581-a587-97629968f371"
    assert att.matched_name == "文德"
    assert att.corroborated_by_town     # 貼文寫了蘆洲，與主檔的行政區一致


def test_the_fixture_post_naming_another_city_is_refused_by_the_cross_city_rule():
    """那則貼文問的正是「新聞裡那家是不是我們這家」——猜一次就複製了它擔心的傷害。

    沒有跨縣市規則的話它會歸屬到文德：貼文裡「文德幼兒園」四個字是齊的。
    """
    posts = {p.threads_id: p for p in threads.load_fixture(FIXTURE)}
    att = mention_store.attribute_post(posts["17851990341270562"], INSTITUTIONS)
    assert att.institution_id is None
    assert "台北市" in att.basis


# ── 入庫 ────────────────────────────────────────────────────────────


def test_the_same_batch_twice_leaves_one_row_because_threads_id_is_unique():
    """同步會重跑——排程、手動、demo 前再跑一次。重跑必須是無操作。"""
    db = _db()
    posts = threads.load_fixture(FIXTURE)

    first = mention_store.record(db, posts, INSTITUTIONS)
    second = mention_store.record(db, posts, INSTITUTIONS)

    assert first["inserted"] == 3 and first["duplicate"] == 0
    assert second["inserted"] == 0 and second["duplicate"] == 3
    assert db.execute(select(func.count(ThreadsMention.id))).scalar_one() == 3
    db.close()


def test_replies_are_left_out_unless_the_caller_asks_for_them():
    db = _db()
    posts = threads.load_fixture(FIXTURE)

    counts = mention_store.record(db, posts, INSTITUTIONS)
    assert counts["skipped_reply"] == 1
    assert [r["is_reply"] for r in _rows(db)] == [False, False, False]

    counts = mention_store.record(db, posts, INSTITUTIONS, replies=True)
    assert counts["inserted"] == 1      # 只多了那一則回覆
    assert counts["skipped_reply"] == 0
    db.close()


def test_a_mention_we_cannot_attribute_is_still_stored_with_the_reason_why():
    """丟掉拒配的列，畫面上就會變成從來沒有人通報過——那是一種假的安靜。"""
    db = _db()
    mention_store.record(db, threads.load_fixture(FIXTURE), INSTITUTIONS)

    unattributed = [r for r in _rows(db) if r["institution_id"] is None]
    assert len(unattributed) == 2
    assert all(r["attribution_basis"] for r in unattributed)
    assert any("台北市" in r["attribution_basis"] for r in unattributed)

    summary = mention_store.stats(db)
    assert (summary["total"], summary["attributed"], summary["unattributed"]) == (3, 1, 2)
    db.close()


def _rows(db) -> list[dict]:
    """recent() 只回展示欄位，這裡要看 is_reply，所以直接讀表。"""
    return [
        {"is_reply": row.is_reply, "institution_id": row.institution_id,
         "attribution_basis": row.attribution_basis}
        for row in db.execute(select(ThreadsMention)).scalars()
    ]


# ── 串下的回覆：取用 ────────────────────────────────────────────────


def test_parse_reads_the_nested_root_and_parent_ids_without_choking_on_absence():
    """`root_post` / `replied_to` 是巢狀物件，而且三種缺法都真的會發生。

    /me/mentions 整頁都沒有這兩個 key；串端點的最上層回覆沒有 `replied_to`；
    而任何一個的值都可能不是物件。缺了就是空字串，不是例外，也不是猜一個。
    """
    posts = {p.threads_id: p for p in threads.parse(_payload(
        _row(id="child", root_post={"id": "root-1"}, replied_to={"id": "parent-1"}),
        _row(id="plain"),
        _row(id="weird", root_post="root-1", replied_to=None),
    ))}
    assert posts["child"].root_threads_id == "root-1"
    assert posts["child"].reply_to_threads_id == "parent-1"
    assert (posts["plain"].root_threads_id, posts["plain"].reply_to_threads_id) == ("", "")
    assert (posts["weird"].root_threads_id, posts["weird"].reply_to_threads_id) == ("", "")


def _fake_fetch(monkeypatch, handler):
    """換掉 `threads.fetch`，測分頁與端點選擇時不連網。"""
    calls: list[str] = []

    def fake(url, _token, _timeout=30):
        calls.append(url)
        return handler(url)

    monkeypatch.setattr(threads, "fetch", fake)
    return calls


def test_replies_of_falls_back_to_the_replies_endpoint_when_conversation_refuses(
        monkeypatch):
    """Conversations 端點只保證讀得到自己發的串，而我方的 root 一律是別人發的。

    也就是說退路不是防禦性程式碼，是主要路徑之一。
    """
    def handler(url):
        if f"/{threads.CONVERSATION}" in url:
            raise threads.ThreadsError("Threads API HTTP 400: unsupported", status=400)
        return json.dumps(_payload(
            _row(id="r1", is_reply=True, root_post={"id": ROOT},
                 replied_to={"id": ROOT}))).encode()

    calls = _fake_fetch(monkeypatch, handler)
    posts = threads.replies_of(ROOT, "a-token")

    assert [p.threads_id for p in posts] == ["r1"]
    assert any(f"/{threads.CONVERSATION}?" in u for u in calls)
    assert any(f"/{threads.REPLIES}?" in u for u in calls)


def test_a_thread_with_no_replies_and_a_thread_we_cannot_read_are_not_the_same_thing(
        monkeypatch):
    """空陣列是關於公眾的陳述；讀不到是關於我方權限的陳述。混起來就看不出差別。"""
    _fake_fetch(monkeypatch, lambda _url: json.dumps(_payload()).encode())
    assert threads.replies_of(ROOT, "a-token") == []

    def refused(_url):
        raise threads.ThreadsError("Threads API HTTP 403: no permission", status=403)

    _fake_fetch(monkeypatch, refused)
    with pytest.raises(threads.ThreadsPermissionError) as caught:
        threads.replies_of(ROOT, "a-token")
    assert "threads_read_replies" in str(caught.value)


def test_a_network_failure_is_not_reported_as_a_missing_permission(monkeypatch):
    """把斷線講成缺權限，會讓人去申請一個他已經有的權限，並且相信資料是空的。"""
    def offline(_url):
        raise threads.ThreadsError("Threads API network error: timed out")

    _fake_fetch(monkeypatch, offline)
    with pytest.raises(threads.ThreadsError) as caught:
        threads.replies_of(ROOT, "a-token")
    assert not isinstance(caught.value, threads.ThreadsPermissionError)


def test_a_missing_token_refuses_instead_of_looking_like_an_empty_thread():
    """`mentions()` 沒 token 回 [] 是在描述管道；這裡回 [] 會變成在描述公眾。"""
    with pytest.raises(threads.ThreadsPermissionError):
        threads.replies_of(ROOT, "")


def test_the_conversation_endpoint_returns_the_root_itself_and_it_is_not_its_own_reply(
        monkeypatch):
    _fake_fetch(monkeypatch, lambda _url: json.dumps(_payload(
        _row(id=ROOT, root_post={"id": ROOT}),
        _row(id="r1", is_reply=True, root_post={"id": ROOT}),
    )).encode())
    assert [p.threads_id for p in threads.replies_of(ROOT, "a-token")] == ["r1"]


def test_a_reply_whose_root_the_platform_did_not_state_is_filed_under_the_one_we_asked(
        monkeypatch):
    """我方是指名對這一串發問的，所以答案屬於這一串——這個不必猜。"""
    _fake_fetch(monkeypatch, lambda _url: json.dumps(_payload(
        _row(id="r1", is_reply=True))).encode())
    assert threads.replies_of(ROOT, "a-token")[0].root_threads_id == ROOT


def test_the_demo_fixture_is_one_thread_with_the_shapes_the_platform_really_returns():
    """離線 demo 跑的就是這一份，所以它的形狀先在這裡對過。"""
    posts = threads.load_fixture(REPLIES)
    assert len(posts) == 5
    assert sum(1 for p in posts if not p.root_threads_id) == 2   # root_post 沒回
    assert sum(1 for p in posts if not p.reply_to_threads_id) == 1
    assert sum(1 for p in posts if not p.text) == 1              # 只有貼圖那則


def test_group_by_root_climbs_replied_to_because_root_post_is_not_guaranteed():
    """實測那一則回覆帶了 `replied_to` 卻沒有 `root_post`——欄位有要，平台沒給。

    離線檔沒有「我問的是哪一串」這個線索，所以缺 root 的要往上爬：爬到有 root
    的那則，或爬到一個不在檔案裡的 id（直接回覆的父節點就是主貼文）。
    fixture 兩種缺法都放了一則，因為兩種都真的會回。
    """
    grouped = threads.group_by_root(threads.load_fixture(REPLIES))
    assert sorted(grouped) == [ROOT]
    assert len(grouped[ROOT]) == 5


def test_a_reply_that_cannot_be_attached_to_any_thread_is_not_silently_dropped():
    """接不回任何一串的要看得見。丟掉它，離線檔的則數就會對不上而沒有人知道。"""
    posts = threads.parse(_payload(
        _row(id="floating", is_reply=True),
        _row(id="placed", is_reply=True, root_post={"id": ROOT}),
    ))
    grouped = threads.group_by_root(posts)
    assert [p.threads_id for p in grouped[""]] == ["floating"]
    assert [p.threads_id for p in grouped[ROOT]] == ["placed"]


# ── 串下的回覆：入庫 ────────────────────────────────────────────────


def _with_thread(db) -> dict:
    mention_store.record(db, threads.load_fixture(FIXTURE), INSTITUTIONS)
    return mention_store.record_replies(
        db, ROOT, threads.load_fixture(REPLIES), INSTITUTIONS)


def test_a_reply_that_names_another_kindergarten_is_about_that_one_not_the_thread():
    """串是討論的容器，不是主體的容器。

    在文德那串底下說「吉尼爾幼兒園也這樣」的那一則，講的是吉尼爾。把它壓成
    文德，就是把「討論裡冒出第二家」這條線索改寫成一則附和。
    """
    db = _db()
    counts = _with_thread(db)
    assert counts["inserted"] == 5 and counts["attributed_own"] == 1

    stray = {r["threads_id"]: r for r in mention_store.thread(db, ROOT)
             }["17851122066394518"]
    assert stray["institution_id"] == GINEER
    assert stray["attribution_source"] == mention_store.OWN
    # 複查的人要看得出這則是在誰的串裡講的，所以主貼文那一家也寫進 basis。
    assert "吉尼爾" in stray["attribution_basis"] and "文德" in stray["attribution_basis"]
    # 呼叫端要能把這件事印出來，而不是自己去比對 institution_id。
    assert [o["threads_id"] for o in counts["named_others"]] == ["17851122066394518"]
    db.close()


def test_a_reply_that_names_nobody_inherits_the_thread_it_is_sitting_in():
    """「+1」沒有主體。獨立跑歸屬的結果是整串躺進待人工認園，那不是工作量。"""
    db = _db()
    counts = _with_thread(db)
    assert counts["attributed_inherited"] == 4

    plus_one = {r["threads_id"]: r for r in mention_store.thread(db, ROOT)
                }["17851120844172396"]
    assert plus_one["institution_id"] == WENDE
    assert plus_one["attribution_source"] == mention_store.INHERITED
    assert ROOT in plus_one["attribution_basis"]
    db.close()


def test_a_cram_school_is_not_a_childcare_institution_so_the_reply_inherits():
    """「XX補習班也這樣」比對不到任何機構，這是對的。

    短期補習班歸不同科管轄，不是教保服務機構，`institutions_ntpc.csv` 裡沒有
    也不該有。為了讓這句話配得上而放寬 `alerts.py` 的比對規則，換來的是每一則
    提到「XX幼兒園附近」的貼文都多一次誤配機會。
    """
    db = _db()
    _with_thread(db)

    row = {r["threads_id"]: r for r in mention_store.thread(db, ROOT)
           }["17851123177405629"]
    assert row["attribution_source"] == mention_store.INHERITED
    assert row["institution_id"] == WENDE
    db.close()


def test_a_refused_root_and_a_silent_reply_produce_no_attribution_at_all():
    """主貼文認不出是哪一園、回覆也沒指名——那就是沒有，不是猜一個。"""
    db = _db()
    mention_store.record(db, threads.load_fixture(FIXTURE), INSTITUTIONS)
    counts = mention_store.record_replies(
        db, NO_NAME_ROOT,
        threads.parse(_payload(_row(id="x1", text="我也遇過欸", is_reply=True))),
        INSTITUTIONS)

    assert counts["inserted"] == 1 and counts["unattributed"] == 1
    row = mention_store.thread(db, NO_NAME_ROOT)[1]
    assert row["institution_id"] is None
    assert row["attribution_source"] == mention_store.NONE
    db.close()


def test_a_reply_with_a_name_still_wins_when_the_root_has_none_to_inherit():
    """繼承不到就用自己的。這是收穫：一串認不出園名的討論裡，有人把園名寫了。"""
    db = _db()
    mention_store.record(db, threads.load_fixture(FIXTURE), INSTITUTIONS)
    counts = mention_store.record_replies(
        db, NO_NAME_ROOT,
        threads.parse(_payload(
            _row(id="x2", text="我講的是蘆洲的文德幼兒園，不是新聞那家", is_reply=True))),
        INSTITUTIONS)

    assert counts["attributed_own"] == 1
    row = mention_store.thread(db, NO_NAME_ROOT)[1]
    assert row["institution_id"] == WENDE
    assert row["attribution_source"] == mention_store.OWN
    assert "主貼文未歸屬" in row["attribution_basis"]
    db.close()


def test_replies_to_a_root_we_never_stored_do_not_become_orphan_rows():
    """一則掛不到主貼文的回覆，在畫面上是一句沒有上下文的抱怨。"""
    db = _db()
    counts = mention_store.record_replies(
        db, "17999999999999999", threads.load_fixture(REPLIES), INSTITUTIONS)

    assert counts["inserted"] == 0 and counts["skipped"] == 5
    assert "17999999999999999" in counts["reason"]
    assert db.execute(select(func.count(ThreadsMention.id))).scalar_one() == 0
    db.close()


def test_the_same_replies_twice_leave_one_row_each_like_mentions_do():
    """回覆走同一個 threads_id 唯一索引去重——重跑同步必須是無操作。"""
    db = _db()
    _with_thread(db)
    again = mention_store.record_replies(
        db, ROOT, threads.load_fixture(REPLIES), INSTITUTIONS)

    assert again["inserted"] == 0 and again["duplicate"] == 5
    assert db.execute(select(func.count(ThreadsMention.id))).scalar_one() == 3 + 5
    db.close()


def test_kind_records_who_addressed_the_authority_and_who_only_addressed_the_poster():
    """標註官方帳號的是主貼文的作者。回覆的人是在跟原 PO 講話，不是跟機關。"""
    db = _db()
    _with_thread(db)

    summary = mention_store.stats(db)
    assert (summary["roots"], summary["replies"], summary["total"]) == (3, 5, 8)
    assert [r["threads_id"] for r in mention_store.recent(db, kind=mention_store.REPLY)] \
        != []
    assert all(r["kind"] == mention_store.MENTION
               for r in mention_store.recent(db, kind=mention_store.MENTION))
    db.close()


def test_thread_returns_the_root_first_then_the_replies_in_posting_order():
    """fixture 刻意把子留言排在父留言前面——平台真的會這樣回。

    順序靠 `posted_at` 與「root 是自己的 root」重建，不靠回傳順序：上游那支
    實作就是假設了順序，才需要事後再接一次父子關係。
    """
    db = _db()
    _with_thread(db)

    rows = mention_store.thread(db, ROOT)
    assert [r["threads_id"] for r in rows] == [
        ROOT,                   # 主貼文永遠在最前面，不論它的時間
        "17851119933040281",    # 09-08 12:03 直接回覆
        "17851121955283407",    # 09-08 13:41 只有貼圖，text 是空字串
        "17851120844172396",    # 09-09 07:15 回覆的回覆
        "17851122066394518",    # 09-09 21:30 指名了另一所園
        "17851123177405629",    # 09-10 09:22 提到補習班（比對不到，走繼承）
    ]
    assert rows[0]["kind"] == mention_store.MENTION
    assert all(r["kind"] == mention_store.REPLY for r in rows[1:])
    # 回覆的回覆指向的是那則回覆，不是 root；平台沒說的就留 NULL，不補一個。
    assert rows[3]["reply_to_threads_id"] == "17851119933040281"
    assert rows[2]["reply_to_threads_id"] is None
    assert rows[2]["text"] == ""
    db.close()
