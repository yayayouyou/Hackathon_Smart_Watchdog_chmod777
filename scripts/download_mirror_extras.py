"""抓上游鏡像的逐園裁罰檔，釘成一份帶 manifest 的快照。

    python run.py mirror-extras                 # 抓取並寫入 data/external/mirror/
    python run.py mirror-extras -- --verify-only # 不連外，只驗現有快照的 sha256
    python run.py mirror-extras -- --report      # 不連外，印出這份資料能回答什麼

為什麼獨立一份 manifest 而不寫進 `data/external/manifest.json`：那一份記錄的是
`preschools.json`／`punish_all.json`／`kids_vehicles.json` 三個**已公布數字所依據**
的快照。動它就等於動 AUC 0.640、1,386 筆裁罰這些對外講過的數字。這份逐園裁罰是
新增的旁證資料，不參與那些計算，所以自己一份 manifest，互不干擾。

⚠️ 這份資料**不接進模型特徵**，理由見 `smart_watchdog/scrape/mirror.py` 的模組說明。

授權：原始資料來源為全國教保資訊網（教育部），資料 CC-BY；
取得管道為 kiang/ap.ece.moe.edu.tw 鏡像（程式碼 MIT）。見 data/external/README.md。
"""

from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.console import use_utf8
from smart_watchdog.scrape import mirror, registry

use_utf8()

CITY = "新北市"
OUT_DIR = mirror.DEFAULT_DIR
SNAPSHOT = mirror.DEFAULT_PENALTIES
MANIFEST = OUT_DIR / "manifest.json"
INSTITUTIONS_CSV = pathlib.Path("data/processed/institutions_ntpc.csv")

#: 我們釘住的 punish_all.json 的最新裁罰日。晚於它的就是快照凍結後新增的。
#: 不寫死成常數會讓「前瞻驗證」變成一句沒有基準的宣稱。
PINNED_MAX_DATE = "2026/08/05"


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_snapshot(raw: dict, retrieved_on: str) -> dict:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "source_url": f"{mirror.MIRROR}/data/punish/{CITY}/",
        "city": CITY,
        "retrieved_on": retrieved_on,
        "attribution": {
            "original_source": "https://ap.ece.moe.edu.tw/webecems/pubSearch.aspx",
            "original_source_name": "全國教保資訊網（教育部）",
            "obtained_via": "https://github.com/kiang/ap.ece.moe.edu.tw",
            "licence": "資料 CC-BY（須標註原始來源）；鏡像程式碼 MIT",
        },
        "schools": raw,
    }
    SNAPSHOT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    entry = {
        "source_url": payload["source_url"],
        "retrieved_on": retrieved_on,
        "byte_length": SNAPSHOT.stat().st_size,
        "sha256": sha256(SNAPSHOT),
        "schema_version": "punish-by-school-v1",
        "school_count": len(raw),
        "record_count": sum(len(v) for v in raw.values()),
    }
    MANIFEST.write_text(
        json.dumps({"manifest_version": 1, "files": {SNAPSHOT.name: entry}},
                   ensure_ascii=False, indent=1),
        encoding="utf-8")
    return entry


def verify() -> int:
    if not MANIFEST.exists() or not SNAPSHOT.exists():
        print("✗ 找不到快照或 manifest。先跑：python run.py mirror-extras")
        return 2
    want = json.loads(MANIFEST.read_text(encoding="utf-8"))["files"][SNAPSHOT.name]
    got_sha, got_len = sha256(SNAPSHOT), SNAPSHOT.stat().st_size
    ok = got_sha == want["sha256"] and got_len == want["byte_length"]
    mark = "✓" if ok else "✗"
    print(f"  {mark} {SNAPSHOT.name}  {got_len:,} bytes  sha256 {got_sha[:16]}…")
    if not ok:
        print(f"    manifest 記的是 {want['byte_length']:,} bytes / "
              f"{want['sha256'][:16]}…　檔案與 manifest 不符")
    by_school = mirror.load_penalties_by_school(SNAPSHOT)
    print(f"  {len(by_school)} 園　{sum(len(v) for v in by_school.values())} 列")
    return 0 if ok else 1


def report() -> int:
    """印出這份資料能回答、而 punish_all 回答不了的東西。不連外。"""
    if not SNAPSHOT.exists():
        print("✗ 尚無快照。先跑：python run.py mirror-extras")
        return 2
    by_school = mirror.load_penalties_by_school(SNAPSHOT)
    rows = [r for v in by_school.values() for r in v]
    docs = mirror.documents(by_school)
    print(f"逐園裁罰快照：{len(by_school)} 園　{len(rows)} 條款列　"
          f"{len(docs)} 張處分書\n")

    print("── 1. 條款列數 ≠ 被抓次數")
    diff = [(s, len(v), len({mirror.event_key(s, r['doc_no']) for r in v}))
            for s, v in by_school.items()]
    worse = sorted((d for d in diff if d[1] != d[2]), key=lambda d: d[2] - d[1])
    print(f"  {len(worse)} 園的條款列數多於處分書件數。差距最大的幾家：")
    for s, n_rows, n_docs in worse[:5]:
        print(f"    {s[:30]:32s} {n_rows:3d} 列 → {n_docs:3d} 件")

    print("\n── 2. 可辨識的重複條款列（同文號同法源且處分內容逐字相同）")
    dupes = mirror.duplicate_rows(by_school)
    # 用 registry.fine_amount() 而不是自己把字串裡的數字串起來：處分內容有
    # 「停止招生：自115年1月1日起至115年6月30日止」這種帶日期的寫法，
    # 串數字會得到 34,366,980,090,070,371 元這種數字（實際印出來過）。
    money = sum(registry.fine_amount(r["punishment"]) or 0 for r in dupes)
    print(f"  {len(dupes)} 列　涉 {len({r['school'] for r in dupes})} 園　"
          f"重複計入的罰鍰 {money:,} 元")

    print(f"\n── 3. 快照凍結後（>{PINNED_MAX_DATE}）的新裁罰＝零洩漏前瞻驗證集")
    fresh = mirror.new_since(by_school, PINNED_MAX_DATE)
    print(f"  {len(fresh)} 列　涉 {len({r['school'] for r in fresh})} 園")
    ranks = _ranks_for(fresh)
    if ranks:
        ranks.sort()
        n = len(ranks)
        med = ranks[n // 2]
        top100 = sum(1 for r in ranks if r <= 100)
        top300 = sum(1 for r in ranks if r <= 300)
        print(f"  對照已發布的稽查優先序：中位名次 {med}/1213")
        print(f"    前 100 名命中 {top100}／{n}　"
              f"（隨機抽查期望 {n * 100 / 1213:.1f}，lift {top100 / (n * 100 / 1213):.2f}x）")
        print(f"    前 300 名命中 {top300}／{n}　"
              f"（隨機抽查期望 {n * 300 / 1213:.1f}，lift {top300 / (n * 300 / 1213):.2f}x）")
        print("  ⚠️ 這是**旁證**不是重新量測：樣本只有幾十列，且未分層。"
              "\n     對外要講的效能數字仍是 run.py baseline 的時序切分結果。")
    else:
        print("  （找不到 data/processed/audit_priority_ntpc.csv，跳過名次對照）")

    print("\n── 4. 已從 punish_all 消失的退場園")
    known = _known_titles()
    if known:
        gone = {s: v for s, v in by_school.items() if s not in known}
        n = sum(len(v) for v in gone.values())
        print(f"  {len(gone)} 個檔名對不上現行主檔　{n} 列")
        for s, v in list(gone.items())[:6]:
            kinds = collections.Counter(
                "廢止設立許可" if "廢止" in str(r["punishment"]) else
                "停止招生" if "停止招生" in str(r["punishment"]) else "罰鍰"
                for r in v)
            print(f"    {s[:30]:32s} {len(v):2d} 列  {dict(kinds)}")
    return 0


def _known_titles() -> set:
    # 比對的是**完整機構主檔** 1,213 筆，不是 penalties_ntpc.csv——
    # 後者只含有裁罰的園，拿它當「現行名冊」會把首次受罰的園誤判成退場。
    if not INSTITUTIONS_CSV.exists():
        return set()
    with INSTITUTIONS_CSV.open(encoding="utf-8") as fh:
        return {r["title"] for r in csv.DictReader(fh)}


def _ranks_for(rows: list) -> list:
    path = pathlib.Path("data/processed/audit_priority_ntpc.csv")
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        rank = {r["title"]: int(r["priority_rank_overall"])
                for r in csv.DictReader(fh) if r.get("priority_rank_overall")}
    return [rank[s] for s in {r["school"] for r in rows} if s in rank]


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verify-only", action="store_true",
                    help="不連外，只比對現有快照與 manifest 的 sha256")
    ap.add_argument("--report", action="store_true",
                    help="不連外，印出這份資料能回答什麼")
    ap.add_argument("--retrieved-on", default="",
                    help="快照日期 YYYY-MM-DD（預設由呼叫端給，不自動取系統時間）")
    a = ap.parse_args()

    if a.verify_only:
        sys.exit(verify())
    if a.report:
        sys.exit(report())

    if not a.retrieved_on:
        sys.exit("需要 --retrieved-on YYYY-MM-DD：快照日期要能被人核對，"
                 "不從系統時鐘偷取（跑在時區不同的機器上會記錯日期）。")

    print(f"抓取 {CITY} 逐園裁罰檔…")
    raw = mirror.fetch_penalties_by_school(CITY)
    entry = write_snapshot(raw, a.retrieved_on)
    print(f"  {entry['school_count']} 園　{entry['record_count']} 列")
    print(f"  → {SNAPSHOT}　{entry['byte_length']:,} bytes")
    print(f"  → {MANIFEST}")
    print("\n接著：python run.py mirror-extras -- --report")


if __name__ == "__main__":
    main()
