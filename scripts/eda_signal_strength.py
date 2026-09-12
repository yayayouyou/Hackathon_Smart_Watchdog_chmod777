"""Univariate signal test: which zero-OCR features actually separate future violators?

Strictly time-split -- features are computed as of ``AS_OF`` and the label is a
penalty in the window after it. Every association is reported with a p-value and
an effect size so weak signals cannot masquerade as strong ones.

Run:  PYTHONPATH=src .venv/bin/python scripts/eda_signal_strength.py
"""

from __future__ import annotations

import pathlib
import sys

import pandas as pd
from scipy.stats import fisher_exact, mannwhitneyu

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.features.build import (
    build_features,
    label_future_penalty,
    load_penalties,
    load_vehicles,
)

AS_OF = pd.Timestamp("2024-01-01")
LABEL_END = pd.Timestamp("2026-01-01")  # 2026 is partial; stop at the year boundary

NUMERIC = [
    "n_penalties_prior", "sum_severity_prior", "max_severity_prior", "n_severe_prior",
    "count_approved", "monthly", "age_years", "size_in", "indoor_area_per_child",
    "n_vehicles", "oldest_vehicle_age_yr", "op_sibling_penalty_rate",
]
BINARY = ["has_prior_penalty", "has_vehicle", "after_care"]


def main() -> None:
    inst = pd.read_csv("data/processed/institutions_ntpc.csv")
    pen = load_penalties("data/processed/penalties_ntpc.csv")
    veh = load_vehicles("data/external/kids_vehicles.json")

    f = build_features(inst, pen, veh, AS_OF)
    f["y"] = label_future_penalty(pen, f["id"], AS_OF, LABEL_END)

    base = f["y"].mean()
    print(f"as_of={AS_OF.date()}  label window=[{AS_OF.date()}, {LABEL_END.date()})")
    print(f"母體 {len(f)}  正樣本 {int(f['y'].sum())}  基準率 {base * 100:.1f}%\n")

    print("=== 連續變數（Mann-Whitney，受罰組 vs 未罰組）===")
    print("rbc = rank-biserial correlation；正值代表受罰組較高（中位數相同時仍可判方向）")
    print(
        f"{'feature':<26}{'n':>6}{'受罰中位數':>12}{'未罰中位數':>12}{'p':>10}{'rbc':>8}"
    )
    rows = []
    for col in NUMERIC:
        s = f[[col, "y"]].dropna()
        if s[col].nunique() < 2 or s["y"].nunique() < 2:
            print(f"{col:<26}{len(s):>6}{'--- 變異不足或無正樣本 ---':>34}")
            continue
        a, b = s[s["y"] == 1][col], s[s["y"] == 0][col]
        u, p = mannwhitneyu(a, b, alternative="two-sided")
        # Rank-biserial correlation gives the direction and effect size even when
        # both medians are identical -- which they are for every count feature
        # here, since most institutions have zero prior penalties.
        rbc = 2 * u / (len(a) * len(b)) - 1
        rows.append((col, len(s), a.median(), b.median(), p, rbc))
        print(
            f"{col:<26}{len(s):>6}{a.median():>12.2f}{b.median():>12.2f}"
            f"{p:>10.2e}{rbc:>+8.3f}"
        )

    print("\n=== 二元變數（Fisher exact）===")
    print(f"{'feature':<26}{'有':>8}{'受罰率':>9}{'無':>8}{'受罰率':>9}{'OR':>7}{'p':>10}")
    for col in BINARY:
        s = f[[col, "y"]].dropna()
        if s[col].nunique() < 2:
            print(f"{col:<26}{'--- 無變異，略過 ---':>40}")
            continue
        hi, lo = s[s[col] == 1], s[s[col] == 0]
        table = [
            [int(hi["y"].sum()), len(hi) - int(hi["y"].sum())],
            [int(lo["y"].sum()), len(lo) - int(lo["y"].sum())],
        ]
        odds, p = fisher_exact(table)
        print(
            f"{col:<26}{len(hi):>8}{hi['y'].mean() * 100:>8.1f}%"
            f"{len(lo):>8}{lo['y'].mean() * 100:>8.1f}%{odds:>7.2f}{p:>10.2e}"
        )

    print("\n=== 機構類型 ===")
    g = f.groupby("type").agg(n=("y", "size"), 受罰=("y", "sum"))
    g["受罰率%"] = (g["受罰"] / g["n"] * 100).round(1)
    print(g.sort_values("n", ascending=False).to_string())

    print("\n=== 行政區 top 10（樣本 >=20）===")
    t = f.groupby("town").agg(n=("y", "size"), 受罰=("y", "sum"))
    t = t[t["n"] >= 20]
    t["受罰率%"] = (t["受罰"] / t["n"] * 100).round(1)
    print(t.sort_values("受罰率%", ascending=False).head(10).to_string())

    print("\n顯著（p<0.05）連續特徵，依 p 排序：")
    for col, n, _ma, _mb, p, rbc in sorted(
        [r for r in rows if r[4] < 0.05], key=lambda r: r[4]
    ):
        direction = "↑受罰組較高" if rbc > 0 else "↓受罰組較低"
        print(f"  {direction} {col:<26} p={p:.2e}  rbc={rbc:+.3f}  (n={n})")

    # --- confound check -------------------------------------------------
    # 私立 charge far more than the fee-capped 公立/非營利 and are penalised far
    # more often, so a raw `monthly` effect may be institution type in disguise.
    print("\n=== 混淆檢查：monthly 在機構類型內是否仍有效 ===")
    for t in ["私立", "公立", "非營利"]:
        s = f[(f["type"] == t)][["monthly", "y"]].dropna()
        if s["y"].nunique() < 2 or s["monthly"].nunique() < 2:
            print(f"  {t}: 樣本不足或無變異，略過")
            continue
        a, b = s[s["y"] == 1]["monthly"], s[s["y"] == 0]["monthly"]
        u, p = mannwhitneyu(a, b, alternative="two-sided")
        rbc = 2 * u / (len(a) * len(b)) - 1
        print(
            f"  {t:<4} n={len(s):>4}  受罰中位數 {a.median():>8.0f}"
            f"  未罰 {b.median():>8.0f}  p={p:.3f}  rbc={rbc:+.3f}"
        )


if __name__ == "__main__":
    main()
