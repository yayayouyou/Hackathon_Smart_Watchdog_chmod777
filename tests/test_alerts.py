"""News attribution: every test here is a misattribution that actually happened.

The first live run of this matcher named the wrong institution four times on real
headlines. Each case below locks that specific failure shut. They are worth
keeping verbatim because the harm is asymmetric: failing to attribute an article
costs an inspector nothing, while attributing an abuse story to the wrong 園 is
the exact injury one of these very headlines is about --
「衰被誤認虐童！林口幼兒園急澄清」.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.features.alerts import (
    article_kind,
    attribute,
    distinctive_name,
    strip_publisher,
)
from smart_watchdog.scrape.news import NewsItem

INSTITUTIONS = [
    {"id": "a1", "title": "新北市私立吉尼爾幼兒園", "town": "新莊區"},
    {"id": "a2", "title": "新北市私立林口幼兒園", "town": "林口區"},
    {"id": "a3", "title": "新北市私立太平洋幼兒園", "town": "汐止區"},
    {"id": "a4", "title": "新北市私立聯合幼兒園", "town": "汐止區"},
    {"id": "a5", "title": "新北市私立何嘉仁幼兒園", "town": "新店區"},
    {"id": "a6", "title": "新北市私立中冠幼兒園", "town": "蘆洲區"},
    {"id": "a7", "title": "新北市碧城非營利幼兒園(委託財團法人仁德醫護管理專科學校辦理)",
     "town": "汐止區"},
    {"id": "a8",
     "title": "新北市碧城非營利幼兒園(委託社團法人中華音樂舞蹈暨表演藝術教育協會辦理)",
     "town": "汐止區"},
]


def test_distinctive_name_strips_shared_wrappers():
    assert distinctive_name("新北市私立吉尼爾幼兒園") == "吉尼爾"
    assert distinctive_name(
        "新北市安溪非營利幼兒園(委託社團法人桃園市教保服務人員協會辦理)") == "安溪"


def test_publisher_suffix_is_removed_before_matching():
    assert strip_publisher("汐止牛排店火警…疏散隔壁幼兒園150人 - 聯合影音") \
        == "汐止牛排店火警…疏散隔壁幼兒園150人"


# --- the four real misattributions ------------------------------------------

def test_district_name_is_not_an_institution_name():
    """「林口幼兒園」 reads as both the 園 called 林口 and a 幼兒園 in 林口.

    The same batch carried 「衰被誤認虐童！林口幼兒園急澄清」 -- an institution
    publicly objecting to exactly this confusion.
    """
    a = attribute("林口某雙語補習班被網紅Ted控涉虐童 新北教育局查獲違法教保先罰",
                  INSTITUTIONS)
    assert not a.attributed


def test_publisher_name_does_not_attribute():
    assert not attribute("太平洋新聞網 - pacificnews.com.tw", INSTITUTIONS).attributed
    assert not attribute("汐止牛排店火警 消防急打火…還疏散隔壁幼兒園150人 - 聯合影音",
                         INSTITUTIONS).attributed


def test_another_city_blocks_attribution():
    """何嘉仁 runs branches in several cities; 南港分校/北市府 is not ours."""
    a = attribute("何嘉仁幼兒園又被抓！南港分校超收1倍多 北市府罰84萬", INSTITUTIONS)
    assert not a.attributed
    assert "其他縣市" in a.basis


def test_our_own_city_name_does_not_block_attribution():
    """「新北市」 contains 「北市」, and 「北市」 is on the other-city veto list.

    Unmasked, the veto fired on the city's own name: every institution title in
    the registry starts with 新北市, so the full official name could never match
    itself, and any post written 「新北市○○幼兒園」 was refused. News headlines
    mostly write 「新北」 and rarely tripped it; Threads posts from parents write
    the full city name constantly, which is how this surfaced.
    """
    for headline in ("新北市私立吉尼爾幼兒園遭家長投訴",
                     "新北市新莊區吉尼爾幼兒園超收教材費"):
        a = attribute(headline, INSTITUTIONS)
        assert a.attributed, f"{headline} 應歸屬卻被拒：{a.basis}"
        assert a.institution_id == "a1"


def test_masking_our_city_still_lets_a_real_other_city_veto():
    """The mask is 「新北市」 only -- it must not swallow a genuine 台北市."""
    a = attribute("新北市吉尼爾幼兒園與台北市某園同時被查", INSTITUTIONS)
    assert not a.attributed
    assert "台北市" in a.basis


def test_name_must_sit_before_the_word_kindergarten():
    """A bare occurrence is not a naming; 太平洋新聞網 contains 太平洋."""
    assert not attribute("太平洋沿岸降雨", INSTITUTIONS).attributed
    assert attribute("汐止太平洋幼兒園遭檢舉超收", INSTITUTIONS).attributed


# --- the attributions that must still work -----------------------------------

def test_named_institution_is_attributed():
    a = attribute("新莊吉尼爾幼兒園爆虐童！3教職員拍打+拖行 教育局重罰24萬", INSTITUTIONS)
    assert a.institution_id == "a1"
    assert a.corroborated_by_town


def test_quoted_name_is_attributed():
    a = attribute("蘆洲「中冠幼兒園」違規超收 新北重罰勒令停業1年", INSTITUTIONS)
    assert a.institution_id == "a6"


def test_shared_name_across_two_institutions_refuses():
    """兩所碧城 differ only by 受託法人; naming one would be a coin flip."""
    a = attribute("汐止碧城幼兒園遭檢舉", INSTITUTIONS)
    assert not a.attributed
    assert "無法唯一歸屬" in a.basis


# --- anonymised coverage ------------------------------------------------------

def test_anonymised_report_is_never_attributed():
    """The publisher chose not to name the 園; inferring one inverts that."""
    item = NewsItem(title="15名幼童擠計程車返園！三重某幼兒園違規挨罰9萬",
                    link="x", published="2026-07-14", publisher="鏡週刊")
    assert item.is_anonymised
    a = attribute(item.title, INSTITUTIONS, is_anonymised=True)
    assert not a.attributed
    assert "匿名" in a.basis


def test_article_kind_sorts_the_queue_without_judging_the_institution():
    assert article_kind("新莊吉尼爾幼兒園爆虐童 教育局重罰24萬") == "incident"
    assert article_kind("某幼兒園辦理親子活動 家長參與踴躍") == "routine"
    assert article_kind("幼兒園招生說明會將於週六舉行") == "routine"


def test_another_city_does_not_block_when_the_district_corroborates():
    """跨縣市否決排在候選比對之後，且只在沒有行政區佐證時生效。

    真實貼文暴露的洞：一位家長寫「台北市那則不當管教的新聞，名字跟板橋的
    ○○幼兒園好像，到底是不是同一家」。舊規則第一眼看到「台北市」就否決，
    但那則貼文**正是**本模組說明開頭引的「衰被誤認虐童！林口幼兒園急澄清」
    那個情境——擋掉它，等於擋掉我們最該看見的那一種。

    「板橋的」把哪一家講明了；另一個縣市是被拿來對比的，不是事件所在地。
    """
    a = attribute("剛剛看到台北市那則不當管教的新聞，名字跟新莊的吉尼爾幼兒園好像，"
                  "到底是不是同一家", INSTITUTIONS)
    assert a.attributed and a.institution_id == "a1"
    assert a.corroborated_by_town


def test_another_city_still_blocks_a_chain_without_district_corroboration():
    """何嘉仁那則必須維持拒配——鬆綁不能把記錄在案的誤配放回來。

    連鎖園在多個縣市有分校，文中沒有出現該園登記的行政區（新店）時，
    光憑名稱無從分辨是哪一家的事，所以否決照樣生效。
    """
    a = attribute("何嘉仁幼兒園又被抓！南港分校超收1倍多 北市府罰84萬", INSTITUTIONS)
    assert not a.attributed
    assert "其他縣市" in a.basis
