"""PTT parsing: dates come from the URL, and complaint markers sort a queue."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.scrape.ptt import PttPost, parse, search_url

_ROW = """
<div class="r-ent">
  <div class="title"><a href="{href}">{title}</a></div>
  <div class="author">{author}</div>
</div>
"""
PAGE = (
    _ROW.format(href="/bbs/BabyMother/M.1786795839.A.5F5.html",
                title="[心得] 反推某幼兒園", author="someuser")
    + _ROW.format(href="/bbs/BabyMother/M.1700000000.A.111.html",
                  title="[寶寶] 請問推薦幼兒園", author="other")
).encode("utf-8")


def test_search_url_encodes_chinese_query():
    url = search_url("BabyMother", "幼兒園")
    assert url.startswith("https://www.ptt.cc/bbs/BabyMother/search?q=")
    assert "%E5%B9%BC" in url


def test_dates_come_from_the_url_timestamp_not_the_displayed_month_day():
    """PTT 只顯示 月/日，跨年時會猜錯；網址內嵌的 unix time 是精確的。"""
    posts = parse(PAGE, "BabyMother")
    assert len(posts) == 2
    assert posts[0].published == "2026-08-15"
    assert posts[1].published == "2023-11-14"


def test_complaint_and_question_are_separated():
    posts = parse(PAGE, "BabyMother")
    assert posts[0].kind == "complaint"   # 反推
    assert posts[1].kind == "question"    # 請問推薦


def test_kind_is_unclear_when_both_or_neither_marker_appears():
    both = PttPost(board="b", title="[心得] 請問反推這間好嗎", url="u",
                   published="2026-01-01", author="a")
    neither = PttPost(board="b", title="[閒聊] 今天天氣", url="u",
                      published="2026-01-01", author="a")
    assert both.kind == "unclear"
    assert neither.kind == "unclear"


def test_parse_survives_a_page_with_no_results():
    assert parse(b"<html><body>no results</body></html>", "BabyMother") == []
