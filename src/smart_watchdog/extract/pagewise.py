"""Page-level extraction contract: any page, not just the three we knew about.

``schema.py`` describes one statement whose page we located in advance. That
contract carried the pipeline as far as the three tables plus four notes the
forensic checks needed -- 1,180 of the corpus's 5,162 pages. The remaining 77%
was never read, and it is not filler: 現金流量表 and 淨值變動表 both live there,
as do the 收支明細表 breakdowns that say what a lump-sum expenditure line is
actually made of.

This schema holds *a* page rather than *every* page: what has actually been
extracted is a targeted selection (see ``scripts/plan_from_toc.py``), and the
container has to work for whatever page that selection names.

The difference in contract is that here **the page type is an output, not an
input**. We do not know what is on page 27 of a report we have never opened, and
the layouts are not positionally predictable (`render_nonprofit_pages.py`
documents the 110-era reports that fold 附註三/五 into differently-titled
sections). So one schema has to be able to hold any page.

## Why a page holds a *list* of tables

The first version of this schema gave the page one ``period_labels`` list, on the
assumption that a page shows one table. It does not. N01 安溪 113 p17 prints the
人事費 detail for 112.8.1~113.7.31 in its upper half and the 業務費 detail for
**113.8.1~114.7.31** in its lower half. With a single column header list the
second table's figures were written under the first table's periods -- a silent
one-year shift in exactly the kind of number this project exists to check. The
model noticed and said so in ``issues``; the container simply could not express
it. Pages carrying a note plus an embedded 關係人交易 table (p22, p23) failed the
same way from the other direction: 49 rows across 86 pilot pages ended up with
values and *no* column headers at all.

So the unit is the table, not the page, and prose is a parallel list rather than
one blob. A page is then: some tables, some text, and the footer code.

## Why alignment is validated rather than trusted

Structured output guarantees the JSON *shape*, not that ``values`` lines up with
``period_labels``. ``validate_page`` checks the one invariant that makes a row
aggregatable -- one value per column -- and marks the table ``aligned: false``
when it does not hold. Downstream aggregation refuses unaligned tables instead of
silently averaging a misaligned row into a feature. The page is still kept: its
prose and its other tables are unaffected, and throwing away a whole scanned page
because one embedded table was ragged loses more than it protects.

``footer_code`` is not metadata. Attributing numbers to the wrong institution is
this pipeline's most severe failure mode -- worse than misreading a figure,
because every compliance finding downstream then names the wrong 園, and it has
happened twice before (see ``docs/EXTRACTION_GUIDE.md`` step zero). Every page of
these reports is printed with ``<代號>-<頁碼>`` in the footer, so the model reads
it back and the runner quarantines any page whose code disagrees -- **or cannot
be read at all**. An unread footer is an unknown, and an unknown must not enter
the corpus under a name we merely assumed.

The numeric conventions are deliberately identical to ``schema.EXTRACTION_PROMPT``
-- blank is ``null`` and never ``0``, parentheses are negative, columns align by
visual position -- because the two extractions land in the same tables and a
convention that holds for one page but not its neighbour is worse than no
convention at all.
"""

from __future__ import annotations

import dataclasses

#: Page kinds. ASCII keys so downstream grouping never depends on the model
#: reproducing a Chinese label byte-for-byte; the printed title is kept verbatim
#: in each table's ``title`` for anyone who needs the original wording.
PAGE_KINDS = [
    "cover",              # 封面
    "toc",                # 目錄
    "auditor_report",     # 會計師查核報告／查核意見
    "balance_sheet",      # 資產負債表
    "income_statement",   # 收支餘絀表
    "cash_flow",          # 現金流量表
    "equity_change",      # 淨值變動表
    "detail_schedule",    # 收支明細表及各類明細附表
    "note",               # 財務報表附註
    "property_list",      # 財產目錄
    "blank",              # 空白頁
    "other",
]

_ITEM_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "description": "項目名稱，逐字照抄"},
        "note_ref": {
            "type": ["string", "null"],
            "description": "附註欄內容，例如「二、三」；無則 null",
        },
        "values": {
            "type": "array",
            "items": {"type": ["number", "null"]},
            "description": (
                "金額，**長度必須等於本表的 period_labels 長度**，順序對齊。"
                "空白格填 null 而非 0。括號代表負數，須轉為負值。"
                "破折號或 - 視為空白填 null"
            ),
        },
        "percents": {
            "type": ["array", "null"],
            "items": {"type": ["number", "null"]},
            "description": "百分比欄（若表上有），順序對齊 period_labels",
        },
    },
    "required": ["label", "values"],
    "additionalProperties": False,
}

_TABLE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title": {
            "type": ["string", "null"],
            "description": "本表表頭或標題，逐字照抄（例如「現金流量表」「人事費明細表」）",
        },
        "context_heading": {
            "type": ["string", "null"],
            "description": (
                "本表上方**最近的一個印刷標題**，逐字照抄，例如"
                "「附表二：經費流用及勻支檢查表」「(3) 代收代付收入及支出」"
                "「三、重要會計項目說明」。表格自己沒有標題時，這是唯一能判斷"
                "它是什麼的線索，務必填。真的沒有任何上層標題才填 null"
            ),
        },
        "period_labels": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "**本表自己的**金額欄標題，逐字照抄且維持左至右順序。"
                "同一頁的另一張表若期間不同，必須放在另一個 table 物件裡，"
                "絕不可共用這一組標題"
            ),
        },
        "unit": {"type": ["string", "null"], "description": "金額單位，例如 新臺幣元"},
        "items": {"type": "array", "items": _ITEM_SCHEMA, "description": "本表的資料列"},
    },
    "required": ["period_labels", "items"],
    "additionalProperties": False,
}

_TEXT_SECTION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "heading": {
            "type": ["string", "null"],
            "description": "本段標題，逐字照抄（例如「五、關係人交易」）；無則 null",
        },
        "text": {
            "type": "string",
            "description": (
                "本段全文逐字轉錄，保留 (一)(二)(三) 款次編號與換行。"
                "**這是轉錄不是摘要**"
            ),
        },
    },
    "required": ["text"],
    "additionalProperties": False,
}

PAGE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "footer_code": {
            "type": ["string", "null"],
            "description": (
                "頁尾印刷的機構代號，格式 <代號>-<頁碼> 的前半，例如 N16-5 的 N16。"
                "看不到頁尾或無法辨識填 null，不要從內文推測"
            ),
        },
        "printed_page": {
            "type": ["integer", "null"],
            "description": "頁尾印刷的頁碼（<代號>-<頁碼> 的後半）。無則 null",
        },
        "page_kind": {
            "type": "string",
            "enum": PAGE_KINDS,
            "description": "本頁主要性質。依表頭或節標題判斷，不要依頁碼位置推測",
        },
        "tables": {
            "type": "array",
            "items": _TABLE_SCHEMA,
            "description": (
                "本頁的所有表格，由上而下。**每張表各自帶自己的 period_labels**。"
                "沒有表格則填空陣列"
            ),
        },
        "text_sections": {
            "type": "array",
            "items": _TEXT_SECTION_SCHEMA,
            "description": "本頁的敘述性段落，由上而下。純表格頁填空陣列",
        },
        "issues": {
            "type": "array",
            "items": {"type": "string"},
            "description": "辨識困難、文件自我矛盾、任何需人工判讀之處",
        },
    },
    "required": ["footer_code", "page_kind", "tables", "text_sections", "issues"],
    "additionalProperties": False,
}


PAGE_PROMPT = """你正在把一張掃描的非營利幼兒園財務報告頁面轉成結構化資料。這份資料會
用於政府監理稽查排序，數字錯誤會導致對合法機構的錯誤指控，因此**準確性遠重於完整性**。

這一頁可能是任何東西——封面、目錄、會計師查核報告、四張主要報表之一、明細附表、
財務報表附註、財產目錄，或空白頁。**先看表頭或節標題判斷它是什麼，不要用頁碼推測。**

## 先做這件事：讀頁尾代號

每頁頁尾印有 `<代號>-<頁碼>`（例如 `N16-5`）。**先讀它並填入 `footer_code` 與
`printed_page`。** 這是防止把數字歸到錯誤機構的唯一護欄，比頁面上任何數字都重要。
看不到或無法辨識就填 null，**不要從內文的園名推測代號**。

## ⚠️ 一頁可能有多張表，而它們的期間未必相同

這是本任務最容易出錯的地方。**同一頁上下半部常是兩張不同的明細表，期間不同**
（例如上半是 112.8.1~113.7.31 的人事費明細，下半是 113.8.1~114.7.31 的業務費明細）。

**每一張表都要是 `tables` 裡獨立的一個物件，帶自己的 `period_labels`。**
絕對不要把兩張表的列合併到同一個物件、也不要讓第二張表沿用第一張表的欄位標題——
那會讓數字被掛到錯誤的年度，是比看錯數字更嚴重的錯誤。

附註頁裡內嵌的小表格（例如關係人交易明細、賸餘款執行概況表）**也是一張表**，
同樣要有自己的 `period_labels`。如果那張小表只有一個金額欄，就填一個標題；
連標題都沒印，就用該欄實際代表的期間逐字填寫。

## ⚠️ 沒有標題的表，一定要填 `context_heading`

很多明細表本身不印表頭，只靠上方的章節標題辨識。**這種表如果連
`context_heading` 都空著，後續就再也分不出它是「代收代付收入及支出」還是
「專案補助收入及支出」**——兩者的法遵意義完全相反。

所以每一張表都要回答「它在這一頁的哪個標題底下」：往上找**最近的一個印刷標題**，
逐字照抄進 `context_heading`。例如：

- 表格上方印著「附表二：經費流用及勻支檢查表」→ 照抄這一整串
- 表格上方印著「(3) 代收代付收入及支出」→ 照抄「(3) 代收代付收入及支出」
- 只找得到更上層的「三、重要會計項目說明」→ 就填那個

`title` 是表格自己印的表頭，`context_heading` 是它所屬的章節，兩者都要盡量填。

**每一列的 `values` 長度必須等於該表 `period_labels` 的長度。** 若某列跳過前面幾欄，
前面補 null 讓數字落在正確位置，而不是縮短陣列。

## 數字規則（與本專案其餘抽取完全一致）

1. **逐字照抄，不要正規化。** 項目名稱、期間標題、單位照表面文字抄，包含全形空白
   與標點。不要把「合　　計」改成「合計」。

2. **空白格填 null，不要填 0。** 這兩者意義完全不同：「業務發展費」預算欄空白代表
   「未編列預算」，是稽查發現；填 0 是對機構捏造財務陳述。
   破折號（—、-、`$ -`）也視為空白，填 null。

3. **括號是負數。** `(1,988,771)` 要輸出 `-1988771`。百分比同理：`(2)` → `-2`。

4. **欄位對齊靠視覺位置，不是靠順序。** 請逐欄對照表頭的垂直位置確認。

5. **看不清楚就說看不清楚。** 若紅色關防遮住文字、數字模糊、或欄位歸屬不確定，
   該格填 null 並在 `issues` 說明。**絕對不要猜測數字。**
   寧可回報缺漏讓人工補，也不要產生看似合理的錯誤數字。

6. **包含所有小計與合計列**（流動資產合計、資產總計、負債及餘絀總計等）。
   這些是驗算用的，缺了就無法自動檢核。

7. **章節標題列也要收錄**（流動資產、非流動資產、流動負債、餘絀、收入、支出）。
   這些列本身沒有金額，`values` 全填 null（長度仍須對齊 period_labels）。

8. **`label` 只放項目名稱本身，不要保留階層縮排的前導空白。** 報表用縮排表示層級，
   那是版面而非名稱。項目名稱內部的全形空白要保留（如「合　　計」）。

9. **貨幣符號 `$` 不計入數值。**

## 敘述段落

附註、查核報告這類以文字為主的內容，依標題拆成 `text_sections`，每段
**逐字轉錄**進 `text`，保留 (一)(二)(三) 款次編號與換行。
**這是轉錄不是摘要**——判斷哪一款重要是分析，不是抽取。
看不清的字用 `□` 標記並在 `issues` 說明。

同一頁同時有表格與文字時，兩邊都要填。

## 形近字已定案，不要重新判讀

附註固定出現三組字，全部以 12 倍放大驗證過：**賸**餘款（不是膡、膸）、
累積餘**絀**（不是餘紐）、**倘**有不足（不是尚）。
⚠️ 不要用「左偏旁是月還是貝」分辨賸／膡——「賸」＝月＋关＋貝，左偏旁本來就是月。

## 空白頁

沒有內容的頁面：`page_kind` 填 `blank`，`tables` 與 `text_sections` 填空陣列。
不要編造內容。

只輸出符合 schema 的 JSON，不要加任何說明文字。"""


@dataclasses.dataclass
class PageResult:
    """One extracted page plus everything needed to audit how it was produced."""

    key: str                      # <code>_<short>_<year>/p<NN>
    payload: dict
    backend: str
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None
    raw_response: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.payload)


def identity_ok(payload: dict, expected_code: str) -> bool | None:
    """Does the page's printed footer code match the report it came from?

    Returns ``None`` when the model could not read a footer at all. That is an
    unknown, not a pass -- the runner treats it the same as a mismatch, because a
    page we cannot attribute must not enter the corpus under an assumed name.
    """
    got = payload.get("footer_code")
    if not got:
        return None
    return str(got).strip().upper() == expected_code.strip().upper()


def validate_page(payload: dict) -> list[str]:
    """Check the one invariant that makes a row aggregatable, and mark each table.

    Mutates each table in ``payload`` with ``aligned`` so downstream aggregation
    can refuse the ragged ones without re-deriving this. Returns human-readable
    problems for the run log; an empty list means every table is aggregatable.

    Structured output guarantees the JSON shape, never the semantics: a table can
    be perfectly well-formed JSON and still carry two numbers under zero column
    headers, which is what 49 rows of the pilot did.
    """
    problems: list[str] = []
    for t, table in enumerate(payload.get("tables") or []):
        labels = table.get("period_labels") or []
        rows = table.get("items") or []
        valued = [r for r in rows if r.get("values")]
        ragged = [r for r in valued if len(r.get("values") or []) != len(labels)]
        table["aligned"] = not ragged
        if not labels and valued:
            problems.append(
                f"表 {t + 1}（{table.get('title') or '無標題'}）有 {len(valued)} 列數字"
                f"但沒有任何 period_labels"
            )
        elif ragged:
            problems.append(
                f"表 {t + 1}（{table.get('title') or '無標題'}）有 {len(ragged)} 列的"
                f"values 長度不等於 period_labels（{len(labels)} 欄）"
            )
    return problems


def has_content(payload: dict) -> bool:
    """True when the page carried anything worth storing."""
    if any((t.get("items") or []) for t in (payload.get("tables") or [])):
        return True
    return any((s.get("text") or "").strip() for s in (payload.get("text_sections") or []))
