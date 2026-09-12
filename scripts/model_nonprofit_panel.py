"""Fit models on the 園 × 學年度 feature table and report what they are worth.

Two models, because the panel supports one of them and not the other, and the
useful output is knowing which:

**Supervised** -- features from 學年度 N predict "any compliance rule fails in
N+1". 94 usable pairs, 10 positives. This is below the 12-positive floor
``CLAUDE.md`` sets for fitting anything supervised, so it is fitted here to
*measure* that, not to deploy it. Every number comes with a bootstrap interval,
and the comparison that matters is against the trivial baseline (did it fail this
year?) rather than against 0.5.

**Unsupervised** -- IsolationForest over the same features, no label at all. This
is the one the sample size supports, and it answers the question the product
actually asks: which 園 look unlike the rest, so an inspector sees them first.

## The two things that decide whether any of this is honest

**Splitting is by 園, never by row.** A 園 contributes up to four 園-年 whose
financials barely move year to year. Random k-fold puts 安溪 111 in train and
安溪 112 in test, and the model scores well by recognising the institution rather
than the pattern. ``GroupKFold`` on ``code`` removes that.

**The label comes from the following year.** Features are what the 學年度 N report
filed; the label is what the N+1 report turned out to contain. A model trained on
the same year's compliance outcome would be reading the answer.

Usage
    PYTHONPATH=src .venv/Scripts/python scripts/model_nonprofit_panel.py
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from smart_watchdog.console import use_utf8

FEATURES = pathlib.Path("data/processed/nonprofit_ml_features.csv")
FINDINGS = pathlib.Path("data/processed/compliance_findings.csv")
OUT = pathlib.Path("data/processed/nonprofit_model_scores.csv")

SEED = 20260912
#: Identifier and bookkeeping columns that are not features.
NON_FEATURES = {"code", "short_name", "academic_year", "available_from",
                "n_sections"}


def load_labels() -> dict[tuple, bool]:
    labels: dict[tuple, bool] = {}
    with FINDINGS.open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            k = (r["code"], str(r["academic_year"]))
            labels.setdefault(k, False)
            if str(r["passed"]).lower() in ("false", "0"):
                labels[k] = True
    return labels


def auc(y: np.ndarray, s: np.ndarray) -> float | None:
    """Rank-based AUC. None when one class is absent."""
    pos, neg = s[y == 1], s[y == 0]
    if not len(pos) or not len(neg):
        return None
    order = np.argsort(np.concatenate([pos, neg]))
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(order) + 1)
    # Average ranks over ties so a constant score gives exactly 0.5.
    allv = np.concatenate([pos, neg])
    for v in np.unique(allv):
        m = allv == v
        ranks[m] = ranks[m].mean()
    return (ranks[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def boot_ci(y: np.ndarray, s: np.ndarray, groups: np.ndarray,
            iters: int = 2000) -> tuple[float, float]:
    """Bootstrap the AUC by resampling 園, not rows."""
    rng = np.random.default_rng(SEED)
    parks = np.unique(groups)
    vals = []
    for _ in range(iters):
        pick = rng.choice(parks, size=len(parks), replace=True)
        idx = np.concatenate([np.flatnonzero(groups == p) for p in pick])
        a = auc(y[idx], s[idx])
        if a is not None:
            vals.append(a)
    if not vals:
        return (float("nan"), float("nan"))
    return (float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5)))


def main() -> None:
    ap = argparse.ArgumentParser(description="在園×學年度特徵表上跑模型")
    ap.add_argument("--min-coverage", type=float, default=0.85,
                    help="特徵至少要有這個比例非空才納入")
    a = ap.parse_args()
    use_utf8()

    df = pd.read_csv(FEATURES)
    df["academic_year"] = df["academic_year"].astype(str)
    labels = load_labels()

    # ── 時序配對：學年度 N 的特徵 → N+1 的結果 ────────────────────────
    df["next_year"] = (df["academic_year"].astype(int) + 1).astype(str)
    df["y"] = [labels.get((c, n)) for c, n in zip(df["code"], df["next_year"])]
    pairs = df[df["y"].notna()].copy()
    pairs["y"] = pairs["y"].astype(int)

    feat_cols = [c for c in df.columns
                 if c not in NON_FEATURES | {"next_year", "y"}
                 and pd.api.types.is_numeric_dtype(df[c])]
    keep = [c for c in feat_cols
            if pairs[c].notna().mean() >= a.min_coverage]
    print(f"特徵表 {len(df)} 列 × {len(feat_cols)} 個數值欄")
    print(f"時序配對（N → N+1）：{len(pairs)} 組，正樣本 {int(pairs.y.sum())}"
          f"（{pairs.y.mean():.1%}）")
    print(f"覆蓋率 ≥{a.min_coverage:.0%} 的特徵：{len(keep)} 個\n")

    X = pairs[keep].to_numpy(dtype=float)
    y = pairs["y"].to_numpy()
    groups = pairs["code"].to_numpy()

    # ── 1. 監督式（分園切分） ─────────────────────────────────────────
    print("=== 1. 監督式：邏輯迴歸，GroupKFold 以園為單位 ===")
    pipe = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(),
                         LogisticRegression(max_iter=2000, C=0.1,
                                            class_weight="balanced"))
    n_splits = min(5, len(np.unique(groups)))
    oof = np.full(len(y), np.nan)
    for tr, te in GroupKFold(n_splits=n_splits).split(X, y, groups):
        if len(np.unique(y[tr])) < 2:
            continue
        pipe.fit(X[tr], y[tr])
        oof[te] = pipe.predict_proba(X[te])[:, 1]
    ok = ~np.isnan(oof)
    model_auc = auc(y[ok], oof[ok])
    lo, hi = boot_ci(y[ok], oof[ok], groups[ok])
    print(f"  out-of-fold AUC = {model_auc:.3f}　95% CI [{lo:.3f}, {hi:.3f}]")

    # 對照組：本年是否已未通過，這是最強的單一候選特徵
    prior = np.array([1.0 if labels.get((c, yy)) else 0.0
                      for c, yy in zip(pairs["code"], pairs["academic_year"])])
    base_auc = auc(y, prior)
    blo, bhi = boot_ci(y, prior, groups)
    print(f"  對照：只用「本年是否未通過」 AUC = {base_auc:.3f}"
          f"　95% CI [{blo:.3f}, {bhi:.3f}]")
    note = "區間涵蓋 0.5，無法宣稱優於隨機。" if lo <= 0.5 <= hi else ""
    print(f"\n  信賴區間寬度 {hi - lo:.3f}。{note}")

    # ── 2. 非監督式（不用標籤） ───────────────────────────────────────
    print("\n=== 2. 非監督式：IsolationForest（不使用任何標籤）===")
    full_keep = [c for c in feat_cols if df[c].notna().mean() >= a.min_coverage]
    Xall = df[full_keep].to_numpy(dtype=float)
    iso = make_pipeline(
        SimpleImputer(strategy="median"), StandardScaler(),
        IsolationForest(n_estimators=500, random_state=SEED, contamination="auto"))
    iso.fit(Xall)
    df["iso_score"] = -iso[-1].score_samples(iso[:-1].transform(Xall))
    print(f"  {len(df)} 列 × {len(full_keep)} 特徵，分數越高越不像同儕")

    have = df[[bool(labels.get((c, yy)) is not None)
               for c, yy in zip(df["code"], df["academic_year"])]].copy()
    have["lab"] = [int(bool(labels.get((c, yy))))
                   for c, yy in zip(have["code"], have["academic_year"])]
    base = have["lab"].mean()
    print(f"  同期外部對照：{len(have)} 列中有法遵發現 {int(have.lab.sum())}"
          f"（基準 {base:.1%}）")
    order = have.sort_values("iso_score", ascending=False)
    print(f"\n  {'K':>4}{'命中':>6}{'命中率':>9}{'相對基準':>10}")
    for k in (5, 10, 20):
        hit = int(order.head(k)["lab"].sum())
        print(f"  {k:>4}{hit:>6}{hit / k:>9.1%}{(hit / k) / base:>9.2f}x")

    df[["code", "short_name", "academic_year", "iso_score", "n_sections"]] \
        .sort_values("iso_score", ascending=False) \
        .to_csv(OUT, index=False, encoding="utf-8")
    print(f"\n寫入 {OUT}")
    print("\n⚠️ 監督式模型在此資料量下不具部署價值，列出僅為量化該限制；"
          "非監督式分數是排序輔助，不是違規機率。")


if __name__ == "__main__":
    main()
