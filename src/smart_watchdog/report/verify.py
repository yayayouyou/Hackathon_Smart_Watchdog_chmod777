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
# 「不是違法認定」是 `agent/tools.py` 的 CAVEAT 用的說法，每個 tool 回傳都帶著，
# 所以模型很常照講。它跟這裡的「非違法認定」是同一句話的兩種寫法。
NEGATED = ("非違法認定", "不是違法認定", "不構成違法認定", "非屬違法認定")

_NUM = re.compile(r"\d[\d,]*")
_INSTITUTION = re.compile(r"[一-鿿]{2,12}(?:幼兒園|教保服務中心)")


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

    scan = draft
    for phrase in NEGATED:
        scan = scan.replace(phrase, "")
    problems.extend(
        f"使用違法認定字眼「{word}」" for word in FORBIDDEN if word in scan
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
