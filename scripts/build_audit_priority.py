"""Build the audit priority list -- the system's deliverable output.

Output: data/processed/audit_priority_ntpc.csv, one row per registered 園 with a
score, an evidence tier, and the reason it is surfaced.

Two windows, deliberately separate:

* **fit window** -- features as of 2024-01-01, labels from penalties in
  [2024, 2026). This is the most recent window whose labels are complete.
* **scoring** -- features as of today, so the ranking uses everything known now.

The performance we report (AUC 0.641, P@100 2.17x) comes from neither of these:
it comes from scripts/baseline_model.py, which trains on 2022 features and tests
on this fit window, so no test-period penalty ever reaches a training feature.
Fitting the deployed model on the most recent complete window and quoting a
held-out number measured on an earlier split is the standard arrangement; quoting
a score computed on the same rows the model was fitted on would be meaningless.

A high position means 建議查核, never 疑似不法. A low position means only that
nothing in the available data raised a question -- for the 94.8% of 園 with no
public financial statements, that is a statement about our coverage, not about
them, and the ``data_sufficiency`` column says so on every row.

Run:  PYTHONPATH=src .venv/bin/python scripts/build_audit_priority.py
"""

from __future__ import annotations

import pathlib
import sys
import warnings

import pandas as pd

# See scripts/baseline_model.py: numpy 2.0.2 on Apple Accelerate emits spurious
# BLAS warnings that a bare matmul reproduces. Results are asserted finite.
warnings.filterwarnings("ignore", message="divide by zero encountered in matmul")
warnings.filterwarnings("ignore", message="overflow encountered in matmul")
warnings.filterwarnings("ignore", message="invalid value encountered in matmul")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.features.build import (
    build_features,
    label_future_penalty,
    load_evaluations,
    load_penalties,
    load_vehicles,
)
from smart_watchdog.risk.priority import (
    PRIORITY_FEATURES,
    assemble,
    compliance_summary,
    evaluation_summary,
    event_summary,
    fit_priority_model,
    prepare_frame,
    usable_features,
)

FIT_AS_OF, FIT_END = pd.Timestamp("2024-01-01"), pd.Timestamp("2026-01-01")
OUT = pathlib.Path("data/processed/audit_priority_ntpc.csv")
TOP_K = 100


def main() -> None:
    score_as_of = pd.Timestamp.today().normalize()

    inst = pd.read_csv("data/processed/institutions_ntpc.csv")
    pen = load_penalties("data/processed/penalties_ntpc.csv")
    veh = load_vehicles("data/external/kids_vehicles.json")

    fit_frame = prepare_frame(build_features(inst, pen, veh, FIT_AS_OF))
    y = label_future_penalty(pen, fit_frame["id"], FIT_AS_OF, FIT_END)
    feats = usable_features(fit_frame)
    model = fit_priority_model(fit_frame, y, feats)
    print(
        f"配適窗：特徵 as_of={FIT_AS_OF.date()}，標籤 [{FIT_AS_OF.date()},"
        f"{FIT_END.date()})　n={len(fit_frame)} 正樣本={int(y.sum())}"
        f" ({y.mean() * 100:.1f}%)"
    )
    dropped = [f for f in PRIORITY_FEATURES if f not in feats]
    print(f"　　實際使用特徵 {len(feats)}/{len(PRIORITY_FEATURES)}", end="")
    print(f"　不可得：{', '.join(dropped)}" if dropped else "")
    if dropped:
        print(
            "　　（車輛快照 pinned 於 2026-08-10，早於配適窗即拒絕描述當時車隊，"
            "避免把現在洩漏進過去；量測 AUC 0.640 的窗口同樣不可得，故兩者一致）"
        )

    live = build_features(inst, pen, veh, score_as_of)
    print(f"評分窗：特徵 as_of={score_as_of.date()}　n={len(live)}")

    findings = pd.read_csv("data/processed/compliance_findings.csv")
    crosswalk = pd.read_csv("data/processed/nonprofit_registry_crosswalk.csv")
    events = pd.read_csv("data/processed/official_events_ntpc.csv")
    fees = pd.read_csv("data/processed/fee_summary_ntpc.csv")

    evaluations = load_evaluations("data/processed/evaluations_ntpc_full.csv")
    out = assemble(
        live,
        model,
        feats,
        compliance_summary(findings, crosswalk),
        event_summary(events, score_as_of),
        fee_ids=set(fees["title"]),
        evaluations=evaluation_summary(evaluations, live["title"], score_as_of),
        top_k=TOP_K,
    )

    cols = [
        "id", "title", "entity", "type", "town",
        "priority_score", "priority_rank_overall", "priority_rank_within_type",
        "priority_decile", "review_reason", "flagged",
        "evidence_tier", "financial_data_available", "data_sufficiency",
        "compliance_years_checked", "compliance_failed_total",
        "compliance_failed_high", "compliance_top_finding",
        "events_90d", "events_365d", "latest_event_type", "latest_event_date",
        "eval_result", "eval_date", "eval_partial", "eval_recent_partial",
        "n_penalties_prior", "sum_severity_prior", "days_since_last_penalty",
        "has_prior_penalty", "count_approved", "age_years", "monthly",
    ]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out[cols].to_csv(OUT, index=False)
    print(f"\nwrote {OUT}  ({len(out)} 園)\n")

    print("=== 為什麼被列入（可複選）===")
    in_top = int((out["priority_rank_overall"] <= TOP_K).sum())
    print(f"  分數前 {TOP_K}                 {in_top:>5}")
    print(f"  財報法遵未通過（高嚴重度）    {(out['compliance_failed_high'] > 0).sum():>5}")
    print(f"  財報法遵未通過（全部）        {(out['compliance_failed_total'] > 0).sum():>5}")
    print(f"  近 90 日有官方事件            {(out['events_90d'] > 0).sum():>5}")
    print(f"  近兩年評鑑部分指標未通過      {int(out['eval_recent_partial'].sum()):>5}")
    print(f"  合計列入複查                  {int(out['flagged'].sum()):>5}")

    print("\n=== 證據層級分布 ===")
    for tier, n in out["evidence_tier"].value_counts().items():
        print(f"  {tier:<24} {n:>5}")

    print("\n=== 依機構類型（前 100 名的組成）===")
    top = out.head(TOP_K)
    for t, n in top["type"].value_counts().items():
        total = int((out["type"] == t).sum())
        print(f"  {t:<4} {n:>3} / 全市 {total:<5} ({n / total * 100:.1f}%)")

    print("\n=== 前 15 名 ===")
    for r in out.head(15).itertuples(index=False):
        print(
            f"  #{r.priority_rank_overall:<3} {r.priority_score:.3f}  "
            f"{r.type} {r.title[:26]:<26} {r.review_reason}"
        )

    print(
        "\n⚠️ 本表是**建議查核的優先序**，不是違法認定。"
        "\n   排在後段只代表現有資料未提出疑問；全市 94.8% 的園沒有公開財報，"
        "\n   對這些園而言那是我們的涵蓋範圍限制，不是它們的合規證明。"
    )


if __name__ == "__main__":
    main()
