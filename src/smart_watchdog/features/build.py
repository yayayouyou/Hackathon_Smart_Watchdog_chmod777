"""Assemble the institution-level feature table for the breadth model (軌 A).

Everything here is derivable without OCR, so the whole table can be rebuilt from
the registry snapshot in seconds. The financial forensics features (軌 B) live
separately because they only exist for the ~59 公共化園.

The central design constraint is **temporal honesty**. Prior penalties are the
strongest single predictor, so a random train/test split leaks an institution's
future penalties into its own training features and produces a meaninglessly high
AUC. Every function here takes an ``as_of`` date and refuses to look past it.
"""

from __future__ import annotations

import json
import pathlib

import pandas as pd

from smart_watchdog.scrape import registry

# Article -> (行為類型, 嚴重度 1-5). Severity is ordered by harm to children, not
# by fine amount: the median fine is NT$9,000 whether the violation is 不當對待 or
# a filing lapse, so money is a poor severity proxy.
ARTICLE_TAXONOMY: dict[int, tuple[str, int]] = {
    33: ("不當對待幼兒", 5),
    30: ("安全管理規範未落實", 4),
    32: ("安全事件", 4),
    12: ("教保服務禁止規定", 4),
    16: ("教保人員進用/資格", 3),
    17: ("未辦理停聘", 3),
    29: ("負責人/董監事資格", 3),
    26: ("設施設備", 3),
    31: ("幼童專用車", 3),
    8: ("設立許可/負責人", 2),
    15: ("教職員資料未報備查", 2),
    43: ("收費超收", 2),
    38: ("收費未報備查/超收", 2),
    41: ("其他行政義務", 1),
    42: ("未訂書面契約", 1),
    48: ("課後照顧違規", 1),
}
DEFAULT_SEVERITY = 2

ACTIVE_VEHICLE_TRANSACTIONS = frozenset(
    {
        "本區新領",
        "外區新領",
        "外站新領",
        "外站移入-新領",
        "外區移入-新領",
        "本區繳銷重領",
        "本區註銷重領",
    }
)
INACTIVE_VEHICLE_TRANSACTIONS = frozenset(
    {
        "一般報廢",
        "環保回收轉報廢",
        "繳銷轉報廢",
        "繳銷",
        "註銷轉報廢",
        "停駛轉報廢",
        "停駛轉繳銷",
        "逕行註銷執行",
        "逾檢註銷",
    }
)
SUSPENDED_VEHICLE_TRANSACTIONS = frozenset(
    {"停用報停", "執行條例處分吊扣"}
)


def classify_vehicle_status(txn_name: object) -> str:
    """Map the upstream transaction label without fuzzy substring mistakes."""
    value = str(txn_name or "").strip()
    if value in ACTIVE_VEHICLE_TRANSACTIONS:
        return "active"
    if value in INACTIVE_VEHICLE_TRANSACTIONS:
        return "inactive"
    if value in SUSPENDED_VEHICLE_TRANSACTIONS:
        return "suspended"
    return "unknown"


def severity_of(article: object) -> int:
    """Map a 幼照法 article number to an ordinal severity."""
    try:
        return ARTICLE_TAXONOMY[int(article)][1]
    except (TypeError, ValueError, KeyError):
        return DEFAULT_SEVERITY


def category_of(article: object) -> str:
    try:
        return ARTICLE_TAXONOMY[int(article)][0]
    except (TypeError, ValueError, KeyError):
        return "其他"


def load_penalties(path: pathlib.Path | str) -> pd.DataFrame:
    """Load the canonical penalty table with nullable monetary fines."""
    p = pd.read_csv(path)
    required = {
        "sanction_type",
        "is_monetary",
        "actor_role",
        "source_row_count",
        "penalty_group_id",
    }
    missing = sorted(required - set(p.columns))
    if missing:
        raise ValueError(
            f"penalty table is not canonical (missing {missing}); rebuild it from "
            "the pinned snapshots instead of inferring legacy zero-fine semantics"
        )
    p["date"] = pd.to_datetime(p["date"], format="%Y/%m/%d", errors="coerce")
    p["fine"] = pd.to_numeric(p["fine"], errors="coerce").astype("Int64")
    p["is_monetary"] = pd.to_numeric(p["is_monetary"], errors="raise").astype(int)
    expected_monetary = p["fine"].notna().astype(int)
    if not p["is_monetary"].equals(expected_monetary):
        raise ValueError("penalty fine/is_monetary fields disagree")
    if p["sanction_type"].eq("other").any():
        raise ValueError("penalty table contains an unclassified sanction")
    p["year"] = p["date"].dt.year
    p["severity"] = p["article"].map(severity_of)
    p["category"] = p["article"].map(category_of)
    return p


def load_vehicles(path: pathlib.Path | str) -> pd.DataFrame:
    """Flatten kids_vehicles.json into one row per vehicle.

    ``next_exam_dt`` is deliberately **not** turned into an "inspection overdue"
    feature. In the current snapshot every 新北市 vehicle's exam date is in the
    past (the most recent is ~2025-04, minimum 478 days stale), because the
    upstream vehicle feed stopped refreshing -- not because 332 kindergartens are
    all driving uninspected buses. A red flag built on it would fire on 100% of
    vehicle-operating 園. Only possession, count, and build date are usable.
    """
    snapshot_path = pathlib.Path(path)
    manifest_entry = registry.verify_snapshot(snapshot_path)
    raw = json.loads(snapshot_path.read_text(encoding="utf-8"))
    rows = [
        {
            "id": preschool_id,
            "plate_no": car.get("plate_no"),
            "on_production_date": car.get("on_production_date"),
            "next_exam_dt": car.get("next_exam_dt"),
            "txn_name": str(car.get("txn_name") or "").strip(),
        }
        for preschool_id, cars in raw.items()
        for car in cars
    ]
    v = pd.DataFrame(rows)
    v["vehicle_status"] = v["txn_name"].map(classify_vehicle_status)
    unknown = sorted(v.loc[v["vehicle_status"] == "unknown", "txn_name"].unique())
    if unknown:
        raise ValueError(f"unclassified vehicle transaction status: {unknown}")
    v["is_active_vehicle"] = v["vehicle_status"] == "active"
    v["built"] = pd.to_datetime(v["on_production_date"], format="%Y%m", errors="coerce")
    retrieved_on = manifest_entry.get("retrieved_on")
    v.attrs["snapshot_retrieved_on"] = (
        pd.Timestamp(retrieved_on) if retrieved_on else None
    )
    return v


def vehicle_features(vehicles: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """Count active vehicles and age the oldest active vehicle at ``as_of``.

    ``txn_name`` is a latest-known registry state without a transaction date, so
    it cannot reconstruct historical fleet membership. Status-aware features are
    therefore unavailable when ``as_of`` predates the pinned snapshot date. For
    current/future analysis, the filter removes vehicles explicitly reported as
    scrapped, cancelled, suspended, or stopped.

    Vehicles built after ``as_of`` are excluded to prevent a direct future leak.
    Unparseable build dates remain in the active count but not the age metric.
    """
    required = {"id", "plate_no", "built", "vehicle_status"}
    missing = sorted(required - set(vehicles.columns))
    if missing:
        raise ValueError(f"vehicle table missing status-aware columns: {missing}")

    snapshot_retrieved_on = vehicles.attrs.get("snapshot_retrieved_on")
    snapshot_usable = bool(
        snapshot_retrieved_on is not None
        and as_of.normalize() >= pd.Timestamp(snapshot_retrieved_on).normalize()
    )
    if not snapshot_usable:
        unavailable = pd.DataFrame(
            columns=["id", "n_vehicles", "oldest_vehicle_age_yr"]
        )
        unavailable.attrs["snapshot_usable"] = False
        unavailable.attrs["snapshot_retrieved_on"] = snapshot_retrieved_on
        return unavailable

    vehicles = vehicles[vehicles["vehicle_status"] == "active"]
    known = vehicles["built"].notna()
    vehicles = vehicles[~known | (vehicles["built"] <= as_of)]
    if vehicles.empty:
        empty = pd.DataFrame(columns=["id", "n_vehicles", "oldest_vehicle_age_yr"])
        empty.attrs["snapshot_usable"] = True
        empty.attrs["snapshot_retrieved_on"] = snapshot_retrieved_on
        return empty
    g = vehicles.groupby("id").agg(
        n_vehicles=("plate_no", "size"),
        oldest_built=("built", "min"),
    )
    age_days = (as_of - g["oldest_built"]).dt.days
    g["oldest_vehicle_age_yr"] = (age_days / 365.25).round(2)
    result = g.drop(columns=["oldest_built"]).reset_index()
    result.attrs["snapshot_usable"] = True
    result.attrs["snapshot_retrieved_on"] = snapshot_retrieved_on
    return result


def load_evaluations(path: pathlib.Path | str) -> pd.DataFrame:
    """官方評鑑紀錄，一列一次評鑑。

    Why this source earns a place next to penalty history: financial statements
    reach 5.2% of 園 and none of the 私立園, but evaluations reach 98% of both
    公立 and 私立. Measured on this corpus, an evaluation ending 部分指標通過 is
    followed by a penalty within a year in 17.9% of cases versus 7.4% for
    全數指標通過 (OR 2.72, Fisher p=0.0009), and the association survives
    stratification by 設立別 (私立 OR 2.31) and by prior-penalty status
    (無前科 OR 2.44) -- the group the whole project exists for, since 56% of
    future violators have no record. The subsequent penalties are spread across
    the year (median 132 days) over varied articles, so they are not the
    evaluation's own follow-up processing.
    """
    ev = pd.read_csv(path)
    ev["date"] = pd.to_datetime(ev["evaluation_completed_date"], errors="coerce")
    ev["partial"] = ev["evaluation_result"].str.contains("部分指標", na=False).astype(int)
    ev["sanctioned"] = ev["evaluation_result"].str.contains("行政處分", na=False).astype(int)
    return ev[ev["date"].notna()]


def evaluation_features(
    evaluations: pd.DataFrame, titles: pd.Series, as_of: pd.Timestamp
) -> pd.DataFrame:
    """Latest evaluation known at ``as_of``, joined on the official 園名.

    Only evaluations completed strictly before ``as_of`` are visible, the same
    rule every other feature here follows. A 園 with no evaluation yet gets
    ``has_evaluation = 0`` and NaN elsewhere rather than a zero that would read
    as "evaluated and clean".
    """
    seen = evaluations[evaluations["date"] < as_of]
    latest = seen.sort_values("date").groupby("title").tail(1).set_index("title")
    agg = seen.groupby("title").agg(
        n_evaluations_prior=("partial", "size"),
        n_partial_prior=("partial", "sum"),
    )
    out = pd.DataFrame({"title": titles.unique()}).set_index("title")
    out["has_evaluation"] = out.index.isin(latest.index).astype(int)
    out["eval_partial"] = latest["partial"].reindex(out.index)
    out["eval_sanctioned"] = latest["sanctioned"].reindex(out.index)
    out["n_evaluations_prior"] = agg["n_evaluations_prior"].reindex(out.index)
    out["n_partial_prior"] = agg["n_partial_prior"].reindex(out.index)
    days = (as_of - latest["date"].reindex(out.index)).dt.days
    out["days_since_evaluation"] = days
    return out.reset_index()


def penalty_history(
    penalties: pd.DataFrame, as_of: pd.Timestamp, lookback_years: int | None = None
) -> pd.DataFrame:
    """Per-institution penalty history **strictly before** ``as_of``.

    ``lookback_years`` optionally windows the history, which matters because a
    2018 filing lapse says less about today than a 2023 one does.
    """
    past = penalties[penalties["date"] < as_of]
    if lookback_years is not None:
        past = past[past["date"] >= as_of - pd.DateOffset(years=lookback_years)]
    if past.empty:
        return pd.DataFrame(
            columns=[
                "id", "n_penalties_prior", "total_fine_prior",
                "n_nonmonetary_prior", "n_responsible_party_prior",
                "n_individual_actor_prior", "max_severity_prior",
                "sum_severity_prior", "n_severe_prior",
                "days_since_last_penalty",
            ]
        )
    g = past.groupby("id").agg(
        n_penalties_prior=("id", "size"),
        total_fine_prior=("fine", "sum"),
        max_severity_prior=("severity", "max"),
        sum_severity_prior=("severity", "sum"),
        last_penalty=("date", "max"),
    )
    g["n_nonmonetary_prior"] = (
        past["fine"].isna().groupby(past["id"]).sum().reindex(g.index, fill_value=0)
    )
    g["n_responsible_party_prior"] = (
        past["actor_role"].eq("負責人")
        .groupby(past["id"])
        .sum()
        .reindex(g.index, fill_value=0)
    )
    g["n_individual_actor_prior"] = (
        past["actor_role"].eq("行為人")
        .groupby(past["id"])
        .sum()
        .reindex(g.index, fill_value=0)
    )
    g["n_severe_prior"] = (
        past[past["severity"] >= 4].groupby("id").size().reindex(g.index, fill_value=0)
    )
    g["days_since_last_penalty"] = (as_of - g["last_penalty"]).dt.days
    return g.drop(columns=["last_penalty"]).reset_index()


def operator_risk(
    institutions: pd.DataFrame,
    penalties: pd.DataFrame,
    as_of: pd.Timestamp,
) -> pd.DataFrame:
    """Leave-one-out 受託法人 risk, computed only from penalties before ``as_of``.

    Two correctness requirements, both easy to get wrong:

    1. **Leave-one-out.** An institution must not contribute to its own operator's
       risk score, or the feature smuggles the institution's own penalty history
       back in under a different name.
    2. **Role-aware.** Only ``負責人`` sanctions feed operator spillover;
       penalties issued personally to an individual ``行為人`` remain available
       in institution history but are not attributed to sibling parks.
    3. **Operator identity is de-duplicated by 園.** The registry carries multiple
       entries for a 園 whose 受託法人 changed between contract periods (北大, 新林,
       安興 each appear under two operators), so a naive count double-counts.
    """
    past = penalties[
        (penalties["date"] < as_of) & penalties["actor_role"].eq("負責人")
    ]
    counts = past.groupby("id").size()

    has_op = institutions[institutions["operator"].notna() & (institutions["operator"] != "")]
    # One row per (operator, 園) so a re-tendered 園 is counted once per operator.
    pairs = has_op[["operator", "parent", "id"]].drop_duplicates(["operator", "parent"])
    pairs = pairs.assign(n=pairs["id"].map(counts).fillna(0))

    totals = pairs.groupby("operator").agg(
        op_parks=("parent", "nunique"), op_penalties=("n", "sum")
    )
    out = []
    for _, row in has_op.iterrows():
        op = row["operator"]
        t = totals.loc[op]
        own = counts.get(row["id"], 0)
        # Leave this 園 out of both numerator and denominator.
        parks = max(int(t["op_parks"]) - 1, 0)
        pens = float(t["op_penalties"]) - own
        out.append(
            {
                "id": row["id"],
                "op_sibling_parks": parks,
                "op_sibling_penalties": pens,
                "op_sibling_penalty_rate": (pens / parks) if parks else None,
            }
        )
    return pd.DataFrame(out)


def build_features(
    institutions: pd.DataFrame,
    penalties: pd.DataFrame,
    vehicles: pd.DataFrame,
    as_of: pd.Timestamp,
    evaluations: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """One row per institution, using only information available at ``as_of``."""
    d = institutions.copy()

    reg = pd.to_datetime(d["reg_date"], format="%Y/%m/%d", errors="coerce")
    d["age_years"] = ((as_of - reg).dt.days / 365.25).round(2)
    # A 園 registered after as_of did not exist yet; NaN keeps it out of the model.
    d.loc[reg > as_of, "age_years"] = None

    d = d.merge(penalty_history(penalties, as_of), on="id", how="left")
    vehicle_table = vehicle_features(vehicles, as_of)
    vehicle_snapshot_usable = bool(vehicle_table.attrs.get("snapshot_usable"))
    vehicle_snapshot_date = vehicle_table.attrs.get("snapshot_retrieved_on")
    d = d.merge(vehicle_table, on="id", how="left")
    d = d.merge(operator_risk(d, penalties, as_of), on="id", how="left")
    if evaluations is not None:
        d = d.merge(evaluation_features(evaluations, d["title"], as_of),
                    on="title", how="left")
        d["has_evaluation"] = d["has_evaluation"].fillna(0).astype(int)
        for col in ("n_evaluations_prior", "n_partial_prior"):
            d[col] = d[col].fillna(0)

    for col in [
        "n_penalties_prior", "total_fine_prior", "n_nonmonetary_prior",
        "n_responsible_party_prior", "n_individual_actor_prior",
        "max_severity_prior", "sum_severity_prior", "n_severe_prior",
    ]:
        d[col] = d[col].fillna(0)
    d["vehicle_snapshot_usable"] = int(vehicle_snapshot_usable)
    d["vehicle_snapshot_retrieved_on"] = vehicle_snapshot_date
    if vehicle_snapshot_usable:
        d["n_vehicles"] = d["n_vehicles"].fillna(0)
        d["has_vehicle"] = (d["n_vehicles"] > 0).astype(int)
    else:
        d["n_vehicles"] = float("nan")
        d["oldest_vehicle_age_yr"] = float("nan")
        d["has_vehicle"] = pd.Series(pd.NA, index=d.index, dtype="Int64")
    d["has_prior_penalty"] = (d["n_penalties_prior"] > 0).astype(int)
    return d


def label_future_penalty(
    penalties: pd.DataFrame,
    ids: pd.Series,
    start: pd.Timestamp,
    end: pd.Timestamp,
    min_severity: int = 1,
    actor_roles: set[str] | None = None,
) -> pd.Series:
    """Label any selected actor-role penalty in ``[start, end)``.

    The default deliberately includes both institution ``負責人`` sanctions and
    personal ``行為人`` sanctions. Callers can pass an explicit role set when the
    prediction target requires a narrower legal interpretation.
    """
    window = penalties[
        (penalties["date"] >= start)
        & (penalties["date"] < end)
        & (penalties["severity"] >= min_severity)
    ]
    if actor_roles is not None:
        window = window[window["actor_role"].isin(actor_roles)]
    hit = set(window["id"])
    return ids.isin(hit).astype(int)
