"""上傳的原件怎麼入庫：先看內容認檔，認不得就真的抽取。

使用者決定「上傳就自動抽取」（2026-09-13），所以一份沒見過的 PDF 進來，這裡
會在背景跑 Bedrock 視覺抽取，完成後併進資料室。三件事不能妥協：

1. **不看檔名。** 已知的 132 份原件用 SHA-256 認，認得就用既有的抽取結果、
   不花錢。認不得的，機構與學年度只從抽出來的頁面判斷。
2. **判斷不出來就不猜。** 頁尾代號彼此矛盾、期間欄推不出學年度，一律標
   「無法辨識」、不併入。把數字歸到錯的園是這條管線最嚴重的錯誤
   （``docs/EXTRACTION_PIPELINE.md`` 護欄一）；讀不到頁尾的頁也照那裡的規則
   排除，不用推定的名義進來。
3. **先試讀再花錢。** 先抽前三頁，連一個名冊上的代號都讀不到就停——不為一份
   根本不是財報的 PDF 付整份的錢。

學年度：期間欄迄日是 Y.7.31 → 學年度 Y−1，取最大；沒有期間才看基準日。
只看迄日是因為開辦年的起日不是 8/1（東湖 111.8.30、新樂 111.2.1）；取最大
是因為比較欄是前一年。對 132 份已知答案的報告逐一驗證過（tests/test_dataroom.py）。

抽出來的頁與報告寫在 ``data/runtime/``，不寫 ``data/extracted/``——那是交付
語料，``build_pagewise_facts.py`` 的 glob 會把新資料夾併進事實表。
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import store

DPI = 170          # 與 scripts/extract_pages_bedrock.py 同值，理由寫在那裡
MAX_PAGES = 60     # 一份非營利園財報 37–45 頁；超過就不像，沒花錢前先擋
PROBE_PAGES = 3
# token 上限是整個帳號共用的（EXTRACTION_PIPELINE.md §4），抽取時助理本身也在
# 用同一份額度，所以不開到 16。
WORKERS = 8
# 牌價估算，不是帳單（理由同 extract_pages_bedrock.py 的 USD_IN_PER_M）。
USD_IN_PER_M, USD_OUT_PER_M = 3.00, 15.00

_END = re.compile(r"[~～至\-－—到]\s*(\d{3})\s*[./年]\s*0?7\s*[./月]\s*31")
_BASE = re.compile(r"^\D*(\d{3})\s*[./年]\s*0?7\s*[./月]\s*31\D*$")

#: 這個行程的代號。工作紀錄上的不一樣，代表抽到一半伺服器重啟過。
_BOOT = uuid.uuid4().hex
_client = None


def academic_year(pages: list[dict]) -> int | None:
    """只從頁面內容推學年度。推不出來回 None，不猜。"""
    ends: set[int] = set()
    bases: set[int] = set()
    for p in pages:
        for t in p.get("tables") or []:
            for label in t.get("period_labels") or []:
                s = str(label)
                ends.update(int(m.group(1)) - 1 for m in _END.finditer(s))
                m = _BASE.match(s)
                if m:
                    bases.add(int(m.group(1)) - 1)
    if ends:
        return max(ends)
    return max(bases) if bases else None


def _code(page: dict) -> str | None:
    got = page.get("footer_code")
    return str(got).strip().upper() if got else None


def _bedrock_page(image: bytes, page: int) -> tuple[dict, int, int, str]:
    del page  # 模型只看影像；頁碼是給測試替身用的
    global _client
    from .. import bedrock
    from ..extract.pagewise import extract_page

    if _client is None:
        _client = bedrock.client(timeout=bedrock.TIMEOUT_VISION, max_retries=4)
    payload, tin, tout, _raw = extract_page(_client, bedrock.DEFAULT_MODEL, image, 16000)
    return payload, tin, tout, bedrock.DEFAULT_MODEL


def _spawn(fn) -> None:
    threading.Thread(target=fn, daemon=True).start()


#: 抽一頁的函式、開背景工作的方式。測試換成替身：不打 Bedrock、不開執行緒。
EXTRACT = _bedrock_page
SPAWN = _spawn


# ── 對外 ──────────────────────────────────────────────────────────────
def submit(filename: str, blob: bytes) -> dict:
    """收一份 PDF。認得就直接入庫；認不得就開始抽取，回傳工作。"""
    sha = hashlib.sha256(blob).hexdigest()
    name = pathlib.Path(filename or "上傳.pdf").name
    known = next((r for r in store.index().get("reports", [])
                  if r.get("sha256") == sha), None)
    if known is not None:
        if store.is_loaded(known["id"]):
            return {"ok": True, "status": "already_loaded", "report": known["id"],
                    "institution": known["short_name"],
                    "academic_year": known["academic_year"],
                    "detail": f"{known['short_name']} {known['academic_year']} 學年度"
                              "的這份原件已在庫中，沒有重複入庫"}
        return store.register(known["id"], name, blob)

    pages = store.page_count(blob)
    if not pages:
        return {"ok": False, "reason": "unreadable", "detail": "這個 PDF 打不開或沒有頁面"}
    if pages > MAX_PAGES:
        return {"ok": False, "reason": "too_many_pages",
                "detail": f"這份有 {pages} 頁，超過 {MAX_PAGES} 頁，不像一份非營利園財報；"
                          "為免白花抽取費用，沒有處理"}

    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "status": "extracting", "filename": name,
           "pages_total": pages, "pages_done": 0, "tokens_in": 0, "tokens_out": 0,
           "started": time.time(), "boot": _BOOT,
           "upload": {"filename": name, "sha256": sha, "bytes": len(blob),
                      "pdf_pages": pages, "stored": store.save_upload(sha, blob)}}
    store.mutate(lambda s: s.setdefault("jobs", {}).__setitem__(job_id, job))
    SPAWN(lambda: _run(job_id, blob))
    return {"ok": True, "status": "extracting", "job": job_view(job)}


def job_view(j: dict) -> dict:
    out = {k: v for k, v in j.items() if k not in ("boot", "upload")}
    if j["status"] == "extracting" and j.get("boot") != _BOOT:
        out["status"] = "interrupted"
        out["detail"] = "伺服器在抽取途中重新啟動，這次抽取沒有完成，請重新上傳"
    out["usd_list_price"] = round(j["tokens_in"] / 1e6 * USD_IN_PER_M
                                  + j["tokens_out"] / 1e6 * USD_OUT_PER_M, 3)
    return out


def job(job_id: str) -> dict | None:
    j = (store.state().get("jobs") or {}).get(job_id)
    return job_view(j) if j else None


def recent_jobs(limit: int = 5) -> list[dict]:
    """最近的抽取工作，不含完整結果（那份很長，助理只需要狀態）。"""
    jobs = sorted((store.state().get("jobs") or {}).values(),
                  key=lambda j: -j["started"])
    return [{k: v for k, v in job_view(j).items() if k != "result"}
            for j in jobs[:limit]]


# ── 背景抽取 ──────────────────────────────────────────────────────────
def _update(job_id: str, refresh: bool = False, **fields) -> None:
    # 抽到一半被「重設」清掉的話，改一份沒人讀的暫存就好，不要讓執行緒炸掉。
    store.mutate(lambda s: s.get("jobs", {}).get(job_id, {}).update(fields),
                 refresh=refresh)


def _run(job_id: str, blob: bytes) -> None:
    try:
        _extract(job_id, blob)
    except Exception as exc:  # noqa: BLE001 - 背景執行緒的例外沒有人接，要寫進工作紀錄
        from .. import bedrock
        _update(job_id, status="failed", detail=bedrock.explain_error(exc))


def _extract(job_id: str, blob: bytes) -> None:
    import pymupdf

    from ..extract.pagewise import validate_page
    from .build import build_report, index_entry

    job = store.state()["jobs"][job_id]
    tally = {"pages_done": 0, "tokens_in": 0, "tokens_out": 0}
    lock = threading.Lock()

    def one(page: int) -> dict:
        with pymupdf.open(stream=blob, filetype="pdf") as doc:
            image = doc[page - 1].get_pixmap(dpi=DPI).tobytes("png")
        payload, tin, tout, model = EXTRACT(image, page)
        problems = validate_page(payload)
        with lock:
            tally["pages_done"] += 1
            tally["tokens_in"] += tin
            tally["tokens_out"] += tout
            _update(job_id, **tally)
        return {**payload, "pdf_page": page, "model": model, "dpi": DPI,
                "backend": "bedrock", "schema_problems": problems}

    def read(first: int, last: int) -> list[dict]:
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            return list(ex.map(one, range(first, last + 1)))

    roster = {r["code"]: r["short_name"] for r in store.index().get("reports", [])}
    probe = read(1, min(PROBE_PAGES, job["pages_total"]))
    if not {_code(p) for p in probe} & roster.keys():
        return _update(job_id, status="unidentified",
                       detail=f"前 {len(probe)} 頁讀不到任何名冊上的機構代號（頁尾的"
                              "「代號-頁碼」），看起來不是非營利園財報；為免白花費用，已停止抽取")
    pages = probe + read(len(probe) + 1, job["pages_total"])

    codes = {_code(p) for p in pages} - {None}
    if len(codes) != 1:
        return _update(job_id, status="unidentified",
                       detail="頁尾代號彼此不一致（" + "、".join(sorted(codes)) + "），"
                              "無法確定是哪一所園，沒有併入")
    code = codes.pop()
    good = [p for p in pages if _code(p) == code]
    year = academic_year(good)
    if year is None:
        return _update(job_id, status="unidentified",
                       detail="抽到的頁面沒有能判斷學年度的期間欄（迄日 7/31），"
                              "無法確定學年度，沒有併入")
    short = roster[code]
    rid = f"{code}_{short}_{year}"
    if any(r["id"] == rid for r in store.index().get("reports", [])):
        return _update(job_id, status="duplicate", report=rid,
                       detail=f"庫中已有 {short} {year} 學年度（不同的檔案），"
                              "保留原本的抽取結果，沒有覆蓋")

    records = sorted(({**p, "report": rid, "code": code, "short_name": short,
                       "academic_year": year, "identity_ok": True} for p in good),
                     key=lambda p: p["pdf_page"])
    folder = store.EXTRACTED / "pages" / rid
    folder.mkdir(parents=True, exist_ok=True)
    for rec in records:
        (folder / f"p{rec['pdf_page']:02d}.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    rep = build_report({"id": rid, "pages": records})
    store.add_extracted(rep, {**index_entry(rep), "sha256": job["upload"]["sha256"]},
                        job["upload"])
    _update(job_id, refresh=True, status="done", report=rid,
            quarantined=len(pages) - len(good), result=store.summary(rid))
