"""Acquire every 新北市 evaluation record from evaSearch, page by page.

Why this matters more than its size suggests: financial statements exist for 60
of 1,149 園 (5.2%), and 私立園 -- 75% of the market, highest penalty rate -- file
none. Evaluations are the only quality signal published for *every* type, so
this is the one source that can reach the 94.8% the forensic track cannot.

    PYTHONPATH=src .venv/bin/python scripts/download_evaluation_ntpc.py \
        --snapshot-id 2026-09-03-ntpc-full-v1

Same discipline as scripts/download_evaluation_pilot.py, which stays as the
narrow reproducible case: raw HTML pinned with its sha256 before anything is
parsed, an ordered manifest, no cookies or viewstate written to disk, and a
``--rebuild-from`` path that re-derives the CSV from pinned bytes with no network.

The site paginates 10 rows per page behind ``PageControl1$lbNextPage``. The
total is read from ``PageControl1_lblTotalCount`` on the first response and
checked against what was actually parsed, so a silently truncated crawl fails
loudly instead of producing a short table that looks complete.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.scrape import evaluation as E

CITY_LABEL = "新北市"
CITY_VALUE = "03"
ROOT = pathlib.Path("data/external/evaluation")
OUT_CSV = pathlib.Path("data/processed/evaluations_ntpc_full.csv")
PARSER_SCHEMA = "eva-search-results-v1"
# One request every 1.5 s. The whole city is ~111 pages, so this is about three
# minutes of traffic against a public search form -- deliberate, not hurried.
DELAY_SECONDS = 1.5
MAX_PAGES = 400

# The outer listing row carries every 園 in the result set, including those the
# nested history GridView omits because they have none. Its text ends with
# 「評鑑情形：尚未接受評鑑」, which is the site stating the absence explicitly --
# the difference between "no record" and "passed" has to survive into our data.
LISTING_FIELDS = {
    "city": "縣市", "town": "鄉鎮", "establishment_type": "設立別",
    "address": "地址", "phone": "電話", "website": "園所網址",
    "approved_capacity": "核定人數", "after_school": "兼辦國小課後",
    "evaluation_status": "評鑑情形",
}
TOTAL_RE = re.compile(r'id="PageControl1_lblTotalCount"[^>]*>(\d+)<')
PAGES_RE = re.compile(r'id="PageControl1_lblTotalPage"[^>]*>(\d+)<')
CURRENT_RE = re.compile(r'id="PageControl1_lblCurrentPage"[^>]*>(?:<b>)?(\d+)')

LISTING_CSV = pathlib.Path("data/processed/evaluation_institutions_ntpc.csv")
LISTING_OUT = [
    "snapshot_id", "page", "listing_index", "response_sha256", "title",
    "city", "town", "establishment_type", "address", "phone", "website",
    "approved_capacity", "after_school", "evaluation_status", "record_count",
]

FIELDS = [
    "snapshot_id", "page", "row_index", "response_sha256",
    "title", "city", "town", "establishment_type", "address", "phone",
    "website", "approved_capacity", "after_school",
    "evaluation_academic_year", "evaluation_completed_date", "evaluation_result",
    "evaluation_report_label",
]
SOURCE_MAP = {
    "title": "園名", "city": "縣市", "town": "鄉鎮",
    "establishment_type": "設立別", "address": "地址", "phone": "電話",
    "website": "園所網址", "approved_capacity": "核定人數",
    "after_school": "兼辦國小課後", "evaluation_academic_year": "評鑑學年度",
    "evaluation_completed_date": "評鑑完成日", "evaluation_result": "評鑑結果",
    "evaluation_report_label": "評鑑報告",
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")


def _listing_from(body: bytes, snapshot_id: str, page: int) -> list[dict]:
    """Every 園 shown on the page, whether or not it has an evaluation record."""
    digest = _sha(body)
    doc = E._parse_document(body)
    out: list[dict] = []
    # Identify listing rows by their own field labels rather than by picking the
    # biggest table: the last page holds a single 園, so "most rows" selects a
    # nested history table instead and silently drops that 園.
    rows = [r for t in doc.tables for r in t.rows]
    for i, row in enumerate(rows):
        text = " ".join(str(c.get("text") or "") for c in row).strip()
        if "縣市：" not in text or "設立別：" not in text:
            continue
        rec = {"snapshot_id": snapshot_id, "page": page, "listing_index": i,
               "response_sha256": digest}
        rec["title"] = text.split("顯示更多")[0].strip() or text.split("縣市：")[0].strip()
        for field, label in LISTING_FIELDS.items():
            m = re.search(rf"{label}：\s*(.*?)(?=\s+\S+：|$)", text)
            rec[field] = (m.group(1).strip() if m else "")
        out.append(rec)
    return out


def _rows_from(body: bytes, snapshot_id: str, page: int) -> list[dict]:
    digest = _sha(body)
    out = []
    for rec in E.parse_evaluation_results(body):
        src = rec.get("source") or {}
        row = {"snapshot_id": snapshot_id, "page": page,
               "row_index": rec.get("source_row_index"), "response_sha256": digest}
        for field, key in SOURCE_MAP.items():
            row[field] = str(src.get(key, "")).strip()
        if row["title"]:
            out.append(row)
    return out


def crawl(snapshot_id: str, timeout: int) -> None:
    snap = ROOT / snapshot_id
    raw = snap / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    client = E.EvaluationClient(timeout=timeout)
    started = _utc()
    requests: list[dict] = []
    rows: list[dict] = []
    listing: list[dict] = []

    def pin(artifact, role: str, stem: str, public_query: dict) -> bytes:
        path = raw / f"{artifact.sequence:03d}_{stem}.html"
        path.write_bytes(artifact.body)
        requests.append({
            "sequence": artifact.sequence, "role": role, "method": artifact.method,
            "source_url": artifact.source_url, "final_url": artifact.final_url,
            "public_query": public_query,
            "response_sha256": _sha(artifact.body),
            "byte_length": len(artifact.body), "status": artifact.status,
            "retrieved_at_utc": _utc(),
            "raw_file": path.relative_to(snap).as_posix(),
            "parser_schema_version": PARSER_SCHEMA,
        })
        return artifact.body

    initial = client.get()
    pin(initial, "initial_get", "initial", {"city_label": CITY_LABEL})

    payload = dict(E.form_payload(initial.body))
    payload.update({"ddlCityS": CITY_VALUE, "ddlAreaS": "", "ddlEResult": "",
                    "txtSchNameS": "", "btnSearch": "查詢"})
    payload.pop("reset", None)
    current = client.post(initial, list(payload.items()))
    body = pin(current, "city_search", "ntpc_page_001",
               {"city_label": CITY_LABEL, "page": 1})

    total = int(m.group(1)) if (m := TOTAL_RE.search(body.decode("utf-8", "ignore"))) else 0
    pages = int(m.group(1)) if (m := PAGES_RE.search(body.decode("utf-8", "ignore"))) else 0
    print(f"{CITY_LABEL}：{total} 筆／{pages} 頁")
    rows.extend(_rows_from(body, snapshot_id, 1))
    listing.extend(_listing_from(body, snapshot_id, 1))

    page = 1
    while page < pages and page < MAX_PAGES:
        time.sleep(DELAY_SECONDS)
        nxt = dict(E.form_payload(current.body))
        nxt.pop("reset", None)
        nxt.pop("btnSearch", None)
        nxt["__EVENTTARGET"] = "PageControl1$lbNextPage"
        nxt["__EVENTARGUMENT"] = ""
        current = client.post(current, list(nxt.items()))
        page += 1
        body = pin(current, "page_next", f"ntpc_page_{page:03d}",
                   {"city_label": CITY_LABEL, "page": page})
        text = body.decode("utf-8", "ignore")
        shown = int(m.group(1)) if (m := CURRENT_RE.search(text)) else None
        if shown != page:
            raise SystemExit(f"第 {page} 頁的頁碼顯示為 {shown}，分頁狀態不同步，中止")
        rows.extend(_rows_from(body, snapshot_id, page))
        listing.extend(_listing_from(body, snapshot_id, page))
        if page % 20 == 0 or page == pages:
            print(f"  第 {page}/{pages} 頁　累計 {len(rows)} 筆")

    # The declared total counts 園, not evaluation records: a 園 can hold several
    # years of history, and 22 hold none at all. Check the listing against it.
    if total and len(listing) != total:
        raise SystemExit(
            f"清單解析出 {len(listing)} 園，網站宣告 {total} 園——不一致，不輸出殘缺結果")
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["title"]] = counts.get(r["title"], 0) + 1
    for r in listing:
        r["record_count"] = counts.get(r["title"], 0)

    (snap / "parsed").mkdir(exist_ok=True)
    parsed = snap / "parsed/evaluation_results.json"
    parsed.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    with LISTING_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LISTING_OUT)
        w.writeheader()
        w.writerows(listing)
    no_record = sum(1 for r in listing if not r["record_count"])
    print(f"  園 {len(listing)}（其中 {no_record} 園未見評鑑紀錄）／評鑑紀錄 {len(rows)} 筆")

    manifest = {
        "manifest_version": 1, "snapshot_id": snapshot_id,
        "source_url": E.DEFAULT_URL,
        "started_at_utc": started, "completed_at_utc": _utc(),
        "tls_verification": "system trust store; no bypass",
        "session_cookies_persisted": False,
        "parser_schema_version": PARSER_SCHEMA,
        "scope": {"city_label": CITY_LABEL, "city_value": CITY_VALUE,
                  "evaluation_result_filter": "不拘", "name_filter": "",
                  "declared_total": total, "declared_pages": pages,
                  "institutions_parsed": len(listing),
                  "institutions_without_record": no_record,
                  "evaluation_records": len(rows),
                  "request_delay_seconds": DELAY_SECONDS},
        "requests": requests,
        "outputs": {
            "parsed/evaluation_results.json": {
                "sha256": _sha(parsed.read_bytes()),
                "byte_length": parsed.stat().st_size, "record_count": len(rows)},
            OUT_CSV.as_posix(): {
                "sha256": _sha(OUT_CSV.read_bytes()),
                "byte_length": OUT_CSV.stat().st_size, "record_count": len(rows)},
            LISTING_CSV.as_posix(): {
                "sha256": _sha(LISTING_CSV.read_bytes()),
                "byte_length": LISTING_CSV.stat().st_size,
                "record_count": len(listing)},
        },
    }
    (snap / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{len(rows)} 筆 → {OUT_CSV}")
    print(f"原始回應 {len(requests)} 份 → {raw}")


def rebuild(snapshot_id: str) -> None:
    """Re-derive the CSV from pinned bytes, verifying every hash. No network."""
    snap = ROOT / snapshot_id
    manifest = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
    rows: list[dict] = []
    listing: list[dict] = []
    for req in manifest["requests"]:
        if req["role"] not in {"city_search", "page_next"}:
            continue
        body = (snap / req["raw_file"]).read_bytes()
        if _sha(body) != req["response_sha256"]:
            raise SystemExit(f"{req['raw_file']} 的 sha256 與 manifest 不符")
        page = req["public_query"]["page"]
        rows.extend(_rows_from(body, snapshot_id, page))
        listing.extend(_listing_from(body, snapshot_id, page))
    declared = manifest["scope"]["declared_total"]
    if declared and len(listing) != declared:
        raise SystemExit(f"重建得到 {len(listing)} 園，manifest 宣告 {declared} 園")
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["title"]] = counts.get(r["title"], 0) + 1
    for r in listing:
        r["record_count"] = counts.get(r["title"], 0)
    with OUT_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    with LISTING_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LISTING_OUT)
        w.writeheader()
        w.writerows(listing)
    no_record = sum(1 for r in listing if not r["record_count"])
    print(f"從 {len(manifest['requests'])} 份 pinned 回應重建："
          f"{len(listing)} 園（{no_record} 園未見評鑑紀錄）／{len(rows)} 筆紀錄，雜湊全部相符")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--snapshot-id")
    g.add_argument("--rebuild-from")
    ap.add_argument("--timeout", type=int, default=60)
    a = ap.parse_args()
    if a.rebuild_from:
        rebuild(a.rebuild_from)
    else:
        crawl(a.snapshot_id, a.timeout)


if __name__ == "__main__":
    main()
