"""查詢索引：把一個問題變成「哪一份文件的哪一頁」。

四個入口，對應四種真實問法：

    locate()            「安溪 113 學年度的附註二在哪？」      → 檔案 + 頁碼
    find_facts()        「安溪 113 的資遣費準備金是多少？」    → 數字 + 出處
    find_passages()     「哪些園提到業務發展準備金上限？」      → 原文 + 頁碼
    summarise_coverage() 「這個索引涵蓋到什麼程度？」          → 涵蓋率 + 缺口

`retrieve()` 把三者合起來，是給 LLM 或 `/api/docsearch` 用的單一入口。

**每一筆結果都帶 (檔案, 頁碼)。** 沒有出處的結果不回傳——稽查員要能指著那一頁，
而給錯頁碼比答不出來更糟。

**查不到不等於沒問題。** `retrieve()` 在零命中時會附上涵蓋範圍說明，
因為全市 1,213 園有 94.8% 根本沒有公開財報；對那些園而言「索引裡沒有」
是涵蓋範圍限制，不是合規證明。
"""

from __future__ import annotations

import dataclasses
import re
import sqlite3

#: trigram 分詞器的最短可檢索長度。
MIN_QUERY = 3


@dataclasses.dataclass
class SearchResult:
    """一筆可引用的命中。"""

    kind: str
    label: str
    institution: str | None
    year: int | None
    year_kind: str | None
    doc_id: str | None
    path: str | None
    page_label: str | None
    text: str
    score: float = 0.0

    def citation(self) -> str:
        """人看的出處字串，例如 `N01安溪_113學年度財務報告.pdf p.11-14`。"""
        if not self.path:
            return "（無對應原始檔）"
        name = self.path.rsplit("/", 1)[-1]
        return f"{name} {self.page_label}" if self.page_label else name

    def as_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["citation"] = self.citation()
        return d


def _fts_query(raw: str) -> str:
    """把使用者輸入變成安全的 FTS5 查詢字串。

    FTS5 會把 `-`、`"`、`*`、`(` 當語法。使用者打的是中文問句不是查詢語法，
    所以整串當片語處理，只把雙引號跳脫掉。
    """
    cleaned = re.sub(r'["]+', " ", raw).strip()
    return f'"{cleaned}"'


def locate(conn: sqlite3.Connection, *, institution: str | None = None,
           year: int | None = None, kind: str | None = None,
           code: str | None = None, limit: int = 50) -> list[SearchResult]:
    """某個區段在哪一份文件的哪幾頁。"""
    where, params = [], []
    if institution:
        where.append("(s.institution LIKE ? OR d.institution LIKE ?)")
        params += [f"%{institution}%", f"%{institution}%"]
    if code:
        where.append("s.code = ?")
        params.append(code)
    if year is not None:
        where.append("s.year = ?")
        params.append(year)
    if kind:
        where.append("s.kind LIKE ?")
        params.append(f"%{kind}%")
    sql = ("SELECT s.*, d.path FROM sections s JOIN documents d "
           "ON d.doc_id = s.doc_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY s.year DESC, s.code, s.page_start LIMIT ?"
    params.append(limit)
    return [
        SearchResult(kind=r["kind"], label=r["label"], institution=r["institution"],
                     year=r["year"], year_kind=r["year_kind"], doc_id=r["doc_id"],
                     path=r["path"], page_label=r["page_label"], text=r["label"])
        for r in conn.execute(sql, params)
    ]


def find_facts(conn: sqlite3.Connection, *, institution: str | None = None,
               year: int | None = None, field: str | None = None,
               code: str | None = None, limit: int = 100) -> list[dict]:
    """某個數字是多少、出自哪裡。

    `value` 為 None 時意義是**空白／未編列**，呼叫端不得當成 0——
    這是稽查發現本身（例如業務發展費沒有編列預算）。
    """
    where, params = [], []
    if institution:
        where.append("f.institution LIKE ?")
        params.append(f"%{institution}%")
    if code:
        where.append("f.code = ?")
        params.append(code)
    if year is not None:
        where.append("f.year = ?")
        params.append(year)
    if field:
        where.append("(f.field LIKE ? OR f.label LIKE ?)")
        params += [f"%{field}%", f"%{field}%"]
    sql = ("SELECT f.*, d.path, s.page_label FROM facts f "
           "LEFT JOIN documents d ON d.doc_id = f.doc_id "
           "LEFT JOIN sections s ON s.section_id = f.section_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY f.year DESC, f.institution, f.field LIMIT ?"
    params.append(limit)
    out = []
    for r in conn.execute(sql, params):
        name = (r["path"] or "").rsplit("/", 1)[-1]
        # facts.page 已經是印刷頁碼（1-based），不再加 1。
        page = r["page_label"] or (f"p.{int(r['page'])}" if r["page"] is not None else "")
        out.append({
            "institution": r["institution"], "code": r["code"],
            "year": r["year"], "year_kind": r["year_kind"],
            "field": r["field"], "label": r["label"],
            "value": r["value"], "unit": r["unit"],
            "is_blank": r["value"] is None,
            "blank_means": "空白／未編列，不是 0" if r["value"] is None else None,
            "citation": f"{name} {page}".strip() or "（無對應原始檔）",
            "path": r["path"], "page": r["page"],
        })
    return out


def find_passages(conn: sqlite3.Connection, query: str, *,
                  institution: str | None = None, year: int | None = None,
                  kind: str | None = None, limit: int = 20) -> list[SearchResult]:
    """全文檢索原文，回傳帶頁碼的片段。"""
    if len(query.strip()) < MIN_QUERY:
        return []
    # 欄位一律加 p. 前綴：passages 與 sections 都有 kind／institution／year，
    # 不限定會得到 "ambiguous column name"。
    where = ["passages MATCH ?"]
    params: list = [_fts_query(query)]
    if institution:
        where.append("p.institution LIKE ?")
        params.append(f"%{institution}%")
    if year is not None:
        where.append("p.year = ?")
        params.append(year)
    if kind:
        where.append("p.kind LIKE ?")
        params.append(f"%{kind}%")
    sql = (
        "SELECT p.kind, p.label, p.institution, p.year, p.doc_id, p.page, "
        "       snippet(passages, 0, '⟦', '⟧', ' … ', 24) AS snip, "
        "       bm25(passages) AS score, d.path, d.year_kind, s.page_label "
        "FROM passages p "
        "LEFT JOIN documents d ON d.doc_id = p.doc_id "
        "LEFT JOIN sections s ON s.section_id = p.section_id "
        "WHERE " + " AND ".join(where) +
        " ORDER BY score LIMIT ?"
    )
    params.append(limit)
    out = []
    for r in conn.execute(sql, params):
        page_label = r["page_label"] or (
            f"p.{int(r['page'])}" if r["page"] is not None else None)
        out.append(SearchResult(
            kind=r["kind"] or "", label=r["label"] or "",
            institution=r["institution"], year=r["year"],
            year_kind=r["year_kind"], doc_id=r["doc_id"], path=r["path"],
            page_label=page_label, text=r["snip"], score=float(r["score"] or 0)))
    return out


def summarise_coverage(conn: sqlite3.Connection) -> list[dict]:
    """索引涵蓋到什麼程度，以及**沒有**涵蓋到什麼。"""
    return [dict(r) for r in conn.execute(
        "SELECT scope, covered, total, note FROM coverage ORDER BY scope")]


def stats(conn: sqlite3.Connection) -> dict:
    def n(table: str) -> int:
        return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    return {"documents": n("documents"), "sections": n("sections"),
            "facts": n("facts"), "passages": n("passages")}


def matching_fields(conn: sqlite3.Connection, question: str,
                    *, limit: int = 4) -> list:
    """挑出**出現在問題字串裡**的欄位名。

    方向刻意是「欄位名 ⊂ 問題」而不是反過來。使用者打的是
    「安溪 113 學年度的資遣費準備金是多少」，欄位名是「資遣費準備金（非流動
    資產）」——用 `label LIKE %問題%` 永遠不會命中，而不做過濾又會回傳一堆
    無關的數字充當答案。中文沒有空白可切，所以直接用子字串包含判斷。
    """
    if not question.strip():
        return []
    rows = conn.execute("SELECT DISTINCT label, field FROM facts")
    hits = []
    for r in rows:
        label = r["label"]
        core = label.split("（")[0].split("(")[0].strip()
        if len(core) >= 2 and core in question:
            hits.append((len(core), core))
    # 長的欄位名優先：「業務發展準備金」比「準備金」specific。
    hits.sort(reverse=True)
    out, seen = [], set()
    for _, core in hits:
        if core not in seen:
            seen.add(core)
            out.append(core)
        if len(out) >= limit:
            break
    return out


#: 探測詞的長度範圍。中文沒有空白可切，所以改用「這個子字串在語料裡查得到嗎」
#: 來決定它是不是一個有意義的詞——查得到就是，查不到就不是。
_PROBE_MIN, _PROBE_MAX = 4, 10
#: 探測次數上限。一次 FTS 查詢很便宜，但不該讓一個長問句跑上千次。
_PROBE_BUDGET = 240


def search_terms(conn: sqlite3.Connection, question: str,
                 *, limit: int = 4) -> list:
    """從問句裡挑出**真的能在語料裡查到**的詞。

    把整句話當片語丟進 FTS 幾乎永遠是 0 命中——使用者問的是
    「附註二關於人事費不得流出的原文」，語料裡寫的是
    「人事費不得流出」。中文沒有空白可切，也刻意不引進斷詞字典
    （多一個字典就多一個會和語料不一致的地方）。

    所以改用語料自己當字典：對問句的子字串做長度由長而短的探測，
    查得到的就是詞。這件事是確定性的、可解釋的，而且回傳的 `terms`
    讓稽查員可以自己用同樣的字串複現同一次檢索。
    """
    q = question.strip()
    if len(q) < MIN_QUERY:
        return []
    found: list = []
    probes = 0
    for size in range(min(_PROBE_MAX, len(q)), _PROBE_MIN - 1, -1):
        for start in range(0, len(q) - size + 1):
            if probes >= _PROBE_BUDGET or len(found) >= limit:
                return found
            term = q[start:start + size]
            if any(term in got or got in term for got in found):
                continue
            probes += 1
            hit = conn.execute(
                "SELECT 1 FROM passages WHERE passages MATCH ? LIMIT 1",
                (_fts_query(term),)).fetchone()
            if hit:
                found.append(term)
    return found


def retrieve(conn: sqlite3.Connection, question: str, *,
             institution: str | None = None, year: int | None = None,
             limit: int = 12) -> dict:
    """單一入口：一個問題進來，可引用的證據出去。

    刻意**不做語意改寫**。稽查場景要的是「我查了這個字串，這些文件有」，
    而不是「模型覺得這些文件意思接近」。改寫會讓稽查員無法複現同一次檢索。

    三個來源都**只在真的有過濾條件時才回傳**。先前只要問題超過 20 字就不套
    欄位過濾，於是任何問句都會附上一批與問題無關的數字；在稽查介面上，
    無關的數字擺在答案位置就是錯誤答案。
    """
    terms = search_terms(conn, question)
    passages: list = []
    seen_keys = set()
    for term in terms or [question]:
        for hit in find_passages(conn, term, institution=institution,
                                 year=year, limit=limit):
            key = (hit.doc_id, hit.label, hit.text[:40])
            if key not in seen_keys:
                seen_keys.add(key)
                passages.append(hit)
        if len(passages) >= limit:
            break
    passages = passages[:limit]

    facts: list = []
    for core in matching_fields(conn, question):
        facts += find_facts(conn, institution=institution, year=year,
                            field=core, limit=limit)
        if len(facts) >= limit:
            break
    if not facts and (institution or year is not None):
        facts = find_facts(conn, institution=institution, year=year, limit=limit)
    facts = facts[:limit]

    sections = (locate(conn, institution=institution, year=year, limit=limit)
                if (institution or year is not None) else [])

    found = len(passages) + len(facts) + len(sections)
    return {
        "question": question,
        "filters": {"institution": institution, "year": year},
        "passages": [p.as_dict() for p in passages],
        "facts": facts,
        "sections": [s.as_dict() for s in sections],
        "found": found,
        # 實際拿去查的詞。中文沒有斷詞，這裡是用語料自己當字典探測出來的——
        # 公開它才能讓稽查員用同樣的字串複現同一次檢索。
        "terms": terms,
        # 零命中時必須說清楚是「沒有這份資料」而不是「這家沒問題」。
        "note": (
            "索引中查無相符資料。全市 1,213 園有 94.8% 沒有公開財報，"
            "查不到代表資料不足，不代表低風險。" if found == 0 else
            "每筆結果皆附原始檔與頁碼；數值為空白代表未編列，不是 0。"),
        "coverage": summarise_coverage(conn) if found == 0 else None,
    }
