"""抓公共化園（公立＋非營利）的**逐項**收費明細，供財報交叉分析使用。

Outputs
    data/processed/fee_detail_public.csv    一列 = 園×學年度×年齡組×學期×班別×收費項目
    data/processed/fee_basis_public.csv     一列 = 園×學年度（逐項攤平成欄，含申報狀態）
    data/processed/fee_detail_public.manifest.json   來源、抓取時間、產物 sha256

**為什麼不直接用 fee_summary_ntpc.csv。** 那張表只留「全日班／上學期」的
`全學期總收費` 一個數字，因為它服務的是軌 A 的收費漲幅特徵。但要跟財報對帳，
一個總數不夠：

* 公校決算的 `學雜費收入` 只含 **學費＋雜費**；材料費／活動費／午餐費／點心費
  走代收代辦，不進基金來源。拿 16,675 的總收費去除決算的學雜費收入，
  推估人數會系統性偏低約六成。
* 非營利財報的 `教保費收入` 是家長繳費與政府差額補助**合併**入帳
  （附註二(五)：「收入總額（家長繳交之費用；其有政府差額補助費者，應合併計算）」），
  所以家長端的收費只能當**下界**，不能當等值。
* 「餐點費，不得移作他用」（附註二(十)3.(2)）要對的是午餐費＋點心費這兩項，
  不是總收費。

所以這裡保留逐項，讓每個交叉檢核自己挑對應的項目。

**未申報是事實，不是空值。** 一所園某學年度沒有 slip，可能是真的沒申報
（幼照法第 38 條收費未報備查），也可能是鏡像當年沒收錄。兩者都記成
`slip_status`，不寫成空白讓下游誤讀為零。

Run:  python run.py fee-detail-public        （需網路，約 1-2 分鐘）
"""

from __future__ import annotations

import concurrent.futures
import csv
import dataclasses
import datetime as dt
import hashlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.scrape.fees import (
    ADDON_ITEMS,
    CORE_ITEMS,
    MIRROR,
    TOTAL_ITEM,
    FeeRow,
    fetch_slip,
    parse_slip,
)

CITY = "新北市"
YEARS = (109, 110, 111, 112, 113, 114)
PUBLIC_TYPES = ("公立", "非營利")

MASTER = pathlib.Path("data/processed/institutions_ntpc.csv")
OUT = pathlib.Path("data/processed")
DETAIL = OUT / "fee_detail_public.csv"
BASIS = OUT / "fee_basis_public.csv"
MANIFEST = OUT / "fee_detail_public.manifest.json"

# 決算／財報要對帳的是「一個幼兒讀一個學年度」的金額，而 slip 是逐學期申報。
# 兩學期並非總是都申報（下學期常缺），所以年額一律由**已申報的學期取平均後 ×2**
# 推得，並把用到幾個學期記在 `n_semesters` 上，讓下游知道這個年額有多穩。
SEMESTERS_PER_YEAR = 2

# 公校決算「學雜費收入」的對應項目。
TUITION_ITEMS = ("學費", "雜費")
# 附註二(十)3.(2)「餐點費，不得移作他用」對應的家長收費項目。
MEAL_ITEMS = ("午餐費", "點心費")
# 代收代辦：不進公校基金來源，也不是非營利的教保費。
COLLECTED_ITEMS = ("材料費", "活動費", "午餐費", "點心費")


def _targets() -> list[tuple[str, str]]:
    """(title, type) for every 公共化園 in the master table."""
    with MASTER.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    seen: dict[str, str] = {}
    for r in rows:
        if r["type"] in PUBLIC_TYPES:
            seen.setdefault(r["title"], r["type"])
    return sorted(seen.items())


def _fetch_all(titles: list[str]) -> tuple[list[FeeRow], dict[tuple[str, int], str]]:
    """Fetch every (title, year) slip. Returns rows plus a status per pair."""
    tasks = [(t, y) for t in titles for y in YEARS]
    rows: list[FeeRow] = []
    status: dict[tuple[str, int], str] = {}
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(fetch_slip, CITY, title, year): (title, year)
            for title, year in tasks
        }
        for fut in concurrent.futures.as_completed(futures):
            title, year = futures[fut]
            payload = fut.result()
            done += 1
            if done % 250 == 0:
                print(f"    … {done}/{len(tasks)}", flush=True)
            if payload is None:
                status[(title, year)] = "未申報或鏡像未收錄"
                continue
            parsed = parse_slip(title, year, payload)
            status[(title, year)] = "已申報" if parsed else "有檔但無收費列"
            rows.extend(parsed)
    return rows, status


def _basis(
    rows: list[FeeRow], status: dict[tuple[str, int], str], types: dict[str, str]
) -> list[dict]:
    """Per 園×學年度: the per-child amounts each cross-check needs.

    Age groups are averaged, not summed: a 園 files one slip per 年齡組 and a child
    belongs to exactly one, so summing them would multiply the per-child amount by
    the number of age brackets the 園 happens to run.
    """
    # (title, year, item) -> list of per-semester subtotals, 全日班 only.
    bucket: dict[tuple[str, int, str], list[float]] = {}
    sem_seen: dict[tuple[str, int], set[str]] = {}
    ages: dict[tuple[str, int], set[str]] = {}
    classes: dict[tuple[str, int], set[str]] = {}

    for r in rows:
        key2 = (r.title, r.year)
        classes.setdefault(key2, set()).add(r.class_type)
        if r.class_type != "全日班" or r.subtotal is None:
            continue
        sem_seen.setdefault(key2, set()).add(r.semester)
        ages.setdefault(key2, set()).add(r.age_group)
        bucket.setdefault((r.title, r.year, r.item), []).append(float(r.subtotal))

    out: list[dict] = []
    for (title, year), st in sorted(status.items()):
        key2 = (title, year)
        n_sem = len(sem_seen.get(key2, ()))

        # 迴圈變數以預設引數綁進來：閉包版本會讓每個 per_year 都看到最後一輪的
        # title/year（ruff B023），於是整張表都填成最後一所園的金額。
        def per_year(item: str, _t=title, _y=year, _n=n_sem) -> float | None:
            """One child's whole-學年度 charge for ``item``, or None if not charged."""
            vals = bucket.get((_t, _y, item))
            if not vals or not _n:
                return None
            # mean over (age group × semester) filings, then scale to a full year
            return round(sum(vals) / len(vals) * SEMESTERS_PER_YEAR, 2)

        def group(items: tuple[str, ...], _py=per_year) -> float | None:
            parts = [_py(i) for i in items]
            got = [p for p in parts if p is not None]
            return round(sum(got), 2) if got else None

        row = {
            "title": title,
            "type": types.get(title, ""),
            "academic_year": year,
            "slip_status": st,
            "n_semesters_filed": n_sem,
            "n_age_groups": len(ages.get(key2, ())),
            "class_types": "|".join(sorted(classes.get(key2, ()))),
        }
        for item in (*CORE_ITEMS, *ADDON_ITEMS, TOTAL_ITEM):
            row[f"y_{item}"] = per_year(item)
        row["y_學雜費"] = group(TUITION_ITEMS)
        row["y_餐點費"] = group(MEAL_ITEMS)
        row["y_代收代辦"] = group(COLLECTED_ITEMS)
        row["y_核心合計"] = group(CORE_ITEMS)
        out.append(row)
    return out


def _sha(path: pathlib.Path) -> dict:
    data = path.read_bytes()
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "byte_length": len(data),
    }


def main() -> None:
    if not MASTER.exists():
        sys.exit(f"{MASTER} 不存在；先跑 python run.py institution-master")
    targets = _targets()
    types = dict(targets)
    titles = [t for t, _ in targets]
    print(f"公共化園 {len(titles)} 所"
          f"（公立 {sum(1 for v in types.values() if v == '公立')}、"
          f"非營利 {sum(1 for v in types.values() if v == '非營利')}）"
          f" × 學年度 {YEARS[0]}–{YEARS[-1]}，共 {len(titles) * len(YEARS)} 檔", flush=True)

    rows, status = _fetch_all(titles)
    filed = sum(1 for v in status.values() if v == "已申報")
    print(f"取得 {len(rows):,} 筆收費列；{filed}/{len(status)} 個園年有申報")

    OUT.mkdir(parents=True, exist_ok=True)
    fields = [f.name for f in dataclasses.fields(FeeRow)]
    with DETAIL.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["type", *fields])
        w.writeheader()
        for r in sorted(rows, key=lambda r: (r.title, r.year, r.age_group,
                                             r.semester, r.class_type, r.item)):
            w.writerow({"type": types.get(r.title, ""), **dataclasses.asdict(r)})
    print(f"wrote {DETAIL} ({len(rows):,} 列)")

    basis = _basis(rows, status, types)
    with BASIS.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(basis[0].keys()))
        w.writeheader()
        w.writerows(basis)
    print(f"wrote {BASIS} ({len(basis)} 園年)")

    MANIFEST.write_text(json.dumps({
        "source": f"{MIRROR}/data/slip{{year}}/{CITY}/{{title}}.json",
        "source_note": "kiang/ap.ece.moe.edu.tw 鏡像（程式 MIT／資料 CC-BY）"
                       "；官方端點有保存期限，見 docs/research/03-external-data.md §1.2",
        "retrieved_on": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "city": CITY,
        "years": list(YEARS),
        "institution_types": list(PUBLIC_TYPES),
        "institutions": len(titles),
        "pairs_requested": len(status),
        "pairs_filed": filed,
        "absence_semantics": "slip_status 為「未申報或鏡像未收錄」時，不得讀成收費為零，"
                             "也不得單獨據以認定違反幼照法第 38 條。",
        "version_controlled": {
            str(BASIS): True,
            str(DETAIL): False,
            str(MANIFEST): True,
        },
        "version_control_note": "逐項明細 13 MB、可重抓，依 .gitignore 不進版控；"
                                "此處仍記其 sha256，重抓後可比對是否與本次一致。"
                                "上游是鏡像而非釘住快照，內容可能隨上游更新而變動，"
                                "sha 不符先看 retrieved_on。",
        "outputs": {str(p): _sha(p) for p in (DETAIL, BASIS)},
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {MANIFEST}")

    # --- 申報覆蓋率與收費一致性 ------------------------------------
    print("\n=== 申報覆蓋率（依學年度）===")
    print(f"{'學年度':<8}{'公立已申報':>12}{'非營利已申報':>14}")
    for year in YEARS:
        pub = [t for t, ty in types.items() if ty == "公立"]
        npo = [t for t, ty in types.items() if ty == "非營利"]
        p = sum(1 for t in pub if status.get((t, year)) == "已申報")
        n = sum(1 for t in npo if status.get((t, year)) == "已申報")
        print(f"{year:<8}{p:>7}/{len(pub):<4}{n:>9}/{len(npo):<4}")

    print("\n=== 全學期總收費 vs 逐項合計（申報表自身的 checksum）===")
    bad = [
        b for b in basis
        if b["y_全學期總收費"] and b["y_核心合計"]
        and abs(b["y_全學期總收費"] - b["y_核心合計"]) >= 1
    ]
    ok = [b for b in basis if b["y_全學期總收費"] and b["y_核心合計"]]
    print(f"可比對 {len(ok)} 園年，其中 {len(bad)} 組不一致")
    for b in sorted(bad, key=lambda b: -abs(b["y_全學期總收費"] - b["y_核心合計"]))[:10]:
        print(f"  {b['title'][:28]:<30}{b['academic_year']}  "
              f"宣告 {b['y_全學期總收費']:>10,.0f}  逐項 {b['y_核心合計']:>10,.0f}  "
              f"差 {b['y_全學期總收費'] - b['y_核心合計']:>+9,.0f}")


if __name__ == "__main__":
    main()
