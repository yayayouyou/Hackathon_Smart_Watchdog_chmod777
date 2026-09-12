"""Invariants of the peer-relative anomaly scoring.

The scorer produces a list an inspector works down, so the tests here pin the
properties that decide whether that list means anything: that a thin row cannot
look safe by being thin, that a tightly-clustered cohort cannot manufacture
infinities, and that peers are peers.
"""

from __future__ import annotations

import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.features.anomaly import (
    MIN_PEERS,
    REASON_Z,
    Z_CAP,
    mad,
    median,
    percentile_of,
    robust_scale,
    robust_z,
    score_cohort,
)

FEATURES = {"a": "甲比率", "b": "乙比率"}


def cohort(values_a, values_b=None):
    """One 學年度 of rows; None means the feature is absent for that 園."""
    vb = values_b if values_b is not None else values_a
    return [{"code": f"N{i:02d}", "short_name": f"園{i}", "academic_year": "113",
             "a": a, "b": b}
            for i, (a, b) in enumerate(zip(values_a, vb), start=1)]


def test_median_and_mad_ignore_missing() -> None:
    assert median([1.0, None, 3.0, 2.0]) == 2.0
    assert mad([1.0, 2.0, 3.0]) == 1.0
    assert median([]) is None
    assert mad([]) is None


def test_zero_mad_does_not_produce_infinity() -> None:
    """行政管理費執行率 is contractually exactly 1.0 in most reports.

    Every robust scale built from that column alone is zero, and a z-score is not
    a defined quantity however far a given 園 sits from it.
    """
    peers = [1.0] * 20
    assert mad(peers) == 0
    assert robust_scale(peers) is None
    assert robust_z(0.5, peers) is None


def test_tight_middle_uses_the_wider_scale() -> None:
    """A near-zero MAD must not inflate z when the IQR says otherwise."""
    peers = [1.0] * 10 + [0.9, 0.8, 0.7, 0.6]
    m = mad(peers)
    scale = robust_scale(peers)
    assert scale is not None
    assert scale >= 1.4826 * (m or 0), "取 MAD 與 IQR 兩種尺度的較大者"


def test_percentile_is_defined_when_z_is_not() -> None:
    peers = [1.0] * 20
    assert robust_z(0.5, peers) is None
    assert percentile_of(0.5, peers) == 0.0
    assert percentile_of(1.0, peers) == 100.0


def test_missing_feature_is_absent_not_zero() -> None:
    """A 園 with no 附註三 has an unknown agency share, not a zero one.

    A zero would place it at the bottom of that feature's distribution and
    manufacture an anomaly out of a gap in the paperwork.
    """
    rows = cohort([1.0, 1.1, 0.9, 1.05, 0.95, 1.2, 0.8, 1.15, 1.0, 0.85])
    rows[0]["a"] = None
    scored = {s.code: s for s in score_cohort(rows, FEATURES)}
    assert scored["N01"].n_features == 1, "缺值的特徵不計入，而不是當成 0"
    assert all(r.feature != "a" for r in scored["N01"].contributions)


def test_row_with_no_features_scores_none_not_zero() -> None:
    rows = cohort([1.0, 1.1, 0.9, 1.05, 0.95, 1.2, 0.8, 1.15, 1.0, 0.85])
    rows[0]["a"] = rows[0]["b"] = None
    scored = {s.code: s for s in score_cohort(rows, FEATURES)}
    assert scored["N01"].score is None, "沒有任何特徵時不得給 0 分（那會排在最安全端）"
    assert scored["N01"].n_features == 0


def test_thin_cohort_is_not_scored() -> None:
    """Fewer peers than MIN_PEERS means a median and MAD say nothing."""
    rows = cohort([1.0, 5.0, 2.0])
    assert len(rows) < MIN_PEERS
    assert all(s.score is None for s in score_cohort(rows, FEATURES))


def test_outlier_outranks_the_cohort() -> None:
    vals = [1.0, 1.02, 0.98, 1.01, 0.99, 1.03, 0.97, 1.0, 1.01, 5.0]
    scored = sorted(score_cohort(cohort(vals), FEATURES),
                    key=lambda s: -(s.score or -1))
    assert scored[0].code == "N10"
    assert scored[0].contributions, "必須說明為什麼排第一"
    assert scored[0].contributions[0].feature in FEATURES


def test_score_is_capped_so_one_feature_cannot_own_the_ranking() -> None:
    vals_a = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.02, 1e9]
    vals_b = [1.0, 1.02, 0.98, 1.01, 0.99, 1.03, 0.97, 1.0, 1.01, 1.0]
    scored = {s.code: s for s in score_cohort(cohort(vals_a, vals_b), FEATURES)}
    assert scored["N10"].score is not None
    assert scored["N10"].score <= Z_CAP


def test_reason_text_carries_the_raw_value_and_the_peer_median() -> None:
    """An inspector must be able to check the claim against the printed report."""
    vals = [1.0, 1.02, 0.98, 1.01, 0.99, 1.03, 0.97, 1.0, 1.01, 5.0]
    scored = {s.code: s for s in score_cohort(cohort(vals), FEATURES)}
    text = scored["N10"].contributions[0].as_text()
    assert "5.000" in text
    assert "同年中位" in text
    assert "百分位" in text
    assert "同儕" in text, "稀疏特徵要標示實際有該欄位的同儕數"
    assert "z=" not in text, "對外文字不得呈現原始 z（-21 之類會被誤讀為嚴重度）"


def test_anomalies_are_only_those_past_the_threshold() -> None:
    """97 of the panel's 132 rows have no feature past REASON_Z.

    Those rows must report an empty anomaly list, not three padded ones: calling
    a row's largest contributions "異常" when none reached the threshold turns an
    ordinary 園 into a finding.
    """
    ordinary = [1.0, 1.02, 0.98, 1.01, 0.99, 1.03, 0.97, 1.0, 1.01, 1.02]
    for s in score_cohort(cohort(ordinary), FEATURES):
        assert s.anomalies == [], "沒有單項達門檻時，異常清單必須是空的"
        assert s.contributions, "但仍要能說明分數是由什麼構成"

    extreme = [1.0, 1.02, 0.98, 1.01, 0.99, 1.03, 0.97, 1.0, 1.01, 50.0]
    hit = {s.code: s for s in score_cohort(cohort(extreme), FEATURES)}["N10"]
    assert hit.anomalies, "真正的極端值必須被列為異常"
    assert all(r.is_anomalous for r in hit.anomalies)
    assert all(abs(r.z) >= REASON_Z for r in hit.anomalies if r.z is not None)


def test_peer_count_is_per_feature_not_per_cohort() -> None:
    """A share computed from 10 of 14 peers is a weaker comparison than from 14.

    The cohort size alone cannot show that, so each reason carries the count of
    peers that actually reported *its* feature.
    """
    rows = cohort([1.0, 1.1, 0.9, 1.05, 0.95, 1.2, 0.8, 1.15, 1.0, 0.85,
                   1.07, 0.93, 1.12, 0.88])
    for r in rows[:4]:
        r["b"] = None
    scored = {s.code: s for s in score_cohort(rows, FEATURES)}
    by_feature = {r.feature: r for r in scored["N06"].contributions}
    assert by_feature["a"].n_peers_with_feature == 14
    assert by_feature["b"].n_peers_with_feature == 10


def test_feature_below_min_peers_is_dropped_entirely() -> None:
    """Six peers cannot establish a median and a spread, so the feature is unused."""
    rows = cohort([1.0, 1.1, 0.9, 1.05, 0.95, 1.2, 0.8, 1.15, 1.0, 0.85])
    for r in rows[:4]:
        r["b"] = None
    assert MIN_PEERS > 6, "本測試假設 6 個同儕不足以評分"
    scored = {s.code: s for s in score_cohort(rows, FEATURES)}
    assert all(r.feature != "b" for r in scored["N10"].contributions)
    assert scored["N10"].n_features == 1


def test_scores_are_finite() -> None:
    vals = [1.0] * 9 + [1e12]
    for s in score_cohort(cohort(vals), FEATURES):
        assert s.score is None or math.isfinite(s.score)
