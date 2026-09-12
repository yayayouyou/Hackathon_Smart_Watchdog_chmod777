"""把機構主檔對應到 Google place_id，並記錄評分與評論數。

    PYTHONPATH=src .venv/bin/python scripts/resolve_place_ids.py --limit 40
    PYTHONPATH=src .venv/bin/python scripts/resolve_place_ids.py          # 全量

輸出 data/processed/place_ids_ntpc.csv。place_id 解析是一次性成本，之後查評論
直接用 id，不必再搜尋。已解析過的會跳過，可分批跑、可中斷續跑。

歸屬用的是與新聞、PTT 相同的嚴格規則：Google 回傳的名稱必須包含機構的可辨識
名稱才採用。Google 的名稱常帶品牌前綴（「邦仁國際文教機構吉尼爾幼兒園」），
與登記全名不同，因此比對的是識別核心而非全名；核心對不上就記為未解析，
不用「距離最近」之類的方式硬湊——那是把一所園的評論掛到另一所園的做法。
"""

from __future__ import annotations

import argparse
import csv
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog import config
from smart_watchdog.features.alerts import distinctive_name

OUT = pathlib.Path("data/processed/place_ids_ntpc.csv")
SEARCH = "https://places.googleapis.com/v1/places:searchText"
# 大量解析只取識別欄位：rating／userRatingCount 會把 Text Search 推到較貴的
# SKU，而評分本來就該在開卷宗時即時取（也才是最新的）。--with-rating 供抽樣
# 分析使用。
MASK_MIN = "places.id,places.displayName,places.formattedAddress"
MASK_FULL = MASK_MIN + ",places.rating,places.userRatingCount"
DELAY = 0.25
FIELDS = ["id", "title", "type", "town", "place_id", "google_name",
          "google_address", "rating", "review_count", "match_basis"]


def _search(key: str, query: str, mask: str = MASK_MIN) -> list[dict]:
    req = urllib.request.Request(
        SEARCH, data=json.dumps({"textQuery": query,
                                 "languageCode": "zh-TW"}).encode(),
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": key,
                 "X-Goog-FieldMask": mask})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return (json.loads(r.read(500_000)) or {}).get("places") or []
    except urllib.error.HTTPError as e:
        body = json.loads(e.read(20_000) or b"{}")
        msg = (body.get("error") or {}).get("message", "")
        raise SystemExit(f"HTTP {e.code}：{msg[:160]}") from e


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0, help="只處理前 N 筆未解析者")
    ap.add_argument("--sample", action="store_true",
                    help="抽樣：裁罰最多的 20 所 + 無裁罰的 20 所")
    ap.add_argument("--with-rating", action="store_true",
                    help="一併取評分與評論數（較貴的 SKU，抽樣分析用）")
    a = ap.parse_args()

    key = config.get("GOOGLE_MAPS_API_KEY")
    if not key:
        sys.exit("未設定 GOOGLE_MAPS_API_KEY")

    inst = pd.read_csv("data/processed/institutions_ntpc.csv")
    done: dict[str, dict] = {}
    if OUT.exists():
        with OUT.open(encoding="utf-8") as fh:
            done = {r["id"]: r for r in csv.DictReader(fh)}
        print(f"已解析 {len(done)} 筆，將跳過")

    todo = inst[~inst["id"].isin(done)]
    if a.sample:
        pri = pd.read_csv("data/processed/audit_priority_ntpc.csv")
        merged = todo.merge(pri[["id", "n_penalties_prior"]], on="id", how="left")
        top = merged.nlargest(20, "n_penalties_prior")
        zero = merged[merged["n_penalties_prior"].fillna(0) == 0].head(20)
        todo = pd.concat([top, zero])
    if a.limit:
        todo = todo.head(a.limit)

    print(f"待解析 {len(todo)} 筆")
    rows, hits = [], 0
    for i, r in enumerate(todo.itertuples(index=False), start=1):
        if i > 1:
            time.sleep(DELAY)
        core = distinctive_name(r.title)
        places = _search(key, f"{r.title} {r.town}",
                         MASK_FULL if a.with_rating else MASK_MIN)
        chosen, basis = None, "查無相符名稱"
        for p in places:
            name = (p.get("displayName") or {}).get("text", "")
            if core and core in name:
                chosen, basis = p, "Google 名稱含可辨識名稱"
                break
        if chosen is None and places:
            basis = f"Google 回傳 {len(places)} 筆但名稱皆不含「{core}」"
        if chosen:
            hits += 1
        rows.append({
            "id": r.id, "title": r.title, "type": r.type, "town": r.town,
            "place_id": chosen.get("id", "") if chosen else "",
            "google_name": ((chosen.get("displayName") or {}).get("text", "")
                            if chosen else ""),
            "google_address": chosen.get("formattedAddress", "") if chosen else "",
            "rating": chosen.get("rating", "") if chosen else "",
            "review_count": chosen.get("userRatingCount", "") if chosen else "",
            "match_basis": basis,
        })
        if i % 25 == 0:
            print(f"  {i}/{len(todo)}　命中 {hits}")

    merged_rows = list(done.values()) + rows
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(merged_rows)

    rated = [r for r in merged_rows if r.get("rating")]
    print(f"\n本次解析 {len(rows)} 筆，命中 {hits}"
          f"（{hits / max(len(rows), 1) * 100:.0f}%）")
    print(f"累計 {len(merged_rows)} 筆，其中有評分 {len(rated)} 筆 → {OUT}")


if __name__ == "__main__":
    main()
