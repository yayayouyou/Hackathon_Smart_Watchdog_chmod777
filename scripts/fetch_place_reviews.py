"""逐園抓 Google 地圖的評分與評論數，只留彙總。

## 為什麼只留彙總

評論全文是第三方著作，與 `data/external/news/` 的立場一致：**保存 provenance，
不重製內文、不對外轉載**。所以這支腳本寫出去的每一列只有評分、評論數、抽樣
評論裡低星的則數、最近一則的日期——沒有任何一個字的評論內容。這也正好是
地圖圖層需要的全部。

## 為什麼要先抓齊才談圖層

`place_ids_ntpc.csv` 裡原本只有 34 園有評分（那是先前逐一開卷宗時順手取到的）。
在那 34 園上量到的是：評論數 ≥ 中位的事後受罰率 **0.75x**——比基準還低，
與「評論越多越該派工」的方向相反。但 n=34 太小，不能據此否定，也不能據此
建圖層。抓齊之後才有辦法真的回答。

⚠️ 已經量過並記錄在 `api/social.py::reviews_of` 的是**評分**：裁罰 ≥5 件的園
評分中位 4.20、無裁罰者 4.60（p=0.061，不顯著），個案完全不具鑑別力——
16 件裁罰的幼苗國際 4.7 星、因虐童停招的吉尼爾 4.4 星。**評分不是風險訊號。**
這支抓的是評論**數**與低星則數，是不同的量，所以值得單獨測。

## 成本

Places 每月前 1,000 次免費（本機計數，非 Google 帳單）。每次呼叫都走
`api/social.reviews_of`，計費與額度閘門沿用那一條，不另寫第二份——
金額與免費額度的計算在 `realtime/pricing.places_meter`。

Usage
    PYTHONPATH=src .venv/Scripts/python scripts/fetch_place_reviews.py --dry-run
    PYTHONPATH=src .venv/Scripts/python scripts/fetch_place_reviews.py --limit 200
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pandas as pd

from smart_watchdog.realtime import ledger, pricing

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLACE_IDS = ROOT / "data/processed/place_ids_ntpc.csv"
OUT = ROOT / "data/processed/place_reviews_ntpc.csv"

#: 幾星以下算「低星」。3 星是分水嶺：1–2 星是抱怨，4–5 星是滿意。
LOW_STAR = 2

FIELDS = ["id", "title", "type", "town", "place_id", "rating", "review_count",
          "sampled", "n_low", "latest_review", "fetched_at", "status"]


def existing() -> dict[str, dict]:
    """已經抓過的不重抓。刪掉輸出檔就會全部重來（並且重新計費）。"""
    if not OUT.exists():
        return {}
    with OUT.open(encoding="utf-8", newline="") as fh:
        return {r["id"]: r for r in csv.DictReader(fh)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0, help="這一輪最多抓幾園（0＝不限）")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()
    out_path = pathlib.Path(a.out)

    pid = pd.read_csv(PLACE_IDS)
    have = existing()
    todo = [r for r in pid.itertuples(index=False)
            if isinstance(r.place_id, str) and r.place_id
            and r.id not in have]
    if a.limit:
        todo = todo[:a.limit]

    b = ledger.budget()
    meter = pricing.places_meter(len(todo), used_this_month=b.places_used_this_month)
    print(f"place_id 可用 {int(pid['place_id'].notna().sum())} 園　"
          f"已抓 {len(have)}　這一輪 {len(todo)}")
    print(f"本月免費已用 {b.places_used_this_month}／1000　"
          f"剩餘免費 {meter.free_remaining}")
    print(f"估價 US${meter.usd_max}　{meter.formula}")
    print(f"預估 {meter.est_seconds:.0f} 秒")
    if a.dry_run or not todo:
        print("（--dry-run，未呼叫）" if a.dry_run else "沒有待抓的。")
        return

    from smart_watchdog.api import social

    rows: list[dict] = []
    ok = fail = 0
    t0 = time.time()
    for i, r in enumerate(todo, 1):
        try:
            # ⚠️ reviews_of 吃的是 payload 的**短 id**（registry UUID 的前 8 碼），
            # 不是完整 UUID。傳完整的會拿到 404「查無機構」，而且是靜默失敗——
            # 第一版 959 筆全失敗、免費計數紋風不動，因為請求根本沒送出去。
            res = social.reviews_of(str(r.id)[:8])
        except Exception as exc:  # noqa: BLE001 - 單園失敗不該中斷整輪
            res = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
        if not res.get("available"):
            fail += 1
            rows.append({"id": r.id, "title": r.title, "type": r.type,
                         "town": r.town, "place_id": r.place_id,
                         "rating": "", "review_count": "", "sampled": 0,
                         "n_low": "", "latest_review": "",
                         "fetched_at": dt.date.today().isoformat(),
                         "status": str(res.get("reason", "unavailable"))[:80]})
            # 額度用盡就停，不要用剩下的 900 次去撞同一道牆。
            if "額度" in str(res.get("reason", "")):
                print(f"  額度閘門關閉於第 {i} 筆：{res.get('reason')}")
                break
            continue
        ok += 1
        revs = res.get("reviews") or []
        # ⚠️ 只取彙總。評論全文是第三方著作，不落地、不進版控。
        stars = [rv.get("rating") for rv in revs if rv.get("rating") is not None]
        dates = [rv.get("published", "") for rv in revs if rv.get("published")]
        rows.append({
            "id": r.id, "title": r.title, "type": r.type, "town": r.town,
            "place_id": r.place_id,
            "rating": res.get("rating") if res.get("rating") is not None else "",
            "review_count": res.get("review_count")
            if res.get("review_count") is not None else "",
            "sampled": len(revs),
            "n_low": sum(1 for s in stars if s <= LOW_STAR),
            "latest_review": max(dates) if dates else "",
            "fetched_at": dt.date.today().isoformat(), "status": "ok",
        })
        if i % 50 == 0:
            print(f"  {i}/{len(todo)}　成功 {ok}　失敗 {fail}　"
                  f"{time.time() - t0:.0f}s")

    merged = list(have.values()) + rows
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for row in merged:
            w.writerow({k: row.get(k, "") for k in FIELDS})

    b2 = ledger.budget()
    print(f"\n成功 {ok}　失敗 {fail}　累計 {len(merged)} 園")
    print(f"本月免費已用 {b2.places_used_this_month}／1000　"
          f"本機帳本本月 US${b2.month_spent_usd}")
    print(f"寫出 {out_path}（只有彙總，沒有評論內文）")


if __name__ == "__main__":
    main()
