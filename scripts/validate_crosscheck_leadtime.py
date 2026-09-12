"""交叉比對的發現，有沒有領先後續裁罰？時序切分，不挑個案。

Output: data/processed/crosscheck_leadtime.csv -- 一列 = （園, 學年度／年度, 是否被點到,
        觀察起日, 之後是否受罰, 領先天數）

**為什麼需要這支腳本。** 「某園被交叉比對點到，後來真的被罰了」這種句子，靠翻名單
一定找得到——27 所裡有 6 所有裁罰紀錄，隨便挑一所都能講成故事。那是
`CLAUDE.md`「先算基準率」要防的事，而這個專案已經在別的訊號上踩過三次
（`monthly`、收費漲幅、以及 docs/research/05-phase1-results.md §12 記載的第三次）。

要能說「有預警作用」，需要的是三件事同時成立：

1. **裁罰發生在發現「可被看到」之後**，不是之前。財報是回顧性文件：113 學年度的
   報告在 115 年初才簽證公告，拿它去「預測」114 年的裁罰是看著答案填空。
2. **被點到的園受罰率高於沒被點到的園**，且差距不是抽樣雜訊。
3. **在沒有前科的園裡也成立**。否則訊號只是「以前被罰過的園比較容易再被罰」的
   換皮，那件事 `features/build.penalty_history` 已經在做了。

三件缺一件，結論就只能寫成「尚無法證實」。

## 觀察起日怎麼定

學年度 N 的財報涵蓋 N/8/1–(N+1)/7/31，經會計師簽證後公告。取
**(N+2) 年 1 月 1 日**（民國）當可觀察起日，即期間結束後約 5 個月，偏保守。
    學年度 110 → 2023-01-01　學年度 113 → 2026-01-01
公校決算書用年度（曆年），決算書於次年度審定公告，取 **(N+1) 年 7 月 1 日**。
    112 年度 → 2024-07-01　114 年度 → 2026-07-01

## 標籤

`penalties_ntpc.csv` 裡 `date` 落在 [觀察起日, 資料截止) 的任何一筆裁罰。
資料截止取裁罰檔裡的最大日期——用「今天」會把還沒發生的空窗算成「沒被罰」，
把最近的世代系統性標成負樣本。

Run:  python run.py validate-crosscheck
"""

from __future__ import annotations

import collections
import csv
import datetime as dt
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

FINDINGS = pathlib.Path("data/processed/cross_findings.csv")
PENALTIES = pathlib.Path("data/processed/penalties_ntpc.csv")
CROSSWALK = pathlib.Path("data/processed/nonprofit_registry_crosswalk.csv")
MASTER = pathlib.Path("data/processed/institutions_ntpc.csv")
PUBLIC = pathlib.Path("data/extracted/public_kindergartens.csv")
OUT = pathlib.Path("data/processed/crosscheck_leadtime.csv")

ROC_OFFSET = 1911


def _rows(path: pathlib.Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _date(text: object) -> dt.date | None:
    try:
        return dt.datetime.strptime(str(text), "%Y/%m/%d").date()
    except (TypeError, ValueError):
        return None


def observable_from(year: int, kind: str) -> dt.date:
    """發現可被外界看到的最早日期。"""
    if kind == "學年度":
        return dt.date(year + ROC_OFFSET + 2, 1, 1)
    return dt.date(year + ROC_OFFSET + 1, 7, 1)


def fisher(a: int, b: int, c: int, d: int) -> float | None:
    """2x2 單尾 Fisher exact（被點到組受罰率是否較高）。無 scipy 時回 None。"""
    try:
        from scipy.stats import fisher_exact
    except ImportError:
        return None
    return float(fisher_exact([[a, b], [c, d]], alternative="greater")[1])


def main() -> None:
    for path in (FINDINGS, PENALTIES, MASTER):
        if not path.exists():
            sys.exit(f"{path} 不存在；先跑 python run.py crosscheck")

    findings = _rows(FINDINGS)
    pens = _rows(PENALTIES)
    inst = _rows(MASTER)

    data_end = max(d for d in (_date(p["date"]) for p in pens) if d)
    print(f"裁罰資料截止日：{data_end}（用它當標籤窗右界，不用今天）")

    # ── 園 → registry id ────────────────────────────────────────────
    # 非營利走 crosswalk 的 operator_matched_ids，與 risk/priority 同一套：
    # 法人更替時不可把發現掛到沒承辦的法人頭上。
    np_ids: dict[str, set] = collections.defaultdict(set)
    if CROSSWALK.exists():
        for r in _rows(CROSSWALK):
            matched = json.loads(r.get("operator_matched_ids") or "[]")
            ids = matched or json.loads(r.get("registry_ids") or "[]")
            np_ids[r["short_name"]] |= set(ids)
    pub_ids: dict[str, set] = collections.defaultdict(set)
    for r in inst:
        if r["type"] == "公立":
            pub_ids[r["parent"]].add(r["id"])

    pen_by_id: dict[str, list] = collections.defaultdict(list)
    for p in pens:
        d = _date(p["date"])
        if d:
            pen_by_id[p["id"]].append((d, p))

    # ── 母體：每個「有被檢核過」的園-年度 ────────────────────────────
    # 通過與資料不足也算被檢核過。只看未通過的那些，會把「沒被點到」的對照組
    # 整個弄丟，於是算不出基準率——這正是本腳本要防的錯。
    checked: dict[tuple, dict] = {}
    for f in findings:
        if f["code"] == "COHORT":
            continue
        key = (f["entity_type"], f["short_name"], f["academic_year"])
        rec = checked.setdefault(key, {"kind": f["year_kind"], "fail": [], "n": 0})
        rec["n"] += 1
        if f["passed"] == "False":
            rec["fail"].append(f["rule"])

    rows: list[dict] = []
    for (etype, name, year_s), rec in sorted(checked.items()):
        ids = np_ids.get(name, set()) if etype == "非營利" else pub_ids.get(name, set())
        if not ids:
            continue
        obs = observable_from(int(year_s), rec["kind"])
        if obs > data_end:
            continue  # 標籤窗還沒打開，不可用
        hist = [(d, p) for i in ids for d, p in pen_by_id.get(i, [])]
        prior = [d for d, _ in hist if d < obs]
        after = sorted(d for d, _ in hist if obs <= d <= data_end)
        rows.append({
            "entity_type": etype, "short_name": name,
            "year": year_s, "year_kind": rec["kind"],
            "flagged": int(bool(rec["fail"])),
            "n_failed": len(rec["fail"]),
            "top_rules": "；".join(sorted(set(rec["fail"]))),
            "observable_from": obs.isoformat(),
            "label_end": data_end.isoformat(),
            "n_prior_penalties": len(prior),
            "has_prior": int(bool(prior)),
            "penalised_after": int(bool(after)),
            "n_penalties_after": len(after),
            "first_penalty_after": after[0].isoformat() if after else "",
            "lead_days": (after[0] - obs).days if after else "",
        })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {OUT}  ({len(rows)} 個可用的園-年度)\n")

    # ── 主表：被點到 vs 沒被點到，之後受罰率 ────────────────────────
    def table(subset: list[dict], label: str) -> None:
        fl = [r for r in subset if r["flagged"]]
        nf = [r for r in subset if not r["flagged"]]
        a = sum(r["penalised_after"] for r in fl)
        b = len(fl) - a
        c = sum(r["penalised_after"] for r in nf)
        d = len(nf) - c
        if not fl or not nf:
            print(f"  {label}: 樣本不足（被點到 {len(fl)}、未點到 {len(nf)}）")
            return
        pf, pn = a / len(fl), c / len(nf)
        p = fisher(a, b, c, d)
        lift = (pf / pn) if pn else float("inf")
        print(f"  {label}")
        print(f"    被點到  {a:>3}/{len(fl):<3} = {pf:>6.1%}"
              f"　未點到  {c:>3}/{len(nf):<3} = {pn:>6.1%}"
              f"　提升 {lift:.2f}×"
              + (f"　Fisher 單尾 p={p:.3f}" if p is not None else "　（無 scipy）"))

    print("=== 觀察起日之後是否受罰（時序切分）===")
    table(rows, "全部")
    for etype in ("非營利", "公立"):
        table([r for r in rows if r["entity_type"] == etype], etype)
    print()
    print("=== 冷啟動子群：觀察起日前沒有裁罰紀錄的園-年度 ===")
    cold = [r for r in rows if not r["has_prior"]]
    table(cold, "無前科")
    print()
    print("=== 逐年度（避免把不同長度的標籤窗混在一起）===")
    for year in sorted({r["year"] for r in rows}):
        table([r for r in rows if r["year"] == year], f"{year}")

    # ── 逐規則：哪一條規則的發現真的領先裁罰 ─────────────────────────
    # 這是接下來該投資哪條規則的依據。單一規則的樣本更小，所以只列計數，
    # 不做檢定——對 9 個園年做 11 次檢定，一定會有一條看起來顯著。
    print("\n=== 逐規則命中（樣本極小，只列計數，不做檢定）===")
    per_rule: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        for rule in (r["top_rules"].split("；") if r["top_rules"] else []):
            per_rule[rule].append(r)
    base = [r for r in rows if not r["flagged"]]
    base_rate = (sum(r["penalised_after"] for r in base) / len(base)) if base else 0
    print(f"  （對照：未被任何規則點到的 {len(base)} 個園年，"
          f"事後受罰率 {base_rate:.1%}）")
    print(f"  {'規則':<28}{'園年':>5}{'事後受罰':>8}{'受罰率':>9}{'中位領先天數':>13}")
    for rule, rs in sorted(per_rule.items(), key=lambda kv: -len(
            [r for r in kv[1] if r["penalised_after"]])):
        hit = [r for r in rs if r["penalised_after"]]
        leads = sorted(int(r["lead_days"]) for r in hit)
        med = leads[len(leads) // 2] if leads else None
        print(f"  {rule:<28}{len(rs):>5}{len(hit):>8}"
              f"{len(hit) / len(rs):>9.1%}"
              f"{('—' if med is None else str(med)):>13}")

    hits = [r for r in rows if r["flagged"] and r["penalised_after"]]
    print(f"\n=== 被點到且事後受罰 {len(hits)} 例 ===")
    for r in sorted(hits, key=lambda r: r["lead_days"]):
        print(f"  {r['entity_type']} {r['short_name']:<12}{r['year']} "
              f"{r['year_kind']}　可觀察 {r['observable_from']}"
              f" → 首次裁罰 {r['first_penalty_after']}"
              f"（領先 {r['lead_days']} 天，前科 {r['n_prior_penalties']} 筆）")
        print(f"      規則：{r['top_rules']}")

    print("\n⚠️ 三個限制，缺一個結論就不能寫成「已證實有預警作用」：")
    print("   1. **門檻是在這批資料上定的。** 基準率取自同一個 corpus，"
          "所以本測試對門檻選擇而言是 in-sample。")
    print("   2. **查的不是同一件事。** 公共化園 63 筆裁罰中收費類"
          "（第43／38條）為 0，主要是第33條不當對待（42 筆）；"
          "交叉比對查財務與人數對帳。相關不代表因果。")
    print("   3. **樣本撐不起 p<0.05。** 母體 38 非營利園 + 22 市立園，"
          "近兩個世代的標籤窗只有 7–19 個月。")
    print("   → 正確的下一步是**前瞻測試**：把規則凍結在今天，"
          "只用今天之後開出的裁罰驗證。見 docs/research/09-crosscheck-leadtime.md §6。")


if __name__ == "__main__":
    main()
