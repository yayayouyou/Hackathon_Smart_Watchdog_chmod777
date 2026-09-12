"""拿決算／財報去對收費明細與園所基本資料——單看一份文件查不到的東西。

Outputs
    data/processed/cross_findings.csv   一列 = （園, 年度, 規則）的跨來源查核問題
    data/processed/cross_metrics.csv    一列 = 園-年度的跨來源衍生量
                                        （推估在園人數、滿園率、每生政府投入、
                                          隱含收費年額……決算書本身沒有的量）

規則與門檻的依據、已驗證的恆等式、以及基準率，全部寫在
``src/smart_watchdog/features/crosscheck.py`` 的模組 docstring。

缺可選輸入時降級不中斷（house style）：沒有 ``fee_basis_public.csv`` 就讓所有
需要收費明細的檢核停在「資料不足」，並印一行說明怎麼補。

未通過的檢核是**要問的問題**，不是違法認定。

Run:  python run.py crosscheck
"""

from __future__ import annotations

import csv
import json
import pathlib
import re
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.features.crosscheck import (
    check_nonprofit,
    check_public,
    cohort_findings,
    nonprofit_guards,
    nonprofit_metrics,
    parse_fee_split,
    public_guards,
    public_metrics,
)

EXTRACT_DIR = pathlib.Path("data/extracted/nonprofit")
PAGEWISE_FACTS = pathlib.Path("data/processed/nonprofit_pagewise_facts.csv")
PUBLIC_CSV = pathlib.Path("data/extracted/public_kindergartens.csv")
CROSSWALK = pathlib.Path("data/processed/nonprofit_registry_crosswalk.csv")
MASTER = pathlib.Path("data/processed/institutions_ntpc.csv")
FEE_BASIS = pathlib.Path("data/processed/fee_basis_public.csv")
EXTERNAL_MANIFEST = pathlib.Path("data/external/manifest.json")

FINDINGS = pathlib.Path("data/processed/cross_findings.csv")
METRICS = pathlib.Path("data/processed/cross_metrics.csv")

FILENAME_RE = re.compile(r"^(N\d\d)_(.+?)_(\d{3})$")

#: 非營利園跨來源檢核用到的同儕數列。
NONPROFIT_PEER_COLS = ("implied_fee_per_child", "parent_share")
#: 公立園跨來源檢核用到的同儕數列。
PUBLIC_PEER_COLS = ("occupancy", "gov_per_child")


def _rows(path: pathlib.Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _num(v: object) -> float | None:
    text = str(v if v is not None else "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _income_line(payload: dict, label: str) -> float | None:
    lines = (payload.get("income_statement") or {}).get("lines") or []
    for ln in lines:
        if "".join(str(ln.get("label", "")).split()) == label:
            return _num(ln.get("actual"))
    return None


def _snapshot_date() -> str:
    if not EXTERNAL_MANIFEST.exists():
        return ""
    try:
        man = json.loads(EXTERNAL_MANIFEST.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    entries = man.get("files", man) if isinstance(man, dict) else {}
    for key, value in (entries or {}).items():
        if "preschools" in str(key) and isinstance(value, dict):
            return str(value.get("retrieved_on", ""))
    return ""


def _fee_tables() -> tuple[dict[tuple[str, int], float], dict[tuple[str, int], float],
                           dict[str, tuple[int, int]]]:
    """(非營利年額 by (title, 學年度), 公立學雜費半年額, 非營利申報覆蓋率)."""
    npo: dict[tuple[str, int], float] = {}
    pub_half: dict[tuple[str, int], float] = {}
    coverage: dict[str, list[int]] = {}
    if not FEE_BASIS.exists():
        return npo, pub_half, {}
    for r in _rows(FEE_BASIS):
        year = int(r["academic_year"])
        filed = r["slip_status"] == "已申報"
        if r["type"] == "非營利":
            c = coverage.setdefault(str(year), [0, 0])
            c[1] += 1
            if filed:
                c[0] += 1
            total = _num(r.get("y_全學期總收費"))
            if filed and total:
                npo[(r["title"], year)] = total
        elif r["type"] == "公立":
            tuition = _num(r.get("y_學雜費"))
            if filed and tuition:
                pub_half[(r["title"], year)] = tuition / 2
    return npo, pub_half, {y: (a, b) for y, (a, b) in coverage.items()}


def _peers(
    metrics: list[dict], cols: tuple[str, ...], eligible_key: str | None = None
) -> dict[str, dict[str, list[float]]]:
    """{year: {col: [values]}}, 缺失保持缺失（不補零）。

    ``eligible_key`` 指向一個布林欄位，False 的列不進同儕池但仍會被檢核——
    部分年度的報告要被比較的對象是全年度的同儕，自己不該當別人的基準。
    """
    out: dict[str, dict[str, list[float]]] = {}
    for m in metrics:
        bucket = out.setdefault(m["year"], {c: [] for c in cols})
        if eligible_key is not None and not m.get(eligible_key, True):
            continue
        for c in cols:
            v = m.get(c)
            if v is not None:
                bucket[c].append(float(v))
    return out


def build_nonprofit(
    fee_npo: dict[tuple[str, int], float],
) -> tuple[list[dict], list[dict], list[str]]:
    files = sorted(EXTRACT_DIR.glob("*.json"))
    if not files:
        sys.exit(f"{EXTRACT_DIR} 裡沒有抽取結果")

    caps: dict[tuple[str, int], list[float]] = {}
    titles: dict[tuple[str, int], list[str]] = {}
    if CROSSWALK.exists():
        for r in _rows(CROSSWALK):
            key = (r["code"], int(r["academic_year"]))
            caps[key] = [float(x) for x in json.loads(r["registry_capacities"] or "[]")]
            titles[key] = json.loads(r["registry_titles"] or "[]")

    # 登記主檔的全園室內面積與每月收費。前者當核定人數落差的旁證，
    # 後者是家長月均實繳的比較基準。
    area: dict[str, float] = {}
    monthly: dict[str, float] = {}
    if MASTER.exists():
        for r in _rows(MASTER):
            v = _num(r.get("size_in"))
            if v is not None:
                area[r["title"]] = v
            mv = _num(r.get("monthly"))
            if mv is not None:
                monthly[r["title"]] = mv

    # 附註三的教保費收入家長／政府拆分。可選輸入：沒有頁級事實就讓相關檢核
    # 停在「資料不足」，不中斷（house style，同 run_compliance_checks.py）。
    fee_split: dict[tuple[str, str], dict] = {}
    if PAGEWISE_FACTS.exists():
        fee_split = parse_fee_split(_rows(PAGEWISE_FACTS))

    metrics: list[dict] = []
    for path in files:
        m = FILENAME_RE.match(path.stem)
        if not m:
            continue
        code, short, year = m.group(1), m.group(2), int(m.group(3))
        payload = json.loads(path.read_text(encoding="utf-8"))
        n1 = payload.get("note_1") or {}
        inc = payload.get("income_statement") or {}
        key = (code, year)

        fee = next((fee_npo[(t, year)] for t in titles.get(key, [])
                    if (t, year) in fee_npo), None)
        indoor = next((area[t] for t in titles.get(key, []) if t in area), None)
        reg_monthly = next((monthly[t] for t in titles.get(key, [])
                            if t in monthly), None)
        metrics.append(nonprofit_metrics(
            {
                "code": code, "short_name": short, "academic_year": year,
                "tuition_income": _income_line(payload, "教保費收入"),
                "material_cost": _income_line(payload, "材料費"),
                "approved_capacity": n1.get("approved_capacity"),
                "actual_enrolment": n1.get("actual_enrolment"),
                "educators": n1.get("educators"),
                "income_period": inc.get("period"),
                "registry_indoor_area": indoor,
                "registry_monthly": reg_monthly,
                "fee_split": fee_split.get((code, str(year))),
            },
            fee_year_amount=fee,
            registry_capacities=caps.get(key, []),
        ))

    peers = _peers(metrics, NONPROFIT_PEER_COLS, eligible_key="full_year")
    checks = [c.as_dict() for m in metrics
              for c in check_nonprofit(m, peers.get(m["year"], {}))]
    return metrics, checks, nonprofit_guards(metrics)


def build_public(
    pub_half: dict[tuple[str, int], float],
) -> tuple[list[dict], list[dict], list[str]]:
    if not PUBLIC_CSV.exists():
        return [], [], []
    capacity: dict[str, float] = {}
    branches: Counter = Counter()
    if MASTER.exists():
        for r in _rows(MASTER):
            if r["type"] != "公立":
                continue
            cap = _num(r.get("count_approved"))
            if cap is None:
                continue
            capacity[r["parent"]] = capacity.get(r["parent"], 0.0) + cap
            branches[r["parent"]] += 1

    stamp = _snapshot_date()
    metrics = [
        public_metrics(r, pub_half, capacity, dict(branches), stamp)
        for r in _rows(PUBLIC_CSV)
        if _num(r.get("tuition_actual")) is not None
    ]
    peers = _peers(metrics, PUBLIC_PEER_COLS)
    checks = [c.as_dict() for m in metrics
              for c in check_public(m, peers.get(m["year"], {}))]
    return metrics, checks, public_guards(metrics)


def _write_metrics(nonprofit: list[dict], public: list[dict]) -> None:
    fields: list[str] = []
    for m in (*nonprofit, *public):
        for k in m:
            if k not in fields:
                fields.append(k)
    METRICS.parent.mkdir(parents=True, exist_ok=True)
    with METRICS.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for m in (*nonprofit, *public):
            w.writerow({k: m.get(k) for k in fields})
    print(f"wrote {METRICS}  ({len(nonprofit)} 非營利園年 + {len(public)} 公立園年)")


def _state(passed: object) -> str:
    return "資料不足" if passed is None else ("通過" if passed else "未通過")


def main() -> None:
    if not FEE_BASIS.exists():
        print(f"（{FEE_BASIS} 不存在，所有需要收費明細的檢核將停在「資料不足」；"
              f"跑 python run.py fee-detail-public 可解開）\n")
    if not PAGEWISE_FACTS.exists():
        print(f"（{PAGEWISE_FACTS.name} 不存在，附註三的家長／政府拆分無法抽取，"
              f"兩條檢核將停在「資料不足」；跑 python run.py pagewise-facts 可解開）\n")
    fee_npo, pub_half, coverage = _fee_tables()

    np_metrics, np_checks, np_guards = build_nonprofit(fee_npo)
    pub_metrics, pub_checks, pub_guard_problems = build_public(pub_half)
    cohort = [c.as_dict() for c in cohort_findings(np_metrics, pub_metrics, coverage)]

    rows = [*cohort, *np_checks, *pub_checks]
    FINDINGS.parent.mkdir(parents=True, exist_ok=True)
    with FINDINGS.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    units = len({(r["entity_type"], r["code"], r["academic_year"]) for r in rows})
    print(f"wrote {FINDINGS}  ({len(rows)} 項檢核，涵蓋 {units} 個園-年度）")
    _write_metrics(np_metrics, pub_metrics)

    # ── 護欄：從未失敗，失敗代表管線壞了而非園有問題 ──────────────────
    print("\n=== 對帳護欄（失敗代表抽取或 join 有誤，不是發現）===")
    guards = [("非營利：實際招收未超過核定（財報與登記兩側）", np_guards),
              ("公立：預算隱含人數未超過核定人數", pub_guard_problems)]
    for label, problems in guards:
        mark = "✓" if not problems else "✗"
        print(f"  {mark} {label}"
              + ("" if not problems else f"  {len(problems)} 筆例外"))
        for p in problems[:5]:
            print(f"      {p}")

    # ── 收費明細覆蓋率：這個功能的前提本身 ───────────────────────────
    if coverage:
        print("\n=== 非營利園收費明細覆蓋率（交叉分析的前提）===")
        for year in sorted(coverage):
            filed, total = coverage[year]
            bar = "█" * round(20 * filed / total) if total else ""
            print(f"  {year} 學年度  {filed:>3}/{total:<3}  {bar}")
        matched = sum(1 for m in np_metrics if m["filed_fee_per_child"] is not None)
        print(f"  → {len(np_metrics)} 份財報中 {matched} 份有**逐項**家長收費可交叉"
              f"（{matched / len(np_metrics) * 100:.1f}%）")
        split = sum(1 for m in np_metrics if m["parent_share"] is not None)
        print(f"  → 但附註三「收支明細表 1. 教保費收入」在 {split} 份"
              f"（{split / len(np_metrics) * 100:.1f}%）列出家長繳費／政府差額補助，"
              f"四個學年度都可用")
        print("\n=== 家長分攤比率（附註三，僅完整學年度）===")
        buckets: dict[str, list[float]] = {}
        for m in np_metrics:
            if m["parent_share"] is not None and m["full_year"]:
                buckets.setdefault(m["year"], []).append(m["parent_share"])
        print(f"  {'學年度':<8}{'n':>4}{'家長分攤中位':>14}{'每生家長':>11}{'每生政府':>11}")
        for year in sorted(buckets):
            same = [m for m in np_metrics
                    if m["year"] == year and m["parent_share"] is not None
                    and m["full_year"]]
            pc = [m["parent_per_child"] for m in same if m["parent_per_child"]]
            gc = [m["gov_subsidy_per_child"] for m in same
                  if m["gov_subsidy_per_child"]]
            import statistics
            print(f"  {year:<8}{len(same):>4}"
                  f"{statistics.median(buckets[year]):>14.3f}"
                  f"{statistics.median(pc):>11,.0f}{statistics.median(gc):>11,.0f}")

    # ── 分規則統計 ───────────────────────────────────────────────────
    print(f"\n{'規則':<30}{'類型':<6}{'通過':>6}{'未通過':>8}{'資料不足':>10}")
    by_rule: dict[tuple[str, str], Counter] = {}
    for r in rows:
        by_rule.setdefault((r["rule"], r["entity_type"]), Counter())[
            _state(r["passed"])] += 1
    for (rule, etype), c in by_rule.items():
        print(f"{rule:<30}{etype:<6}{c['通過']:>6}{c['未通過']:>8}{c['資料不足']:>10}")

    # ── 未通過清單 ───────────────────────────────────────────────────
    fails = [r for r in rows if r["passed"] is False]
    order = {"high": 0, "medium": 1, "low": 2}
    print(f"\n=== 未通過 {len(fails)} 項（依嚴重度）===")
    for r in sorted(fails, key=lambda r: (order[r["severity"]], r["rule"])):
        head = (f"{r['entity_type']} {r['short_name']} "
                f"{r['academic_year']} {r['year_kind']}")
        print(f"\n[{r['severity']}] {head}")
        print(f"  規則：{r['rule']}")
        print(f"  來源：{r['sources']}")
        print(f"  實況：{r['detail']}")

    print("\n⚠️ 本表每一項都是**建議查核**的問題，不是違法認定。"
          "\n   標「資料不足」的項目不得讀成通過；公共化園的收費明細自 111 學年度起"
          "\n   在公開資料中不可得，那是涵蓋範圍的限制，不是該園的合規證明。")


if __name__ == "__main__":
    main()
