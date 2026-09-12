"""PTT search: the one social platform that is open and needs no permission.

Every other social source is gated -- Dcard and Threads behind logins, Facebook
groups behind an API that Meta withdrew in April 2024, full coverage behind a
vendor contract. PTT has no robots.txt, no login for the parenting boards, and a
plain search endpoint. So it is the channel that can actually run today, and the
one that makes the real-time layer a working feature rather than a diagram.

It was previously dismissed for having no predictive lead over the penalty
record. That was the wrong test: a real-time channel earns its place by
surfacing a complaint while it is still a complaint, not by forecasting a fine a
year out. Judged as live listening, a 反推 post naming a 園 is exactly the signal
an inspector wants to see this week.

Two things the article URL gives us for free:

* ``M.<unixtime>.A.<hash>`` carries the exact post time, so we never have to
  guess a year from PTT's 月/日 display.
* the board name, which lets a caller weight 新北-local boards over national ones.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import html
import re
import urllib.parse
import urllib.request

BASE = "https://www.ptt.cc"
USER_AGENT = "smart-watchdog-realtime/1 (+public monitoring; contact via repo)"
MAX_BYTES = 3_000_000

# Boards where 新北 parents actually discuss local 園.
DEFAULT_BOARDS = ("BabyMother", "Preschooler", "banciao", "sanchong", "XinZhuang")

_TITLE = re.compile(
    r'<div class="title">\s*<a href="([^"]+)">([^<]+)</a>', re.S)
_TS = re.compile(r"/M\.(\d{9,11})\.A\.")
_AUTHOR = re.compile(r'<div class="author">([^<]*)</div>')

# Post prefixes that mark dissatisfaction rather than a question. PTT convention
# puts the type in brackets, and 反推 is the community's word for a negative
# recommendation -- the closest thing the platform has to a complaint marker.
COMPLAINT_MARKERS = ("反推", "負雷", "抱怨", "爆料", "申訴", "投訴", "黑名單",
                     "不當", "體罰", "虐", "受傷", "退費", "超收")
QUESTION_MARKERS = ("請問", "推薦", "選擇", "怎麼選", "如何", "評價", "心得分享")


@dataclasses.dataclass(frozen=True)
class PttPost:
    board: str
    title: str
    url: str
    published: str
    author: str

    @property
    def kind(self) -> str:
        """`complaint` / `question` / `unclear` -- sorts a queue, judges nobody."""
        complaint = any(m in self.title for m in COMPLAINT_MARKERS)
        question = any(m in self.title for m in QUESTION_MARKERS)
        if complaint and not question:
            return "complaint"
        if question and not complaint:
            return "question"
        return "unclear"


def search_url(board: str, query: str) -> str:
    return f"{BASE}/bbs/{board}/search?{urllib.parse.urlencode({'q': query})}"


def fetch(url: str, timeout: int = 30) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "www.ptt.cc":
        raise ValueError(f"refused PTT request origin: {url!r}")
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError(f"PTT response exceeds {MAX_BYTES} bytes")
    return body


def parse(body: bytes, board: str) -> list[PttPost]:
    """Titles, links and exact timestamps from one search page."""
    text = body.decode("utf-8", "ignore")
    authors = _AUTHOR.findall(text)
    posts: list[PttPost] = []
    for i, (href, title) in enumerate(_TITLE.findall(text)):
        stamp = _TS.search(href)
        published = ""
        if stamp:
            published = dt.datetime.fromtimestamp(
                int(stamp.group(1)), dt.timezone.utc).date().isoformat()
        posts.append(PttPost(
            board=board,
            title=html.unescape(title).strip(),
            url=f"{BASE}{href}",
            published=published,
            author=(authors[i].strip() if i < len(authors) else ""),
        ))
    return posts


def search(query: str, boards: tuple[str, ...] = DEFAULT_BOARDS,
           timeout: int = 30) -> list[PttPost]:
    """Search each board for one query, skipping boards that are gone or gated."""
    out: list[PttPost] = []
    for board in boards:
        out.extend(_search_board(board, query, timeout))
    return out


def _search_board(board: str, query: str, timeout: int) -> list[PttPost]:
    try:
        return parse(fetch(search_url(board, query), timeout), board)
    except Exception:  # noqa: BLE001 - a missing or gated board must not stop the rest
        return []
