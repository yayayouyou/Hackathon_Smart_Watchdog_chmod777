"""Check the anomaly ranking three ways, none of which is an AUC.

The panel has 15 園-年 with a compliance finding, across 10 園. Reporting an AUC on
that would put a number with a confidence interval wider than its range in front
of judges, which ``docs/FINDINGS.md`` §3.3 already criticises the project for
nearly doing once. So the checks here are the ones the sample size supports:

**Top-K hit rate, one row per 園.** Of the K 園 ranked highest, how many turn out
to have a compliance finding, against the base rate?

The unit has to be the 園, not the 園-年. Pooling all 132 園-年 and ranking them
together breaks two things at once: the scores are peer-relative and therefore
only comparable *within* a 學年度 -- the scorer's own rule -- and a 園 contributing
up to four rows makes the hypergeometric independence assumption false, since the
same institution can occupy four places in one "top 20". So the primary check
takes each 園's most recent 學年度 and ranks by its **percentile within that year's
cohort**, which is comparable across years in a way the raw score is not.

The all-years view is kept as a secondary read, tested by a permutation that
shuffles labels **at the 園 level**, so the clustering is respected rather than
assumed away.

**Ranking stability under feature removal.** Drop each feature in turn, rescore,
and correlate the rank orders. A ranking that reshuffles when one of seventeen
ratios is removed is reporting that ratio, not the 園.

**Leave-one-kindergarten-out.** Remove one 園 from the peer cohort and rescore the
others. Every score is relative to a median and a spread computed from ~30 peers,
so a single extreme 園 can move everyone else. If it does, the ranking is partly
an artefact of who happens to be in the cohort.

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
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from scipy import stats

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from build_nonprofit_anomaly import FEATURES, build_rows

from smart_watchdog.console import use_utf8
from smart_watchdog.features.anomaly import score_cohort

FINDINGS = pathlib.Path("data/processed/compliance_findings.csv")
PERMUTATIONS = 20000
SEED = 20260912


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


def within_year_percentile(scores: dict[tuple, float]) -> dict[tuple, float]:
    """Each score's percentile inside its own 學年度 cohort (100 = most unusual)."""
    by_year: dict[str, list[tuple]] = collections.defaultdict(list)
    for (code, year), v in scores.items():
        by_year[year].append((code, v))
    out: dict[tuple, float] = {}
    for year, group in by_year.items():
        ordered = sorted(group, key=lambda x: x[1])
        n = len(ordered)
        for i, (code, _) in enumerate(ordered):
            out[(code, year)] = 100.0 * i / max(n - 1, 1)
    return out


def latest_per_park(scores: dict[tuple, float]) -> dict[str, str]:
    """園 -> its most recent 學年度 among the scored rows."""
    latest: dict[str, str] = {}
    for code, year in scores:
        if code not in latest or year > latest[code]:
            latest[code] = year
    return latest


def cluster_permutation_p(order: list[tuple], labels: dict[tuple, bool],
                          k: int, hit: int) -> float:
    """P(at least ``hit`` in the top k) with labels shuffled between 園, not rows.

    A 園's four years share a label far more often than chance would give, so
    shuffling rows independently understates the null variance and makes any
    result look stronger than it is.
    """
    by_park: dict[str, list[tuple]] = collections.defaultdict(list)
    for key in order:
        by_park[key[0]].append(key)
    parks = list(by_park)
    park_labels = [any(labels.get(kk, False) for kk in by_park[p]) for p in parks]
    rng = random.Random(SEED)
    top = order[:k]
    at_least = 0
    for _ in range(PERMUTATIONS):
        shuffled = park_labels[:]
        rng.shuffle(shuffled)
        assigned = dict(zip(parks, shuffled))
        if sum(1 for key in top if assigned[key[0]]) >= hit:
            at_least += 1
    return at_least / PERMUTATIONS


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
    pcts = within_year_percentile(base)
    scored_keys = [k for k in base if k in labels]
    ks = [int(x) for x in a.ks.split(",")]

    # ── 1a. 主要檢定：每園一列 ──────────────────────────────────────
    latest = latest_per_park(base)
    park_keys = [(c, y) for c, y in latest.items() if (c, y) in labels]
    p_pos = sum(1 for k in park_keys if labels[k])
    p_n = len(park_keys)
    print("=== 1a. Top-K 命中率｜每園取最新學年度（主要檢定）===")
    print(f"{p_n} 所園各取最新學年度，其中有法遵發現 {p_pos} 所"
          f"（基準率 {p_pos / p_n:.1%}）。依同年度百分位排序。")
    park_order = sorted(park_keys, key=lambda k: -pcts[k])
    print(f"\n{'K':>4}{'命中':>6}{'命中率':>9}{'相對基準':>10}{'P(超幾何)':>12}")
    for k in ks:
        if k > p_n:
            continue
        hit = sum(1 for key in park_order[:k] if labels[key])
        lift = (hit / k) / (p_pos / p_n) if p_pos else float("nan")
        p = float(stats.hypergeom.sf(hit - 1, p_n, p_pos, k)) if hit else 1.0
        print(f"{k:>4}{hit:>6}{hit / k:>9.1%}{lift:>9.2f}x{p:>12.3f}")

    # ── 1b. 次要：全年度合併，以園為 cluster ────────────────────────
    n = len(scored_keys)
    pos = sum(1 for k in scored_keys if labels[k])
    n_parks = len({c for c, _ in scored_keys})
    print("\n=== 1b. 全年度合併（次要；以園為 cluster 的置換檢定）===")
    print(f"{n} 個園-學年度、{n_parks} 所園，有法遵發現 {pos} 個"
          f"（基準率 {pos / n:.1%}）。同一園可重複出現，故不用超幾何。")
    order = sorted(scored_keys, key=lambda k: -pcts[k])
    print(f"\n{'K':>4}{'命中':>6}{'命中率':>9}{'相對基準':>10}{'P(置換,園為單位)':>16}")
    for k in ks:
        if k > n:
            continue
        hit = sum(1 for key in order[:k] if labels[key])
        lift = (hit / k) / (pos / n) if pos else float("nan")
        p = cluster_permutation_p(order, labels, k, hit) if hit else 1.0
        print(f"{k:>4}{hit:>6}{hit / k:>9.1%}{lift:>9.2f}x{p:>16.3f}")

    # ── 偵測底線 ────────────────────────────────────────────────────
    print(f"\n偵測底線（主要檢定：{p_n} 所園、{p_pos} 所有發現，"
          f"達 p<0.05 所需命中數）：")
    unreachable = []
    for k in ks:
        if k > p_n:
            continue
        # A hit count above the number of positives is arithmetically impossible,
        # so the floor must be capped at p_pos -- otherwise this prints a target
        # that even a perfect ranking could not reach and calls it a threshold.
        need = next((h for h in range(min(k, p_pos) + 1)
                     if stats.hypergeom.sf(h - 1, p_n, p_pos, k) < 0.05), None)
        if need is None:
            best = float(stats.hypergeom.sf(min(k, p_pos) - 1, p_n, p_pos, k))
            unreachable.append((k, best))
            print(f"  K={k:<4}**不可能達到** —— 即使 {min(k, p_pos)} 所全部命中，"
                  f"p 仍為 {best:.3f}")
        else:
            print(f"  K={k:<4}需命中 {need}（命中率 {need / k:.0%}，"
                  f"{(need / k) / (p_pos / p_n):.2f} 倍基準）")
    print("  → 低於門檻只代表「未能偵測」，不代表「無關」。")
    if unreachable:
        print(f"  → 其中 {len(unreachable)} 個 K 值在本樣本數下"
              f"**無論排序多準都無法達到顯著**，這是樣本量的限制，不是排序的表現。")

    # ── 2. 排序穩定性 ───────────────────────────────────────────────
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

    # ── 3. LOO ──────────────────────────────────────────────────────
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
        shifts.append(sum(moves) / len(moves))
        worst.append((max(moves), code))
    if shifts:
        worst.sort(reverse=True)
        print(f"移除任一園後，其餘園-年的平均名次變動："
              f"中位 {sorted(shifts)[len(shifts) // 2]:.2f} 名")
        print("  移除後造成最大單筆名次變動的三所園：")
        for mx, code in worst[:3]:
            print(f"    移除 {code}　最大變動 {mx} 名")

    print("\n⚠️ 以上皆非模型效能指標。Top-K 命中率衡量的是「這份清單先看到的"
          "幾所園，是否較常對應到已知的法遵發現」，不是對違規的預測力。")


if __name__ == "__main__":
    main()
