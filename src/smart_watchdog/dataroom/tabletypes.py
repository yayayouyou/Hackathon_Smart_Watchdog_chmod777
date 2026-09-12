"""表單類型：把 4,450 張表分到可以當成選單的類別。

分類鍵為什麼是 ``section`` 而不是 ``page_kind``
------------------------------------------------
``page_kind`` 是模型逐頁自報的 11 種頁型，不能當表單分類：
``detail_schedule`` 與 ``note`` 兩種就吃掉七成，而且只有 17 份報告有
``balance_sheet`` 頁——那不是抽取失敗，是 ``scripts/plan_from_toc.py``
刻意只挑目錄命中的六類章節。用它分類，一百多份報告的「資產負債表」
會是空的。

``section`` 由標題字串路由而來（``scripts/build_pagewise_facts.py``
的 ``ROUTES``），一張表一個類別，這才是使用者要切換的東西。

三件事這裡與 ``build_pagewise_facts.py`` 不同
--------------------------------------------
1. **多了六條路由。** 原規則漏掉的教保費收入明細與延長照顧收支，
   合計 200 餘張、涵蓋幾乎每一份報告；教保費正是收費漲幅特徵的來源，
   是這一室最該秀的表。另四條收錯字與寫法變體（原件印的是
   「資**遺**費準備」而不是「資遣」）。
   **一律附加在尾端。** ``route()`` 是逐條掃、第一個命中就贏，
   「教保費」是短詞，插在 ``income_by_function`` 之前會把功能別表搶走。

2. **續表繼承。** 跨頁續印的表不重印標題，於是整張掉進 unrouted。
   規則：``pdf_page`` 差**必須等於 1**、且 title 與 heading 皆空者，
   繼承前一張表的 section。

   ⚠️ **同頁不繼承（差 0 不算）。** 同一頁的下一張表通常是新的一張表，
   不是續表：``N01_安溪_113`` p17 一頁印兩張人事費／業務費明細，
   期間分別是 112.8.1~113.7.31 與 113.8.1~114.7.31。把同頁的下一張
   當續表，就是 CLAUDE.md 那條「一頁一組表頭造成無聲的一年偏移」的同型錯誤。

   ⚠️ **繼承只補分類鍵，不代表它是續表。** 實測繼承到的表裡有七成
   表頭與前一張不同（多數是前一年度的比較表）。所以繼承一律標
   ``section_inherited``，而且**絕不可把父子表的列串接**。

3. **比對前做正規化**：去掉全形／半形空白。顯示一律用原字串。

這個模組不寫檔、不改 ``data/processed``，只是讀同一批頁 JSON 時
換一組分類規則。
"""

from __future__ import annotations

from collections.abc import Iterable

#: 原 `build_pagewise_facts.py::ROUTES`，順序即優先序（第一個命中就贏，
#: 所以具體的排在一般的前面）。這一段**逐字保持同步**，不要在中間插隊。
BASE_ROUTES: list[tuple[str, tuple[str, ...]]] = [
    ("cash_flow", ("現金流量表",)),
    ("equity_change", ("淨值變動表",)),
    ("budget_transfer", ("經費流用", "勻支")),
    ("multiyear_compare", ("各學年收支預決算比較", "收支預決算比較")),
    ("income_by_function", ("功能別",)),
    ("agency_passthrough", ("代收代付",)),
    ("agency_subsidy", ("代收補助",)),
    ("project_subsidy", ("專案補助",)),
    ("net_difference", ("淨額差異",)),
    ("severance_reserve", ("資遣費準備",)),
    ("development_reserve", ("業務發展準備",)),
    ("other_income_expense", ("其他收入及其他支出", "其他收入及支出")),
    ("admin_fee", ("行政管理費",)),
    ("payables", ("其他應付款", "應付款項")),
    ("cash_detail", ("現金及銀行存款",)),
    ("performance_review", ("績效考評",)),
    ("auditor_checklist", ("會計師查核附表", "查核項目")),
    ("personnel_detail", ("人事費",)),
    ("operating_detail", ("業務費",)),
    ("material_detail", ("材料費",)),
    ("maintenance_detail", ("維護費", "修繕購置")),
    ("surplus_execution", ("賸餘款",)),
    ("related_party", ("關係人",)),
    ("property", ("財產",)),
    ("balance_sheet", ("資產負債表",)),
    ("income_statement", ("收支餘絀表",)),
    ("note_items", ("重要會計項目說明",)),
]

#: 附加在尾端的六條。前兩條是真正漏掉的類別，後四條是原件的錯字與變體。
#: 位置不可上移：短詞放前面會把更具體的表搶走。
EXTRA_ROUTES: list[tuple[str, tuple[str, ...]]] = [
    ("tuition_income", ("教保費",)),
    ("extended_care", ("延長照顧",)),
    ("operating_assets", ("營運資產",)),
    # 原件錯字：「資遺費」。只在這裡收，不動 BASE_ROUTES 的字串。
    ("severance_reserve", ("資遺費準備",)),
    # 「各學年收支預算比較表」「各學年收支預算決算比較表」：少一個字的變體。
    ("multiyear_compare", ("收支預算比較", "收支預算決算比較")),
    # 「收支決算表」「收支結餘表」：收支餘絀表的別稱。
    ("income_statement", ("收支決算表", "收支結餘表")),
    # 最一般的那條放最後：帶「收支明細表」但沒有任何細項名稱的封面式標題。
    ("income_detail", ("收支明細表",)),
]

ROUTES: list[tuple[str, tuple[str, ...]]] = BASE_ROUTES + EXTRA_ROUTES

UNROUTED = "unrouted"

#: 給人看的名字。key 沒列在這裡時，前端一律退回顯示 key 本身而不是猜。
SECTION_ZH: dict[str, str] = {
    "balance_sheet": "資產負債表",
    "income_statement": "收支餘絀表",
    "cash_flow": "現金流量表",
    "equity_change": "淨值變動表",
    "tuition_income": "教保費收入明細",
    "income_by_function": "收支餘絀表－功能別",
    "project_subsidy": "專案補助收支",
    "other_income_expense": "其他收入及其他支出",
    "agency_subsidy": "代收補助收支",
    "agency_passthrough": "代收代付收支",
    "extended_care": "延長照顧服務收支",
    "income_detail": "收支明細表（總表）",
    "personnel_detail": "人事費明細",
    "operating_detail": "業務費明細",
    "material_detail": "材料費明細",
    "maintenance_detail": "維護費及修繕購置費明細",
    "budget_transfer": "經費流用及勻支檢查表",
    "multiyear_compare": "各學年收支預決算比較表",
    "severance_reserve": "資遣費準備",
    "development_reserve": "業務發展準備",
    "operating_assets": "營運資產",
    "property": "財產目錄",
    "cash_detail": "現金及銀行存款",
    "surplus_execution": "賸餘款執行概況",
    "payables": "其他應付款",
    "admin_fee": "行政管理費",
    "auditor_checklist": "會計師查核附表",
    "performance_review": "績效考評",
    "related_party": "關係人交易",
    "net_difference": "淨額差異",
    "note_items": "重要會計項目說明",
    UNROUTED: "未分類明細",
}


def zh(section: str) -> str:
    """類別的中文名。查不到就回原 key——猜一個好看的名字會讓人以為分對了。"""
    return SECTION_ZH.get(section, section)


def normalise(text: str | None) -> str:
    """比對用的正規化：去掉所有空白（含全形）。顯示一律用原字串。"""
    if not text:
        return ""
    return "".join(str(text).split()).replace("　", "")


def route(title: str | None, heading: str | None) -> str:
    """這張表屬於哪一類。標題優先，其次段落標題；都不中回 ``unrouted``。"""
    for text in (title, heading):
        flat = normalise(text)
        if not flat:
            continue
        for key, needles in ROUTES:
            if any(n in flat for n in needles):
                return key
    return UNROUTED


def route_tables(tables: Iterable[dict]) -> list[dict]:
    """替一份報告的所有表定出 section，含跨頁續表繼承。

    ``tables`` 需已依 ``(pdf_page, table_index)`` 排序，每項至少要有
    ``pdf_page`` / ``title`` / ``context_heading``。

    回傳同長度的 list，每項 ``{"section", "section_inherited"}``。
    ``section_inherited`` 為 True 時，這張表的類別是**推論**而非標題所寫，
    UI 與 agent 都必須看得到這件事。
    """
    out: list[dict] = []
    prev_page: int | None = None
    prev_section: str | None = None

    for t in tables:
        page = t.get("pdf_page")
        sec = route(t.get("title"), t.get("context_heading"))
        inherited = False

        if sec == UNROUTED and not normalise(t.get("title")) \
                and not normalise(t.get("context_heading")) \
                and prev_section and prev_section != UNROUTED \
                and prev_page is not None and page is not None \
                and page - prev_page == 1:
            # 跨頁續印、且自己沒有任何標題可循，才繼承。
            sec = prev_section
            inherited = True

        out.append({"section": sec, "section_inherited": inherited})
        # 鏈式繼承：這一張拿到的類別可以再傳給下一頁。
        if page is not None:
            prev_page, prev_section = page, sec

    return out
