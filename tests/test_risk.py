"""Invariants of the audit priority list.

These are not tests of "does it run" -- they pin the three decisions that make
the output defensible, each of which is easy to undo by accident:

1. a feature that is missing at fit time must be dropped *explicitly*, so the
   fitted model and the scored frame never disagree about their columns;
2. missing financial data must never read as low risk;
3. compliance failures must escalate a 園 into review regardless of its score,
   because they are a different kind of claim from a fitted probability.
"""

from __future__ import annotations

import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.risk.priority import (
    PRIORITY_FEATURES,
    assemble,
    compliance_summary,
    event_summary,
    prepare_frame,
    usable_features,
)


def _frame(n: int = 4) -> pd.DataFrame:
    return pd.DataFrame({
        "id": [f"id{i}" for i in range(n)],
        "title": [f"園{i}" for i in range(n)],
        "entity": [f"園{i}" for i in range(n)],
        "type": ["私立", "非營利", "公立", "私立"][:n],
        "town": ["板橋"] * n,
        "n_penalties_prior": [0, 1, 0, 3],
        "sum_severity_prior": [0, 2, 0, 6],
        "max_severity_prior": [0, 2, 0, 3],
        "n_severe_prior": [0, 0, 0, 1],
        "days_since_last_penalty": [None, 100.0, None, 30.0],
        "count_approved": [60, 90, 120, 75],
        "age_years": [3.0, 10.0, 20.0, 5.0],
        "size_in": [200.0, 300.0, 400.0, 250.0],
        "indoor_area_per_child": [3.3, 3.3, 3.3, 3.3],
        "n_vehicles": [np.nan] * n,      # snapshot unusable at this as_of
        "has_vehicle": [np.nan] * n,
        "after_care": [0, 1, 0, 1],
        "monthly": [12000, 3000, 3000, 11000],
    })


class _StubModel:
    """Scores by column count so a silent column mismatch surfaces as an error."""

    def __init__(self, expected: int) -> None:
        self.expected = expected

    def predict_proba(self, x):
        assert x.shape[1] == self.expected, "傳入模型的欄位數與配適時不符"
        return np.column_stack([1 - np.linspace(0.1, 0.9, len(x)),
                                np.linspace(0.1, 0.9, len(x))])


def test_all_missing_feature_is_dropped_explicitly():
    """車輛特徵在配適窗全空時必須被明確排除，而不是交給 imputer 靜默丟棄。"""
    feats = usable_features(prepare_frame(_frame()))
    assert "n_vehicles" not in feats
    assert "has_vehicle" not in feats
    assert len(feats) == len(PRIORITY_FEATURES) - 2


def test_never_penalised_keeps_a_sentinel_not_an_imputed_history():
    """沒有裁罰史的園不能被補成「有裁罰史者的平均間隔」——那是憑空造出前科。"""
    d = prepare_frame(_frame())
    assert d.loc[0, "days_since_last_penalty"] == 9999
    assert d.loc[1, "days_since_last_penalty"] == 100.0


def _assembled(compliance=None, events=None):
    frame = prepare_frame(_frame())
    feats = usable_features(frame)
    empty_c = pd.DataFrame(columns=[
        "id", "compliance_years_checked", "compliance_latest_year",
        "compliance_failed_total", "compliance_failed_high",
        "compliance_top_finding",
    ])
    empty_e = pd.DataFrame(columns=[
        "id", "events_90d", "events_365d", "latest_event_type",
        "latest_event_date",
    ])
    return assemble(
        frame, _StubModel(len(feats)), feats,
        compliance if compliance is not None else empty_c,
        events if events is not None else empty_e,
        fee_ids=set(), top_k=1,
    )


def test_no_financial_data_is_labelled_insufficient_not_low_risk():
    """CLAUDE.md 的輸出定位規則落到欄位上：不確定要標「資料不足」，不是低風險。"""
    out = _assembled()
    assert (out["financial_data_available"] == 0).all()
    assert out["data_sufficiency"].str.contains("資料不足，非低風險").all()


def test_compliance_failure_escalates_regardless_of_score():
    """財報法遵未通過的園必須進複查名單，即使分數排在最後。

    分數與法遵發現是兩種不同的主張：一個是統計預測，一個是對已申報文件的
    可引用質疑。後者不該因為前者低就被埋掉。
    """
    lowest = _assembled().tail(1)["id"].item()
    compliance = pd.DataFrame([{
        "id": lowest, "compliance_years_checked": 2,
        "compliance_latest_year": 113, "compliance_failed_total": 3,
        "compliance_failed_high": 1, "compliance_top_finding": "業務發展準備金 資產=負債：…",
    }])
    out = _assembled(compliance=compliance)
    row = out[out["id"] == lowest].iloc[0]
    assert row["flagged"] == 1
    assert "財報法遵未通過(高)" in row["review_reason"]
    assert row["financial_data_available"] == 1


def test_recent_event_counts_only_escalating_types():
    """評鑑結果與決標公告是脈絡，不是「近期需查看」的理由。"""
    events = pd.DataFrame([
        {"source_registry_id": "id0", "event_type": "fine",
         "event_date": "2026-08-20"},
        {"source_registry_id": "id1", "event_type": "official_evaluation_result",
         "event_date": "2026-08-20"},
    ])
    summary = event_summary(events, pd.Timestamp("2026-09-01"))
    by_id = summary.set_index("id")
    assert by_id.loc["id0", "events_90d"] == 1
    assert by_id.loc["id1", "events_90d"] == 0


def test_events_after_as_of_are_excluded():
    """as_of 之後的事件不得進入該時點的特徵——時序切分的基本要求。"""
    events = pd.DataFrame([
        {"source_registry_id": "id0", "event_type": "fine",
         "event_date": "2026-12-01"},
    ])
    summary = event_summary(events, pd.Timestamp("2026-09-01"))
    assert summary.empty or summary["events_90d"].sum() == 0


def test_compliance_summary_maps_reports_to_every_registry_row():
    """一份報告可能對到多筆登記（契約更替），每一筆都要拿到該發現。"""
    findings = pd.DataFrame([{
        "code": "N01", "academic_year": 113, "rule": "業務發展準備金 資產=負債",
        "passed": "False", "severity": "high", "detail": "資產 x vs 負債 y",
    }])
    crosswalk = pd.DataFrame([{
        "code": "N01", "academic_year": 113,
        "registry_ids": '["uuid-a", "uuid-b"]',
    }])
    out = compliance_summary(findings, crosswalk)
    assert set(out["id"]) == {"uuid-a", "uuid-b"}
    assert (out["compliance_failed_high"] == 1).all()


def test_quoted_finding_comes_from_the_newest_year_that_has_one():
    """計數涵蓋所有年度，引用的發現就必須來自「最近一個真的有發現的年度」。

    大觀 113 學年度通過、112 未通過。若直接取最新年度的發現，這一列會出現
    「未通過 1 項」卻沒有發現內容，看起來像資料遺漏，而不是「發現在前一年」。
    """
    findings = pd.DataFrame([
        {"code": "N10", "academic_year": 112, "rule": "業務發展準備金 資產=負債",
         "passed": "False", "severity": "high", "detail": "資產 a vs 負債 b"},
        {"code": "N10", "academic_year": 113, "rule": "業務發展準備金 資產=負債",
         "passed": "True", "severity": "high", "detail": "相符"},
    ])
    crosswalk = pd.DataFrame([
        {"code": "N10", "academic_year": 112, "registry_ids": '["uuid-a"]'},
        {"code": "N10", "academic_year": 113, "registry_ids": '["uuid-a"]'},
    ])
    out = compliance_summary(findings, crosswalk).set_index("id")
    row = out.loc["uuid-a"]
    assert row["compliance_years_checked"] == 2
    assert row["compliance_failed_high"] == 1
    assert row["compliance_top_finding"].startswith("112 學年度")


def test_finding_attaches_only_to_the_operator_that_filed_the_report():
    """契約更替的園有兩個登記，但只有一個受託法人申報了那份報告。

    三多 110–113 每份報告都寫明受託法人為廣亞育達，卻同時被掛到
    社團法人中華巧耕多元教育發展協會身上——等於告訴稽查人員一個沒有申報
    這份報告的法人有 2,000,000 元的專戶短少。
    """
    findings = pd.DataFrame([{
        "code": "N13", "academic_year": 113, "rule": "業務發展準備金 資產=負債",
        "passed": "False", "severity": "high", "detail": "未提撥足額 +2,000,000",
    }])
    crosswalk = pd.DataFrame([{
        "code": "N13", "academic_year": 113,
        "registry_ids": '["uuid-guangya", "uuid-qiaogeng"]',
        "operator_matched_ids": '["uuid-guangya"]',
    }])
    out = compliance_summary(findings, crosswalk)
    assert set(out["id"]) == {"uuid-guangya"}


def test_unresolved_operator_falls_back_to_every_registry_row():
    """無法解析受託法人時寧可掛得寬，也不要靜默丟掉一項發現。"""
    findings = pd.DataFrame([{
        "code": "N99", "academic_year": 113, "rule": "資遣費準備金 資產=負債",
        "passed": "False", "severity": "medium", "detail": "差額 +1,000",
    }])
    crosswalk = pd.DataFrame([{
        "code": "N99", "academic_year": 113,
        "registry_ids": '["uuid-a", "uuid-b"]', "operator_matched_ids": "[]",
    }])
    out = compliance_summary(findings, crosswalk)
    assert set(out["id"]) == {"uuid-a", "uuid-b"}
