"""Public news search, and the rule that decides when an article may name a 園.

The competition brief asks for 輿情 (public sentiment), and
docs/research/06-realtime-event-monitoring-plan.md sets the terms it may enter
on: news never rewrites the historical risk score, never becomes a violation
label, and never produces a public accusation. It can only raise a candidate for
a human to look at.

The hard part is not fetching. It is that Taiwanese reporting of childcare
incidents is frequently anonymised by design -- the very first sample returned

    「新北市私立吉尼爾幼兒園不當對待案件說明 教育局：6人遭處分」   ← names the 園
    「三重某幼兒園違規挨罰9萬」                                    ← district only
    「新莊幼兒園爆不當對待！群組要老師…」                            ← district only

Two of those three cannot be attributed to an institution. A system that guessed
-- by district, by size, by "who else could it be" -- would manufacture an
accusation against whichever 園 happened to fit, and it would be confidently
wrong most of the time. So attribution requires the institution's distinctive
name to appear verbatim, and anonymised coverage is kept as city-level context
with no institution attached.

Only the RSS endpoint is used: it is a public feed meant for consumption, it
carries a publisher and a date, and it needs no login. Nothing here touches
private groups, logged-in content, or anything about an identifiable person.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

FEED = "https://news.google.com/rss/search"
USER_AGENT = "smart-watchdog-news-pilot/1"
MAX_BYTES = 4_000_000

# Phrasing that marks coverage as deliberately anonymised. An article using these
# is telling us it will not name the institution; inferring one anyway inverts
# the publisher's own editorial decision.
ANONYMISED = (
    "某幼兒園", "某私立幼兒園", "某公立幼兒園", "某園",
    "一家幼兒園", "1家幼兒園", "該幼兒園", "北部幼兒園",
)


@dataclasses.dataclass(frozen=True)
class NewsItem:
    title: str
    link: str
    published: str
    publisher: str

    @property
    def is_anonymised(self) -> bool:
        return any(marker in self.title for marker in ANONYMISED)


def build_url(query: str, *, lang: str = "zh-TW", country: str = "TW") -> str:
    params = urllib.parse.urlencode({
        "q": query, "hl": lang, "gl": country, "ceid": f"{country}:zh-Hant"})
    return f"{FEED}?{params}"


def fetch(query: str, timeout: int = 30) -> bytes:
    """Fetch one public feed. HTTPS only, size-capped, no cookies."""
    url = build_url(query)
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "news.google.com":
        raise ValueError(f"refused news request origin: {url!r}")
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError(f"news response exceeds {MAX_BYTES} bytes")
    return body


def parse_items(xml_bytes: bytes) -> list[NewsItem]:
    """Parse the feed, keeping the publisher and date the source supplied."""
    root = ET.fromstring(xml_bytes)
    items: list[NewsItem] = []
    for node in root.findall(".//item"):
        source = node.find("source")
        items.append(NewsItem(
            title=(node.findtext("title") or "").strip(),
            link=(node.findtext("link") or "").strip(),
            published=_iso(node.findtext("pubDate")),
            publisher=(source.text.strip() if source is not None and source.text
                       else ""),
        ))
    return items


def _iso(value: str | None) -> str:
    """RFC 822 -> ISO date, or the raw string if the feed used another format."""
    if not value:
        return ""
    raw = value.strip()
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        parsed = _try_parse(raw, fmt)
        if parsed:
            return parsed
    return raw


def _try_parse(raw: str, fmt: str) -> str:
    try:
        return dt.datetime.strptime(raw, fmt).date().isoformat()
    except ValueError:
        return ""
