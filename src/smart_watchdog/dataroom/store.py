"""資料室的資料存取層。**HTTP 端點與 agent 工具共用同一份。**

兩邊各寫一份查詢的那天，就是畫面上的數字與助理講的數字開始不一致的那天。

切片由 ``scripts/build_dataroom_slice.py`` 產生在 ``data/interim/dataroom/``。
缺檔時每個函式回空值而不是拋例外——資料室起不來不該讓整個派工台掛掉。

## 「已載入 / 待載入」是什麼

``DEFAULT_PENDING`` 裡的報告預設不計入總數、查不到、agent 也讀不到。
上傳它的 PDF 之後才進來。這不是開關，是**同一條入庫路徑的兩端**：
``ingest()`` 做的事就是把一份原件對上它的抽取結果並登錄進來。

狀態寫在 ``data/runtime/dataroom.json``（``data/runtime/`` 已 gitignore），
所以重設示範只要刪掉那個檔。
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[3]
SLICE = ROOT / "data/interim/dataroom"
STATE = ROOT / "data/runtime/dataroom.json"
UPLOADS = ROOT / "data/runtime/dataroom_uploads"

#: 預設保留、等待上傳的報告。挑 N04 海工 113 是因為它的表最有變化
#: （37 頁、59 張表、28 個類別），一份進來就能看出總數與選單的差別。
DEFAULT_PENDING: tuple[str, ...] = ("N04_海工_113",)

#: 原件檔名 → 報告代號。`N04海工_113學年度財務報告.pdf`
_FILENAME = re.compile(r"^(N\d\d)(.+?)_(\d{3})學年度")

_cache: dict[str, Any] = {"index": None, "reports": {}}


# ── 底層讀取 ──────────────────────────────────────────────────────────
def available() -> bool:
    """切片在不在。不在就整室顯示「尚未建立索引」，不是顯示 0。"""
    return (SLICE / "index.json").exists()


def index() -> dict:
    if _cache["index"] is None:
        if not available():
            _cache["index"] = {"totals": {}, "sections": [], "reports": [],
                               "public": [], "missing": True}
        else:
            _cache["index"] = json.loads(
                (SLICE / "index.json").read_text(encoding="utf-8"))
    return _cache["index"]


def _report_raw(rid: str) -> dict | None:
    """整份報告（含尚未載入的）。只給內部用——對外一律走 report()。"""
    if rid in _cache["reports"]:
        return _cache["reports"][rid]
    f = SLICE / "r" / f"{rid}.json"
    if not f.exists():
        return None
    data = json.loads(f.read_text(encoding="utf-8"))
    _cache["reports"][rid] = data
    return data


# ── 載入狀態 ──────────────────────────────────────────────────────────
def state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass  # 壞檔就當沒設過，回預設；示範現場不該卡在這裡
    return {"pending": list(DEFAULT_PENDING), "uploads": {}}


def _save(s: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE)


def pending() -> set[str]:
    return set(state().get("pending") or ())


def is_loaded(rid: str) -> bool:
    return rid not in pending()


def reset() -> dict:
    """回到上傳前。刪掉已上傳的檔，狀態回預設。"""
    for f in UPLOADS.glob("*.pdf"):
        f.unlink()
    _save({"pending": list(DEFAULT_PENDING), "uploads": {}})
    return overview()


# ── 對外查詢 ──────────────────────────────────────────────────────────
def overview() -> dict:
    """總目。**統計只加總已載入的報告**，所以上傳前後的數字會不一樣。"""
    idx = index()
    if idx.get("missing"):
        return {"missing": True, "totals": {}, "sections": [],
                "reports": [], "public": []}

    hold = pending()
    st = state()
    loaded = [r for r in idx["reports"] if r["id"] not in hold]

    totals = {k: sum(r.get(k, 0) for r in loaded)
              for k in ("tables", "refused", "cells", "valued", "blank")}
    totals["reports"] = len(loaded)

    sec_tables: dict[str, int] = {}
    sec_reports: dict[str, int] = {}
    for r in loaded:
        for key, n in (r.get("sec_tables") or {}).items():
            sec_tables[key] = sec_tables.get(key, 0) + n
            sec_reports[key] = sec_reports.get(key, 0) + 1

    from . import tabletypes as tt
    # 家族順序優先於張數：選單要先有結構，同一族內才照張數排。
    # 「未分類明細」不論多大一律排在族內最後——它是殘料，不是門面；點進家族時
    # 落在它上面會讓人以為那一族就長這樣。它仍然看得見，只是不當代表。
    sections = sorted(
        ({"key": k, "zh": tt.zh(k), "tables": n, "reports": sec_reports[k],
          "family": tt.family(k), "family_zh": tt.family_zh(tt.family(k))}
         for k, n in sec_tables.items()),
        key=lambda s: (tt.FAMILY_ORDER.index(s["family"]),
                       s["key"] == tt.UNROUTED, -s["tables"]))

    return {
        "totals": totals,
        "sections": sections,
        "reports": [{**r, "state": "pending" if r["id"] in hold else "loaded",
                     "upload": (st.get("uploads") or {}).get(r["id"])}
                    for r in idx["reports"]],
        "public": idx.get("public") or [],
        "pending": sorted(hold),
    }


def report(rid: str) -> dict | None:
    """一份已載入的報告。尚未載入的一律回 None——它不在庫裡就是不在。"""
    if not is_loaded(rid):
        return None
    return _report_raw(rid)


def _match(r: dict, institution: str | None, year: int | None) -> bool:
    if year is not None and r["academic_year"] != year:
        return False
    if institution:
        q = institution.strip()
        if q not in r["short_name"] and q.upper() != r["code"].upper():
            return False
    return True


def loaded_reports(institution: str | None = None,
                   year: int | None = None) -> list[dict]:
    hold = pending()
    return [r for r in index().get("reports", [])
            if r["id"] not in hold and _match(r, institution, year)]


def find_tables(section: str | None = None, institution: str | None = None,
                year: int | None = None, limit: int = 50) -> list[dict]:
    """符合條件的表。回的是表頭資訊不含列，列要用 get_table() 另取。"""
    out: list[dict] = []
    for meta in loaded_reports(institution, year):
        rep = _report_raw(meta["id"])
        if not rep:
            continue
        for t in rep["tables"]:
            if section and t["section"] != section:
                continue
            out.append(_table_head(rep, t))
            if len(out) >= limit:
                return out
    return out


def _table_head(rep: dict, t: dict) -> dict:
    return {
        "uid": t["uid"], "report": rep["id"],
        "institution": rep["short_name"], "code": rep["code"],
        "academic_year": rep["academic_year"],
        "section": t["section"], "section_zh": t["section_zh"],
        "section_inherited": t["section_inherited"],
        "title": t["title"], "context_heading": t["context_heading"],
        "unit": t["unit"], "aligned": t["aligned"],
        "pdf_page": t["pdf_page"], "printed_page": t["printed_page"],
        "n_columns": len(t["period_labels"]), "n_rows": len(t["rows"]),
    }


def get_table(uid: str) -> dict | None:
    """一張表的全部內容，含空白格。

    ``values`` 裡的 ``null`` 代表原件那一格是空白（未編列），**不是 0**。
    """
    m = re.match(r"^(N\d\d)/(\d{3})/p(\d+)/t(\d+)$", uid or "")
    if not m:
        return None
    code, year, page, ti = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
    for meta in loaded_reports():
        if meta["code"] != code or meta["academic_year"] != year:
            continue
        rep = _report_raw(meta["id"])
        if not rep:
            return None
        for t in rep["tables"]:
            if t["pdf_page"] == page and t["table_index"] == ti:
                return {
                    **_table_head(rep, t),
                    "period_labels": t["period_labels"],
                    "rows": t["rows"],
                    "issues": t["issues"],
                    "pdf": rep.get("pdf"),
                    "null_means": "空白／未編列，不是 0",
                }
    return None


def extraction_notes(institution: str | None = None, year: int | None = None,
                     limit: int = 20) -> list[dict]:
    """抽取過程自報的疑點。

    ⚠️ 這些是「這一格我看不清楚／自相矛盾」，**不是機構的稽查發現**。
    """
    out: list[dict] = []
    for meta in loaded_reports(institution, year):
        rep = _report_raw(meta["id"])
        if not rep:
            continue
        for p in rep["pages"]:
            for text in p["issues"]:
                out.append({
                    "report": rep["id"], "institution": rep["short_name"],
                    "academic_year": rep["academic_year"],
                    "pdf_page": p["pdf_page"], "page_kind": p["page_kind"],
                    "text": text,
                    # 模型自己標了疑問詞的，是還沒解決的問題，要排在前面。
                    "unresolved": any(w in text for w in
                                      ("疑為", "請人工確認", "推測", "難以辨識",
                                       "無法辨識", "疑似")),
                })
                if len(out) >= limit * 3:
                    break
    out.sort(key=lambda n: (not n["unresolved"], n["report"], n["pdf_page"]))
    return out[:limit]


def compare_years(institution: str, section: str, years: list[int] | None = None,
                  max_rows: int = 30) -> dict:
    """同一種表跨學年度對齊。

    ⚠️ **期間逐年照抄，不改寫。** 學年度 N 的資產負債表基準日是 (N+1)/7/31，
    而同一份報告裡兩張同名表期間不同是常態；把 ``academic_year`` 當成期間
    敘述一個數字，就會產生一年偏移。所以每一年都回傳它自己的
    ``period_label`` 原文，並在對不上預期期間時標 ``period_check``。
    """
    reps = sorted(loaded_reports(institution),
                  key=lambda r: r["academic_year"])
    if years:
        reps = [r for r in reps if r["academic_year"] in years]
    if not reps:
        return {"years": [], "items": [], "unmatched_items": [],
                "note": f"沒有「{institution}」已載入的報告"}

    by_year: dict[int, dict] = {}
    for meta in reps:
        rep = _report_raw(meta["id"])
        if not rep:
            continue
        hit = next((t for t in rep["tables"]
                    if t["section"] == section and t["aligned"]), None)
        if hit:
            by_year[meta["academic_year"]] = {"rep": rep, "t": hit}

    ys = sorted(by_year)
    order: list[str] = []
    rows: dict[str, dict] = {}
    for y in ys:
        t = by_year[y]["t"]
        for r in t["rows"]:
            label = r.get("label")
            if label is None:
                continue
            if label not in rows:
                rows[label] = {}
                order.append(label)
            # 同一張表裡重複的 label（例如兩個「小　計」）只留第一次出現的，
            # 並在 unmatched 裡說明——靜默覆蓋會讓數字對到錯的列。
            rows[label].setdefault(y, r)

    items = []
    for label in order[:max_rows]:
        cell = {}
        for y in ys:
            t = by_year[y]["t"]
            r = rows[label].get(y)
            if r is None:
                cell[y] = None
                continue
            vals = r.get("values") or []
            cell[y] = {
                "values": vals,
                "period_labels": t["period_labels"],
                "blanks": [v is None for v in vals],
                "citation": f'{by_year[y]["rep"]["id"]} p.{t["printed_page"]}',
                "uid": t["uid"],
            }
        items.append({"item_label": label, "by_year": cell})

    from . import tabletypes as tt

    return {
        "institution": institution, "section": section,
        "section_zh": tt.zh(section),
        "years": ys,
        "items": items,
        "unmatched_items": [
            label for label in order
            if len(rows[label]) < len(ys)][:20],
        "note": "各年度期間為原件逐字，未經改寫；空白代表未編列，不是 0。",
    }


# ── 入庫 ──────────────────────────────────────────────────────────────
def match_filename(filename: str) -> str | None:
    """原件檔名 → 報告代號。對不上就 None。"""
    m = _FILENAME.match(pathlib.Path(filename or "").name)
    if not m:
        return None
    code, short, year = m.group(1), m.group(2), int(m.group(3))
    rid = f"{code}_{short}_{year}"
    return rid if (SLICE / "r" / f"{rid}.json").exists() else None


def ingest(filename: str, blob: bytes) -> dict:
    """收一份原件，把它的抽取結果登錄進來。

    回傳的每一項都是這個檔案的實際屬性（大小、SHA-256、頁數），以及登錄後
    真正多出來的東西（表數、類別、數字），不做任何估計。
    """
    rid = match_filename(filename)
    if not rid:
        return {"ok": False, "reason": "unknown_document",
                "detail": "檔名對不上任何一份已抽取的報告"}

    rep = _report_raw(rid)
    if rep is None:
        return {"ok": False, "reason": "no_extraction",
                "detail": f"{rid} 沒有頁級抽取結果"}

    sha = hashlib.sha256(blob).hexdigest()
    pages = None
    try:
        import pymupdf
        with pymupdf.open(stream=blob, filetype="pdf") as doc:
            pages = doc.page_count
    except Exception:  # noqa: BLE001 - 缺套件或壞檔都只是拿不到頁數
        pages = None

    UPLOADS.mkdir(parents=True, exist_ok=True)
    (UPLOADS / f"{sha[:16]}.pdf").write_bytes(blob)

    s = state()
    s["pending"] = [p for p in (s.get("pending") or []) if p != rid]
    uploads = s.setdefault("uploads", {})
    uploads[rid] = {"filename": pathlib.Path(filename).name,
                    "sha256": sha, "bytes": len(blob), "pdf_pages": pages,
                    "stored": f"data/runtime/dataroom_uploads/{sha[:16]}.pdf"}
    _save(s)
    _cache["index"] = None  # 總數要重算

    from . import tabletypes as tt
    secs = sorted({t["section"] for t in rep["tables"]})
    cells = sum(len(r.get("values") or [])
                for t in rep["tables"] if t["aligned"] for r in t["rows"])
    blank = sum(1 for t in rep["tables"] if t["aligned"]
                for r in t["rows"] for v in (r.get("values") or []) if v is None)

    return {
        "ok": True, "report": rid, "institution": rep["short_name"],
        "academic_year": rep["academic_year"],
        "upload": uploads[rid],
        "added": {
            "extracted_pages": len(rep["pages"]),
            "tables": len(rep["tables"]),
            "sections": [{"key": k, "zh": tt.zh(k)} for k in secs],
            "cells": cells, "blank": blank, "valued": cells - blank,
            "issues": sum(len(p["issues"]) for p in rep["pages"]),
            "identity_ok": all(p["identity_ok"] for p in rep["pages"]),
        },
        "provenance": {"models": rep.get("models"), "dpi": rep.get("dpi")},
    }


def uploaded_pdf(rid: str) -> pathlib.Path | None:
    """已上傳的原件在哪。沒有就 None（退回 data/raw 的那份）。"""
    up = (state().get("uploads") or {}).get(rid)
    if not up:
        return None
    f = ROOT / up["stored"]
    return f if f.exists() else None
