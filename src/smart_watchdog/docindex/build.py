"""把語料灌進索引。

索引的**內容來源不是 PDF 本身**，而是已經抽取／盤點過的產物，原因寫在
`__init__.py`：132 份非營利財報是純掃描影像，PDF 裡沒有文字可以索引。
所以這裡做的是把「已知的結構化事實」與「已知的頁碼」接起來，讓提問可以一步
落到（檔案, 頁碼）。

來源與各自貢獻的東西：

    data/extracted/nonprofit/*.json   132 份財報的區段頁碼、數字、附註原文、issues
    data/extracted/public_kindergartens.csv   22 所市立幼兒園 × 3 年度的決算數字
    data/extracted/pdf_survey.csv     162 份 PDF 的頁數與文字層統計
    data/extracted/text_quality.csv   公校 11,232 頁的 PUA 亂碼比例
    公校決算書 15 冊                   分基金 → 頁段（唯一切割訊號是頁尾代號）
    data/processed/compliance_findings.csv    法遵檢核結果與其引用的附註條文
    docs/competition/命題文件-教育局.md        題目本身

分基金切割要逐頁讀 15 冊約 11,000 頁，很慢，因此結果快取在
`data/interim/fund_sections.json`；`--rescan` 可強制重掃。
"""

from __future__ import annotations

import csv
import json
import pathlib
import re
import sqlite3

from . import schema

ROOT = pathlib.Path(__file__).resolve().parents[3]
RAW = ROOT / "data/raw/資料集"
EXTRACTED = ROOT / "data/extracted"
PROCESSED = ROOT / "data/processed"
FUND_CACHE = ROOT / "data/interim/fund_sections.json"

#: 資產負債表欄位 → 中文標籤。順序即是報表上的順序。
BALANCE_FIELDS = [
    ("cash", "現金"),
    ("prepaid_receipts", "預收款項"),
    ("current_assets_total", "流動資產合計"),
    ("current_liabilities_total", "流動負債合計"),
    ("reserve_asset", "業務發展準備金（非流動資產）"),
    ("reserve_liability", "業務發展準備（非流動負債）"),
    ("severance_asset", "資遣費準備金（非流動資產）"),
    ("severance_liability", "資遣費準備（非流動負債）"),
    ("accumulated_surplus", "累積餘絀"),
    ("current_surplus", "本期餘絀"),
    ("equity_total", "淨值合計"),
    ("total_assets", "資產總計"),
    ("total_liabilities", "負債總計"),
]

NOTE1_FIELDS = [
    ("approved_capacity", "核定招收人數"),
    ("actual_enrolment", "實際招收人數"),
    ("total_staff", "員工數"),
    ("educators", "教保服務人員數"),
]

NOTE5_FIELDS = [
    ("admin_fee_disclosed", "行政管理費"),
    ("payable_to_operator", "應付受託法人款項"),
]

SECTION_LABELS = {
    "balance_sheet": "資產負債表",
    "income_statement": "收支餘絀表",
    "note_1": "附註一 基本資料",
    "note_2": "附註二 重大會計政策",
    "note_3": "附註三 其他收入與其他支出",
    "note_5": "附註五 關係人交易",
}

# 附註條號：半形與全形都出現過，兩種都要吃。
CLAUSE_RE = re.compile(r"(?=[(（][一二三四五六七八九十]+[)）])")


def _num(v: object) -> float | None:
    """把抽取值轉成可入庫的數字。

    **空白維持 NULL，絕不落成 0。** 這是 CLAUDE.md 的鐵則：業務發展費預算欄
    空白代表未編列預算（一項稽查發現），填 0 等於對真實機構謊稱編列了零元。
    """
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _page_label(start: int, end: int | None = None) -> str:
    """組出人看的引用字串。

    傳進來的必須已經是**文件自己印在頁尾的頁碼**（1-based），這裡不做任何加減。
    抽取結果記錄的就是這個值——N01安溪 113 學年度資產負債表位於 PDF 的
    0-based index 4，頁尾印 "N01-5"，抽取記 5。曾經在這裡多加 1，
    結果每一筆引用都差一頁；**給錯頁碼比答不出來更糟**，所以這條註解留著。
    """
    if end is None or end == start:
        return f"p.{start}"
    return f"p.{start}-{end}"


def _split_clauses(text: str) -> list[tuple[str, str]]:
    """把附註原文依條號切塊，回傳 (條號, 內文)。

    切塊而不是整段入庫，是為了讓檢索命中時能直接指出「附註二(四)」——
    法遵檢核引用的正是這個粒度，兩邊對得上，稽查員才能照著念。
    """
    if not text:
        return []
    parts = [p.strip() for p in CLAUSE_RE.split(text) if p.strip()]
    out: list[tuple[str, str]] = []
    for part in parts:
        m = re.match(r"[(（]([一二三四五六七八九十]+)[)）]", part)
        out.append((f"({m.group(1)})" if m else "前言", part))
    return out


class Builder:
    """一次建索引。每個 `add_*` 只負責一個來源，失敗時只影響那個來源。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.n_docs = 0
        self.n_sections = 0
        self.n_facts = 0
        self.n_passages = 0

    # ── 低階寫入 ─────────────────────────────────────────────────
    def doc(self, **kw) -> None:
        cols = ", ".join(kw)
        marks = ", ".join("?" * len(kw))
        self.conn.execute(
            f"INSERT OR REPLACE INTO documents ({cols}) VALUES ({marks})",
            tuple(kw.values()))
        self.n_docs += 1

    def section(self, **kw) -> int:
        cols = ", ".join(kw)
        marks = ", ".join("?" * len(kw))
        cur = self.conn.execute(
            f"INSERT INTO sections ({cols}) VALUES ({marks})", tuple(kw.values()))
        self.n_sections += 1
        return int(cur.lastrowid or 0)

    def fact(self, **kw) -> None:
        cols = ", ".join(kw)
        marks = ", ".join("?" * len(kw))
        self.conn.execute(
            f"INSERT INTO facts ({cols}) VALUES ({marks})", tuple(kw.values()))
        self.n_facts += 1

    def passage(self, text: str, **kw) -> None:
        text = (text or "").strip()
        if len(text) < 3:            # trigram 分詞器對 3 字以下沒有意義
            return
        self.conn.execute(
            "INSERT INTO passages (text, doc_id, section_id, page, kind, "
            "institution, year, label) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (text, kw.get("doc_id"), kw.get("section_id"), kw.get("page"),
             kw.get("kind"), kw.get("institution"), kw.get("year"),
             kw.get("label")))
        self.n_passages += 1

    def cover(self, scope: str, covered: int, total: int, note: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO coverage (scope, covered, total, note) "
            "VALUES (?, ?, ?, ?)", (scope, covered, total, note))

    # ── 來源一：非營利園財報 ──────────────────────────────────────
    def add_nonprofit(self) -> None:
        survey = self._survey_by_name()
        files = sorted((EXTRACTED / "nonprofit").glob("*.json"))
        for f in files:
            d = json.loads(f.read_text(encoding="utf-8"))
            code, short, year = d["code"], d["short_name"], int(d["academic_year"])
            pdf_name = f"{code}{short}_{year}學年度財務報告.pdf"
            rel = f"data/raw/資料集/非營利園財報/{year}學年度/{pdf_name}"
            doc_id = f"nonprofit/{year}/{code}"
            s = survey.get(pdf_name, {})
            self.doc(doc_id=doc_id, path=rel, kind="非營利財報", institution=short,
                     code=code, year=year, year_kind="學年度",
                     pages=s.get("pages"), text_pages=s.get("text_pages"),
                     needs_ocr=1,
                     note="純掃描影像；內容來自視覺抽取，非 PDF 文字層")

            common = {"doc_id": doc_id, "institution": short, "code": code,
                      "year": year, "year_kind": "學年度"}
            for key, label in SECTION_LABELS.items():
                blk = d.get(key) or {}
                page = blk.get("page")
                if page is None:
                    continue
                end = blk.get("page_end", page)
                # 抽取記的是頁尾印刷頁碼（1-based）；PyMuPDF 索引要減 1。
                sid = self.section(kind=label, label=f"{short} {year}學年度 {label}",
                                   page_start=int(page), page_end=int(end),
                                   pdf_index_start=int(page) - 1,
                                   pdf_index_end=int(end) - 1,
                                   page_label=_page_label(int(page), int(end)),
                                   **common)
                blk["_section_id"] = sid

            self._nonprofit_facts(d, doc_id, short, code, year)
            self._nonprofit_passages(d, doc_id, short, year)

        self.cover("非營利園財報", len(files), 132,
                   "132 份全數抽取完成；每份含資產負債表、收支餘絀表與附註一／二／三／五")

    def _nonprofit_facts(self, d: dict, doc_id: str, short: str, code: str,
                         year: int) -> None:
        common = {"institution": short, "code": code, "year": year,
                  "year_kind": "學年度", "doc_id": doc_id, "unit": "元"}

        bs = d.get("balance_sheet") or {}
        for field, label in BALANCE_FIELDS:
            if field not in bs:
                continue
            self.fact(field=field, label=label, value=_num(bs.get(field)),
                      page=bs.get("page"), section_id=bs.get("_section_id"),
                      **common)

        inc = d.get("income_statement") or {}
        for line in inc.get("lines") or []:
            label = (line.get("label") or "").strip()
            if not label:
                continue
            for suffix, key, unit in (("預算數", "budget", "元"),
                                      ("決算數", "actual", "元"),
                                      ("執行率", "execution_pct", "%")):
                self.fact(field=f"income:{label}:{key}",
                          label=f"{label}（{suffix}）",
                          value=_num(line.get(key)),
                          page=inc.get("page"), section_id=inc.get("_section_id"),
                          **{**common, "unit": unit})

        n1 = d.get("note_1") or {}
        for field, label in NOTE1_FIELDS:
            self.fact(field=field, label=label, value=_num(n1.get(field)),
                      page=n1.get("page"), section_id=n1.get("_section_id"),
                      **{**common, "unit": "人"})

        n5 = d.get("note_5") or {}
        for field, label in NOTE5_FIELDS:
            self.fact(field=field, label=label, value=_num(n5.get(field)),
                      page=n5.get("page"), section_id=n5.get("_section_id"),
                      **common)

    def _nonprofit_passages(self, d: dict, doc_id: str, short: str,
                            year: int) -> None:
        common = {"doc_id": doc_id, "institution": short, "year": year}

        n1 = d.get("note_1") or {}
        if n1.get("operator"):
            self.passage(
                f"{short} {year}學年度 受託法人：{n1['operator']}；"
                f"契約期間：{n1.get('contract_period', '未載')}",
                section_id=n1.get("_section_id"), page=n1.get("page"),
                kind="附註一 基本資料", label="受託法人與契約期間", **common)

        for key in ("note_2", "note_3", "note_5"):
            blk = d.get(key) or {}
            text = blk.get("text") or ""
            label = SECTION_LABELS[key]
            for clause, body in _split_clauses(text):
                self.passage(body, section_id=blk.get("_section_id"),
                             page=blk.get("page"), kind=label,
                             label=f"{label}{clause}", **common)

        # 收支餘絀表科目名稱本身要可檢索——「業務發展費在哪幾園出現」是真問題。
        inc = d.get("income_statement") or {}
        labels = [ln.get("label", "") for ln in inc.get("lines") or []]
        if labels:
            self.passage("；".join(x for x in labels if x),
                         section_id=inc.get("_section_id"), page=inc.get("page"),
                         kind="收支餘絀表", label="科目清單", **common)

        # issues 是抽取模型自報的文件矛盾與待判讀事項，帶頁碼，資訊密度最高。
        for i, issue in enumerate(d.get("issues") or []):
            self.passage(issue, page=(d.get("balance_sheet") or {}).get("page"),
                         kind="抽取註記", label=f"issues[{i}]", **common)

    # ── 來源二：公校決算書 ────────────────────────────────────────
    def add_public_school(self, *, rescan: bool = False) -> None:
        survey = self._survey_by_name()
        sections = self._fund_sections(rescan=rescan)
        pua = self._pua_pages()

        for volume, info in sections.items():
            year = int(info["fiscal_year"])
            doc_id = f"public/{year}/{volume}"
            rel = info["path"]
            s = survey.get(volume, {})
            bad = len(pua.get(volume, set()))
            self.doc(doc_id=doc_id, path=rel, kind="公校決算書", institution=None,
                     code=None, year=year, year_kind="年度",
                     pages=s.get("pages"), text_pages=s.get("text_pages"),
                     needs_ocr=0,
                     note=f"一冊含多個分基金，切割靠頁尾代號；"
                          f"{bad} 頁文字層為 PUA 造字亂碼，不可用")
            for fund in info["funds"]:
                pages = fund["page_indices"]
                if not pages:
                    continue
                is_kg = fund["fund_code"].startswith("136")
                # 公校端的頁碼語意與非營利端不同，不能共用同一個轉換：
                # 頁尾 `13601-4` 的 "4" 是**該分基金內**的第 4 頁，不是冊內頁次。
                # 對「翻到那一頁」有用的是冊內位置，所以這裡存 PDF 頁次
                # （0-based index + 1），並在 page_label 標明是哪一種。
                lo, hi = min(pages), max(pages)
                self.section(
                    doc_id=doc_id,
                    kind="分基金（市立幼兒園）" if is_kg else "分基金",
                    label=f"{fund['fund_code']} {fund['name']}".strip(),
                    institution=fund["name"] or None, code=fund["fund_code"],
                    year=year, year_kind="年度",
                    page_start=lo + 1, page_end=hi + 1,
                    pdf_index_start=lo, pdf_index_end=hi,
                    page_label=f"PDF 第 {lo + 1}-{hi + 1} 頁"
                               f"（頁尾代號 {fund['fund_code']}）")

        n_kg = sum(1 for info in sections.values()
                   for f in info["funds"] if f["fund_code"].startswith("136"))
        self.cover("公校決算書分基金", n_kg, n_kg,
                   "136xx 為市立幼兒園；一冊含多園，唯一切割訊號是頁尾 <分基金代號>-<頁碼>")
        self.cover("公校文字層", 11232 - sum(len(v) for v in pua.values()), 11232,
                   "PUA 私有造字區頁面的文字層無法解碼，且每頁對應表不同——"
                   "這些頁只能定位，不能檢索內文")

    def add_public_kindergarten_facts(self) -> None:
        path = EXTRACTED / "public_kindergartens.csv"
        if not path.exists():
            return
        labels = {
            "fund_source_budget": "基金來源（預算數）",
            "fund_source_actual": "基金來源（決算數）",
            "gov_transfer_actual": "公庫撥款（決算數）",
            "tuition_budget": "學雜費收入（預算數）",
            "tuition_actual": "學雜費收入（決算數）",
            "fund_use_budget": "基金用途（預算數）",
            "fund_use_actual": "基金用途（決算數）",
            "capex_budget": "資本支出（預算數）",
            "capex_actual": "資本支出（決算數）",
            "surplus_actual": "本期餘絀",
            "closing_balance_actual": "期末餘額",
            "staff_actual": "員額（決算數）",
            "gov_dependency": "公庫依賴度",
            "tuition_execution": "學雜費執行率",
        }
        rows = list(csv.DictReader(path.open(encoding="utf-8")))
        for row in rows:
            year = int(row["fiscal_year"])
            name = row["name"]
            code = row["fund_code"]
            doc = self.conn.execute(
                "SELECT doc_id FROM sections WHERE code = ? AND year = ? LIMIT 1",
                (code, year)).fetchone()
            sec = self.conn.execute(
                "SELECT section_id, page_start FROM sections "
                "WHERE code = ? AND year = ? LIMIT 1", (code, year)).fetchone()
            for field, label in labels.items():
                if field not in row:
                    continue
                self.fact(institution=name, code=code, year=year,
                          year_kind="年度", field=field, label=label,
                          value=_num(row[field]),
                          unit="%" if "率" in label or "度" in label else "元",
                          doc_id=doc["doc_id"] if doc else None,
                          page=sec["page_start"] if sec else None,
                          section_id=sec["section_id"] if sec else None)
        self.cover("市立幼兒園決算", len({r["name"] for r in rows}), 22,
                   "22 所市立幼兒園 × 112–114 年度，由決算書文字層座標抽取（零模型成本）")

    # ── 來源三：法遵檢核結果 ──────────────────────────────────────
    def add_compliance(self) -> None:
        path = PROCESSED / "compliance_findings.csv"
        if not path.exists():
            return
        rows = list(csv.DictReader(path.open(encoding="utf-8")))
        seen_rules: set[str] = set()
        for row in rows:
            code, year = row["code"], int(row["academic_year"])
            sec = self.conn.execute(
                "SELECT section_id, doc_id, page_start FROM sections "
                "WHERE code = ? AND year = ? AND kind LIKE '附註二%' LIMIT 1",
                (code, year)).fetchone()
            passed = row["passed"]
            verdict = {"True": "通過", "False": "未通過"}.get(passed, "待判讀")
            self.passage(
                f"{row['short_name']} {year}學年度 法遵檢核「{row['rule']}」"
                f"結果：{verdict}。{row['detail']}",
                doc_id=sec["doc_id"] if sec else None,
                section_id=sec["section_id"] if sec else None,
                page=sec["page_start"] if sec else None,
                kind="法遵檢核", institution=row["short_name"], year=year,
                label=f"{row['rule']}／{verdict}")
            if row["rule"] not in seen_rules:
                seen_rules.add(row["rule"])
                self.passage(row["rule_text"], kind="法遵規則條文",
                             label=row["rule"])
        # 三態各自計數。`covered` 只放「機械上判定得出來的」＝通過＋未通過；
        # 曾經把通過與未通過加在一起當「通過數」報出去，那會同時掩蓋 31 筆
        # 未通過與 342 筆待判讀，兩個方向都錯。
        passed = sum(1 for r in rows if r["passed"] == "True")
        failed = sum(1 for r in rows if r["passed"] == "False")
        undecided = len(rows) - passed - failed
        self.cover("法遵檢核", passed + failed, len(rows),
                   f"{len(rows)} 項檢核中通過 {passed}、未通過 {failed}、"
                   f"待判讀 {undecided}（資料不足，或認定基礎本身有爭議）。"
                   "待判讀不等於通過，也不等於未通過")

    # ── 來源四：命題文件 ──────────────────────────────────────────
    def add_brief(self) -> None:
        path = ROOT / "docs/competition/命題文件-教育局.md"
        if not path.exists():
            return
        self.doc(doc_id="brief/教育局", path="docs/competition/命題文件-教育局.md",
                 kind="命題文件", institution=None, code=None, year=None,
                 year_kind=None, pages=1, text_pages=1, needs_ocr=0,
                 note="單頁掃描件的逐字轉錄")
        for block in re.split(r"\n##+ ", path.read_text(encoding="utf-8")):
            head = block.strip().splitlines()[0] if block.strip() else ""
            self.passage(block.strip()[:1800], doc_id="brief/教育局",
                         kind="命題文件", label=head[:60])

    # ── 涵蓋範圍：索引知道自己不知道什麼 ─────────────────────────
    def add_coverage_limits(self) -> None:
        inst = PROCESSED / "institutions_ntpc.csv"
        if inst.exists():
            rows = list(csv.DictReader(inst.open(encoding="utf-8")))
            entities = {r.get("entity") or r.get("title") for r in rows}
            self.cover(
                "全市教保機構財務可見度", 60, len(entities),
                "只有 38 非營利園與 22 市立幼兒園申報公開財報。其餘（含全部私立園）"
                "在本索引中沒有財務文件——那是涵蓋範圍限制，不是合規證明。"
                "查不到財務資料時應回報「資料不足」，不得回報「低風險」。")

    # ── 輔助 ─────────────────────────────────────────────────────
    def _survey_by_name(self) -> dict:
        path = EXTRACTED / "pdf_survey.csv"
        out: dict[str, dict] = {}
        if not path.exists():
            return out
        for row in csv.DictReader(path.open(encoding="utf-8")):
            out[row["filename"]] = {
                "pages": int(row["pages"] or 0),
                "text_pages": int(row["text_pages"] or 0),
                "path": row["path"],
            }
        return out

    def _pua_pages(self) -> dict:
        """每冊有哪些頁的文字層是 PUA 亂碼（pua_ratio 高於門檻）。"""
        path = EXTRACTED / "text_quality.csv"
        out: dict[str, set] = {}
        if not path.exists():
            return out
        for row in csv.DictReader(path.open(encoding="utf-8")):
            if float(row["pua_ratio"] or 0) > 0.5:
                out.setdefault(row["volume"], set()).add(int(row["page_index"]))
        return out

    def _fund_sections(self, *, rescan: bool = False) -> dict:
        """分基金頁段。逐頁掃 15 冊很慢，結果快取起來。"""
        if FUND_CACHE.exists() and not rescan:
            return json.loads(FUND_CACHE.read_text(encoding="utf-8"))

        from ..ingest.public_school import index_volume

        out: dict = {}
        volumes = sorted((RAW / "公校").rglob("*決算書第*冊.pdf"))
        for i, vol in enumerate(volumes, 1):
            m = re.search(r"(\d{3})年決算書", vol.name)
            fiscal_year = m.group(1) if m else ""
            print(f"  [{i}/{len(volumes)}] 掃描 {vol.name} …", flush=True)
            funds = index_volume(vol, fiscal_year)
            out[vol.name] = {
                "path": vol.relative_to(ROOT).as_posix(),
                "fiscal_year": fiscal_year,
                "funds": [{"fund_code": f.fund_code, "name": f.name,
                           "page_indices": f.page_indices} for f in funds],
            }
        FUND_CACHE.parent.mkdir(parents=True, exist_ok=True)
        FUND_CACHE.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        return out


def build(path: pathlib.Path | str = schema.DEFAULT_PATH, *,
          rescan: bool = False, with_public: bool = True) -> dict:
    """從頭建一次索引，回傳統計。"""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = schema.connect(path)
    schema.create_schema(conn)
    schema.reset(conn)

    b = Builder(conn)
    b.add_nonprofit()
    if with_public:
        b.add_public_school(rescan=rescan)
        b.add_public_kindergarten_facts()
    b.add_compliance()
    b.add_brief()
    b.add_coverage_limits()

    for key, value in (("schema_version", "1"),
                       ("tokenizer", "fts5/trigram"),
                       ("null_means", "空白／未編列，不是 0")):
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                     (key, value))
    conn.commit()
    stats = {"documents": b.n_docs, "sections": b.n_sections,
             "facts": b.n_facts, "passages": b.n_passages}
    conn.close()
    return stats
