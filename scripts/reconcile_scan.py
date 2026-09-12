"""向供應商查回中斷任務的實際金額，把預留結算掉。

    PYTHONPATH=src .venv/bin/python scripts/reconcile_scan.py

當機或重啟會讓一次執行留在「已預留、未結算」。那筆錢是真的花掉了，所以預留
不會被釋放——但它以**上界**佔用額度，比實付高。這支腳本用落地的 ``run_id``
向 Apify 問回 ``usageTotalUsd``，把上界換成實付，額度才不會被虛佔。

只結算已到終局的執行。還在跑的那筆金額仍在變大，結算它會把帳記少。
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog import config
from smart_watchdog.realtime import ledger
from smart_watchdog.realtime.apify import TERMINAL, ApifyThreadsChannel
from smart_watchdog.realtime.jobs import STORE


def main() -> None:
    ch = ApifyThreadsChannel(token=config.get("APIFY_TOKEN"))
    settled = pending = 0

    for row in STORE.list(limit=200):
        job = STORE.read(row["job_id"])
        if not job:
            continue
        dirty = False
        for r in job.get("reservations", []):
            ref = r.get("provider_ref") or job.get("apify_run_id", "")
            if r.get("settled") or not ref:
                continue
            info = ch.poll_run(ref)
            status = info.get("status")
            if status not in TERMINAL:
                pending += 1
                print(f"  {ref} 仍在 {status}，金額還在變大——不結算")
                continue
            usd = float(info.get("usage_usd") or 0.0)
            res = ledger.settle(r["reservation_id"], usd, provider_ref=ref)
            r.update({"settled": True, "actual_usd": usd})
            dirty = True
            settled += 1
            drift = ("　⚠️ 實付超過預估，價目表低估了" if res["rate_card_drift"]
                     else "　（回傳筆數少於要求，屬正常）" if res["under_ran"]
                     else "")
            print(f"  {ref} {status}　預估 US${res['predicted_usd']} → "
                  f"實付 US${res['actual_usd']}（差 {res['drift_usd']}）{drift}")
        if dirty:
            job["usd_actual"] = round(sum(
                float(r["actual_usd"]) for r in job["reservations"]
                if r.get("actual_usd") is not None), 4)
            STORE.write(job)

    b = ledger.budget()
    print(f"\n結算 {settled} 筆，{pending} 筆仍在執行中")
    print(f"本週期已花 US${b.month_spent_usd} / US${4.0}，"
          f"剩 US${b.month_remaining_usd}，未結算 {b.unsettled} 筆")
    prov = ch.provider_usage().get("monthly_usage_usd")
    if prov is not None:
        print(f"供應商端本週期用量 US${prov:.4f}（權威值；含本主控台以外的執行）")


if __name__ == "__main__":
    main()
