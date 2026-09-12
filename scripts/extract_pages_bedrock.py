"""Extract pages of the 非營利園 reports with Bedrock's vision model.

Why this exists: the earlier extraction located three statements and four notes
per report -- 1,180 of 5,162 pages. The other 77% was never opened, and it is not
filler: 現金流量表, 淨值變動表 and the 收支明細表 breakdowns all live there.

**This runner reads the pages it is told to read, and in practice that has been a
*targeted* selection, not the whole corpus.** ``--pages-from`` takes a plan built
by ``plan_from_toc.py`` from each report's own printed 目錄, which is how the
delivered dataset was produced: 1,672 pages covering six named sections, not
5,162. Reading everything is possible (omit the flag) but costs roughly seven
hours at this account's token ceiling against about one for the targeted plan.
Say "定向抽取" when describing what was run, unless a full pass was actually
made -- see ``docs/EXTRACTION_PIPELINE.md`` §4 for the coverage that exists.

Design constraints that are not negotiable, each from a failure this project
already had:

* **Resumable per page.** 5,000 pages is an hour of wall clock. A crash, an
  expired STS token, or a laptop lid must cost the pages in flight and nothing
  else, so each page is its own file and an existing file is never re-fetched.
* **The footer code is checked, not trusted.** Every page reports the
  ``<代號>-<頁碼>`` printed on it; a page whose code disagrees with the PDF it was
  rendered from is quarantined rather than written. Misattribution is this
  pipeline's worst failure and it has happened twice.
* **The ledger is written as it goes.** Token spend is appended after every
  page, so an interrupted run still says what it cost.
* **Pages already extracted the old way are re-read anyway.** Those 1,180 pages
  become a free agreement check between the dev-phase extraction and Bedrock --
  the first time this project can quantify its own extraction accuracy without
  hand-marking ground truth.

Usage
    # pilot: 20 pages, one 學年度, see what it costs before committing
    PYTHONPATH=src .venv/Scripts/python scripts/extract_pages_bedrock.py --limit 20

    # full corpus, 12 concurrent
    PYTHONPATH=src .venv/Scripts/python scripts/extract_pages_bedrock.py --workers 12

    # just one year, or one report
    PYTHONPATH=src .venv/Scripts/python scripts/extract_pages_bedrock.py --year 113
    PYTHONPATH=src .venv/Scripts/python scripts/extract_pages_bedrock.py --code N01
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import fitz

from smart_watchdog import bedrock as _bedrock
from smart_watchdog.console import use_utf8
from smart_watchdog.extract.pagewise import (
    PAGE_PROMPT,
    PAGE_SCHEMA,
    identity_ok,
    validate_page,
)

REPORT_DIR = pathlib.Path("data/raw/資料集/非營利園財報")
OUT_ROOT = pathlib.Path("data/extracted/nonprofit_pages")
QUARANTINE = pathlib.Path("data/extracted/nonprofit_pages/_quarantine")
LEDGER = pathlib.Path("data/runtime/pagewise_ledger.jsonl")

FILENAME_RE = re.compile(r"^(N\d\d)(.+?)_(\d{3})學年度")

# Public list price for Sonnet on Bedrock, us-west-2, per million tokens.
#
# ⚠️ These are hardcoded constants, so every dollar figure this script prints is
# *arithmetic on token counts*, not a reading of what AWS charged. Two reasons it
# can be wrong in either direction: list prices change without this file changing,
# and the competition runs on a Workshop Studio vended account whose Bedrock usage
# bills to the organiser's payer account -- where the real figure may be zero for
# the participant and nonzero for someone else. Cost Explorer is the authority,
# and it lags about two days. Never quote this number as a bill.
USD_IN_PER_M = 3.00
USD_OUT_PER_M = 15.00

# 170dpi keeps printed figures legible on these 300dpi A4 scans while cutting the
# image token count roughly in half versus 200. Raise it if an agreement check
# starts showing digit errors -- that is the signal, not a guess.
DPI = 170

_ledger_lock = threading.Lock()
_print_lock = threading.Lock()


def render(pdf: pathlib.Path, page_index: int, dpi: int) -> bytes:
    doc = fitz.open(pdf)
    try:
        return doc[page_index].get_pixmap(dpi=dpi).tobytes("png")
    finally:
        doc.close()


def page_count(pdf: pathlib.Path) -> int:
    doc = fitz.open(pdf)
    try:
        return doc.page_count
    finally:
        doc.close()


def complete(dest: pathlib.Path) -> bool:
    """Is this page already done *and* readable?

    Existence alone is not completion. A run killed mid-write leaves a truncated
    file, and a resume that skips on existence would step over it forever. Parsing
    it is cheap and is the only check that distinguishes the two.
    """
    if not dest.exists():
        return False
    try:
        json.loads(dest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return True


def write_atomic(dest: pathlib.Path, record: dict) -> None:
    """Write via a temp file in the same directory, then rename.

    os.replace is atomic within a filesystem, so a reader (or a resume) never sees
    a half-written page -- it sees either the previous state or the complete file.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, dest)


def append_ledger(row: dict) -> None:
    with _ledger_lock:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def extract_page(client, model: str, image: bytes,
                 max_tokens: int) -> tuple[dict, int, int, str]:
    """One Bedrock call. Returns (payload, in_tokens, out_tokens, raw_text)."""
    b64 = base64.standard_b64encode(image).decode()
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        # Structured Outputs is enforced on Bedrock, so there is no parse-retry loop.
        output_config={"format": {"type": "json_schema", "schema": PAGE_SCHEMA}},
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                {"type": "text", "text": PAGE_PROMPT},
            ],
        }],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    usage = getattr(resp, "usage", None)
    tin = getattr(usage, "input_tokens", 0) or 0
    tout = getattr(usage, "output_tokens", 0) or 0
    return json.loads(text), tin, tout, text


def do_one(task: dict, client, model: str, dpi: int, max_tokens: int,
           retries: int, state: dict) -> dict:
    """Render, extract, verify identity, write. Never raises."""
    dest = OUT_ROOT / task["report"] / f"p{task['page']:02d}.json"
    if complete(dest):
        state["skipped"] += 1
        return {"status": "skip"}

    try:
        image = render(pathlib.Path(task["pdf"]), task["page"] - 1, dpi)
    except Exception as exc:  # noqa: BLE001 - a bad page must not kill the run
        state["failed"] += 1
        return {"status": "render_error", "error": str(exc)}

    last = ""
    for attempt in range(retries + 1):
        try:
            payload, tin, tout, _raw = extract_page(client, model, image, max_tokens)
            break
        except Exception as exc:  # noqa: BLE001
            last = _bedrock.explain_error(exc)
            name = type(exc).__name__
            if "Throttl" in name or "Throttl" in str(exc) or "TooManyRequests" in str(exc):
                state["throttled"] += 1
            # Throttling and transient 5xx are worth waiting out; an expired
            # token or a denied model will never succeed, so stop immediately.
            if "Expired" in str(exc) or "AccessDenied" in name or "AccessDenied" in str(exc):
                state["fatal"] = last
                state["failed"] += 1
                return {"status": "fatal", "error": last}
            if attempt == retries:
                state["failed"] += 1
                append_ledger({"key": f"{task['report']}/p{task['page']:02d}",
                               "error": last, "ts": time.time()})
                return {"status": "error", "error": last}
            time.sleep(min(2 ** attempt + random.random() * 2, 30))
    else:  # pragma: no cover - loop always breaks or returns
        return {"status": "error", "error": last}

    ident = identity_ok(payload, task["code"])
    problems = validate_page(payload)
    record = {
        "report": task["report"], "code": task["code"], "short_name": task["short"],
        "academic_year": int(task["year"]), "pdf_page": task["page"],
        "dpi": dpi, "model": model, "backend": "bedrock",
        "identity_ok": ident,
        "schema_problems": problems,
        **payload,
    }

    if ident is not True:
        # Either the page names a different report, or its footer could not be
        # read at all. Both are unknowns, and an unknown must not enter the corpus
        # under a name we merely assumed from the filename.
        QUARANTINE.mkdir(parents=True, exist_ok=True)
        q = QUARANTINE / f"{task['report']}_p{task['page']:02d}.json"
        write_atomic(q, record)
        state["quarantined"] += 1
        state["unreadable_footer"] += int(ident is None)
        status = "quarantine"
    else:
        write_atomic(dest, record)
        state["done"] += 1
        state["ragged"] += int(bool(problems))
        status = "ok"

    state["tin"] += tin
    state["tout"] += tout
    append_ledger({"key": f"{task['report']}/p{task['page']:02d}", "status": status,
                   "kind": payload.get("page_kind"), "in": tin, "out": tout,
                   "identity_ok": ident, "problems": problems, "ts": time.time()})
    return {"status": status}


def load_plan(path: pathlib.Path) -> dict[str, set]:
    """Read a targeting plan: {"<report>": [pdf page numbers]}.

    Written by ``plan_from_toc.py`` from each report's own printed table of
    contents, so the pages we fetch are the ones that report says carry the
    sections we want -- not a page range guessed from a different report.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: set(v.get("pages", v) if isinstance(v, dict) else v) for k, v in raw.items()}


def build_tasks(years: list[str], code_filter: str | None) -> list[dict]:
    tasks: list[dict] = []
    for year in years:
        src = REPORT_DIR / f"{year}學年度"
        if not src.is_dir():
            continue
        for pdf in sorted(src.glob("*.pdf")):
            m = FILENAME_RE.match(pdf.name)
            if not m:
                print(f"  ⚠ 檔名不符預期，略過：{pdf.name}")
                continue
            code, short, _ = m.groups()
            if code_filter and code != code_filter:
                continue
            report = f"{code}_{short}_{year}"
            tasks.extend(
                {"pdf": str(pdf), "report": report, "code": code,
                 "short": short, "year": year, "page": page}
                for page in range(1, page_count(pdf) + 1)
            )
    return tasks


def main() -> None:
    ap = argparse.ArgumentParser(description="以 Bedrock 視覺模型逐頁抽取非營利園財報")
    ap.add_argument("--year", action="append",
                    help="限定學年度，可重複；預設 110 111 112 113")
    ap.add_argument("--code", help="限定單一園代號，例如 N01")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 頁（試跑用）")
    ap.add_argument("--only-page", type=int, action="append",
                    help="只抽指定的 PDF 頁碼，可重複（例如 --only-page 2 抽目錄）")
    ap.add_argument("--pages-from", help="依 plan_from_toc.py 產生的計畫檔只抽指定頁")
    ap.add_argument("--workers", type=int, default=8, help="並行數")
    ap.add_argument("--dpi", type=int, default=DPI)
    ap.add_argument("--max-tokens", type=int, default=16000)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--model", default=_bedrock.DEFAULT_MODEL)
    ap.add_argument("--dry-run", action="store_true", help="只列出要做什麼，不呼叫模型")
    a = ap.parse_args()
    use_utf8()

    years = a.year or ["110", "111", "112", "113"]
    tasks = build_tasks(years, a.code)

    if a.only_page:
        want = set(a.only_page)
        tasks = [t for t in tasks if t["page"] in want]
    if a.pages_from:
        plan = load_plan(pathlib.Path(a.pages_from))
        before = len(tasks)
        tasks = [t for t in tasks if t["page"] in plan.get(t["report"], ())]
        missing = sorted(set(plan) - {t["report"] for t in tasks})
        print(f"計畫檔鎖定 {len(tasks)} 頁（語料 {before} 頁）")
        if missing:
            print(f"  ⚠ 計畫檔有 {len(missing)} 份報告在語料中找不到：{missing[:5]}")
    pending = [t for t in tasks
               if not (OUT_ROOT / t["report"] / f"p{t['page']:02d}.json").exists()]
    already = len(tasks) - len(pending)
    if a.limit:
        pending = pending[:a.limit]

    print(f"語料 {len({t['report'] for t in tasks})} 份報告／{len(tasks)} 頁")
    print(f"待抽取 {len(pending)} 頁（已完成 {already} 頁）")
    if a.dry_run or not pending:
        print("（--dry-run，未呼叫模型）" if a.dry_run else "沒有待抽取的頁面。")
        return

    _bedrock.load_env()
    if not _bedrock.credentials_present():
        sys.exit("找不到 AWS 憑證。請確認 .env 內的 AWS_ACCESS_KEY_ID 等四項。")

    client = _bedrock.client()
    state = {"done": 0, "skipped": 0, "failed": 0, "quarantined": 0,
             "throttled": 0, "ragged": 0, "unreadable_footer": 0,
             "tin": 0, "tout": 0, "fatal": None}
    started = time.time()
    print(f"模型 {a.model}　並行 {a.workers}　{a.dpi}dpi")

    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futures = {pool.submit(do_one, t, client, a.model, a.dpi,
                               a.max_tokens, a.retries, state): t for t in pending}
        for i, fut in enumerate(as_completed(futures), start=1):
            fut.result()
            if state["fatal"]:
                for f in futures:
                    f.cancel()
                break
            if i % 25 == 0 or i == len(pending):
                el = time.time() - started
                rate = i / el if el else 0.0
                usd = state["tin"] / 1e6 * USD_IN_PER_M + state["tout"] / 1e6 * USD_OUT_PER_M
                eta = f"　剩約 {(len(pending) - i) / rate / 60:.0f} 分" if rate else ""
                with _print_lock:
                    print(f"  {i}/{len(pending)}　成功 {state['done']}　"
                        f"失敗 {state['failed']}　隔離 {state['quarantined']}　"
                        f"限流 {state['throttled']}　"
                        f"{rate:.2f} 頁/秒　估US${usd:.2f}{eta}")

    el = time.time() - started
    usd = state["tin"] / 1e6 * USD_IN_PER_M + state["tout"] / 1e6 * USD_OUT_PER_M
    print("")
    print(f"完成 {state['done']} 頁　失敗 {state['failed']}　"
          f"隔離 {state['quarantined']}　限流重試 {state['throttled']}")
    if state["ragged"]:
        print(f"⚠ {state['ragged']} 頁含欄位對不齊的表（已標 aligned=false，彙整時會拒收）")
    if state["unreadable_footer"]:
        print(f"⚠ {state['unreadable_footer']} 頁讀不到頁尾代號，一併隔離")
    print(f"吞吐 {state['done'] / el:.2f} 頁/秒（並行 {a.workers}）")
    print(f"用時 {el / 60:.1f} 分　tokens in {state['tin']:,} out {state['tout']:,}")
    print(f"牌價估算 US${usd:.2f}　"
          f"⚠ 這是 token 數 × 公開牌價的本地算術，不是 AWS 帳單")
    print("  實際費用以 Cost Explorer 為準（約有兩天延遲）；"
          "Workshop 帳號的用量可能結算到主辦方")
    print(f"帳本：{LEDGER}")
    if state["quarantined"]:
        print(f"⚠ {state['quarantined']} 頁的頁尾代號與檔名不符，已隔離到 {QUARANTINE}")
    if state["fatal"]:
        print(f"⚠ 中止：{state['fatal']}")


if __name__ == "__main__":
    main()
