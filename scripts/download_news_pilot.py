"""Acquire public news mentions and raise alert candidates for human review.

    PYTHONPATH=src .venv/bin/python scripts/download_news_pilot.py \
        --snapshot-id 2026-09-03-news-v1

Outputs data/processed/alert_candidates_ntpc.csv. Every row is a *candidate*
awaiting a person's disposition, never a score and never a label -- the terms
docs/research/06-realtime-event-monitoring-plan.md sets for unverified public
content.

Topic queries rather than one query per 園: 1,213 name searches would be a lot of
traffic to answer a question a dozen topic feeds already answer, and attribution
happens locally against the institution master afterwards. The queries are the
regulatory categories the penalty data shows actually recur.

Raw feeds are pinned with their sha256 before parsing, same as every other
external source here, so a candidate can always be traced to the bytes that
produced it.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import pathlib
import sys
import time

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.features.alerts import article_kind, attribute
from smart_watchdog.scrape import news

ROOT = pathlib.Path("data/external/news")
OUT = pathlib.Path("data/processed/alert_candidates_ntpc.csv")
DELAY = 2.0

QUERIES = {
    "不當對待": '"幼兒園" 新北 (不當對待 OR 虐童 OR 體罰)',
    "裁罰處分": '"幼兒園" 新北 (裁罰 OR 開罰 OR 處分)',
    "停辦廢止": '"幼兒園" 新北 (停辦 OR 廢止 OR 勒令)',
    "收費爭議": '"幼兒園" 新北 (超收 OR 退費 OR 收費爭議)',
    "安全事故": '"幼兒園" 新北 (受傷 OR 食安 OR 娃娃車)',
    "教保人力": '"幼兒園" 新北 (師生比 OR 超收人數 OR 人力不足)',
}

FIELDS = [
    "snapshot_id", "query", "published", "publisher", "headline", "link",
    "article_kind", "institution_id", "institution_title", "matched_name",
    "attribution_basis", "town_corroborated", "anonymised", "status",
]


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot-id", required=True)
    ap.add_argument("--timeout", type=int, default=30)
    a = ap.parse_args()

    snap = ROOT / a.snapshot_id
    raw = snap / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    inst_df = pd.read_csv("data/processed/institutions_ntpc.csv")
    institutions = inst_df[["id", "title", "town"]].to_dict("records")

    requests, rows = [], []
    seen_links: set[str] = set()
    for i, (label, query) in enumerate(QUERIES.items(), start=1):
        if i > 1:
            time.sleep(DELAY)
        body = news.fetch(query, timeout=a.timeout)
        path = raw / f"{i:03d}_{label}.xml"
        path.write_bytes(body)
        requests.append({
            "sequence": i, "label": label, "query": query,
            "url": news.build_url(query), "response_sha256": _sha(body),
            "byte_length": len(body),
            "retrieved_at_utc": dt.datetime.now(dt.timezone.utc)
                .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "raw_file": path.relative_to(snap).as_posix(),
        })
        items = news.parse_items(body)
        print(f"  [{label}] {len(items)} 則")
        for item in items:
            if item.link in seen_links:
                continue
            seen_links.add(item.link)
            att = attribute(item.title, institutions,
                            is_anonymised=item.is_anonymised)
            title = ""
            if att.institution_id:
                match = inst_df.loc[inst_df["id"] == att.institution_id, "title"]
                title = str(match.iloc[0]) if len(match) else ""
            rows.append({
                "snapshot_id": a.snapshot_id, "query": label,
                "published": item.published, "publisher": item.publisher,
                "headline": item.title, "link": item.link,
                "article_kind": article_kind(item.title),
                "institution_id": att.institution_id or "",
                "institution_title": title,
                "matched_name": att.matched_name,
                "attribution_basis": att.basis,
                "town_corroborated": int(att.corroborated_by_town),
                "anonymised": int(item.is_anonymised),
                # No automatic disposition: a person decides what an item means.
                "status": "待人工研判",
            })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    manifest = {
        "manifest_version": 1, "snapshot_id": a.snapshot_id,
        "source": "Google News RSS（公開來源，無登入、無個資）",
        "tls_verification": "system trust store; no bypass",
        "policy": (
            "未經查證之公開內容不改寫歷史風險分數、不作違規標籤、不公開指控機構；"
            "僅產生待人工研判之警示候選。刻意匿名之報導不予歸屬。"
        ),
        "queries": QUERIES, "requests": requests,
        "outputs": {str(OUT): {"sha256": _sha(OUT.read_bytes()),
                               "record_count": len(rows)}},
    }
    (snap / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    named = [r for r in rows if r["institution_id"]]
    anon = [r for r in rows if r["anonymised"]]
    incident = [r for r in rows if r["article_kind"] == "incident"]
    print(f"\n{len(rows)} 則（去重後）→ {OUT}")
    print(f"  可歸屬到特定機構      {len(named)}")
    print(f"  刻意匿名，不予歸屬    {len(anon)}")
    print(f"  無可辨識名稱          {len(rows) - len(named) - len(anon)}")
    print(f"  標題屬事件類          {len(incident)}")
    if named:
        print("\n可歸屬者：")
        for r in named:
            print(f"  [{r['published']}] {r['institution_title'][:24]}"
                  f"　{r['headline'][:44]}")


if __name__ == "__main__":
    main()
