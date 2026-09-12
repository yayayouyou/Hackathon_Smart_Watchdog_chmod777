"""Threads mentions: the posts that named us, fetched with the platform's blessing.

``ptt.py`` opens by writing Threads off -- "behind logins" -- and for keyword
search that is still true: ``threads_keyword_search`` needs Meta's App Review,
which is why ``realtime.sources.ThreadsKeywordChannel`` has sat at
``needs_approval`` with a ``NotImplementedError`` body.

``GET /me/mentions`` is a different door. It returns posts that @-mentioned the
authenticated account -- not a search of other people's writing, but an inbox
someone deliberately addressed to us. That distinction is the whole point:

* **Legally** it is inbound. Nobody is being watched; a person chose to tell the
  authority something in public and tagged the authority so it would be seen.
* **Technically** it needs no keyword-search permission, so it can run the day a
  token exists rather than after a two-to-four week review.
* **Evidentially** every record arrives with the platform's own ``id``,
  ``timestamp`` and ``permalink``. Provenance is given, not reconstructed -- the
  property ``scrape/observations.py`` goes to some trouble to manufacture for
  sources that do not supply it.

What this module deliberately does **not** do: publish, reply, delete, or read
conversations. The reference implementation this borrows its API handling from
(``Hackathon_MaiCoin_chmod777/agent-core/agent_ui/threads_bridge.py``) does all
four, because it runs a discussion room. An official 教育局 account replying
「已收到您的通報」 to an unverified allegation is a public acknowledgement of
receipt, made before any person has read it. Read-only is a design decision here,
not an unfinished feature.

Two behaviours are carried over from that implementation because they were
learned the expensive way:

* **Cursor pagination must guard against repeats.** A cursor that comes back
  unchanged loops forever; ``mentions`` stops when it sees one twice.
* **A cutoff is mandatory in practice.** Granting the token exposes the whole
  mention history at once, so a first sync without ``THREADS_MENTIONS_NOT_BEFORE``
  imports years of old posts as if they had arrived today. See ``cutoff()``.

Reading the replies *under* a mention (``replies_of``) is still inbound -- the
thread someone started by addressing us -- but it is a different permission and a
different kind of failure.

``/{id}/conversation`` is documented for threads the authenticated account
**owns**, and our roots are by definition other people's posts. Measured
2026-09-12 against a live token and a real third-party post (root
``18429670012183745``): it answered **HTTP 200**, so the main path works on posts
we do not own. Two things that measurement did *not* settle, and which are
therefore still handled rather than assumed:

* **The fallback stays.** One post on one day with one token is not a guarantee.
  Visibility settings, token expiry and the other account's own settings can all
  take the endpoint away, so ``/{id}/replies`` remains the second attempt.
* **When both refuse, the answer is not ``[]``.** An empty list reads as
  「沒有人回覆」, a statement about the public that a permission failure gives us
  no basis to make. ``ThreadsPermissionError`` keeps the two apart -- the same
  reason ``realtime/jobs.py`` spends six outcome names on what could have been a
  boolean.

That same measurement showed the reply carrying ``replied_to`` but **no**
``root_post``, even though the field was requested. So which thread a reply
belongs to is taken from the id we asked about, never from the response. See
``replies_of`` and ``group_by_root``.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request

API_ROOT = "https://graph.threads.net"
API_HOST = "graph.threads.net"
USER_AGENT = "smart-watchdog-realtime/1 (+public monitoring; contact via repo)"
MAX_BYTES = 3_000_000
PAGE_LIMIT = 50          # Threads caps `limit` at 50 regardless of what we ask.
MAX_PAGES = 40           # 2,000 mentions in one sync is already an anomaly.

#: What we ask the platform for. `is_reply` matters because a reply that happens
#: to mention us is a fragment of someone else's conversation, not a report
#: addressed to us -- see `ThreadsPost.addressed_to_us`.
FIELDS = "id,text,username,timestamp,permalink,is_reply"

#: Replies need two more fields than mentions do. ``root_post`` says which thread
#: a row belongs to and ``replied_to`` says who it answers -- the platform's own
#: answer to both, which is why we never infer either from arrival order.
REPLY_FIELDS = FIELDS + ",root_post,replied_to"

CONVERSATION, REPLIES = "conversation", "replies"
MAX_REPLY_PAGES = 10     # 500 replies under one post; past that, nobody reads them.


class ThreadsError(RuntimeError):
    """Transport, authorisation, or response-shape failure from the Threads API.

    ``status`` is the HTTP status when the platform answered and refused, and
    ``None`` when the request never got an answer. ``replies_of`` needs that
    difference: a refusal is about permissions, a timeout is about the network,
    and telling someone to apply for a permission they already hold wastes a day.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ThreadsPermissionError(ThreadsError):
    """The endpoint would not let us read. Distinct from 「this thread has no replies」.

    Both look like "no replies came back" at the call site, and only one of them
    is a fact about the public. Swallowing this into an empty list would show an
    operator a working feature that is silently returning nothing.
    """


@dataclasses.dataclass(frozen=True)
class ThreadsPost:
    """One post that mentioned the account. Carries no judgement of any kind.

    ``text`` is the author's words verbatim. Nothing here classifies, scores or
    attributes -- attribution is ``features.alerts.attribute``'s job, and that
    module refuses far more often than it guesses.
    """

    threads_id: str
    text: str
    username: str
    posted_at: str          # ISO-8601 exactly as the platform returned it.
    permalink: str
    is_reply: bool
    raw: dict
    # 平台說的「這則屬於哪一串」與「這則回的是誰」。兩個都給預設值：/me/mentions
    # 不回這兩個欄位，既有呼叫端也不傳它們。
    root_threads_id: str = ""
    reply_to_threads_id: str = ""

    @property
    def content_hash(self) -> str:
        """sha256 of the text, so an edit made upstream later is detectable."""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def addressed_to_us(self) -> bool:
        """A root post mentioning us is a report; a reply is someone else's thread.

        Kept as a property rather than a filter inside ``parse`` because a caller
        may legitimately want to see both and decide afterwards. The sync script
        stores only the posts for which this is True.
        """
        return not self.is_reply

    def as_dict(self) -> dict:
        return {
            "threads_id": self.threads_id, "text": self.text,
            "username": self.username, "posted_at": self.posted_at,
            "permalink": self.permalink, "is_reply": self.is_reply,
            "content_hash": self.content_hash,
            "root_threads_id": self.root_threads_id,
            "reply_to_threads_id": self.reply_to_threads_id,
        }


def token() -> str | None:
    """The user access token, or None. Absence is a state, not an error."""
    from .. import config

    value = (config.get("THREADS_ACCESS_TOKEN") or "").strip()
    return value or None


def cutoff() -> dt.datetime | None:
    """Ignore mentions older than ``THREADS_MENTIONS_NOT_BEFORE``.

    Without this, the first sync after a token is issued pulls the account's
    entire mention history in one go and stamps years-old posts with today's
    observation time. Every one of them then reads as a fresh report.

    An unparseable timestamp is treated as *older* than any cutoff, never newer:
    a record whose age cannot be established must not be admitted by default.
    Same instinct as CLAUDE.md's 「看不清就填 null，絕不猜」.
    """
    from .. import config

    raw = (config.get("THREADS_MENTIONS_NOT_BEFORE") or "").strip()
    if not raw:
        return None
    try:
        value = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ThreadsError(
            "THREADS_MENTIONS_NOT_BEFORE 必須是 ISO-8601，例如 2026-09-01T00:00:00Z"
        ) from exc
    return value.replace(tzinfo=value.tzinfo or dt.timezone.utc).astimezone(dt.timezone.utc)


def _before(timestamp: str, limit: dt.datetime | None) -> bool:
    if limit is None:
        return False
    try:
        value = dt.datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return True     # Unknown age is refused, not admitted. See cutoff().
    value = value.replace(tzinfo=value.tzinfo or dt.timezone.utc)
    return value.astimezone(dt.timezone.utc) < limit


def fetch(url: str, access_token: str, timeout: int = 30) -> bytes:
    """One GET against the Threads Graph API, origin-checked before it leaves.

    The host check mirrors ``ptt.fetch``, but matters more here: this request
    carries an Authorization header, so a mistyped constant or a redirect would
    hand the token to whatever host the URL happened to name.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != API_HOST:
        raise ThreadsError(f"refused Threads request origin: {url!r}")
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(MAX_BYTES)
    except urllib.error.HTTPError as exc:
        body = exc.read(2000).decode("utf-8", "replace")
        raise ThreadsError(
            f"Threads API HTTP {exc.code}: {body[:300]}", status=exc.code) from exc
    except OSError as exc:
        raise ThreadsError(f"Threads API network error: {exc}") from exc


def _nested_id(row: dict, key: str) -> str:
    """``root_post`` / ``replied_to`` are objects, and only appear when asked for.

    A mention page never carries them, a reply page carries them only when the
    platform knows the answer, and a top-level reply's ``replied_to`` is absent
    rather than null. Three ways to be missing, one way to be present.
    """
    value = row.get(key)
    return str(value.get("id") or "").strip() if isinstance(value, dict) else ""


def parse(payload: bytes | str | dict,
          *, not_before: dt.datetime | None = None) -> list[ThreadsPost]:
    """Turn one API page into posts. Pure -- this is what the tests exercise.

    Malformed entries are skipped rather than repaired. A mention with no ``id``
    cannot be deduplicated and one with no ``timestamp`` cannot be aged; stored
    anyway, either would be indistinguishable from a real report.
    """
    if isinstance(payload, (bytes, str)):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ThreadsError(f"Threads API returned non-JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ThreadsError("Threads API response was not an object")

    rows = payload.get("data")
    if not isinstance(rows, list):
        return []

    out: list[ThreadsPost] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        threads_id = str(row.get("id") or "").strip()
        timestamp = str(row.get("timestamp") or "").strip()
        if not threads_id or not timestamp:
            continue
        if _before(timestamp, not_before):
            continue
        out.append(ThreadsPost(
            threads_id=threads_id,
            text=str(row.get("text") or ""),
            username=str(row.get("username") or ""),
            posted_at=timestamp,
            permalink=str(row.get("permalink") or ""),
            is_reply=bool(row.get("is_reply")),
            raw=row,
            root_threads_id=_nested_id(row, "root_post"),
            reply_to_threads_id=_nested_id(row, "replied_to"),
        ))
    return out


def next_cursor(payload: dict) -> str:
    return str(((payload.get("paging") or {}).get("cursors") or {}).get("after") or "")


def _walk(url_for, access_token: str, *,
          max_pages: int, not_before: dt.datetime | None) -> list[ThreadsPost]:
    """Read every cursor page of one collection. ``url_for(after)`` builds each page.

    Shared by ``mentions`` and ``replies_of`` so the two cannot drift: the repeat
    guard below is the reason this is one function and not two loops.
    """
    out: list[ThreadsPost] = []
    seen_ids: set[str] = set()
    seen_cursors: set[str] = set()
    after = ""
    for _ in range(max_pages):
        raw = fetch(url_for(after), access_token)
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ThreadsError(f"Threads API returned non-JSON: {exc}") from exc
        for post in parse(payload, not_before=not_before):
            if post.threads_id in seen_ids:
                continue
            seen_ids.add(post.threads_id)
            out.append(post)
        cursor = next_cursor(payload)
        # An absent or repeated cursor ends the walk. Threads has been observed
        # returning the same `after` on a final page; without this guard the loop
        # never terminates, and the guard is cheaper than the incident.
        if not cursor or cursor in seen_cursors:
            break
        seen_cursors.add(cursor)
        after = cursor
    return out


def mentions_url(*, after: str = "", limit: int = PAGE_LIMIT) -> str:
    """The request URL. The token is not in it -- it travels in the header."""
    params: dict[str, object] = {"fields": FIELDS, "limit": min(max(limit, 1), PAGE_LIMIT)}
    if after:
        params["after"] = after
    return f"{API_ROOT}/me/mentions?{urllib.parse.urlencode(params)}"


def mentions(access_token: str | None = None, *,
             not_before: dt.datetime | None = None,
             max_pages: int = MAX_PAGES) -> list[ThreadsPost]:
    """Every mention the account can see, deduplicated by the platform's id.

    Returns ``[]`` when no token is configured -- the contract every other
    channel in ``realtime.sources`` follows, so a missing credential reads as
    「這個管道還沒開」 rather than as an outage or as silence from the public.
    """
    access_token = access_token or token()
    if not access_token:
        return []
    if not_before is None:
        not_before = cutoff()

    return _walk(lambda after: mentions_url(after=after), access_token,
                 max_pages=max_pages, not_before=not_before)


def replies_url(root_threads_id: str, *, endpoint: str = CONVERSATION,
                after: str = "", limit: int = PAGE_LIMIT) -> str:
    """One page of a thread. The id is percent-encoded: it goes in the path."""
    params: dict[str, object] = {
        "fields": REPLY_FIELDS, "limit": min(max(limit, 1), PAGE_LIMIT)}
    if endpoint == CONVERSATION:
        # 讓平台照時間先後回，我方就不必假設「先回的一定是先發的」。
        params["reverse"] = "false"
    if after:
        params["after"] = after
    root = urllib.parse.quote(str(root_threads_id).strip(), safe="")
    return f"{API_ROOT}/{root}/{endpoint}?{urllib.parse.urlencode(params)}"


def replies_of(root_threads_id: str, access_token: str | None = None, *,
               max_pages: int = MAX_REPLY_PAGES) -> list[ThreadsPost]:
    """Every reply under one mention. Empty means empty; refused raises.

    ``/{id}/conversation`` is tried first because it returns the whole tree in
    one walk, including replies to replies. It is documented for threads the
    authenticated account owns and **our roots are always other people's posts**;
    measured 2026-09-12 it answered 200 on exactly that case, which is why this
    is the first attempt rather than the hopeful one. ``/{id}/replies`` remains
    the fallback -- one post, one day, one token is evidence, not a guarantee.
    Only if both refuse does this raise.

    ``ThreadsPermissionError`` rather than ``[]`` when that happens, and rather
    than the ``[]``-on-missing-token contract ``mentions`` follows: there, empty
    means 「這個管道還沒開」 and the channel is the thing being described; here it
    would mean 「沒有人回覆」, a claim about the public that a permission failure
    gives us no basis for. A transport failure is re-raised as itself -- an
    unreachable network is not a missing permission, and sending someone to apply
    for ``threads_read_replies`` when the venue's wifi is down costs a day.

    No ``not_before``: the root already passed the cutoff, and dropping the
    replies underneath it by age would leave a thread we chose to keep with holes
    in it.
    """
    root = str(root_threads_id or "").strip()
    if not root:
        raise ThreadsError("replies_of() 需要一個主貼文 id")
    access_token = access_token or token()
    if not access_token:
        raise ThreadsPermissionError(
            "未設定 THREADS_ACCESS_TOKEN，回覆一則也讀不到。"
            "這與「這串沒有人回覆」是兩件事。")

    failures: list[tuple[str, ThreadsError]] = []
    for endpoint in (CONVERSATION, REPLIES):
        try:
            posts = _walk(
                lambda after, e=endpoint: replies_url(root, endpoint=e, after=after),
                access_token, max_pages=max_pages, not_before=None)
        except ThreadsError as exc:
            failures.append((endpoint, exc))
            continue
        # conversation 會把主貼文本身一起回。它不是自己的回覆。
        # root 從**我方問的那個 id** 補，不從回應裡讀：實測回來的那則就沒有帶
        # root_post（欄位有要，平台沒給）。我方是指名對這一串問的，答案屬於
        # 哪一串由問題決定，這件事不需要平台同意。
        return [
            dataclasses.replace(post, root_threads_id=post.root_threads_id or root)
            for post in posts if post.threads_id != root
        ]

    transport = next((exc for _, exc in failures if exc.status is None), None)
    if transport is not None:
        raise transport
    detail = "；".join(f"/{e}: {exc}" for e, exc in failures)
    raise ThreadsPermissionError(
        f"讀不到 {root} 的回覆——/conversation 與 /replies 兩個端點都被拒絕。"
        f"（{detail}）這是端點讀不到，不是這串沒有人回覆。"
        "多半缺 threads_read_replies 權限，或權杖已過期、該貼文的可見性變了。"
        "「主貼文是別人發的」不是原因——2026-09-12 實測那樣讀得到。")


def group_by_root(posts: list[ThreadsPost]) -> dict[str, list[ThreadsPost]]:
    """把一堆回覆分到各自的串下。離線路徑用的，live 不需要。

    ``replies_of`` 知道自己問的是哪一串；一份存下來的檔案不知道，而 ``root_post``
    實測**不保證回傳**。所以缺 root 的那些沿 ``replied_to`` 往上爬：爬到一則有
    root 的，或爬到一個不在這份檔案裡的 id——後者就是主貼文本身，因為直接回覆
    的父節點正是 root。

    順序無關：爬的是平台給的 id，不是陣列位置。上游那支實作假設了「父一定先
    到」，於是需要一支 ``_reconcile_parent_links`` 在事後補救；這裡從一開始就
    不靠順序。真的攀不上去的（既沒 root 也沒 parent）歸在 ``""`` 這個 key 下，
    由呼叫端決定怎麼說——它們不能被靜靜丟掉。
    """
    by_id = {post.threads_id: post for post in posts}
    grouped: dict[str, list[ThreadsPost]] = {}
    for post in posts:
        grouped.setdefault(_root_of(post, by_id), []).append(post)
    return grouped


def _root_of(post: ThreadsPost, by_id: dict) -> str:
    current: ThreadsPost | None = post
    seen: set[str] = set()
    while current is not None:
        if current.root_threads_id:
            return current.root_threads_id
        parent = current.reply_to_threads_id
        # 迴圈防護與游標那邊同一個道理：資料壞掉時要停，不是要轉。
        if not parent or parent in seen:
            return ""
        seen.add(parent)
        if parent not in by_id:
            return parent       # 父節點不在這份檔案裡 = 它就是主貼文
        current = by_id[parent]
    return ""


def load_fixture(path: str | os.PathLike) -> list[ThreadsPost]:
    """Parse a saved API response. The offline path, for tests and for the demo.

    Every pinned artefact in this repo rebuilds without a network; a live channel
    is the one thing that cannot. A fixture keeps the panel demonstrable when the
    token has expired or the venue's network has not materialised.
    """
    import pathlib

    return parse(pathlib.Path(path).read_bytes(), not_before=None)
