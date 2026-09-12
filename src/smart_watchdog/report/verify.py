"""Check a drafted letter against the facts before it can carry a real 園's name.

This is the safety layer that makes a generated letter usable at all. The letter
is addressed to a named institution and cites its filed accounting policy; a
fabricated amount, a wrong 學年度, or another 園's name in the text is not a
style problem, it is a false statement about a real organisation. So the draft
is treated as untrusted output and must pass every check below before anything
downstream may use it.

The checks are deliberately mechanical -- they compare the draft against
:class:`~smart_watchdog.report.facts.AuditFacts`, which is the closed set of
things we actually established:

1. **No invented figures.** Every number of four digits or more in the draft must
   appear in the facts. Small numbers are exempt because clause numbers, item
   counts and dates legitimately appear in公文 phrasing.
2. **No other institution.** Only the addressee may be named.
3. **No verdict language.** 「違法」「不法」「違規」 assert a finding the system is
   not entitled to make; the output is 建議查核, per CLAUDE.md's 輸出定位 rule.
4. **The disclaimer survives.** A letter that loses its "非違法認定" line reads as
   an accusation regardless of the wording above it.
5. **Quoted rule text is real.** Any 附註二 quotation must match wording we hold.

A failure returns the reasons rather than raising, so a caller can fall back to
the deterministic template -- the fact layer alone always produces a sendable
letter, which is why the generative layer is allowed to be strict.

:func:`verify_reply` gates the other outbound document, the drafted reply to a
民眾 report (``report/reply.py``). It is a second entry point, **not a second
word list**: ``FORBIDDEN``, ``NEGATED`` and the stripping rule are shared through
``verdict_words``. Inventing a parallel set of checks for the newer document is
how one of them ends up a word behind.
"""

from __future__ import annotations

import dataclasses
import re

from .facts import AuditFacts

# Below this, a number is a clause reference, an item count, or a date part. The
# figures that could defame an institution are amounts, and those are large.
SIGNIFICANT_DIGITS = 4

FORBIDDEN = ("違法", "不法", "違規", "犯罪", "舞弊", "掏空", "圖利")
REQUIRED_DISCLAIMER = "非違法認定"
# Phrases that legitimately contain a forbidden word because they *deny* it.
# Without this the mandatory disclaimer trips the check it exists to satisfy --
# the first version of this verifier rejected all 143 of its own letters.
NEGATED = ("非違法認定", "不構成違法認定", "非屬違法認定")

_NUM = re.compile(r"\d[\d,]*")
_INSTITUTION = re.compile(r"[一-鿿]{2,12}(?:幼兒園|教保服務中心)")

#: 回覆草稿也必須帶的第二句聲明。`REQUIRED_DISCLAIMER` 說的是「這不是違法
#: 認定」，這一句說的是「這件事本身還沒查證過」——兩件事，缺一不可。一封只說
#: 「非違法認定」的回覆，讀起來仍像機關已經確認發生過什麼、只是還沒定性。
REQUIRED_UNVERIFIED = "未經查證"

#: 求助專線。回覆草稿裡唯一允許出現、卻不來自來源文本的數字——它們是固定的
#: 公開號碼，不是本案的事實。少了這條，樣板自己就會因為寫了「1999 市民專線」
#: 而被退件，而那句話是這份草稿最該留下的一句：涉及兒童安全時不要等公文。
HOTLINES = (1999, 113, 110)

#: 生成段落與原貼文重疊超過這麼多字，就當成把陌生人的句子搬進官方發言。
#: 理由見 ``verify_reply``。12 個中文字夠長到不會誤殺「本局將依規定查明」
#: 這類公務套語，又短到擋得住整句照抄。
ECHO_WINDOW = 12


def verdict_words(text: str) -> list[str]:
    """文本裡出現了哪些違法認定字眼。**唯一的那份清單，唯一的那套剝除規則。**

    `NEGATED` 要先剝掉，否則強制性的免責句「非違法認定」自己就會踩到 `違法`
    ——這支驗證器的第一版正是這樣退掉了它自己產生的全部 143 封信。
    抽成函式是因為現在有兩個呼叫端（建議書、回覆草稿）：各寫一份的那天，
    就是其中一份少剝一個否定詞、開始退掉自己的免責句的那天。
    """
    scan = str(text)
    for phrase in NEGATED:
        scan = scan.replace(phrase, "")
    return [word for word in FORBIDDEN if word in scan]


@dataclasses.dataclass
class VerificationResult:
    ok: bool
    problems: list[str]

    def __bool__(self) -> bool:
        return self.ok


def verify_letter(
    draft: str, facts: AuditFacts, known_titles: set[str] | None = None
) -> VerificationResult:
    """Return the reasons a draft may not be used, or an empty list."""
    problems: list[str] = []
    allowed = facts.allowed_numbers()

    for m in _NUM.finditer(draft):
        raw = m.group(0)
        digits = raw.replace(",", "")
        if len(digits) < SIGNIFICANT_DIGITS:
            continue
        value = int(digits)
        if value in allowed:
            continue
        # A ROC year written as 民國114年 is derivable from a 學年度 we hold.
        if 100 <= value <= 130:
            continue
        problems.append(f"出現無來源的數字 {raw}")

    # Flag anything that reads as an institution and is not the addressee. The
    # first version only flagged names that exactly matched a known title, so a
    # match that over-captured a leading character ("另新北市三多非營利幼兒園")
    # matched no known title and passed silently -- the default has to be reject.
    for name in set(_INSTITUTION.findall(draft)):
        if name in facts.title or facts.title in name:
            continue
        known = known_titles and any(name in t or t in name for t in known_titles)
        problems.append(
            f"提及其他機構「{name}」" if known else f"提及非本案機構「{name}」")

    problems.extend(
        f"使用違法認定字眼「{word}」" for word in verdict_words(draft)
    )

    if REQUIRED_DISCLAIMER not in draft:
        problems.append(f"缺少「{REQUIRED_DISCLAIMER}」聲明")

    if facts.findings:
        # A finding's own detail may itself quote a clause (安溪's 業務發展準備金
        # detail cites 附註二(一) about who bears the resulting tax), so both the
        # rule text and the detail count as wording we hold.
        held = {f.rule_text for f in facts.findings}
        held |= {f.detail for f in facts.findings}
        for quote in re.findall(r"附註二\([一二三四五六七八九十]+\)[^。\n]*", draft):
            core = quote.strip()
            if not any(core[:12] in text or text[:12] in core for text in held):
                problems.append(f"引用了未經核對的條文「{core[:28]}」")

    if not facts.findings and "未提撥足額" in draft:
        problems.append("在無財務發現的情況下敘述財務短缺")

    return VerificationResult(ok=not problems, problems=problems)


def _allowed_from(sources) -> set[int]:
    out: set[int] = set()
    for text in sources:
        for m in _NUM.finditer(str(text)):
            out.add(int(m.group(0).replace(",", "")))
    return out


def _echoes(generated: str, sources) -> str:
    """生成段落裡第一段與原貼文重疊 ``ECHO_WINDOW`` 字以上的文字，沒有就空字串。"""
    body = "".join(str(generated).split())
    if len(body) < ECHO_WINDOW:
        return ""
    for text in sources:
        source = "".join(str(text).split())
        for i in range(len(body) - ECHO_WINDOW + 1):
            chunk = body[i:i + ECHO_WINDOW]
            if chunk in source:
                return chunk
    return ""


def verify_reply(
    draft: str,
    *,
    sendable: str = "",
    generated: str = "",
    sources=(),
    permalink: str = "",
) -> VerificationResult:
    """Gate a drafted reply to a 民眾 report before a 承辦人 may see it as sendable.

    Same gate as :func:`verify_letter`, same word list, same negation rules --
    ``verdict_words`` is shared, deliberately. What differs is what the document
    is: a letter arranges *our own* established facts, while this one answers a
    post **written by a stranger**, which makes three further failure modes live.

    1. **The reply must not name an institution.** The letter is addressed to
       one and cites its own filings; a public reply from the 教育局 that names a
       園 next to an unverified allegation publicly associates the two before
       anybody has checked anything. The 承辦人 needs the name -- it is in the
       header block, which is not the sendable part -- and the public text does
       not. ``sendable`` is checked, not ``draft``.
    2. **The model must not launder the allegation into official prose.** A post
       saying 「老師打小孩」 quoted back inside 本局's own sentence reads as the
       authority repeating the claim. Any ``ECHO_WINDOW``-character run shared
       between the generated section and the source posts is rejected. This is
       also the prompt-injection stop: text the stranger wrote cannot reach the
       sendable reply through the model.
    3. **Both sentences must survive.** ``REQUIRED_DISCLAIMER`` says this is not
       a finding of illegality; ``REQUIRED_UNVERIFIED`` says the matter itself is
       unverified. A reply carrying only the first still reads as 「we have
       confirmed something happened, we just have not named it yet」.

    Figures are checked as in the letter -- anything four digits or more must
    come from ``sources`` (the posts, their timestamps, the permalink). An
    invented 件號, amount or date in a reply to a citizen is the same failure as
    an invented amount in a letter to a 園.

    Returns the reasons rather than raising, so the caller can fall back to the
    deterministic template -- which is why the generative path is allowed to be
    this strict.
    """
    problems: list[str] = []
    sources = list(sources)

    problems.extend(f"使用違法認定字眼「{word}」" for word in verdict_words(draft))

    if REQUIRED_DISCLAIMER not in draft:
        problems.append(f"缺少「{REQUIRED_DISCLAIMER}」聲明")
    if REQUIRED_UNVERIFIED not in draft:
        problems.append(f"缺少「{REQUIRED_UNVERIFIED}」聲明")
    if permalink and permalink not in draft:
        problems.append("草稿未載明原貼文 permalink")

    allowed = _allowed_from([*sources, permalink])
    for m in _NUM.finditer(draft):
        digits = m.group(0).replace(",", "")
        if len(digits) < SIGNIFICANT_DIGITS:
            continue
        value = int(digits)
        if value in allowed or value in HOTLINES or 100 <= value <= 130:
            continue
        problems.append(f"出現無來源的數字 {m.group(0)}")

    problems.extend(f"可送出的回覆內文提及機構「{name}」"
                    for name in sorted(set(_INSTITUTION.findall(sendable))))

    echo = _echoes(generated, sources)
    if echo:
        problems.append(f"回覆內文照抄了原貼文的文字「{echo}」")

    return VerificationResult(ok=not problems, problems=problems)
