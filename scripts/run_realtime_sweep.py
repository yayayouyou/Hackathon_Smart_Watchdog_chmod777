"""Sweep the live channels once and record what they say about named 園.

    PYTHONPATH=src .venv/bin/python scripts/run_realtime_sweep.py

Output: data/processed/realtime_mentions_ntpc.csv, plus a stamp of when the
sweep ran so the console can say how fresh the panel is.

This is the shape the daily job would take: a handful of topic queries per
channel, then strict local attribution against the 1,213-institution master.
Asking each channel about each 園 individually would be hundreds of requests to
answer the same question, and most 園 are never mentioned at all.

Every row is a candidate awaiting a person's judgement. Unverified public content
does not rewrite historical risk, does not become a violation label, and does not
enter the score -- the terms docs/research/06-realtime-event-monitoring-plan.md
sets and §20-22 of the results doc measures.
"""

from __future__ import annotations

import csv
import json
import pathlib
import sys

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.realtime.sources import LIVE, default_channels

OUT = pathlib.Path("data/processed/realtime_mentions_ntpc.csv")
STAMP = pathlib.Path("data/processed/realtime_sweep.json")
FIELDS = ["channel", "institution_id", "institution_title", "headline", "url",
          "published", "publisher", "attribution_basis", "kind", "status"]


def _kind(headline: str) -> str:
    from smart_watchdog.features.alerts import article_kind

    if headline.startswith("[complaint]"):
        return "complaint"
    if headline.startswith("[question]"):
        return "question"
    return article_kind(headline)


def main() -> None:
    inst = pd.read_csv("data/processed/institutions_ntpc.csv")
    institutions = inst[["id", "title", "town"]].to_dict("records")
    by_id = dict(zip(inst["id"], inst["title"]))

    channels = default_channels()
    rows: list[dict] = []
    per_channel: dict[str, int] = {}
    errors: list[dict] = []

    for ch in channels:
        if ch.status != LIVE:
            continue
        try:
            mentions = ch.sweep(institutions)
        except Exception as exc:  # noqa: BLE001 - report, never abort the sweep
            errors.append({"channel": ch.key, "error": f"{type(exc).__name__}: {exc}"})
            continue
        per_channel[ch.key] = len(mentions)
        rows.extend({
            "channel": m.channel, "institution_id": m.institution_id,
            "institution_title": by_id.get(m.institution_id, ""),
            "headline": m.headline, "url": m.url, "published": m.published,
            "publisher": m.publisher, "attribution_basis": m.attribution_basis,
            "kind": _kind(m.headline),
            # No automatic disposition: a person decides what a mention means.
            "status": "待人工研判",
        } for m in mentions)

    rows.sort(key=lambda r: r["published"], reverse=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    live = [c for c in channels if c.status == LIVE]
    STAMP.write_text(json.dumps({
        "swept_at": pd.Timestamp.today().strftime("%Y-%m-%d %H:%M"),
        "channels_live": len(live), "channels_total": len(channels),
        "channels": [c.describe() for c in channels],
        "per_channel": per_channel, "mentions": len(rows),
        "institutions_mentioned": len({r["institution_id"] for r in rows}),
        "errors": errors,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"管道 {len(live)}/{len(channels)} 已啟用")
    for k, n in per_channel.items():
        print(f"  {k:<14} 可歸屬 {n} 則")
    print(f"\n{len(rows)} 則 → {OUT}")
    print(f"涉及 {len({r['institution_id'] for r in rows})} 所園")
    if errors:
        print("錯誤:", errors)
    for r in rows[:8]:
        print(f"  [{r['published']}] {r['kind']:<9} {r['institution_title'][:20]:<20}"
              f" {r['headline'][:38]}")


if __name__ == "__main__":
    main()
