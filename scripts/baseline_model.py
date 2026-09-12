"""軌 A baseline: can zero-OCR features rank next year's violators?

Trained and evaluated on **disjoint time windows**, and reported with
precision@k alongside AUC. precision@k is the metric that matters here: the
education bureau can only inspect so many 園 per year, so "of the top 100 we
flag, how many actually get penalised" is the operational question. AUC alone
hides whether the ranking is useful at the top.

Windows
    train  features as of 2022-01-01, label = penalty in [2022, 2024)
    test   features as of 2024-01-01, label = penalty in [2024, 2026)

The two feature snapshots are built independently, so no test-period penalty can
reach a training feature. Nested models isolate how much each block adds.

Run:  PYTHONPATH=src .venv/bin/python scripts/baseline_model.py
"""

from __future__ import annotations

import pathlib
import sys
import warnings

import numpy as np
import pandas as pd

# numpy 2.0.2 on Apple Accelerate emits a spurious "divide by zero encountered in
# matmul" from inside BLAS -- a bare `np.random.rand(1213, 5) @ np.random.rand(5)`
# reproduces it, so it is not a property of these features. Results are finite and
# are asserted as such below.
warnings.filterwarnings("ignore", message="divide by zero encountered in matmul")
warnings.filterwarnings("ignore", message="overflow encountered in matmul")
warnings.filterwarnings("ignore", message="invalid value encountered in matmul")
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.features.build import (
    build_features,
    label_future_penalty,
    load_evaluations,
    load_penalties,
    load_vehicles,
)

TRAIN_AS_OF, TRAIN_END = pd.Timestamp("2022-01-01"), pd.Timestamp("2024-01-01")
TEST_AS_OF, TEST_END = pd.Timestamp("2024-01-01"), pd.Timestamp("2026-01-01")

# Nested blocks, so each row's lift over the previous one is that block's contribution.
BLOCKS: dict[str, list[str]] = {
    "① 僅裁罰史": [
        "n_penalties_prior", "sum_severity_prior", "max_severity_prior",
        "n_severe_prior", "days_since_last_penalty",
    ],
    "② +機構規模/年資": [
        "n_penalties_prior", "sum_severity_prior", "max_severity_prior",
        "n_severe_prior", "days_since_last_penalty",
        "count_approved", "age_years", "size_in", "indoor_area_per_child",
    ],
    "③ +營運型態": [
        "n_penalties_prior", "sum_severity_prior", "max_severity_prior",
        "n_severe_prior", "days_since_last_penalty",
        "count_approved", "age_years", "size_in", "indoor_area_per_child",
        "n_vehicles", "has_vehicle", "after_care", "monthly", "is_private",
    ],
    # Measured and kept as a recorded negative: ④ scores *below* ③ (AUC 0.634 vs
    # 0.640). The signal itself is real -- an evaluation ending 部分指標通過 is
    # followed by a penalty within a year 17.9% of the time against 7.4%
    # (OR 2.72, p=0.0009, and it survives stratification) -- but evaluations run
    # on a rolling cycle of roughly 250 園 a year, so at the training snapshot
    # only 16% of 園 have one at all and the feature is mostly absent where the
    # model would have to learn it. Its home is the event-triggered escalation
    # channel in risk/priority.py, not a fixed-snapshot feature.
    "④ +官方評鑑": [
        "n_penalties_prior", "sum_severity_prior", "max_severity_prior",
        "n_severe_prior", "days_since_last_penalty",
        "count_approved", "age_years", "size_in", "indoor_area_per_child",
        "n_vehicles", "has_vehicle", "after_care", "monthly", "is_private",
        "has_evaluation", "eval_partial", "n_partial_prior",
        "days_since_evaluation",
    ],
}


def precision_at_k(y_true: np.ndarray, scores: np.ndarray, k: int) -> float:
    order = np.argsort(-scores)[:k]
    return float(y_true[order].mean())


def recall_at_k(y_true: np.ndarray, scores: np.ndarray, k: int) -> float:
    order = np.argsort(-scores)[:k]
    return float(y_true[order].sum() / y_true.sum())


def main() -> None:
    inst = pd.read_csv("data/processed/institutions_ntpc.csv")
    pen = load_penalties("data/processed/penalties_ntpc.csv")
    veh = load_vehicles("data/external/kids_vehicles.json")
    ev = load_evaluations("data/processed/evaluations_ntpc_full.csv")

    frames = {}
    for name, (as_of, end) in {
        "train": (TRAIN_AS_OF, TRAIN_END),
        "test": (TEST_AS_OF, TEST_END),
    }.items():
        f = build_features(inst, pen, veh, as_of, evaluations=ev)
        f["y"] = label_future_penalty(pen, f["id"], as_of, end)
        f["is_private"] = (f["type"] == "私立").astype(int)
        # days_since_last_penalty is NaN for a 園 with no history; a large finite
        # sentinel keeps "never penalised" ordered as the low-risk extreme rather
        # than silently imputed to the mean of those who *have* been penalised.
        f["days_since_last_penalty"] = f["days_since_last_penalty"].fillna(9999)
        # 未受評鑑者沒有「距上次評鑑天數」；與裁罰史同樣用大哨兵值，
        # 避免被補成「已受評者的平均間隔」而憑空造出一次評鑑。
        f["days_since_evaluation"] = f["days_since_evaluation"].fillna(9999)
        f["eval_partial"] = f["eval_partial"].fillna(0)
        frames[name] = f
        print(
            f"{name}: as_of={as_of.date()} label=[{as_of.date()},{end.date()})  "
            f"n={len(f)} 正樣本={int(f['y'].sum())} ({f['y'].mean() * 100:.1f}%)"
        )

    tr, te = frames["train"], frames["test"]
    y_tr, y_te = tr["y"].to_numpy(), te["y"].to_numpy()
    base = y_te.mean()
    k = 100  # a plausible annual inspection capacity

    print(f"\n測試集基準率 {base * 100:.1f}%  |  隨機抽 {k} 家的期望命中 {base * k:.0f} 家")
    print(f"\n{'模型':<20}{'AUC':>7}{'AP':>7}{f'P@{k}':>8}{f'R@{k}':>8}{'提升倍數':>9}")

    dummy = DummyClassifier(strategy="stratified", random_state=0).fit(
        tr[BLOCKS["① 僅裁罰史"]], y_tr
    )
    ds = dummy.predict_proba(te[BLOCKS["① 僅裁罰史"]])[:, 1]
    print(
        f"{'隨機基準':<20}{roc_auc_score(y_te, ds):>7.3f}"
        f"{average_precision_score(y_te, ds):>7.3f}"
        f"{precision_at_k(y_te, ds, k):>8.3f}{recall_at_k(y_te, ds, k):>8.3f}"
        f"{precision_at_k(y_te, ds, k) / base:>9.2f}x"
    )

    results = {}
    for label, cols in BLOCKS.items():
        for algo_name, algo in [
            ("LR", make_pipeline(
                SimpleImputer(strategy="median"),
                StandardScaler(),
                LogisticRegression(max_iter=2000, class_weight="balanced"),
            )),
            ("GB", make_pipeline(
                SimpleImputer(strategy="median"),
                GradientBoostingClassifier(random_state=0),
            )),
        ]:
            algo.fit(tr[cols], y_tr)
            s = algo.predict_proba(te[cols])[:, 1]
            if not np.isfinite(s).all():
                raise AssertionError(f"{label} {algo_name} produced non-finite scores")
            auc = roc_auc_score(y_te, s)
            ap = average_precision_score(y_te, s)
            pk = precision_at_k(y_te, s, k)
            rk = recall_at_k(y_te, s, k)
            results[(label, algo_name)] = (auc, ap, pk, rk, s)
            print(
                f"{label + ' ' + algo_name:<20}{auc:>7.3f}{ap:>7.3f}"
                f"{pk:>8.3f}{rk:>8.3f}{pk / base:>9.2f}x"
            )

    # --- where the ranking helps and where it cannot -------------------
    best = max(results.items(), key=lambda kv: kv[1][1])
    (blabel, balgo), _ = best
    print(f"\n最佳（依 AP）：{blabel} {balgo}")

    # 十分位表與冷啟動診斷一律用**實際部署的那個配置**，不用「AP 最佳」的贏家。
    #
    # 理由是 AP 的名次不穩定到不能拿來做結論：② GB 與 ③ LR 的 AP 差距只有
    # 0.0003，而兩者的冷啟動表現天差地遠。用擲硬幣等級的差距決定要印哪一段
    # 策略結論，會讓同一份程式碼在不同資料版本下印出互相矛盾的建議——這件事
    # 已經發生過：docs/research/05-phase1-results.md §4 記載的冷啟動數字，
    # 就是在 AP 贏家換人之後失效的。
    #
    # risk/priority.py 部署的是 PRIORITY_FEATURES（＝block ③ 加 is_private）
    # 配 LogisticRegression，所以診斷必須描述它，否則報告的數字與上線的系統
    # 不是同一件事。
    deployed = ("③ +營運型態", "LR")
    dlabel, dalgo = deployed
    ds_deployed = results[deployed][4]
    gap = results[(blabel, balgo)][1] - results[deployed][1]
    print(f"以下診斷使用**部署配置** {dlabel} {dalgo}"
          f"（AP 與最佳者相差 {gap:+.4f}，名次不穩定，不作為選型依據）")

    te = te.assign(score=ds_deployed)
    print("\n=== 分數十分位的實際受罰率 ===")
    te["decile"] = pd.qcut(te["score"].rank(method="first"), 10, labels=range(1, 11))
    d = te.groupby("decile", observed=True).agg(n=("y", "size"), 受罰=("y", "sum"))
    d["受罰率%"] = (d["受罰"] / d["n"] * 100).round(1)
    d["對比基準"] = (d["受罰"] / d["n"] / base).round(2)
    print(d.sort_index(ascending=False).to_string())

    # The 56% of future violators with no prior record are the population the
    # bureau most needs help with, and the one a penalty-history model cannot see.
    # Whether the remaining features carry signal there is an empirical question --
    # the interpretation below is derived from the measured AUC, not assumed.
    print("\n=== 冷啟動子群（無前科者）模型是否仍有鑑別力 ===")
    cold = te[te["has_prior_penalty"] == 0]
    print(f"無前科機構 {len(cold)} 家，其中 {int(cold['y'].sum())} 家於測試期受罰")
    if cold["y"].nunique() > 1:
        cauc = roc_auc_score(cold["y"], cold["score"])
        cbase = cold["y"].mean()
        cpk = precision_at_k(cold["y"].to_numpy(), cold["score"].to_numpy(), 50)
        print(
            f"  子群 AUC={cauc:.3f}  基準率={cbase * 100:.1f}%  "
            f"P@50={cpk:.3f} ({cpk / cbase:.2f}x)"
        )
        if cauc < 0.55:
            print("  → 排除裁罰史後幾乎無鑑別力，冷啟動缺口只能靠財務鑑識訊號（軌 B）補。")
        else:
            print(
                f"  → 即使完全沒有前科，非裁罰特徵仍保有鑑別力（AUC {cauc:.3f}，"
                f"前 50 名命中率 {cpk / cbase:.2f} 倍）。"
            )
            print("     軌 A 對冷啟動機構可用，軌 B 的價值在於提升精度與提供可行動理由。")


if __name__ == "__main__":
    main()
