"""Check the anomaly ranking three ways, none of which is an AUC.

The panel has 10 positive compliance outcomes. Reporting an AUC on that would put
a number with a confidence interval wider than its range in front of judges, which
``docs/FINDINGS.md`` §3.3 already criticises the project for nearly doing once.
So the checks here are the ones the sample size can actually support:

**Top-K hit rate.** Of the K 園-年 this ranks highest, how many turn out to have a
compliance finding, against the base rate? This is the operational question -- an
inspector works down a list -- and it needs no model fit. It is reported with the
hypergeometric probability of doing at least that well by chance, because with 15
positives in 132 rows a "2x lift" in the top 10 is entirely ordinary.

**Ranking stability under feature removal.** Drop each feature in turn, rescore,
and correlate the rank orders. A ranking that reshuffles when one of seventeen
ratios is removed is reporting that ratio, not the 園.

**Leave-one-kindergarten-out.** Remove one 園 from the peer cohort and rescore the
others. Because every score is relative to a median and a MAD computed from ~30
peers, a single extreme 園 can move everyone else's score. If it does, the ranking
is partly an artefact of who happens to be in the cohort.

The compliance findings enter only here, after the ranking exists. They were not
available to the scorer, which is the only thing that makes this a check.

Usage
    PYTHONPATH=src .venv/Scripts/python scripts/validate_nonprofit_anomaly.py
"""

from __future__ import annotations

import argparse
import collections
import csv
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from scipy import stats

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from build_nonprofit_anomaly import FEATURES, build_rows

from smart_watchdog.console import use_utf8
from smart_watchdog.features.anomaly import score_cohort

FINDINGS = pathlib.Path("data/processed/compliance_findings.csv")


def load_labels() -> dict[tuple, bool]:
    """(code, year) -> did any compliance rule fail? The external check only."""
    labels: dict[tuple, bool] = {}
    with FINDINGS.open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            k = (r["code"], str(r["academic_year"]))
            labels.setdefault(k, False)
            if str(r["passed"]).lower() in ("false", "0"):
                labels[k] = True
    return labels


def rank_all(rows: list[dict], features: dict[str, str]) -> dict[tuple, float]:
    """Score every 園-年 within its own 學年度 cohort."""
    by_year: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        by_year[r["academic_year"]].append(r)
    out: dict[tuple, float] = {}
    for year, group in by_year.items():
        for s in score_cohort(group, features):
            if s.score is not None:
                out[(s.code, year)] = s.score
    return out


def spearman_of_ranks(a: dict[tuple, float], b: dict[tuple, float]) -> float | None:
    keys = sorted(set(a) & set(b))
    if len(keys) < 5:
        return None
    return float(stats.spearmanr([a[k] for k in keys], [b[k] for k in keys]).statistic)


def main() -> None:
    ap = argparse.ArgumentParser(description="異常排序的三項驗證")
    ap.add_argument("--ks", default="5,10,20", help="Top-K 的 K，逗號分隔")
    a = ap.parse_args()
    use_utf8()

    rows = build_rows()
    labels = load_labels()
    base = rank_all(rows, FEATURES)
    scored_keys = [k for k in base if k in labels]
    pos = sum(1 for k in scored_keys if labels[k])
    n = len(scored_keys)

    print("=== 1. Top-K 命中率（外部驗證，法遵發現未參與評分）===")
    print(f"可比對 {n} 個園-學年度，其中有法遵發現 {pos} 個"
          f"（基準率 {pos / n:.1%}）")
    order = sorted(scored_keys, key=lambda k: -base[k])
    print(f"\n{'K':>4}{'命中':>6}{'命中率':>9}{'相對基準':>9}{'P(≥命中|隨機)':>14}")
    for k in (int(x) for x in a.ks.split(",")):
        if k > n:
            continue
        hit = sum(1 for key in order[:k] if labels[key])
        rate = hit / k
        lift = rate / (pos / n) if pos else float("nan")
        # Hypergeometric: probability a random K-subset contains >= hit positives.
        p = float(stats.hypergeom.sf(hit - 1, n, pos, k)) if hit else 1.0
        print(f"{k:>4}{hit:>6}{rate:>9.1%}{lift:>9.2f}x{p:>14.3f}")

    print("\n=== 2. 排序穩定性（逐一移除單一特徵）===")
    cors = []
    for drop in FEATURES:
        subset = {k: v for k, v in FEATURES.items() if k != drop}
        rho = spearman_of_ranks(base, rank_all(rows, subset))
        if rho is not None:
            cors.append((rho, drop))
    cors.sort()
    if cors:
        vals = [c for c, _ in cors]
        print(f"移除任一特徵後與原排序的 Spearman ρ："
              f"中位 {vals[len(vals) // 2]:.3f}　最低 {vals[0]:.3f}")
        print("  影響最大的三個特徵（移除後 ρ 最低）：")
        for rho, name in cors[:3]:
            print(f"    {FEATURES[name]:<22}ρ={rho:.3f}")

    print("\n=== 3. Leave-one-kindergarten-out（同儕組成的敏感度）===")
    codes = sorted({r["code"] for r in rows})
    worst: list[tuple] = []
    shifts: list[float] = []
    for code in codes:
        kept = [r for r in rows if r["code"] != code]
        re_scored = rank_all(kept, FEATURES)
        common = sorted(set(re_scored) & set(base))
        if len(common) < 5:
            continue
        base_rank = {k: i for i, k in enumerate(
            sorted(common, key=lambda x: -base[x]), start=1)}
        new_rank = {k: i for i, k in enumerate(
            sorted(common, key=lambda x: -re_scored[x]), start=1)}
        moves = [abs(base_rank[k] - new_rank[k]) for k in common]
        mx = max(moves)
        shifts.append(sum(moves) / len(moves))
        worst.append((mx, code))
    if shifts:
        worst.sort(reverse=True)
        print(f"移除任一園後，其餘園-年的平均名次變動："
              f"中位 {sorted(shifts)[len(shifts) // 2]:.2f} 名")
        print("  移除後造成最大單筆名次變動的三所園：")
        for mx, code in worst[:3]:
            print(f"    移除 {code}　最大變動 {mx} 名")

    print("\n⚠️ 以上皆非模型效能指標。Top-K 命中率衡量的是「這份清單"
          "先看到的幾筆，是否較常對應到已知的法遵發現」，"
          "不是對違規的預測力；15 個正樣本不足以支撐後者。")


if __name__ == "__main__":
    main()
