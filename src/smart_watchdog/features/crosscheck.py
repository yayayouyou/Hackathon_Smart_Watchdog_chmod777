"""把公共化園的決算／財報，跟收費明細與園所基本資料對起來查。

## 這個模組補的是哪個洞

公共化園（22 所市立幼兒園的決算書、38 所非營利園的財務報告）**依法都已公告**，
`features/compliance.py` 也已經拿每份報告自己的附註二檢核過它自己，
`features/compliance_public.py` 則檢核決算書自身的預算執行。兩者的共同限制是
**只讀一份文件**：文件內部自我一致，就查不出東西。

而公開資料裡本來就存在另外兩組數字，跟財報講的是同一件事：

* **收費明細**（全國教保資訊網／kiang 鏡像 `slip{學年度}`）——每名幼兒的申報收費。
* **園所基本資料**（同一鏡像的 `preschools.json` → `institutions_ntpc.csv`）——
  核定招收人數、分班結構、每月收費。

財報收入、每生收費、招收人數這三個量之間有算術關係。任兩個可以驗第三個，
而第三個在單看一份文件時是查不到的——**市立幼兒園的決算書完全不含幼兒人數**。

## 已驗證的恆等式：教保費收入 = 申報收費年額 × 實際招收人數

110 學年度是唯一同時有非營利財報與非營利收費明細的學年度（見下節），28 份報告：

    教保費收入決算 ÷ 實際招收人數 ÷ 收費明細年額
      中位數 1.0004　四分位 0.997–1.006　27/28 落在 ±4% 內

唯一的例外是 N28 新樂 110 學年度的 0.455，而它的成因是確定的：那份報告的收支餘絀表
期間自己就寫「民國111年2月1日至111年7月31日」——只有 6 個月，不是一整個學年度。
所以這個檢核以**報告自己申報的期間月數**當閘門，不足 12 個月一律回報「資料不足」
並附上按月數還原後的比值供參（新樂還原後為 0.91，仍不宜與全年收費表直接相比，
因為開辦期招收人數是逐月墊高的）。

⚠️ **不可用 `compliance.is_opening_year` 當這個閘門。** 那個函式問的是「契約是否在本
學年度內起算」，服務的是準備金專戶還沒開的情形。110 學年度有 9 所園契約自 110/8/1
起算（換約或續約），但登記設立日在 2017–2021、收支餘絀表期間是完整的
110.8.1–111.7.31——把它們一起排除會讓可比的 28 份掉到 19 份，而它們本來全部通過。

這條恆等式成立，代表非營利園收費明細上的「全學期總收費」不是家長自付額，而是
**政府與家長分攤前的每名幼兒營運成本**——正好對應附註二(五)所定義的收入總額
（「家長繳交之費用；其有政府差額補助費者，應合併計算」）。主檔 `monthly` 欄
（非營利園一律 2,000）只有它的 21–26%，兩者不可互換。

## ⚠️ 外部收費明細自 111 學年度起查不到非營利園，但財報自己補上了彙總

鏡像 `slip{年}/新北市/` 目錄逐年清點（`scrape.fees.list_slip_files`）：

    學年度      109    110    111    112    113    114
    全市檔數   1,115  1,147  1,109  1,108  1,098  1,085
    含「非營利」   23     31      0      0      0      0
    公立覆蓋    284    287    287    291    289    286

全市檔數沒有掉，公立園的覆蓋率也沒有掉，唯獨非營利園整類歸零，所以不是爬取失敗。
有財報又有**逐項**收費的只有 110 學年度的 28 園年 / 132（21.2%）。

⚠️ **這個缺口比我第一版寫的窄。** 第一版的結論是「111 學年度以後無法確認家長負擔
與政府補助各佔多少」——那是錯的。財報附註三「收支明細表 1. 教保費收入」把兩者分列：

    教保費收入（家長繳費）        1,940,500
    教保費收入（政府學費差額補助）   8,128,760
    小　　計                 10,069,260   ← 等於收支餘絀表的教保費收入
    五日未上課退費 / 轉學退費 / 腸病毒退費 …
    教保費收入淨額

覆蓋 **115/132 份（87.1%）**，四個學年度都有，而且 `家長繳費 + 政府補助 = 同表小計
= 收支餘絀表教保費收入` **115/115 完全相符**（`nonprofit_guards`）。

所以真正還缺的是**費目層級**：學費／雜費／材料費／活動費／餐點費各收多少，只有申報
的收費明細有。附註二(十)3.(2)「餐點費，不得移作他用」要對的就是餐點費那一項，
那一項在 111 學年度後的公開資料裡沒有。這是缺口的正確範圍。

**沒有收費明細不等於沒有申報。** 上面的歸零是資料可得性缺口，鏡像未收錄與該園
未申報在公開資料上無法區分，所以本模組**不會**據此指涉幼照法第 38 條。

## 拆開之後，趨勢的結論反轉

每生教保費收入（＝每名幼兒年度營運成本）中位數 110→113 學年度上升 34.6%。
第一版就停在這裡，並說不知道是誰在付。拆開附註三之後（僅完整學年度）：

    學年度            110      111      112      113      變動
    每生家長繳費     21,998   16,414   16,509   15,675   −28.7%
    每生政府補助     80,240   96,539  100,031  121,719   +51.7%
    家長分攤比率      0.209    0.153    0.146    0.113   −45.9%

**成本上升，但家長每生負擔是下降的，增幅由政府端承擔。** 這與 111 年 8 月起的
學前補助政策方向一致，本身不是異常。所以這條發現的問句要改：不是「誰在付」，
而是「核定之營運成本分攤數逐年調升的依據是什麼」，以及費目層級無法覆核這件事。

## 年度對應

* 非營利財報用**學年度**，收費明細也用學年度，直接對。
* 市立幼兒園決算書用**年度**（曆年）。年度 N 的學雜費收入橫跨
  學年度 N−1 下學期與學年度 N 上學期，所以年基準取這兩個半年額之和。
  新北市公立幼兒園 110–114 學年度的學費＋雜費固定為每學期 7,675 元
  （284–291 所園全部相同），所以這個對應在數值上不會改變答案，但寫對它才不會
  在收費調整的那一年算錯。
* 每一列都帶 `year_kind`，下游不可能把兩種年度誤 join。

## 核定人數是快照，不是當年度值

`institutions_ntpc.csv` 的 `count_approved` 來自釘住的鏡像快照，不是 112 年度當時
的核定數。以它當滿園率的分母有時序風險，所以：

* 每一列都帶 `capacity_basis`，說明分母是快照值；
* 同時算「預算隱含人數 ÷ 核定人數」當護欄——決算書自己編列的學雜費預算除以收費
  基準，58／58 個園年都落在 1.03 以內、無一超過 1.05。預算是照核定量能編的，
  所以這個上界同時驗證了收費基準是對的分母、也界定了快照能有多舊。

## 基準率先算，門檻才設（CLAUDE.md 規則）

    非營利：實際招收 > 財報核定             0/132   0.0%  → 不列為規則（護欄）
    非營利：實際招收 > 登記核定             0/132   0.0%  → 不列為規則（護欄）
    非營利：附註三家長+政府 ≠ 教保費收入     0/115   0.0%  → 不列為規則（護欄）
    非營利：財報核定 vs 登記核定 差 > 20   16/132  12.1%  → 採用
    非營利：師生比 ≥ 13                     7/132   5.3%  → 採用（法定上限 15，
                                                          實測最大恰為 15.0）
    非營利：隱含收費年額同年度 |z| ≥ 2      2/129   1.6%  → 採用（低基準率本身
                                                          就是結論：個別園沒有
                                                          離群，上升是全體的）
    非營利：家長月均實繳 > 登記月費          4/115   3.5%  → 採用（明線檢核，
                                                          低基準率是預期的）
    非營利：家長分攤比率同年度 |z| ≥ 2       9/115   7.8%  → 採用
    公校：滿園率同年度 z ≤ −2               6/58   10.3%  → 採用
    公校：每生政府投入同年度 z ≥ +2         6/58   10.3%  → 採用

師生比那一條是本模組唯一**單一來源**的檢核（只讀附註一）。放在這裡是因為
招收人數是本模組的樞紐，而 `compliance.py`（讀附註二）與 `compliance_public.py`
（讀決算書）都不碰它。它的 `sources` 欄如實只寫一份來源，不假裝是跨來源。

公校那兩條若改用純 MAD 尺度會各變成 8/58 與 7/58。這裡用的是
`features/anomaly.robust_scale`（MAD 與 IQR 取較大者），它在中位數附近很密時
不會製造出巨大的 z，代價是稍微保守——這個取捨的理由寫在該函式的 docstring。

## 輸出定位

每一項都是**建議查核**的問題，不是違法認定。偏遠小校的每生成本天生偏高、非營利園
是成本分攤制、招生不足是少子化與學區變化的結果——這些都寫進 `detail` 裡，讓看的人
自己判斷，而不是由本模組代為結論。不確定時標「資料不足」，不標「低風險」。
"""

from __future__ import annotations

import dataclasses
import re

from smart_watchdog.features import anomaly

#: 一個完整學年度的月數。
FULL_YEAR_MONTHS = 12

#: 幼兒園及其分班基本設施設備標準：室內活動室每人不得少於 2.5 平方公尺
#: （招收 2 歲以上至入國民小學前幼兒之班級）。這裡只拿來當「哪一邊的核定人數
#: 與實體空間相容」的旁證，不當設施違規的認定——主檔的 size_in 是全園室內面積，
#: 不是室內活動室面積，兩者範圍不同。
INDOOR_AREA_PER_CHILD_MIN = 2.5

# ── 恆等式容差 ────────────────────────────────────────────────────────
#: 教保費收入 = 收費年額 × 實際招收人數 的相對容差。實測 27/28 落在 ±4%，
#: 四分位距只有 0.9pp，所以 5% 已經寬鬆到不會把正常填報誤標。
FEE_IDENTITY_TOL = 0.05

#: 「預算隱含人數 ÷ 核定人數」的護欄上界。58/58 實測最大 1.029。
#: 超過代表收費基準錯了或核定人數快照過舊，是關於本管線的消息，不是關於園的。
BUDGET_IMPLIED_CAP = 1.05

# ── 同儕相對門檻 ──────────────────────────────────────────────────────
#: 穩健 z 的回報門檻。刻意比 `features/anomaly.REASON_Z`（3.5）寬：那裡是排序用的
#: 異常標記，這裡是挑出「要問一句」的名單，且兩邊都有第二個來源可以覆核。
PEER_Z = 2.0

#: 教保服務人員配置的法定上限（3 歲以上每 15 名幼兒 1 人）。實測最大恰為 15.0，
#: 無一超過，所以這不是用來抓違規的，而是用來標「已貼齊上限、無緩衝」。
STAFF_RATIO_LEGAL = 15.0
STAFF_RATIO_WATCH = 13.0

#: 財報核定人數與登記核定人數的可容許差距（人）。
CAPACITY_GAP_TOL = 20

# ── 附註三「收支明細表 1. 教保費收入」的家長／政府拆分 ────────────────
#: 兩種括號都出現（全形與半形），且同一份報告內不混用。
PARENT_FEE_LABELS = frozenset({"教保費收入（家長繳費）", "教保費收入(家長繳費)"})
GOV_SUBSIDY_LABELS = frozenset({
    "教保費收入（政府學費差額補助）", "教保費收入(政府學費差額補助)"})
#: 小計列的空白會被排版拉開（「小　　計」）；正規化後仍有兩種長度。
SUBTOTAL_LABELS = frozenset({"小計", "小　計", "小　　計"})

#: 教保費收入拆分的對帳容差（元）。實測 115/115 完全相符，所以 1 元已經很寬。
FEE_SPLIT_TOL = 1.0


@dataclasses.dataclass
class CrossCheck:
    """一項跨來源的查核問題。

    形狀刻意與 `features/compliance.Check` 一致（`passed` 三態、`rule_text` 帶
    依據、`detail` 帶算術），這樣既有的 `risk/priority.compliance_summary` 與
    `api/payload.dossiers` 不必改就能消費。多出來的三欄是跨來源才需要的：

    * `sources` —— 這一項join 了哪幾個來源。單一文件的檢核不需要這欄，
      跨來源的檢核**必須**有，因為看的人要知道去翻哪兩份東西。
    * `year_kind` —— 「學年度」或「年度」。公校與非營利不可對齊。
    * `entity_type` —— 公立／非營利。同一張表混兩種母體，分層前不得比較。
    """

    code: str
    short_name: str
    academic_year: str
    rule: str
    rule_text: str
    passed: bool | None  # None = 資料不足，無法判斷
    detail: str
    severity: str  # high | medium | low
    sources: str
    year_kind: str
    entity_type: str

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def _f(v: object) -> float | None:
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if x != x else x


#: 收支餘絀表期間欄的日期，兩種寫法都要吃：「民國110年8月1日至111年7月31日」
#: 與「110.8.1~111.7.31」。同一份報告有時兩種並列（表頭一種、括號裡另一種），
#: 所以只取前兩個日期——後面的一律是同一段期間的重複標示或註解。
_PERIOD_DATE_RE = re.compile(r"(\d{2,3})[年.](\d{1,2})[月.](\d{1,2})")


def period_months(period: object) -> int | None:
    """收支餘絀表期間涵蓋幾個月（含頭含尾），無法判讀時回 None。

    這是「這份報告是不是整年」的唯一可靠依據。用契約起日判斷會誤殺 9 所只是
    換約的園（見模組 docstring），用登記設立日則答不了「這份報表涵蓋多久」——
    報表自己就寫了。
    """
    found = _PERIOD_DATE_RE.findall("".join(str(period or "").split()))
    if len(found) < 2:
        return None
    (y0, m0, _), (y1, m1, _) = found[0], found[1]
    months = (int(y1) * 12 + int(m1)) - (int(y0) * 12 + int(m0)) + 1
    return months if 1 <= months <= 24 else None


def _norm(value: object) -> str:
    return "".join(str(value or "").split())


def _is_own_year(period_label: object, academic_year: object) -> bool:
    """這一欄是不是這份報告自己的學年度（而非比較期）。

    ⚠️ 不能只用 `startswith(學年度)`。學年度 N 一律自 N/8/1 起，但**開辦年的
    比較欄也以 N 開頭**：新樂 111 學年度報告的比較欄是 `111.2.1~111.7.31`
    （111/2/1 開園那半年），菁湖 110 同理。只比年份會挑到比較欄，於是把上一期的
    金額當成本期——這個錯誤是被「家長繳費＋政府補助＝收支餘絀表教保費收入」
    的對帳抓出來的（原本 119/121，修正後 115/115）。
    """
    return bool(re.match(rf"^{_norm(academic_year)}[.年]\s*8[.月]",
                         _norm(period_label)))


def parse_fee_split(facts) -> dict[tuple[str, str], dict]:
    """從頁級事實抽出附註三的教保費收入家長／政府拆分。

    ``facts`` 是 ``data/processed/nonprofit_pagewise_facts.csv`` 的列，需要
    ``code``／``academic_year``／``context_heading``／``table_title``／
    ``item_label``／``period_label``／``value``。

    回傳 {(code, 學年度): {"parent", "gov", "subtotal"}}，只收齊了家長與政府兩側
    的園年。**小計取第一個出現的**：該表下半段還有一個退費小計與「教保費收入淨額」，
    那是扣除退費後的數字，不該拿來跟毛額對帳。
    """
    out: dict[tuple[str, str], dict] = {}
    for f in facts:
        heading = _norm(f.get("context_heading")) + _norm(f.get("table_title"))
        if "教保費收入" not in heading:
            continue
        if "收支明細" not in heading and not heading.endswith("1.教保費收入"):
            continue
        label = _norm(f.get("item_label"))
        if label not in PARENT_FEE_LABELS | GOV_SUBSIDY_LABELS | SUBTOTAL_LABELS:
            continue
        if not _is_own_year(f.get("period_label"), f.get("academic_year")):
            continue
        value = _f(f.get("value"))
        if value is None:
            continue
        bucket = out.setdefault(
            (str(f.get("code")), str(f.get("academic_year"))), {})
        if label in PARENT_FEE_LABELS:
            bucket["parent"] = value
        elif label in GOV_SUBSIDY_LABELS:
            bucket["gov"] = value
        elif "subtotal" not in bucket:
            bucket["subtotal"] = value
    return {k: v for k, v in out.items() if "parent" in v and "gov" in v}


def _z_text(z: float | None, value: float, median: float | None, unit: str) -> str:
    """同儕相對位置的敘述。z 一併列出，但主詞是原始值與中位數。

    `features/anomaly.Reason.as_text` 的理由同樣適用：讀者會把 z=−5 聽成
    「比正常差五倍」。原始值與中位數是稽查員能翻頁核對的東西。
    """
    med = "—" if median is None else f"{median:,.3f}{unit}"
    zs = "—" if z is None else f"{z:+.2f}"
    return f"{value:,.3f}{unit}（同年度中位 {med}，穩健 z={zs}）"


# ══════════════════════════════════════════════════════════════════════
#  非營利園：財報 × 收費明細 × 登記主檔
# ══════════════════════════════════════════════════════════════════════

def nonprofit_metrics(
    report: dict,
    fee_year_amount: float | None,
    registry_capacities: list[float],
) -> dict:
    """一份非營利財報的跨來源衍生量。

    `report` 需要 `code`／`short_name`／`academic_year`／`tuition_income`
    （教保費收入決算）／`approved_capacity`／`actual_enrolment`／`educators`
    ／`material_cost`（材料費決算）／`income_period`（收支餘絀表期間字串）。

    `registry_indoor_area` 是登記主檔的全園室內面積，只用來當核定人數落差的旁證。
    """
    enrol = _f(report.get("actual_enrolment"))
    tui = _f(report.get("tuition_income"))
    cap = _f(report.get("approved_capacity"))
    edu = _f(report.get("educators"))
    mat = _f(report.get("material_cost"))
    months = period_months(report.get("income_period"))
    reg_cap = max(registry_capacities) if registry_capacities else None
    area = _f(report.get("registry_indoor_area"))

    split = report.get("fee_split") or {}
    parent, gov = _f(split.get("parent")), _f(split.get("gov"))
    split_total = (parent + gov) if (parent is not None and gov is not None) else None
    reg_monthly = _f(report.get("registry_monthly"))

    implied = (tui / enrol) if (tui is not None and enrol) else None
    return {
        "code": report.get("code", ""),
        "short_name": report.get("short_name", ""),
        "year": str(report.get("academic_year", "")),
        "year_kind": "學年度",
        "entity_type": "非營利",
        "tuition_income": tui,
        "approved_capacity": cap,
        "actual_enrolment": enrol,
        "educators": edu,
        "material_cost": mat,
        #: 財報自己反推出來的每名幼兒年度營運成本。111 學年度起唯一可用的
        #: 收費代理量，因為收費明細那年起就不在公開資料裡了。
        "implied_fee_per_child": implied,
        "filed_fee_per_child": fee_year_amount,
        "fee_identity_ratio": (implied / fee_year_amount)
        if (implied is not None and fee_year_amount) else None,
        "enrolment_utilisation": (enrol / cap) if (enrol is not None and cap) else None,
        "staff_ratio": (enrol / edu) if (enrol is not None and edu) else None,
        "material_per_child": (mat / enrol) if (mat is not None and enrol) else None,
        "registry_capacity_max": reg_cap,
        "capacity_gap": (reg_cap - cap)
        if (reg_cap is not None and cap is not None) else None,
        "income_period": str(report.get("income_period") or ""),
        "period_months": months,
        #: 部分年度的報告不得進同儕池：新樂 110 只營運 6 個月，它的每生收入
        #: 50,226 是全體中位數的一半，留在池裡會把 MAD 撐大而稀釋真正的離群。
        "full_year": (months is None) or months >= FULL_YEAR_MONTHS,
        #: 報告只涵蓋部分學年度時，把每生收入還原成全年基準供參。
        "implied_fee_annualised": (implied * FULL_YEAR_MONTHS / months)
        if (implied is not None and months) else None,
        "registry_indoor_area": area,
        "area_per_child_registry": (area / reg_cap)
        if (area is not None and reg_cap) else None,
        "area_per_child_report": (area / cap)
        if (area is not None and cap) else None,
        # ── 附註三的家長／政府拆分 ──────────────────────────────────
        "parent_fee": parent,
        "gov_subsidy": gov,
        "fee_split_subtotal": _f(split.get("subtotal")),
        "fee_split_total": split_total,
        "parent_share": (parent / split_total)
        if (parent is not None and split_total) else None,
        "parent_per_child": (parent / enrol)
        if (parent is not None and enrol) else None,
        "gov_subsidy_per_child": (gov / enrol)
        if (gov is not None and enrol) else None,
        #: 家長每月平均實繳。分母是期末在園人數 × 12，所以名冊在學年度中變動時
        #: 這個平均會偏移——是同儕比較與門檻檢核的量，不是某一名幼兒的收費。
        "parent_monthly_avg": (parent / enrol / FULL_YEAR_MONTHS)
        if (parent is not None and enrol) else None,
        "registry_monthly": reg_monthly,
    }


def check_nonprofit(m: dict, peers: dict[str, list[float]]) -> list[CrossCheck]:
    """一份非營利財報的所有跨來源檢核。``peers`` 是同學年度的各項數列。"""
    out: list[CrossCheck] = []

    def add(rule: str, text: str, passed: bool | None, detail: str,
            sev: str, sources: str) -> None:
        out.append(CrossCheck(
            code=m["code"], short_name=m["short_name"], academic_year=m["year"],
            rule=rule, rule_text=text, passed=passed, detail=detail,
            severity=sev, sources=sources, year_kind="學年度",
            entity_type="非營利",
        ))

    # ── 1. 教保費收入 = 申報收費年額 × 實際招收人數 ──────────────────
    text = (
        "收費明細申報之每生全學年度收費 × 財報附註一所載實際招收人數，"
        "應等於收支餘絀表之教保費收入決算數。此對應經 110 學年度 28 份報告實測，"
        "中位比值 1.0004、27/28 落在 ±4% 內，故偏離可視為三個數字之一有誤。"
    )
    src = "收支餘絀表（教保費收入）／財報附註一（實際招收人數）／收費明細（全學期總收費）"
    ratio = m["fee_identity_ratio"]
    if m["filed_fee_per_child"] is None:
        add("教保費收入 = 收費 × 人數", text, None,
            f"{m['year']} 學年度查無非營利園收費明細"
            f"（鏡像自 111 學年度起全市 0 檔，109／110 分別為 23／31 檔）——"
            f"資料不足，非該園未申報",
            "medium", src)
    elif ratio is None:
        add("教保費收入 = 收費 × 人數", text, None,
            "缺教保費收入決算數或實際招收人數", "medium", src)
    elif m["period_months"] is not None and m["period_months"] < FULL_YEAR_MONTHS:
        annual = m["implied_fee_annualised"]
        restored = (annual / m["filed_fee_per_child"]) if annual else None
        add("教保費收入 = 收費 × 人數", text, None,
            f"比值 {ratio:.3f}，但本份收支餘絀表期間為「{m['income_period']}」，"
            f"僅涵蓋 {m['period_months']} 個月而非 {FULL_YEAR_MONTHS} 個月。"
            f"按月數還原後為 "
            f"{'—' if restored is None else format(restored, '.3f')}；"
            f"開辦期招收人數逐月墊高，仍不宜與全年收費表直接相比，"
            f"請以次學年度報告覆核",
            "medium", src)
    elif abs(ratio - 1) <= FEE_IDENTITY_TOL:
        add("教保費收入 = 收費 × 人數", text, True,
            f"教保費收入 {m['tuition_income']:,.0f} ÷ 實際招收 "
            f"{m['actual_enrolment']:,.0f} 人 = 每生 {m['implied_fee_per_child']:,.0f}，"
            f"申報收費年額 {m['filed_fee_per_child']:,.0f}，比值 {ratio:.3f}",
            "medium", src)
    else:
        gap = m["tuition_income"] - m["filed_fee_per_child"] * m["actual_enrolment"]
        add("教保費收入 = 收費 × 人數", text, False,
            f"教保費收入 {m['tuition_income']:,.0f} vs 申報收費年額 "
            f"{m['filed_fee_per_child']:,.0f} × 實際招收 {m['actual_enrolment']:,.0f} 人 = "
            f"{m['filed_fee_per_child'] * m['actual_enrolment']:,.0f}，"
            f"差額 {gap:+,.0f}（比值 {ratio:.3f}）。"
            f"三者之一有誤：請確認收費是否按申報數收取、招收人數是否為期間平均、"
            f"以及教保費收入是否已含政府差額補助",
            "high", src)

    # ── 2. 核定招收人數：財報 vs 登記主檔 ────────────────────────────
    text = (
        "財報附註一所載核定招收總人數，應與全國教保資訊網登記之核定招收人數相符。"
        "核定人數同時是收費、補助、師生比與樓地板面積的分母，任一系統記載不同，"
        "所有以它為基礎的計算都會跟著錯。"
    )
    src = "財報附註一（核定招收人數）／園所基本資料（count_approved）"
    gap = m["capacity_gap"]
    if gap is None:
        add("核定人數 財報=登記", text, None, "缺財報核定人數或登記核定人數",
            "medium", src)
    elif abs(gap) <= CAPACITY_GAP_TOL:
        add("核定人數 財報=登記", text, True,
            f"財報 {m['approved_capacity']:,.0f} 人，登記 "
            f"{m['registry_capacity_max']:,.0f} 人，差 {gap:+,.0f} 人",
            "medium", src)
    else:
        # 第三個來源當旁證：全園室內面積除以兩邊的核定人數，哪一邊跟實體空間相容。
        # 這不判斷設施合規（主檔的 size_in 是全園室內面積，不是室內活動室面積），
        # 只幫看的人決定該去查哪一邊。
        ar, ap = m["area_per_child_registry"], m["area_per_child_report"]
        evidence = ""
        if ar is not None and ap is not None:
            evidence = (
                f"　旁證：登記室內面積 {m['registry_indoor_area']:,.1f} ㎡，"
                f"除以登記核定為 {ar:.1f} ㎡/人、除以財報核定為 {ap:.1f} ㎡/人"
                f"（室內活動室法定下限 {INDOOR_AREA_PER_CHILD_MIN} ㎡/人；"
                f"主檔面積為全園室內面積，範圍較寬）"
            )
        add("核定人數 財報=登記", text, False,
            f"財報 {m['approved_capacity']:,.0f} 人，登記 "
            f"{m['registry_capacity_max']:,.0f} 人，差 {gap:+,.0f} 人"
            f"（容差 {CAPACITY_GAP_TOL} 人）。請確認哪一邊是現行核定數，"
            f"以及以另一邊為基礎核算之收費與補助是否需更正。{evidence}",
            "high" if abs(gap) >= 100 else "medium", src)

    # ── 3. 師生比：實際招收 ÷ 教保服務人員 ───────────────────────────
    # 全體 132 份最大恰為 15.0、無一超過，所以這裡不是抓違規，是標「無緩衝」。
    text = (
        f"財報附註一之實際招收人數 ÷ 教保服務人員數。幼兒教育及照顧法就 3 歲以上"
        f"幼兒之配置上限為每 {STAFF_RATIO_LEGAL:.0f} 名 1 人（2 歲專班更嚴）。"
        f"全體 132 份報告最大值恰為 {STAFF_RATIO_LEGAL:.0f}，無一超過，"
        f"故本檢核標示的是已貼齊上限、任一人請假即無緩衝的情形。"
    )
    src = "財報附註一（實際招收人數、教保服務人員數）"
    ratio = m["staff_ratio"]
    if ratio is None:
        add("師生比貼齊法定上限", text, None, "缺實際招收人數或教保服務人員數",
            "medium", src)
    elif ratio > STAFF_RATIO_LEGAL:
        add("師生比貼齊法定上限", text, False,
            f"{m['actual_enrolment']:,.0f} 名幼兒 ÷ {m['educators']:,.0f} 位"
            f"教保服務人員 = {ratio:.2f}，超過 3 歲以上之法定上限 "
            f"{STAFF_RATIO_LEGAL:.0f}。請確認班別年齡組成與實際配置",
            "high", src)
    elif ratio >= STAFF_RATIO_WATCH:
        add("師生比貼齊法定上限", text, False,
            f"{m['actual_enrolment']:,.0f} 名幼兒 ÷ {m['educators']:,.0f} 位"
            f"教保服務人員 = {ratio:.2f}，已達法定上限 {STAFF_RATIO_LEGAL:.0f} 的 "
            f"{ratio / STAFF_RATIO_LEGAL:.0%}。若含 2 歲專班則配置標準更嚴，"
            f"請確認各班實際配置與代理人力",
            "medium", src)
    else:
        add("師生比貼齊法定上限", text, True,
            f"{m['actual_enrolment']:,.0f} 名幼兒 ÷ {m['educators']:,.0f} 位 = "
            f"{ratio:.2f}（上限 {STAFF_RATIO_LEGAL:.0f}）", "medium", src)

    # ── 4. 隱含收費年額 vs 同年度同儕 ────────────────────────────────
    text = (
        "教保費收入決算數 ÷ 實際招收人數，即財報反推之每名幼兒年度營運成本"
        "（含政府差額補助）。111 學年度起收費明細不在公開資料中，這是唯一可用的"
        "收費代理量，故僅能與同年度同類型同儕比較，不設絕對門檻。"
    )
    src = "收支餘絀表（教保費收入）／財報附註一（實際招收人數）／同年度非營利園同儕"
    v = m["implied_fee_per_child"]
    pool = peers.get("implied_fee_per_child") or []
    if not m["full_year"]:
        add("隱含收費年額 同儕偏離", text, None,
            f"收支餘絀表期間「{m['income_period']}」僅涵蓋 {m['period_months']} 個月，"
            f"每生收入與全年度同儕不可直接比較", "medium", src)
    elif v is None or len(pool) < anomaly.MIN_PEERS:
        add("隱含收費年額 同儕偏離", text, None,
            "缺教保費收入或實際招收人數，或同年度可比園數不足", "medium", src)
    else:
        z = anomaly.robust_z(v, pool)
        med = anomaly.median(pool)
        body = _z_text(z, v, med, " 元/生")
        if z is not None and abs(z) >= PEER_Z:
            side = "高" if z > 0 else "低"
            add("隱含收費年額 同儕偏離", text, False,
                f"每生教保費收入 {body}，偏{side}。"
                f"請確認核定之營運成本分攤數、班別結構與招收人數認列基礎；"
                f"該年度無收費明細可供覆核",
                "medium", src)
        else:
            add("隱含收費年額 同儕偏離", text, True,
                f"每生教保費收入 {body}", "medium", src)

    # ── 5. 家長每月平均實繳 vs 登記月費 ─────────────────────────────
    text = (
        "附註三「收支明細表 1. 教保費收入」之「教保費收入（家長繳費）」÷ 實際招收人數"
        f" ÷ {FULL_YEAR_MONTHS} 個月，與園所基本資料登記之每月收費比較。"
        "第二胎以上、低收入戶與身心障礙幼兒依規定減免，所以**平均值本應低於登記月費**；"
        "高於登記月費比低於更難解釋。"
    )
    src = ("財報附註三（教保費收入－家長繳費）／財報附註一（實際招收人數）／"
           "園所基本資料（每月收費）")
    pm, reg_m = m["parent_monthly_avg"], m["registry_monthly"]
    if pm is None or not reg_m:
        add("家長月均實繳 ≤ 登記月費", text, None,
            "缺附註三家長繳費、實際招收人數或登記每月收費", "medium", src)
    elif pm <= reg_m:
        add("家長月均實繳 ≤ 登記月費", text, True,
            f"家長繳費 {m['parent_fee']:,.0f} ÷ {m['actual_enrolment']:,.0f} 人 ÷ "
            f"{FULL_YEAR_MONTHS} 月 = 每月 {pm:,.0f}，登記月費 {reg_m:,.0f}",
            "medium", src)
    else:
        add("家長月均實繳 ≤ 登記月費", text, False,
            f"家長繳費 {m['parent_fee']:,.0f} ÷ {m['actual_enrolment']:,.0f} 人 ÷ "
            f"{FULL_YEAR_MONTHS} 月 = 每月 {pm:,.0f}，超過登記月費 {reg_m:,.0f} "
            f"（{pm / reg_m:.2f} 倍）。"
            f"請確認家長繳費是否含代收代辦或延長照顧等另計項目、"
            f"名冊人數在學年度中是否大幅減少（分母偏小會推高平均），"
            f"以及登記月費是否已依核定調整",
            "medium", src)

    # ── 6. 家長分攤比率 vs 同年度同儕 ────────────────────────────────
    text = (
        "家長繳費 ÷（家長繳費 + 政府學費差額補助）。非營利園是成本分攤制，"
        "分攤比率由核定之營運成本與家長收費數額共同決定，同一年度同一制度下應相近。"
        "全市中位數自 110 學年度 0.209 降至 113 學年度 0.113（政府端承擔上升），"
        "故僅與同年度同儕比較，不設絕對門檻。"
    )
    src = ("財報附註三（教保費收入－家長繳費／政府學費差額補助）／"
           "同年度非營利園同儕")
    ps = m["parent_share"]
    pool = peers.get("parent_share") or []
    if ps is None:
        add("家長分攤比率 同儕偏離", text, None,
            "本份報告未抽到附註三之教保費收入拆分", "medium", src)
    elif len(pool) < anomaly.MIN_PEERS:
        add("家長分攤比率 同儕偏離", text, None,
            f"家長分攤 {ps:.3f}，同年度可比園數不足（{len(pool)}）", "medium", src)
    else:
        z = anomaly.robust_z(ps, pool)
        med = anomaly.median(pool)
        body = _z_text(z, ps, med, "")
        if z is not None and abs(z) >= PEER_Z:
            side = "高" if z > 0 else "低"
            add("家長分攤比率 同儕偏離", text, False,
                f"家長分攤比率 {body}，偏{side}"
                f"（家長 {m['parent_fee']:,.0f}、政府 {m['gov_subsidy']:,.0f}）。"
                f"請確認該學年度核定之營運成本分攤數與家長收費數額，"
                f"以及減免人數結構是否足以解釋差異",
                "medium", src)
        else:
            add("家長分攤比率 同儕偏離", text, True,
                f"家長分攤比率 {body}", "medium", src)

    return out


def nonprofit_guards(metrics: list[dict]) -> list[str]:
    """從未失敗、但失敗就代表資料錯了的對帳。刻意與發現分開回報。

    前兩條都是 0/132：財報從未自承超收，登記核定也從未被實際招收超過。
    把 0% 基準率的檢查列成發現，只會在名單上製造永遠通過的雜訊。

    第三條是**跨頁對帳**：附註三的家長繳費 + 政府學費差額補助，應等於同表小計、
    也等於收支餘絀表的教保費收入決算數。115/115 完全相符。它是護欄而非發現，
    因為失敗代表抽取抓錯欄——這正是它第一次跑出來的作用（見 `_is_own_year`）。
    """
    problems: list[str] = []
    for m in metrics:
        enrol, cap = m["actual_enrolment"], m["approved_capacity"]
        if enrol is not None and cap is not None and enrol > cap:
            problems.append(
                f"{m['year']} {m['short_name']}：實際招收 {enrol:,.0f} 超過"
                f"財報核定 {cap:,.0f}")
        reg = m["registry_capacity_max"]
        if enrol is not None and reg is not None and enrol > reg:
            problems.append(
                f"{m['year']} {m['short_name']}：實際招收 {enrol:,.0f} 超過"
                f"登記核定 {reg:,.0f}")
        total, sub, tui = (m["fee_split_total"], m["fee_split_subtotal"],
                           m["tuition_income"])
        if total is not None and sub is not None and abs(total - sub) > FEE_SPLIT_TOL:
            problems.append(
                f"{m['year']} {m['short_name']}：附註三 家長+政府 {total:,.0f} "
                f"≠ 同表小計 {sub:,.0f}")
        if total is not None and tui is not None and abs(total - tui) > FEE_SPLIT_TOL:
            problems.append(
                f"{m['year']} {m['short_name']}：附註三 家長+政府 {total:,.0f} "
                f"≠ 收支餘絀表教保費收入 {tui:,.0f}（差 {total - tui:+,.0f}）")
    return problems


# ══════════════════════════════════════════════════════════════════════
#  市立幼兒園：決算書 × 收費明細 × 登記主檔
# ══════════════════════════════════════════════════════════════════════

def public_fee_basis(
    half_year_tuition: dict[tuple[str, int], float],
    name: str,
    fiscal_year: int,
) -> tuple[float | None, str]:
    """年度 N 的每生學雜費年基準，以及它是怎麼湊出來的。

    政府會計年度是曆年，所以年度 N 的學雜費收入橫跨學年度 N−1 下學期
    （N/2–N/6）與學年度 N 上學期（N/8–N+1/1）。兩個半年額相加才是一名幼兒在
    該年度繳的學雜費。缺一邊時以另一邊 ×2 推估，並在回傳字串裡說明。
    """
    prev = half_year_tuition.get((name, fiscal_year - 1))
    cur = half_year_tuition.get((name, fiscal_year))
    if prev and cur:
        return prev + cur, (f"學年度 {fiscal_year - 1} 下學期 {prev:,.0f} + "
                            f"學年度 {fiscal_year} 上學期 {cur:,.0f}")
    single = cur or prev
    if single:
        which = fiscal_year if cur else fiscal_year - 1
        return single * 2, f"僅取得學年度 {which} 之半年額 {single:,.0f}，×2 推估"
    return None, "查無該園收費明細"


def public_metrics(
    row: dict,
    half_year_tuition: dict[tuple[str, int], float],
    capacity: dict[str, float],
    branches: dict[str, int],
    capacity_retrieved_on: str = "",
) -> dict:
    """一個市立幼兒園園-年度的跨來源衍生量。

    ``row`` 是 ``data/extracted/public_kindergartens.csv`` 的一列。
    ``capacity``／``branches`` 以**本園名**為鍵，已依 `registry.parent_of()`
    把分班聚合上來——決算書是分基金層級（22 筆），主檔是分班層級（67 筆），
    不聚合就會拿一個分班的核定人數去除整個基金的收入。
    """
    name = str(row.get("name", ""))
    year = int(row.get("fiscal_year") or 0)
    fee, fee_note = public_fee_basis(half_year_tuition, name, year)
    tui_a = _f(row.get("tuition_actual"))
    tui_b = _f(row.get("tuition_budget"))
    gov = _f(row.get("gov_transfer_actual"))
    use = _f(row.get("fund_use_actual"))
    cap = capacity.get(name)

    implied = (tui_a / fee) if (tui_a is not None and fee) else None
    implied_budget = (tui_b / fee) if (tui_b is not None and fee) else None
    return {
        "code": str(row.get("fund_code", "")),
        "short_name": name,
        "year": str(year),
        "year_kind": "年度",
        "entity_type": "公立",
        "tuition_actual": tui_a,
        "tuition_budget": tui_b,
        "gov_transfer_actual": gov,
        "fund_use_actual": use,
        "fee_year_basis": fee,
        "fee_basis_note": fee_note,
        "approved_capacity": cap,
        "n_branches": branches.get(name, 0),
        "capacity_basis": (
            f"登記核定人數為釘住之鏡像快照"
            f"（retrieved_on={capacity_retrieved_on or '未記錄'}），"
            f"非該年度當時核定數；以本園名聚合 {branches.get(name, 0)} 筆分班"
        ),
        #: 決算書不含幼兒人數。這是把它接上收費基準後才存在的量。
        "implied_enrolment": implied,
        "implied_enrolment_budget": implied_budget,
        "occupancy": (implied / cap) if (implied is not None and cap) else None,
        "occupancy_budget": (implied_budget / cap)
        if (implied_budget is not None and cap) else None,
        "gov_per_child": (gov / implied) if (gov is not None and implied) else None,
        "cost_per_child": (use / implied) if (use is not None and implied) else None,
    }


def check_public(m: dict, peers: dict[str, list[float]]) -> list[CrossCheck]:
    """一個市立幼兒園園-年度的所有跨來源檢核。"""
    out: list[CrossCheck] = []

    def add(rule: str, text: str, passed: bool | None, detail: str,
            sev: str, sources: str) -> None:
        out.append(CrossCheck(
            code=m["code"], short_name=m["short_name"], academic_year=m["year"],
            rule=rule, rule_text=text, passed=passed, detail=detail,
            severity=sev, sources=sources, year_kind="年度",
            entity_type="公立",
        ))

    src_occ = ("決算書基金來源表（學雜費收入）／收費明細（學費＋雜費）／"
               "園所基本資料（核定招收人數，分班聚合）")

    # ── 1. 推估在園人數與滿園率 ──────────────────────────────────────
    text = (
        "市立幼兒園決算書不刊載幼兒人數。新北市公立幼兒園學費＋雜費為全市統一基準"
        "（110–114 學年度每學期 7,675 元，284–291 所園全部相同），"
        "故學雜費收入 ÷ 該基準 = 推估在園人數，再 ÷ 登記核定人數 = 推估滿園率。"
        "招生不足不是違規，但公庫撥款不隨人數下降，所以它直接決定每生公務成本。"
        "與同一年度其他市立幼兒園比較，不設絕對門檻——全市滿園率中位數"
        "112→114 由 0.793 降至 0.738，絕對門檻會在後段年度大量誤標。"
    )
    occ = m["occupancy"]
    pool = peers.get("occupancy") or []
    if occ is None:
        add("推估滿園率 同儕偏低", text, None,
            f"無法推估：{m['fee_basis_note']}"
            f"{'；查無核定人數' if m['approved_capacity'] is None else ''}",
            "medium", src_occ)
    elif len(pool) < anomaly.MIN_PEERS:
        add("推估滿園率 同儕偏低", text, None,
            f"推估在園 {m['implied_enrolment']:,.1f} 人，"
            f"同年度可比園數不足（{len(pool)}）", "medium", src_occ)
    else:
        z = anomaly.robust_z(occ, pool)
        med = anomaly.median(pool)
        body = _z_text(z, occ, med, "")
        head = (f"學雜費決算 {m['tuition_actual']:,.0f} ÷ 年基準 "
                f"{m['fee_year_basis']:,.0f}（{m['fee_basis_note']}）= 推估在園 "
                f"{m['implied_enrolment']:,.1f} 人；核定 "
                f"{m['approved_capacity']:,.0f} 人（{m['n_branches']} 筆分班聚合）")
        if z is not None and z <= -PEER_Z:
            add("推估滿園率 同儕偏低", text, False,
                f"{head}，滿園率 {body}。"
                f"請確認招生情形、是否應檢討核定人數與班級數，"
                f"以及員額與空間是否已配合調整。"
                f"⚠️ {m['capacity_basis']}",
                "medium", src_occ)
        else:
            add("推估滿園率 同儕偏低", text, True, f"{head}，滿園率 {body}",
                "medium", src_occ)

    # ── 2. 每生政府投入 ──────────────────────────────────────────────
    text = (
        "政府撥入決算數 ÷ 推估在園人數。決算書單獨看不出這個數字，"
        "因為它不含人數；接上收費明細與核定人數才算得出來。"
        "偏遠與小型園每生固定成本天生偏高，屬政策性配置而非不當支出，"
        "故僅回報同年度相對位置與量級，供決定是否檢討核定人數與班級配置。"
    )
    src = ("決算書基金來源表（政府撥入）／收費明細（學費＋雜費）／"
           "園所基本資料（核定招收人數）")
    v = m["gov_per_child"]
    pool = peers.get("gov_per_child") or []
    if v is None:
        add("每生政府投入 同儕偏高", text, None,
            f"無法推估：{m['fee_basis_note']}", "medium", src)
    elif len(pool) < anomaly.MIN_PEERS:
        add("每生政府投入 同儕偏高", text, None,
            f"每生政府投入 {v:,.0f} 元，同年度可比園數不足（{len(pool)}）",
            "medium", src)
    else:
        z = anomaly.robust_z(v, pool)
        med = anomaly.median(pool)
        body = _z_text(z, v, med, " 元/生")
        if z is not None and z >= PEER_Z:
            mult = (v / med) if med else float("nan")
            add("每生政府投入 同儕偏高", text, False,
                f"政府撥入 {m['gov_transfer_actual']:,.0f} ÷ 推估在園 "
                f"{m['implied_enrolment']:,.1f} 人 = {body}，"
                f"為同年度中位數的 {mult:.1f} 倍。"
                f"請確認是否為偏遠或小型園之政策性配置，"
                f"以及核定人數與實際招生落差是否已反映在班級與員額編制",
                "medium", src)
        else:
            add("每生政府投入 同儕偏高", text, True,
                f"每生政府投入 {body}", "medium", src)

    return out


def public_guards(metrics: list[dict]) -> list[str]:
    """收費基準是否為正確分母的護欄。

    決算書自己編列的學雜費預算 ÷ 收費基準 = 預算隱含人數，它不該超過核定人數：
    預算是照核定量能編的。58/58 個園年實測最大 1.029，無一超過 1.05。
    失敗代表收費基準抓錯、分班聚合錯、或核定人數快照過舊——都是關於本管線的消息。
    """
    problems: list[str] = []
    for m in metrics:
        ob = m["occupancy_budget"]
        if ob is not None and ob > BUDGET_IMPLIED_CAP:
            problems.append(
                f"{m['year']} {m['short_name']}：預算隱含人數 "
                f"{m['implied_enrolment_budget']:,.1f} 為核定 "
                f"{m['approved_capacity']:,.0f} 的 {ob:.3f} 倍"
                f"（上界 {BUDGET_IMPLIED_CAP}）——收費基準或核定人數其一有誤")
    return problems


# ══════════════════════════════════════════════════════════════════════
#  母體層級的發現：個別園沒有離群，但全體在移動
# ══════════════════════════════════════════════════════════════════════

def cohort_findings(
    nonprofit: list[dict], public: list[dict], fee_coverage: dict[str, tuple[int, int]]
) -> list[CrossCheck]:
    """不屬於任何單一園的發現，用 code="COHORT" 標記。

    這些是本模組最重要的輸出，卻沒有一個園可以掛：收費明細停止公開、
    每生成本全體上升、滿園率全體下降，都是母體層級的事實。掛到某一所園上
    會讓那所園背了整體趨勢的責任，不掛又會讓它消失。
    """
    out: list[CrossCheck] = []

    def add(rule: str, text: str, detail: str, sev: str, sources: str,
            year: str, kind: str, etype: str) -> None:
        out.append(CrossCheck(
            code="COHORT", short_name="全市公共化園", academic_year=year,
            rule=rule, rule_text=text, passed=False, detail=detail,
            severity=sev, sources=sources, year_kind=kind, entity_type=etype,
        ))

    # ── 1. 非營利園收費明細自 111 學年度起在外部來源不可得 ───────────
    # ⚠️ 措辭已修正過一次。第一版寫成「因此無法交叉分析」，那是錯的：
    # 財報自己的附註三就有家長／政府拆分（115/132 份）。這裡剩下的缺口比較窄，
    # 但確實存在——**逐項**家長收費（學費／雜費／材料費／活動費／餐點費）
    # 只有申報表有，附註三只給彙總。
    years = sorted(fee_coverage)
    zeroed = [y for y in years if fee_coverage[y][0] == 0]
    if zeroed:
        table = "；".join(
            f"{y} 學年度 {fee_coverage[y][0]}/{fee_coverage[y][1]} 檔" for y in years)
        with_split = sum(1 for m in nonprofit if m["parent_share"] is not None)
        add("非營利園收費明細在外部來源停止公開",
            "偵測收費異常需要**逐項**家長收費（學費／雜費／材料費／活動費／餐點費），"
            "那只有向主管機關申報的收費明細有。財報附註三提供的是彙總"
            "（家長繳費與政府學費差額補助兩個數字），足以查分攤比率，"
            "不足以查個別費目是否超收或挪用（例如附註二(十)3.(2)「餐點費，不得移作他用」"
            "要對的就是餐點費那一項）。",
            f"非營利園收費明細檔數：{table}。"
            f"全市總檔數同期為 1,115／1,147／1,109／1,108／1,098／1,085，"
            f"公立園覆蓋 284–291／294，均無下降，故非爬取失敗。"
            f"結果是 132 份非營利財報中，只有 110 學年度的 28 份"
            f"（21.2%）有逐項家長收費可交叉。"
            f"✅ 但彙總層級並未斷炊：財報附註三「收支明細表 1. 教保費收入」"
            f"在 {with_split}/{len(nonprofit)} 份報告中列出家長繳費與政府學費差額補助，"
            f"且與收支餘絀表教保費收入完全對帳，四個學年度都可用。"
            f"⚠️ 逐項缺口是資料可得性問題：鏡像未收錄與該園未申報在公開資料上"
            f"無法區分，不得據此認定違反幼照法第 38 條。"
            f"建議之查核事項為主管機關端的申報收件紀錄是否完整。",
            "medium",
            "收費明細來源目錄清點（scrape.fees.list_slip_files）／"
            "財報附註三（教保費收入拆分）",
            "111-114", "學年度", "非營利")

    # ── 2. 非營利園每生營運成本上升，其中誰在付 ──────────────────────
    # ⚠️ 這一條的第一版說「無法確認家長與政府各佔多少」，那是錯的——附註三就有。
    # 拆開之後結論反轉：每生成本上升，但家長每生負擔是**下降**的。
    def _series(field: str, full_year_only: bool) -> dict[str, float]:
        buckets: dict[str, list[float]] = {}
        for m in nonprofit:
            v = m[field]
            if v is None:
                continue
            if full_year_only and (m["period_months"] or FULL_YEAR_MONTHS) < \
                    FULL_YEAR_MONTHS:
                continue
            buckets.setdefault(m["year"], []).append(v)
        meds = {y: anomaly.median(v) for y, v in sorted(buckets.items())}
        return {y: v for y, v in meds.items() if v is not None}

    total_med = _series("implied_fee_per_child", True)
    parent_med = _series("parent_per_child", True)
    gov_med = _series("gov_subsidy_per_child", True)
    share_med = _series("parent_share", True)
    if len(total_med) >= 2 and len(share_med) >= 2:
        first, last = min(total_med), max(total_med)

        def _line(meds: dict[str, float], fmt: str) -> str:
            return "；".join(f"{y} {format(v, fmt)}" for y, v in meds.items())

        def _delta(meds: dict[str, float]) -> str:
            a, b = min(meds), max(meds)
            return f"{meds[b] / meds[a] - 1:+.1%}"

        add("非營利園每生營運成本上升，增幅由政府端承擔",
            "教保費收入 ÷ 實際招收人數 即每名幼兒之年度營運成本。附註二(五)定義收入"
            "總額時明示家長繳費與政府差額補助合併計算，而附註三「收支明細表 "
            "1. 教保費收入」把兩者分開列出，故增幅來自哪一端是可以查的。",
            f"每生教保費收入中位數：{_line(total_med, ',.0f')}"
            f"（{_delta(total_med)}）。拆開後："
            f"每生家長繳費 {_line(parent_med, ',.0f')}（{_delta(parent_med)}）；"
            f"每生政府學費差額補助 {_line(gov_med, ',.0f')}（{_delta(gov_med)}）；"
            f"家長分攤比率 {_line(share_med, '.3f')}（{_delta(share_med)}）。"
            f"→ 每生營運成本上升，但家長每生負擔下降，增幅由政府端承擔，"
            f"與 111 年 8 月起學前補助政策的方向一致，本身不是異常。"
            f"值得查核的是**成本本身**：核定之營運成本分攤數逐年調升的依據、"
            f"以及 111 學年度起逐項家長收費不在公開資料中"
            f"（見「非營利園收費明細在外部來源停止公開」）使費目層級無法覆核。",
            "medium",
            "收支餘絀表（教保費收入）／財報附註三（家長繳費／政府學費差額補助）／"
            "財報附註一（實際招收人數）",
            f"{first}-{last}", "學年度", "非營利")

    # ── 3. 公立園滿園率下降而每生政府投入上升 ────────────────────────
    occ_by: dict[str, list[float]] = {}
    gov_by: dict[str, list[float]] = {}
    for m in public:
        if m["occupancy"] is not None:
            occ_by.setdefault(m["year"], []).append(m["occupancy"])
        if m["gov_per_child"] is not None:
            gov_by.setdefault(m["year"], []).append(m["gov_per_child"])
    occ_med = {y: anomaly.median(v) for y, v in sorted(occ_by.items())}
    gov_med = {y: anomaly.median(v) for y, v in sorted(gov_by.items())}
    occ_med = {y: v for y, v in occ_med.items() if v is not None}
    gov_med = {y: v for y, v in gov_med.items() if v is not None}
    if len(occ_med) >= 2 and len(gov_med) >= 2:
        y0, y1 = min(occ_med), max(occ_med)
        add("公立園推估滿園率下降、每生政府投入上升",
            "把決算書接上全市統一收費基準與登記核定人數之後，"
            "才能算出決算書本身不含的兩個量：推估在園人數與每生政府投入。"
            "少子化下滿園率下降是預期的；值得查核的是公庫撥款不隨之調整，"
            "使每生公務成本同期上升。",
            "推估滿園率中位數："
            + "；".join(f"{y} 年度 {v:.3f}" for y, v in occ_med.items())
            + f"（{y0}→{y1} {occ_med[y1] / occ_med[y0] - 1:+.1%}）。"
            + "每生政府投入中位數："
            + "；".join(f"{y} 年度 {v:,.0f} 元" for y, v in gov_med.items())
            + f"（{y0}→{y1} {gov_med[y1] / gov_med[y0] - 1:+.1%}）。"
            + "建議查核事項：核定招收人數與班級數是否已依實際招生檢討，"
            + "以及員額編制與園舍使用是否配合調整。",
            "high",
            "決算書基金來源表／收費明細／園所基本資料（核定招收人數）",
            f"{y0}-{y1}", "年度", "公立")

    return out
