"""Assemble the audit priority list -- the system's actual output.

Everything else in this repo produces evidence; this module turns it into the one
artifact an inspector uses: an ordered list of 園 to look at, where every row
carries *why* it is there.

Three design decisions, each of which the data forced:

**1. The two tracks are not blended into one number.**
軌 A (registry, penalty history, operating profile) covers all 1,149 physical 園.
軌 B (compliance checks against each report's own 附註二) covers 60 -- the 38
非營利園 and 22 公立園 that file public financial statements. 私立園, which are 75%
of the market and carry the highest penalty rate (51.6% vs 22.2% / 8.6%), file
none. Folding a compliance failure into the statistical score would make the
score incomparable between a 園 we could check and a 園 we could not, and it
would mix two different kinds of claim: a fitted probability versus a citable
question about a filed document. So the score orders, and compliance findings
escalate independently.

**2. Missing evidence never lowers a score.**
``financial_data_available`` and ``evidence_tier`` are explicit columns, and
``data_sufficiency`` says 資料不足 rather than letting a thin row drift to the
bottom of the list looking safe. This is the 輸出定位 rule in CLAUDE.md applied
to the ranking itself: 不確定時標「資料不足」而非「低風險」.

**3. The fitted model is exactly the configuration that was measured.**
``PRIORITY_FEATURES`` is block ③ from scripts/baseline_model.py, whose honest
time-split performance is AUC 0.641 / P@100 2.17x (docs/research/
05-phase1-results.md §3). Signals we carry but did *not* validate -- operator
sibling risk, recent official events -- travel as context columns, never as
score inputs. Adding an unmeasured feature to the score would silently invalidate
the only performance number we can defend.

A high position on this list means **建議查核**, never 疑似不法.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Block ③ of scripts/baseline_model.py: the configuration whose AUC we measured.
# Do not extend this list without re-running that script -- the reported
# performance belongs to this exact feature set.
#
# Two of these are usually *not* available in practice. The vehicle snapshot is
# pinned and dated (retrieved_on 2026-08-10), and build.vehicle_features refuses
# to describe a fleet at any as_of earlier than the snapshot, because txn_name is
# a latest-known state with no transaction date and would leak the present into
# the past. Every fit window with complete labels predates the snapshot, so
# n_vehicles/has_vehicle arrive all-NaN and drop out -- which is also true of the
# window where AUC 0.640 was measured, so the quoted number and the deployed
# model do describe the same feature set. usable_features() makes that explicit
# instead of letting SimpleImputer discard the columns silently.
PRIORITY_FEATURES = [
    "n_penalties_prior", "sum_severity_prior", "max_severity_prior",
    "n_severe_prior", "days_since_last_penalty",
    "count_approved", "age_years", "size_in", "indoor_area_per_child",
    "n_vehicles", "has_vehicle", "after_care", "monthly", "is_private",
]

# A 園 with no penalty history has no "days since last penalty". A large finite
# sentinel keeps it ordered at the low-risk extreme instead of being imputed to
# the mean of 園 that *have* been penalised, which would invent a history for it.
NEVER_PENALISED_DAYS = 9999

# Event types that warrant a look on their own, independent of the score.
# Deliberately narrow: a 裁罰 or an enrolment sanction is a concrete official act,
# whereas an evaluation result or a contract award is context.
ESCALATING_EVENT_TYPES = frozenset({
    "fine", "stop_enrollment", "reduced_enrollment",
    "permit_revoked", "operation_suspended",
})

RECENT_DAYS = 90
YEAR_DAYS = 365


def usable_features(frame: pd.DataFrame) -> list[str]:
    """The priority features that actually carry data in this frame.

    A feature that is entirely missing cannot be imputed, and SimpleImputer drops
    it without telling the caller. Selecting explicitly means the fitted model and
    the scored frame always agree on their columns, and the script can report what
    it really used.
    """
    return [f for f in PRIORITY_FEATURES if frame[f].notna().any()]


def fit_priority_model(frame: pd.DataFrame, labels: pd.Series, features: list[str]):
    """Fit the ranking model on a fully-observed label window.

    Imported lazily so that importing this module does not require scikit-learn;
    only the deployment script needs it.
    """
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    model = make_pipeline(
        SimpleImputer(strategy="median"),
        StandardScaler(),
        LogisticRegression(max_iter=1000),
    )
    model.fit(frame[features].astype(float), labels)
    return model


def prepare_frame(features: pd.DataFrame) -> pd.DataFrame:
    """Add the derived columns the model expects, without touching the originals."""
    d = features.copy()
    d["is_private"] = (d["type"] == "私立").astype(int)
    d["days_since_last_penalty"] = d["days_since_last_penalty"].fillna(
        NEVER_PENALISED_DAYS
    )
    return d


def compliance_summary(findings: pd.DataFrame, crosswalk: pd.DataFrame) -> pd.DataFrame:
    """Per-institution compliance状態, joined through the report/registry crosswalk.

    The crosswalk maps a report (N01_110) to the registry UUIDs of the 園 it
    belongs to. A 園 whose contract was re-tendered keeps two registry rows, one
    per 受託法人, and 27 of the 132 reports match both -- but only one of those
    法人 actually filed the report. Attaching the finding to both tells an
    inspector that 社團法人中華巧耕多元教育發展協會 has 三多's 2,000,000 reserve
    shortfall, when every 三多 report from 110 to 113 names 廣亞育達科技大學 as
    its 受託法人. So the operator the report itself names decides, via the
    crosswalk's ``operator_matched_ids``; ``registry_ids`` is the fallback for
    the rare report whose operator cannot be resolved, because silently dropping
    a finding is worse than attaching it broadly.
    """
    import json

    failed = findings[findings["passed"].astype(str) == "False"]
    per_report: dict[tuple[str, str], dict] = {}
    for (code, year), grp in failed.groupby(["code", "academic_year"]):
        high = int((grp["severity"] == "high").sum())
        top = grp.sort_values("severity").iloc[0]
        per_report[(str(code), str(year))] = {
            "failed_total": len(grp),
            "failed_high": high,
            "top_rule": top["rule"],
            "top_detail": str(top["detail"])[:160],
        }

    checked = {
        (str(c), str(y)) for c, y in
        findings[["code", "academic_year"]].drop_duplicates().itertuples(index=False)
    }

    rows: list[dict] = []
    for r in crosswalk.itertuples(index=False):
        key = (str(r.code), str(r.academic_year))
        if key not in checked:
            continue
        summary = per_report.get(key)
        matched = json.loads(getattr(r, "operator_matched_ids", "") or "[]")
        targets = matched or json.loads(r.registry_ids or "[]")
        rows.extend(
            {
                "id": rid,
                "academic_year": int(r.academic_year),
                "failed_total": summary["failed_total"] if summary else 0,
                "failed_high": summary["failed_high"] if summary else 0,
                "top_rule": summary["top_rule"] if summary else "",
                "top_detail": summary["top_detail"] if summary else "",
            }
            for rid in targets
        )
    if not rows:
        return pd.DataFrame(
            columns=[
                "id", "compliance_years_checked", "compliance_latest_year",
                "compliance_failed_total", "compliance_failed_high",
                "compliance_top_finding",
            ]
        )

    d = pd.DataFrame(rows)
    agg = d.groupby("id").agg(
        compliance_years_checked=("academic_year", "nunique"),
        compliance_latest_year=("academic_year", "max"),
        compliance_failed_total=("failed_total", "sum"),
        compliance_failed_high=("failed_high", "sum"),
    )
    # Counts span every checked year, so the quoted finding must come from the
    # most recent year that actually *has* one. Taking the latest year outright
    # leaves a 園 whose newest report passed showing a non-zero failure count
    # beside an empty finding -- which reads as a missing value rather than as
    # "the failure was in an earlier year".
    failed_only = d[d["top_rule"].astype(bool)]
    newest_failure = (
        failed_only.sort_values("academic_year").groupby("id").tail(1)
        .set_index("id")
    )
    agg["compliance_top_finding"] = [
        f"{newest_failure.loc[i, 'academic_year']} 學年度 "
        f"{newest_failure.loc[i, 'top_rule']}：{newest_failure.loc[i, 'top_detail']}"
        if i in newest_failure.index else ""
        for i in agg.index
    ]
    return agg.reset_index()


def event_summary(events: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """Recent-official-event counts per institution.

    Kept out of the score on purpose (see module docstring): these counts have
    never been tested against a held-out label window, so they inform a human
    rather than move a number.
    """
    d = events.copy()
    d["event_date"] = pd.to_datetime(d["event_date"], errors="coerce")
    d = d[d["event_date"].notna() & (d["event_date"] <= as_of)]
    d = d[d["source_registry_id"].notna() & (d["source_registry_id"] != "")]

    esc = d[d["event_type"].isin(ESCALATING_EVENT_TYPES)]
    recent = esc[esc["event_date"] >= as_of - pd.Timedelta(days=RECENT_DAYS)]
    year = esc[esc["event_date"] >= as_of - pd.Timedelta(days=YEAR_DAYS)]

    latest = (
        d.sort_values("event_date").groupby("source_registry_id").tail(1)
        .set_index("source_registry_id")
    )
    out = pd.DataFrame({
        "events_90d": recent.groupby("source_registry_id").size(),
        "events_365d": year.groupby("source_registry_id").size(),
    })
    out = out.reindex(latest.index).fillna(0).astype(int)
    out["latest_event_type"] = latest["event_type"]
    out["latest_event_date"] = latest["event_date"].dt.date.astype(str)
    return out.reset_index().rename(columns={"source_registry_id": "id"})


def evaluation_summary(evaluations, titles, as_of) -> pd.DataFrame:
    """Latest official evaluation per 園, for the escalation channel.

    Kept out of the fitted score deliberately (see scripts/baseline_model.py
    block ④): evaluations arrive on a rolling cycle, so a fixed snapshot has the
    feature missing for most 園 and adding it lowered AUC. As an event trigger it
    is the strongest thing we have for the population the forensic track cannot
    reach -- 私立園 file no financial statements but 98% of them are evaluated,
    and among 園 with no penalty history a 部分指標通過 result carries OR 2.44.
    """
    import pandas as pd

    seen = evaluations[evaluations["date"] < as_of]
    latest = seen.sort_values("date").groupby("title").tail(1).set_index("title")
    out = pd.DataFrame({"title": pd.Series(titles).unique()}).set_index("title")
    out["eval_result"] = latest["evaluation_result"].reindex(out.index)
    out["eval_year"] = latest["evaluation_academic_year"].reindex(out.index)
    out["eval_partial"] = latest["partial"].reindex(out.index).fillna(0).astype(int)
    out["eval_date"] = latest["date"].reindex(out.index).dt.date.astype(str)
    out["days_since_evaluation"] = (as_of - latest["date"].reindex(out.index)).dt.days
    # A recent adverse result is the actionable window; an old one is history.
    out["eval_recent_partial"] = (
        (out["eval_partial"] == 1) & (out["days_since_evaluation"] <= 730)
    ).astype(int)
    return out.reset_index()


def assemble(
    features: pd.DataFrame,
    model,
    model_features: list[str],
    compliance: pd.DataFrame,
    events: pd.DataFrame,
    fee_ids: set[str],
    evaluations: pd.DataFrame | None = None,
    top_k: int = 100,
) -> pd.DataFrame:
    """Join score, evidence tier and escalation flags into the final list.

    ``model_features`` must be the exact list the model was fitted on; passing the
    full PRIORITY_FEATURES here would score a differently-shaped matrix whenever a
    feature was unavailable at fit time.
    """
    d = prepare_frame(features)
    d["priority_score"] = model.predict_proba(
        d[model_features].astype(float)
    )[:, 1]
    assert np.isfinite(d["priority_score"]).all(), "分數出現非有限值"

    d = d.merge(compliance, on="id", how="left")
    d = d.merge(events, on="id", how="left")
    if evaluations is not None:
        d = d.merge(evaluations, on="title", how="left")
        d["eval_partial"] = d["eval_partial"].fillna(0).astype(int)
        d["eval_recent_partial"] = d["eval_recent_partial"].fillna(0).astype(int)
        d["eval_result"] = d["eval_result"].fillna("")
        d["eval_date"] = d["eval_date"].fillna("")
    else:
        for c in ("eval_partial", "eval_recent_partial"):
            d[c] = 0
        for c in ("eval_result", "eval_date"):
            d[c] = ""
    for col in [
        "compliance_years_checked", "compliance_failed_total",
        "compliance_failed_high", "events_90d", "events_365d",
    ]:
        # to_numeric first: a left join against an empty summary yields an object
        # column, and fillna on object dtype is deprecated downcasting in pandas.
        d[col] = pd.to_numeric(d[col], errors="coerce").fillna(0).astype(int)

    d["financial_data_available"] = (d["compliance_years_checked"] > 0).astype(int)
    d["has_fee_schedule"] = d["title"].isin(fee_ids).astype(int)
    d["evidence_tier"] = np.where(
        d["financial_data_available"] == 1, "登記+裁罰+收費+財報",
        np.where(d["has_fee_schedule"] == 1, "登記+裁罰+收費", "登記+裁罰"),
    )
    # An inspector reading a low row must not conclude the 園 is clean; say so in
    # the data, not only in the documentation.
    d["data_sufficiency"] = np.where(
        d["financial_data_available"] == 1, "可做財務法遵檢核",
        "無公開財報，僅能就登記與裁罰資料排序（資料不足，非低風險）",
    )

    d = d.sort_values("priority_score", ascending=False).reset_index(drop=True)
    d["priority_rank_overall"] = np.arange(1, len(d) + 1)
    d["priority_rank_within_type"] = (
        d.groupby("type")["priority_score"].rank(ascending=False, method="first")
        .astype(int)
    )
    d["priority_decile"] = pd.qcut(
        d["priority_score"].rank(method="first"), 10, labels=range(1, 11)
    ).astype(int)

    reasons = []
    for r in d.itertuples(index=False):
        why = []
        if r.compliance_failed_high > 0:
            why.append("財報法遵未通過(高)")
        elif r.compliance_failed_total > 0:
            why.append("財報法遵未通過")
        if r.eval_recent_partial:
            why.append("近兩年評鑑部分指標未通過")
        if r.events_90d > 0:
            why.append(f"近{RECENT_DAYS}日official事件")
        if r.priority_rank_overall <= top_k:
            why.append(f"分數前{top_k}")
        reasons.append("；".join(why))
    d["review_reason"] = reasons
    d["flagged"] = (d["review_reason"] != "").astype(int)
    return d
