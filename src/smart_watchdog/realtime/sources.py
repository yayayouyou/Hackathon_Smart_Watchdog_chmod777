"""Real-time channels: what may be watched, on whose authority, and right now.

This layer answers a different question from the risk score. The score asks
"which 園 should be inspected this month"; a real-time channel asks "is anyone
complaining about this 園 *today*". §20-21 measured the first question and found
news has no predictive lead over the penalty record -- but that was the wrong
test for this layer. Surfacing a complaint while it is still a complaint is
valuable even when it never becomes a penalty, and a channel that never predicts
anything can still be the reason an inspector looks a week earlier.

So channels are not judged on backtest accuracy here. They are judged on whether
we may lawfully watch them, and the system reports honestly which ones are live.

**Why availability is a first-class field.** Every channel a city government
could want is gated by someone: Google permits displaying reviews but forbids
scraping them (Maps Platform ToS 3.2.3(a): "will not export, extract, or
otherwise scrape... pre-fetch, index, store, reshare, or rehost"), Threads
requires app review for `threads_keyword_search`, and full social coverage in
Taiwan is sold by licensed vendors on the 共同供應契約 procurement schedule. None
of those gates is a technical obstacle to route around -- a municipal system
caught breaching a platform's terms is a problem for the 教育局, not for us. So
each channel declares its legal basis and its status, and a channel that is not
yet cleared shows as pending rather than quietly returning nothing.

That honesty is also the more useful product: an operator seeing
「4 個管道，1 個已啟用，3 個待申請／採購」 knows what to ask for. A panel that
silently shows an empty list teaches them the 園 is quiet.
"""

from __future__ import annotations

import abc
import dataclasses
import pathlib
from typing import Any, ClassVar

# Availability is about permission, not about whether code exists.
LIVE = "live"                      # running now, nothing further needed
NEEDS_KEY = "needs_api_key"        # lawful, but a credential must be supplied
NEEDS_APPROVAL = "needs_approval"  # provider must approve an application
NEEDS_PROCUREMENT = "needs_procurement"  # licensed vendor feed must be bought


@dataclasses.dataclass(frozen=True)
class Mention:
    """One public mention of one 園, awaiting a person's judgement.

    Never carries a verdict. `06-realtime-event-monitoring-plan.md` is explicit
    that unverified public content does not rewrite historical risk, so a mention
    is evidence to look at, not a score input.
    """

    channel: str
    institution_id: str | None
    headline: str
    url: str
    published: str
    publisher: str
    attribution_basis: str
    stored: bool = False   # False = display-only, must not be persisted

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class Channel(abc.ABC):
    """A source of real-time public mentions."""

    key: str = "base"
    label: str = ""
    legal_basis: str = ""
    status: str = NEEDS_APPROVAL
    #: Whether results may be written to disk. Display-only channels are the
    #: reason this exists: Google permits showing a review and forbids keeping it.
    may_store: bool = True
    #: What a person must do to turn this on, in their own words.
    how_to_enable: str = ""

    @abc.abstractmethod
    def search(self, institution: dict, limit: int = 20) -> list[Mention]:
        """Public mentions of one 園. Returns [] when the channel is not live."""

    def sweep(self, institutions: list[dict], limit: int = 200) -> list[Mention]:
        """Everything the channel has said about *any* 園 lately.

        The daily job wants this, not ``search``: asking each channel about each
        of 1,213 園 individually is hundreds of requests to answer a question a
        handful of topic queries already answers. Channels that can only be
        queried per-institution leave this returning [].
        """
        del institutions, limit
        return []

    def describe(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "status": self.status,
            "legal_basis": self.legal_basis, "may_store": self.may_store,
            "how_to_enable": self.how_to_enable,
        }


class NewsRssChannel(Channel):
    """Google News RSS. Public feed, no login, no personal data."""

    key = "news_rss"
    label = "新聞（Google News RSS）"
    legal_basis = "公開 RSS feed，無登入、無個資；發布者與日期由來源提供"
    status = LIVE
    may_store = True
    how_to_enable = "已啟用，無須額外授權"

    def search(self, institution: dict, limit: int = 20) -> list[Mention]:
        from ..features.alerts import attribute
        from ..scrape import news

        title = str(institution.get("title", ""))
        body = news.fetch(f'"{title}" OR "{_short(title)}幼兒園" 新北')
        out: list[Mention] = []
        for item in news.parse_items(body)[:limit]:
            att = attribute(item.title, [institution],
                            is_anonymised=item.is_anonymised)
            if not att.attributed:
                continue
            out.append(Mention(
                channel=self.key, institution_id=institution["id"],
                headline=item.title, url=item.link, published=item.published,
                publisher=item.publisher, attribution_basis=att.basis))
        return out

    def sweep(self, institutions: list[dict], limit: int = 200) -> list[Mention]:
        import time

        from ..features.alerts import attribute
        from ..scrape import news

        seen: set[str] = set()
        out: list[Mention] = []
        for i, topic in enumerate(_NEWS_TOPICS):
            if i:
                time.sleep(1.5)
            for item in news.parse_items(news.fetch(topic)):
                if item.link in seen or len(out) >= limit:
                    continue
                seen.add(item.link)
                att = attribute(item.title, institutions,
                                is_anonymised=item.is_anonymised)
                if not att.attributed:
                    continue
                out.append(Mention(
                    channel=self.key, institution_id=att.institution_id,
                    headline=item.title, url=item.link,
                    published=item.published, publisher=item.publisher,
                    attribution_basis=att.basis))
        return out


_NEWS_TOPICS = (
    '"幼兒園" 新北 (不當對待 OR 虐童 OR 體罰)',
    '"幼兒園" 新北 (裁罰 OR 開罰 OR 處分)',
    '"幼兒園" 新北 (停辦 OR 廢止 OR 勒令)',
    '"幼兒園" 新北 (超收 OR 退費 OR 收費爭議)',
    '"幼兒園" 新北 (受傷 OR 食安 OR 娃娃車)',
)


class PttChannel(Channel):
    """PTT search. The only social platform that needs no permission at all.

    No robots.txt, no login on the parenting boards, a plain search endpoint.
    Coverage is thin -- a sweep of the whole corpus matched 8 of 1,213 園 -- but
    that thinness is the honest state of the platform, not a defect in the
    channel: most 園 are simply never discussed. What it does surface is the real
    thing, a parent posting 反推 about a named 園 while it is still a complaint.
    """

    key = "ptt"
    label = "PTT（親子與新北地區板）"
    legal_basis = "無 robots.txt、無登入、公開搜尋端點；不蒐集推文者個資"
    status = LIVE
    may_store = True
    how_to_enable = "已啟用，無須額外授權"

    def search(self, institution: dict, limit: int = 20) -> list[Mention]:
        from ..features.alerts import attribute
        from ..scrape import ptt

        title = str(institution.get("title", ""))
        core = _short(title)
        if len(core) < 2:
            return []
        out: list[Mention] = []
        for post in ptt.search(core)[:limit * 2]:
            att = attribute(post.title, [institution])
            if not att.attributed:
                continue
            out.append(Mention(
                channel=self.key, institution_id=institution["id"],
                headline=f"[{post.kind}] {post.title}", url=post.url,
                published=post.published, publisher=f"PTT {post.board}",
                attribution_basis=att.basis))
            if len(out) >= limit:
                break
        return out

    def sweep(self, institutions: list[dict], limit: int = 200) -> list[Mention]:
        from ..features.alerts import attribute
        from ..scrape import ptt

        out: list[Mention] = []
        seen: set[str] = set()
        for post in ptt.search("幼兒園"):
            if post.url in seen or len(out) >= limit:
                continue
            seen.add(post.url)
            att = attribute(post.title, institutions)
            if not att.attributed:
                continue
            out.append(Mention(
                channel=self.key, institution_id=att.institution_id,
                headline=f"[{post.kind}] {post.title}", url=post.url,
                published=post.published, publisher=f"PTT {post.board}",
                attribution_basis=att.basis))
        return out


class PlacesReviewChannel(Channel):
    """Google Maps reviews, fetched at render time and never stored.

    The Places API policy page documents exactly how reviews may be shown --
    with the Google logo, the author, and a link back. Displaying is sanctioned;
    keeping is not. So this channel is display-only: `may_store` is False and the
    monitor refuses to persist what it returns.

    Scraping maps.google.com instead is not an alternative. ToS 3.2.3(a) names
    that behaviour directly, and a 教育局 system in breach of it is a municipal
    problem, not a technical shortcut.
    """

    key = "places_reviews"
    label = "Google 地圖評論（即時顯示）"
    legal_basis = (
        "Places API 政策允許即時顯示評論並要求標示 Google 與作者連結；"
        "ToS 3.2.3(a) 禁止匯出或爬取、3.2.3(b) 禁止快取、3.2.3(c)(vii) 禁止"
        "用於訓練或驗證模型。因此僅即時顯示，不入庫、不進特徵、不影響排序。"
    )
    status = NEEDS_KEY
    may_store = False
    how_to_enable = (
        "由教育局申請 Google Maps Platform 金鑰並啟用 Places API（Place Details）。"
        "一次性解析 1,213 園的 place_id 後，place_id 依條款可保存。"
    )

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key
        if api_key:
            self.status = LIVE

    #: place_id may be kept under the Service Specific Terms; review text may not.
    RESOLVE = "https://places.googleapis.com/v1/places:searchText"
    DETAILS = "https://places.googleapis.com/v1/places/{place_id}"

    def search(self, institution: dict, limit: int = 5) -> list[Mention]:
        """Fetch reviews at render time. Results are returned, never persisted."""
        if not self.api_key:
            return []
        import json
        import urllib.request

        place_id = institution.get("place_id") or self._resolve(institution)
        if not place_id:
            return []
        req = urllib.request.Request(
            self.DETAILS.format(place_id=place_id),
            headers={"X-Goog-Api-Key": self.api_key,
                     "X-Goog-FieldMask": "id,displayName,rating,reviews"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read(1_000_000))
        out = []
        for rv in (data.get("reviews") or [])[:limit]:
            text = (rv.get("originalText") or rv.get("text") or {}).get("text", "")
            author = (rv.get("authorAttribution") or {}).get("displayName", "")
            out.append(Mention(
                channel=self.key, institution_id=institution.get("id"),
                headline=str(text)[:160],
                url=(rv.get("authorAttribution") or {}).get("uri", ""),
                published=str(rv.get("publishTime", ""))[:10],
                publisher=f"Google 評論・{author}",
                attribution_basis="Places API place_id 精確對應",
                stored=False))
        return out

    def _resolve(self, institution: dict) -> str | None:
        """One-off place_id lookup. Only the id is retained, per the SSTs."""
        import json
        import urllib.request

        body = json.dumps({"textQuery": str(institution.get("title", ""))}).encode()
        req = urllib.request.Request(
            self.RESOLVE, data=body,
            headers={"Content-Type": "application/json",
                     "X-Goog-Api-Key": self.api_key,
                     "X-Goog-FieldMask": "places.id,places.displayName"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read(200_000))
        places = data.get("places") or []
        return places[0]["id"] if places else None


class ThreadsKeywordChannel(Channel):
    """Threads keyword search. Public posts, but only after app review."""

    key = "threads"
    label = "Threads 關鍵字搜尋"
    legal_basis = (
        "Meta 官方 Threads API。未取得 threads_keyword_search 權限前僅能搜尋"
        "自身貼文；核准後可搜尋公開貼文。每日 2,200 次查詢上限。"
        "官方文件載明對其認定敏感或冒犯之關鍵字回傳空陣列。"
    )
    status = NEEDS_APPROVAL
    may_store = True
    how_to_enable = (
        "建立 Meta 開發者應用程式，申請 threads_basic 與 threads_keyword_search "
        "權限並通過 App Review；以新北市教育局名義申請，用途敘明為教保機構監理。"
    )

    def __init__(self, access_token: str | None = None) -> None:
        self.access_token = access_token
        if access_token:
            self.status = LIVE

    def search(self, institution: dict, limit: int = 20) -> list[Mention]:
        del institution, limit
        if not self.access_token:
            return []
        raise NotImplementedError("待 threads_keyword_search 權限核准後接上")


class ThreadsMentionChannel(Channel):
    """Threads @標註：民眾主動寄給我們的，不是我們去找的。

    與上面那個 `ThreadsKeywordChannel` **刻意並存**，因為兩者是不同性質的東西，
    而畫面上要看得出差別：

    * 關鍵字搜尋是我方去搜別人的貼文，需要 `threads_keyword_search` 的 App
      Review，所以是 `needs_approval`。
    * `@標註` 是別人指名寄給我方的收件匣，只要帳號授權就能讀，所以有 token
      就是 `live`。

    兩個一起顯示，操作的人才會知道「等審核」與「填一把金鑰」是兩種不同的
    待辦事項——這正是模組說明講的 availability 是一等公民。

    **這個管道不回覆、不發文。** 取用層 `scrape/threads.py` 根本沒有那些函式，
    理由寫在那支模組的說明裡：官方帳號自動回「已收到您的通報」，是在任何人
    讀過內容之前就做出的公開受理表態。

    **歸屬沿用 `alerts.attribute()`，與新聞、PTT 同一套規則。** 拒配時
    `institution_id` 是 None，那是「認不出是哪一園」，不是「與機構無關」。

    **串下的回覆不從這裡出去。** `scrape/threads.py::replies_of()` 會抓、
    `realtime/mention_store.py::record_replies()` 會存，但這個管道只回主貼文：
    回覆的人沒有標註官方帳號，把一串十則「+1」當成十筆 mention 交出去，就是
    `06-plan` §6 不准的重複加權。要看整串請查 `mention_store.thread()`。
    """

    key = "threads_mentions"
    label = "Threads（@標註官方帳號）"
    legal_basis = (
        "Meta 官方 Threads API `/me/mentions`。只讀取主動 @標註本帳號的公開貼文，"
        "不搜尋、不爬取、不進入私人社團；不需 threads_keyword_search 權限。"
    )
    status = NEEDS_KEY
    may_store = True
    how_to_enable = (
        "以教育局名義建立 Meta 開發者應用程式並取得使用者存取權杖，填入 .env 的 "
        "THREADS_ACCESS_TOKEN；另建議設 THREADS_MENTIONS_NOT_BEFORE，"
        "否則首次同步會把帳號歷年被標註的貼文全部當成新通報匯入。"
    )

    def __init__(self, access_token: str | None = None) -> None:
        self.access_token = access_token
        if access_token:
            self.status = LIVE

    def _posts(self) -> list:
        from ..scrape import threads

        if not self.access_token:
            return []
        return [p for p in threads.mentions(self.access_token) if p.addressed_to_us]

    def _mention(self, post, institution_id: str | None, basis: str) -> Mention:
        from ..realtime.mention_store import _headline

        return Mention(
            channel=self.key, institution_id=institution_id,
            headline=_headline(post.text)[:120], url=post.permalink,
            published=post.posted_at[:10], publisher=f"Threads @{post.username}",
            attribution_basis=basis, stored=True)

    def search(self, institution: dict, limit: int = 20) -> list[Mention]:
        """One 園's mentions.

        The inbox is a single collection, so this fetches the same one page set
        as ``sweep`` and filters locally. Asking the API per-institution would be
        1,213 requests against one endpoint that already returned everything.
        """
        from ..features.alerts import attribute
        from .mention_store import _headline

        out: list[Mention] = []
        for post in self._posts():
            att = attribute(_headline(post.text), [institution])
            if not att.attributed:
                continue
            out.append(self._mention(post, institution["id"], att.basis))
            if len(out) >= limit:
                break
        return out

    def sweep(self, institutions: list[dict], limit: int = 200) -> list[Mention]:
        """The whole inbox, including the posts we could not attribute.

        Unattributed mentions are returned rather than dropped. A report we
        cannot match to a 園 is a queue item for a person, and silently removing
        it would make the panel claim nobody wrote in.
        """
        from ..features.alerts import attribute
        from .mention_store import _headline

        out: list[Mention] = []
        for post in self._posts():
            if len(out) >= limit:
                break
            att = attribute(_headline(post.text), institutions)
            out.append(self._mention(post, att.institution_id, att.basis))
        return out


class VendorFeedChannel(Channel):
    """第三方社群資料服務：Apify、QSearch、OpView、KEYPO 等。

    這是決賽期間取得 Threads／Dcard／FB 覆蓋的路徑——官方 API 需 2–4 週審核，
    第三方服務當天訂閱即可用。

    ``VENDOR_FEED_PATH`` 可以是兩種形態，程式自動判斷：

    * **本機檔案**（``.json`` / ``.jsonl`` / ``.csv``）——每日批次下載的結果
    * **HTTP 端點**——即時查詢；``{query}`` 會被替換成搜尋詞

    供應商的欄位名稱各不相同，因此 ``FIELD_ALIASES`` 對常見命名做對應，
    找不到就跳過該筆而不是猜。歸屬一律走 ``alerts.attribute()``，與新聞、PTT
    同一套規則。
    """

    key = "vendor_feed"
    label = "社群資料服務（第三方）"
    legal_basis = "由資料服務供應商提供；涵蓋範圍與更新頻率依合約"
    status = NEEDS_PROCUREMENT
    may_store = True
    how_to_enable = (
        "訂閱 Apify／QSearch／OpView 等服務，把每日檔案路徑或查詢端點"
        "填入 .env 的 VENDOR_FEED_PATH。端點可用 {query} 佔位符。"
    )

    #: 供應商欄位名稱互異，逐一嘗試；找不到就跳過該筆。
    FIELD_ALIASES: ClassVar[dict[str, tuple[str, ...]]] = {
        "text": ("text", "content", "title", "post_text", "message", "caption",
                 "snippet", "body"),
        "url": ("url", "link", "permalink", "post_url", "postUrl", "href"),
        "date": ("date", "published", "publishedAt", "timestamp", "created_at",
                 "createdAt", "post_time", "publish_time"),
        "author": ("author", "username", "user", "screen_name", "channel",
                   "source", "authorName"),
    }

    def __init__(self, feed_path: str | None = None) -> None:
        self.feed_path = feed_path
        if feed_path:
            self.status = LIVE

    def _pick(self, row: dict, field: str) -> str:
        for name in self.FIELD_ALIASES[field]:
            value = row.get(name)
            if isinstance(value, dict):
                value = value.get("text") or value.get("value")
            if value:
                return str(value)
        return ""

    def _load(self, query: str | None = None) -> list[dict]:
        """Read the feed, whether it is a file on disk or an HTTP endpoint."""
        import json

        src = str(self.feed_path or "")
        if src.startswith(("http://", "https://")):
            import urllib.parse
            import urllib.request

            url = src.replace("{query}", urllib.parse.quote(query or "幼兒園"))
            req = urllib.request.Request(
                url, headers={"User-Agent": "smart-watchdog-realtime/1",
                              "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                raw = resp.read(20_000_000)
            return _rows(json.loads(raw))

        path = pathlib.Path(src)
        if not path.exists():
            return []
        if path.suffix == ".jsonl":
            return [json.loads(line) for line in
                    path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if path.suffix == ".csv":
            import csv
            import io

            return list(csv.DictReader(io.StringIO(
                path.read_text(encoding="utf-8"))))
        return _rows(json.loads(path.read_text(encoding="utf-8")))

    def _to_mentions(self, rows: list[dict], institutions: list[dict],
                     limit: int) -> list[Mention]:
        from ..features.alerts import article_kind, attribute

        out: list[Mention] = []
        for row in rows:
            if len(out) >= limit:
                break
            text = self._pick(row, "text")
            if not text:
                continue
            att = attribute(text, institutions)
            if not att.attributed:
                continue
            out.append(Mention(
                channel=self.key, institution_id=att.institution_id,
                headline=f"[{article_kind(text)}] {text[:160]}",
                url=self._pick(row, "url"), published=self._pick(row, "date")[:10],
                publisher=self._pick(row, "author") or "社群資料服務",
                attribution_basis=att.basis))
        return out

    def search(self, institution: dict, limit: int = 20) -> list[Mention]:
        if not self.feed_path:
            return []
        core = _short(str(institution.get("title", "")))
        return self._to_mentions(self._load(core), [institution], limit)

    def sweep(self, institutions: list[dict], limit: int = 200) -> list[Mention]:
        if not self.feed_path:
            return []
        return self._to_mentions(self._load("幼兒園 新北"), institutions, limit)


def _rows(data: object) -> list[dict]:
    """Vendors wrap their payloads differently; find the list of records."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("data", "items", "results", "posts", "records", "list"):
            value = data.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    return []


def _short(title: str) -> str:
    from ..features.alerts import distinctive_name

    return distinctive_name(title)


def default_channels(*, use_env: bool = True, **credentials) -> list[Channel]:
    """Every channel the design contemplates, live or not.

    Pending channels are returned deliberately: the operator needs to see which
    ones are waiting on an application or a purchase, because that is an action
    for them, not an absence of news.

    Credentials come from ``.env`` unless the caller overrides them, so adding a
    key to that file is the only step needed to switch a channel on.
    """
    from ..config import credentials as env_credentials

    base = env_credentials() if use_env else {}
    credentials = {**base, **{k: v for k, v in credentials.items() if v}}
    from .apify import ApifyThreadsChannel

    return [
        NewsRssChannel(),
        PttChannel(),
        ApifyThreadsChannel(token=credentials.get("apify_token")),
        PlacesReviewChannel(api_key=credentials.get("places_api_key")),
        ThreadsKeywordChannel(access_token=credentials.get("threads_token")),
        # 同一把 token 開兩個管道：搜尋要 App Review（needs_approval），
        # @標註不用（有 token 就 live）。兩個都列出來，差別才看得見。
        ThreadsMentionChannel(access_token=credentials.get("threads_token")),
        VendorFeedChannel(feed_path=credentials.get("vendor_feed_path")),
    ]
