"""Decide which 園, if any, a news item is about.

This is the module where a false accusation would be created, so its default is
refusal, and every rule below was added because a looser version produced a real
misattribution on real headlines.

The first version matched a 園's distinctive name anywhere in the headline. On
the first live run it attributed a virtual-reality of incidents to the wrong
institutions:

    「林口某雙語補習班遭控涉不當對待」        -> 新北市私立林口幼兒園
    「太平洋新聞網」                          -> 新北市私立太平洋幼兒園
    「汐止牛排店火警…疏散隔壁幼兒園150人 - 聯合影音」 -> 新北市私立聯合幼兒園
    「何嘉仁幼兒園又被抓！南港分校…北市府罰84萬」   -> 新北市私立何嘉仁幼兒園

The first of those is the one to keep in mind: another headline in the same batch
read 「衰被誤認虐童！林口幼兒園急澄清」 -- an institution publicly complaining that
it had been mistaken for the one in the news. A matcher that guesses reproduces
exactly the harm the article is reporting.

So attribution now requires, in order:

1. the name to sit immediately before 幼兒園, not merely appear somewhere
   (kills 太平洋新聞網, 聯合影音);
2. the publisher suffix to be stripped before matching at all;
3. a name identical to a 行政區 to carry the institution's full official title,
   because 「林口幼兒園」 reads equally as "the 園 called 林口" and "a 幼兒園 in
   林口" and we cannot tell which was meant;
4. no other city named in the headline (kills 南港分校/北市府);
5. a single candidate -- two 園 sharing a name means naming one is a coin flip
   printed as a fact.

The output is never a score. `06-realtime-event-monitoring-plan.md` is explicit
that unverified public content does not rewrite historical risk, so an attributed
item becomes an alert *candidate* carrying its source link, awaiting a human.
"""

from __future__ import annotations

import dataclasses
import re

NOISE = re.compile(
    r"新北市|私立|市立|公立|非營利|附設|幼兒園|教保服務中心|"
    r"股份有限公司|有限公司|財團法人|社團法人|學校財團法人|基金會|"
    r"[（(].*?[)）]"
)
MIN_DISTINCTIVE = 2

# 新北市 29 行政區. A 園 whose identifying name is one of these cannot be matched
# on that name alone, because the same string is how reporters refer to location.
DISTRICTS = frozenset([
    "板橋", "三重", "中和", "永和", "新莊", "新店", "樹林", "鶯歌", "三峽",
    "淡水", "汐止", "土城", "蘆洲", "五股", "泰山", "林口", "深坑", "石碇",
    "坪林", "三芝", "石門", "八里", "平溪", "雙溪", "貢寮", "金山", "萬里",
    "烏來", "瑞芳",
])

# Naming another municipality is strong evidence the story is not ours; chains
# such as 何嘉仁 operate branches in several cities under one name.
OTHER_CITIES = ("臺北市", "台北市", "北市", "桃園", "臺中", "台中", "臺南", "台南",
                "高雄", "基隆", "新竹", "苗栗", "彰化", "南投", "雲林", "嘉義",
                "屏東", "宜蘭", "花蓮", "臺東", "台東")

# Google News appends the publisher as " - X" or " | X"; 「太平洋新聞網」 and
# 「聯合影音」 both matched 園 names purely through that suffix.
PUBLISHER_SUFFIX = re.compile(r"\s*[-|｜]\s*[^-|｜]{1,24}$")


@dataclasses.dataclass(frozen=True)
class Attribution:
    institution_id: str | None
    matched_name: str
    basis: str
    corroborated_by_town: bool

    @property
    def attributed(self) -> bool:
        return self.institution_id is not None


def distinctive_name(title: str) -> str:
    """The identifying core of an official 園名.

    「新北市私立吉尼爾幼兒園」 -> 「吉尼爾」
    「新北市安溪非營利幼兒園(委託社團法人桃園市教保服務人員協會辦理)」 -> 「安溪」
    """
    return NOISE.sub("", str(title)).strip()


def strip_publisher(headline: str) -> str:
    """Drop the trailing publisher so its name cannot match an institution."""
    return PUBLISHER_SUFFIX.sub("", str(headline)).strip()


def _names_an_institution(text: str, name: str) -> bool:
    """Whether ``name`` is used as an institution name, not as a location.

    Requires the name to sit immediately before 幼兒園/幼稚園/托嬰中心 or the
    quoted form 「name」, which is how reporters mark a proper noun.
    """
    if re.search(rf"{re.escape(name)}\s*(?:幼兒園|幼稚園|教保服務中心)", text):
        return True
    return bool(re.search(rf"[「『\"]{re.escape(name)}[」』\"]", text))


def attribute(
    headline: str,
    institutions: list[dict],
    *,
    is_anonymised: bool = False,
) -> Attribution:
    """Return the institution a headline names, or an unattributed result.

    ``institutions`` entries need ``id``, ``title`` and ``town``.
    """
    if is_anonymised:
        return Attribution(None, "", "報導刻意匿名，不予歸屬", False)

    text = strip_publisher(headline)

    # 「新北市」 contains 「北市」, which is in OTHER_CITIES to catch 「北市府」.
    # Left unmasked, the city's own name vetoes attribution for the city's own
    # institutions -- and every title in the registry begins with 新北市, so the
    # full official name could never match itself. Masked only for this scan;
    # `text` stays intact for the name matching below.
    #
    # Only 「新北市」 needs masking, not 「新北」: no other entry in OTHER_CITIES
    # is a substring of it. Keeping the mask minimal means a headline about
    # 新北投 (which is in 台北市) still hits 台北市 on its own terms.
    scanned = text.replace("新北市", "")
    for city in OTHER_CITIES:
        if city in scanned:
            return Attribution(None, "", f"標題提及其他縣市（{city}），不予歸屬", False)

    hits: list[tuple[dict, str, bool]] = []
    for inst in institutions:
        full = str(inst.get("title", ""))
        if full and full in text:
            hits.append((inst, full, True))
            continue
        core = distinctive_name(full)
        if len(core) < MIN_DISTINCTIVE:
            continue
        if core in DISTRICTS:
            # 「林口幼兒園」 is ambiguous by construction; only the full official
            # title distinguishes the institution from the location.
            continue
        if _names_an_institution(text, core):
            hits.append((inst, core, False))

    if not hits:
        return Attribution(None, "", "標題未以機構名稱形式出現可辨識名稱", False)

    ids = {inst["id"] for inst, _, _ in hits}
    if len(ids) > 1:
        names = sorted({name for _, name, _ in hits})
        return Attribution(None, "、".join(names),
                           f"名稱可對應 {len(ids)} 所機構，無法唯一歸屬", False)

    inst, name, is_full = hits[0]
    town = str(inst.get("town", "")).replace("區", "")
    corroborated = bool(town and town in text)
    return Attribution(
        inst["id"], name,
        "標題出現機構全名" if is_full else "標題以機構名稱形式出現可辨識名稱",
        corroborated)


# Wording that marks an article as describing a regulatory event rather than
# routine coverage. Used only to sort a human's queue -- it categorises the
# article, never the institution.
INCIDENT_TERMS = (
    "不當對待", "虐童", "體罰", "裁罰", "開罰", "違反", "停辦", "廢止",
    "超收", "退費", "申訴", "調查", "查處", "處分", "勒令", "食安", "受傷",
)
ROUTINE_TERMS = ("招生", "開學", "活動", "評鑑通過", "揭牌", "參訪", "捐贈", "表揚")


def article_kind(headline: str) -> str:
    """`incident` / `routine` / `unclear` -- a queue-sorting hint, not a verdict."""
    incident = any(t in headline for t in INCIDENT_TERMS)
    routine = any(t in headline for t in ROUTINE_TERMS)
    if incident and not routine:
        return "incident"
    if routine and not incident:
        return "routine"
    return "unclear"
