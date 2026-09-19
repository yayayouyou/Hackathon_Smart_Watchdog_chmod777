"""Parse the signing audit firm / accountant off each report's 會計師查核報告 page.

``scripts/plan_from_toc.py`` now targets this section for every report (see its
``WANTED`` entry), so ``data/extracted/nonprofit_pages/<report>/`` carries a page
with ``page_kind == "auditor_report"`` for each of the 132 財報, not just the 3
pilot 補充 JSON this project started with. That page's ``text_sections`` is a
verbatim OCR transcript, not a structured field -- the firm name and the signing
accountant's name sit in a signature block at the end of the prose:

    誠明聯合會計師事務所
    會計師：張景嵐
    核准文號：台財證登六字第4319號
    民國114年9月30日

This module turns that transcript into ``(firm, accountant, licence_no)``.

## Why 核准文號 matters more than the accountant's name

Scanning the corpus, the licence number (執業核准字號) is byte-identical across
every report this pipeline has read -- 「台財證登六字第4319號」-- while the printed
name varies: 張景嵐 / 張景崗 / 張景巍 / 張燕嵐 / 張惠嵐 / 張忠崗 all appear across
different 園-學年度, always immediately preceded by 誠明聯合會計師事務所 and
followed by the same licence number. These are not different people signing
under one firm; a licence number is issued to one named individual and does not
get shared. This is OCR noise on a small, similarly-shaped stroke count (嵐/崗/
巍/燕/惠/忠 are graphically close, especially at scan resolution on a small
seal/signature-adjacent line), not a rotation of accountants.

So the licence number is the trustworthy anchor. Firm-name comparison is exact
(a firm changing name is itself notable and not something OCR fabricates at this
rate). Accountant-name comparison, where the licence number agrees, is treated as
the same person misread -- disagreement is not asserted as a personnel change
without corroboration elsewhere. Where the licence number is missing or itself
disagrees, ``compare_accountant_names`` (below) is the fallback, because at that
point a name difference could be real.
"""

from __future__ import annotations

import dataclasses
import re

#: 事務所名稱：中文機構名 + 「聯合」（可選）+「會計師事務所」，逐字比對。
_FIRM_RE = re.compile(r"([\u4e00-\u9fff]{2,20}(?:聯合)?會計師事務所)")

#: 簽名區塊的「會計師：<姓名>」一行。姓名後面緊接換行、全形空白或字串結尾，
#: 避免吃進下一行的「核准文號」。
_ACCOUNTANT_RE = re.compile(r"會計師[：:][\s　]*([\u4e00-\u9fff]{2,8})")

#: 核准文號，例如「台財證登六字第4319號」。字號本身可能有多種格式（各年代主管
#: 機關字軌不同），所以只抓「號」字前的完整字串，不假設固定長度。
_LICENCE_RE = re.compile(r"核准文號[：:][\s　]*([^\n]{4,40}?號)")


@dataclasses.dataclass(frozen=True)
class AuditorSignature:
    """One report's signing firm/accountant, as printed in its 查核報告."""

    firm: str | None
    accountant: str | None
    licence_no: str | None


def parse_auditor_signature(pages: list[dict]) -> AuditorSignature:
    """Extract the signature block from one report's ``auditor_report`` page(s).

    ``pages`` is the list of page payloads (from ``nonprofit_pages/<report>/p*.
    json``) whose ``page_kind == "auditor_report"``. The signature usually sits on
    the last of these pages, but this scans every page's every ``text_sections``
    entry and keeps the first hit for each field independently, because 該區塊在
    有些報告被拆進不同頁（例如查核意見在 p3、責任段與簽名在 p4）。
    """
    text = "\n".join(
        (section.get("text") or "")
        for page in pages
        for section in (page.get("text_sections") or [])
    )
    firm = _FIRM_RE.search(text)
    accountant = _ACCOUNTANT_RE.search(text)
    licence = _LICENCE_RE.search(text)
    return AuditorSignature(
        firm=firm.group(1) if firm else None,
        accountant=accountant.group(1).strip() if accountant else None,
        licence_no=re.sub(r"\s+", "", licence.group(1)) if licence else None,
    )


# ── 姓名差異：OCR 誤讀 vs 真的換人 ──────────────────────────────────────
#
# 核准文號一致時，姓名不同幾乎必然是 OCR 誤讀（見模組說明）。但核准文號缺失或
# 本身就不同的案例，仍需要判斷「這兩個字串是不是同一個人被認錯字」。這裡刻意
# 交給 LLM 判斷而非寫死一張形近字表——形近字的清單會隨字體、掃描品質而變，
# 而「這兩個字看起來像不像」正是語言模型比規則表更擅長的判斷。


@dataclasses.dataclass(frozen=True)
class NameComparison:
    """LLM（或其退路）對兩個姓名是否為同一人的判斷。"""

    same_person: bool
    reason: str
    #: "llm" 表示由模型判斷；"deterministic" 表示 Bedrock 不可用時的退路，
    #: 呼叫端應該把這個欄位一起記下來，讓「這個結論打哪來的」可回溯。
    method: str


_NAME_COMPARE_SCHEMA: dict = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "same_person": {
                "type": "boolean",
                "description": "兩個姓名是否為同一人被 OCR 認錯字（而非真的換了另一個人）",
            },
            "reason": {
                "type": "string",
                "description": "一句話說明依據，例如指出形近的字、或指出姓氏不同不可能是誤讀",
            },
        },
        "required": ["same_person", "reason"],
        "additionalProperties": False,
    },
}

_NAME_COMPARE_PROMPT = """你正在協助判斷兩個中文人名是否為「同一人」在不同頁面被 OCR
（光學文字辨識）誤讀成不同字，而非真的換了一個人。

背景：這些姓名來自財務報表查核報告上「會計師：<姓名>」的簽名區塊，同一位會計師
連續多年簽名，但掃描品質不一，OCR 有時會把筆畫相近的字認錯（例如「嵐」認成
「崗」「巍」「燕」「惠」，或「景」認成「忠」）。

判斷原則：
- 若兩個姓名只差一個字，且該字筆畫或部件相近，很可能是同一人被誤讀。
- 若姓氏不同（例如「張」對「李」），幾乎不可能是同一人的誤讀，應判定為不同人。
- 若整個姓名完全不同、找不到任何形近的字，應判定為不同人。
- 不確定時，傾向判定為不同人（換人的訊號應該被看見，而不是被「可能是誤讀」吞掉）。

只回答兩個姓名本身，不要考慮其他上下文。"""


def compare_accountant_names(
    name_a: str,
    name_b: str,
    *,
    client: object | None = None,
    model: str | None = None,
) -> NameComparison:
    """判斷兩個會計師姓名是否為同一人的 OCR 誤讀。

    優先呼叫 Bedrock（``smart_watchdog.bedrock.FAST_MODEL``，同 ``api/chat.py``
    的查詢意圖解析用同一顆——這裡也只需要一個快速的是非判斷，不需要推理模型）。
    ``client`` 可注入既有的 ``AnthropicBedrock`` 實例以重複使用連線；不傳則臨時
    建立一個。

    Bedrock 不可用時（未設憑證、逾時、任何例外）退回確定性判斷：姓氏不同直接
    判定為不同人；姓氏相同且長度相同、僅一字之差則判定為可能的誤讀但標低確信度
    ——``method`` 欄位記下走的是哪一條路，呼叫端與人工複核都看得出這一筆判斷
    有沒有經過模型。
    """
    a, b = name_a.strip(), name_b.strip()
    if a == b:
        return NameComparison(True, "姓名完全相同", "deterministic")

    try:
        from .. import bedrock as _bedrock

        c = client or _bedrock.client(timeout=_bedrock.TIMEOUT_FAST, max_retries=1)
        resp = c.messages.create(
            model=model or _bedrock.FAST_MODEL, max_tokens=200,
            system=_NAME_COMPARE_PROMPT,
            messages=[{"role": "user", "content": f"姓名一：{a}\n姓名二：{b}"}],
            output_config={"format": _NAME_COMPARE_SCHEMA},
        )
        import json

        got = json.loads(resp.content[0].text)
        return NameComparison(bool(got["same_person"]), str(got["reason"]), "llm")
    except Exception as exc:  # noqa: BLE001 - 任何原因都要降級，不是只擋特定例外
        return _compare_accountant_names_fallback(a, b, error=exc)


def _compare_accountant_names_fallback(
    a: str, b: str, *, error: BaseException | None = None
) -> NameComparison:
    """Bedrock 不可用時的確定性判斷：姓氏比對 + 單字編輯距離。"""
    note = f"（Bedrock 不可用：{type(error).__name__}）" if error else ""
    if not a or not b:
        return NameComparison(False, f"姓名缺失，無法比對{note}", "deterministic")
    if a[0] != b[0]:
        return NameComparison(
            False, f"姓氏不同（{a[0]} vs {b[0]}），不太可能是同一人{note}",
            "deterministic")
    if len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1:
        return NameComparison(
            True, f"姓氏相同、僅一字之差，判定為可能的 OCR 誤讀{note}",
            "deterministic")
    return NameComparison(
        False, f"姓氏相同但差異超過一字，判定為不同人{note}", "deterministic")


# ── 查核意見類型：從逐頁文字反推 ─────────────────────────────────────────
#
# smart_watchdog.extract.supplement 的 audit_report.opinion_type 是結構化欄位
# （unmodified/qualified/adverse/disclaimer），但那只在 3 份試點補充 JSON 裡有。
# 逐頁抽取（nonprofit_pages）沒有這個結構化欄位，只有 auditor_report 頁的
# text_sections——所以這裡從標題反推，供 classify_accountant_change 的
# ``opinion`` 參數使用。

#: 依嚴重度排列，因為一份報告若真的出現保留以下的意見，標題會明確寫出意見
#: 類型。「保留意見」單獨檢查會被「無保留意見」（標準無保留意見標題的常見寫法）
#: 誤判命中——兩者互為子字串——所以先排除帶「無」字首的寫法。
_OPINION_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("無法表示意見", "disclaimer"),
    ("否定意見", "adverse"),
    ("保留意見", "qualified"),
)

#: 這些字串本身就含有上面某個關鍵字，但意思是「無保留」，必須先排除，
#: 否則「保留意見」會命中「無保留意見」裡的子字串。
_UNMODIFIED_KEYWORDS = ("無保留意見",)


def classify_opinion_type(pages: list[dict]) -> str:
    """從 ``auditor_report`` 頁的段落標題判斷查核意見類型。

    找不到任何非無保留意見的關鍵字時回傳 ``"unmodified"``——這是預設值而非
    「已確認為無保留」的斷言，但這個語料至今抽到的每一份都確實是無保留意見
    （非營利園財報公開發布的前提之一），若真的出現保留或以下的意見，標題會
    直接寫出來，不需要額外的「找不到就當作缺資料」路徑。
    """
    headings = " ".join(
        str(section.get("heading") or "")
        for page in pages
        for section in (page.get("text_sections") or [])
    )
    if any(keyword in headings for keyword in _UNMODIFIED_KEYWORDS):
        return "unmodified"
    for keyword, opinion in _OPINION_KEYWORDS:
        if keyword in headings:
            return opinion
    return "unmodified"
