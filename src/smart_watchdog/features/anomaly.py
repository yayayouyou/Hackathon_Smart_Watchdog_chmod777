"""Peer-relative anomaly scoring for the 非營利園 financial panel.

This ranks 園-年 by how unlike their peers they look, and says why. It is **not** a
classifier and produces nothing that may be read as a probability of wrongdoing:
there are 10 positive labels in the whole panel, which is below the threshold
``CLAUDE.md`` sets for fitting anything supervised, and a fitted score on that many
events would be a number with no content dressed as a finding.

Four design rules, each of which the project has a scar from:

**Peers are the same 學年度 and the same 類型.** ``CLAUDE.md`` records two features
that looked strong until stratified (``monthly`` p=0.00066 → 0.050; 收費漲幅 rbc
+0.399 → +0.097) and one city-wide trend that would mislabel a whole cohort
(學雜費執行率 median fell 5.5pp from 112 to 114, Wilcoxon p=0.026). Comparing a 園
only against its own year's 非營利 cohort removes both without modelling either.

**Ratios, not amounts.** A large 園 is not an anomalous 園. Every feature here is a
per-child figure, a share of income or expenditure, or an execution rate, so scale
cancels before the comparison starts.

**Median and MAD, not mean and SD.** With 28-38 peers per year, one extreme 園
drags a mean far enough to hide itself. MAD is bounded-influence, and the 1.4826
factor rescales it to be comparable to a standard deviation for normal data.

**Missing stays missing.** A 園 with no 附註三 contributes no agency-share feature;
it does not contribute a zero. Scores are averaged over *observed* features and
every row carries ``n_features`` so a thin row is visible as thin rather than
quietly scoring low. This is the same rule the extraction follows for blank cells,
for the same reason: a zero is a claim, an absence is not.

The compliance findings and the penalty history are deliberately absent from this
module. They are how the ranking gets checked afterwards, and a check you trained
on is not a check.
"""

from __future__ import annotations

import dataclasses
import math

#: Scale factor making MAD comparable to a standard deviation under normality.
MAD_TO_SD = 1.4826

#: Robust z beyond which a feature may be called an *anomaly*. 3.5 is the usual
#: Iglewicz-Hoaglin cutoff for modified z-scores. It is a reporting threshold, not
#: a decision boundary: crossing it names a line item worth looking at, and not
#: crossing it is the normal state -- 97 of the panel's 132 園-年 have no single
#: feature past it. A row below the threshold still has a score and a rank, but
#: what it reports are *contributions to that score*, not findings.
REASON_Z = 3.5

#: Cap on |z| before aggregation. One feature that is wildly off should raise a
#: 園's score, but not so far that the other features stop mattering -- without a
#: cap a single divide-by-tiny-MAD would decide the whole ranking.
Z_CAP = 8.0

#: A year needs enough peers for a median and MAD to mean anything.
MIN_PEERS = 8


def median(values: list[float]) -> float | None:
    xs = sorted(v for v in values if v is not None and not math.isnan(v))
    if not xs:
        return None
    n = len(xs)
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2


def mad(values: list[float], center: float | None = None) -> float | None:
    """Median absolute deviation. Returns None when undefined."""
    xs = [v for v in values if v is not None and not math.isnan(v)]
    if not xs:
        return None
    c = center if center is not None else median(xs)
    if c is None:
        return None
    return median([abs(v - c) for v in xs])


def percentile_of(value: float, values: list[float]) -> float | None:
    """Fraction of peers at or below ``value``, as a percentage."""
    xs = [v for v in values if v is not None and not math.isnan(v)]
    if not xs:
        return None
    return 100.0 * sum(1 for v in xs if v <= value) / len(xs)


def quartiles(values: list[float]) -> tuple[float, float] | None:
    xs = sorted(v for v in values if v is not None and not math.isnan(v))
    if len(xs) < 4:
        return None
    lo = xs[: len(xs) // 2]
    hi = xs[(len(xs) + 1) // 2:]
    q1, q3 = median(lo), median(hi)
    return None if q1 is None or q3 is None else (q1, q3)


def robust_scale(peers: list[float]) -> float | None:
    """A stable spread estimate for a peer cohort.

    MAD alone is not enough here. 招收利用率 sits at or near 1.00 for most 非營利園,
    so its MAD is nearly zero and the modified z-score of a 園 at 0.70 comes out
    around −22 -- arithmetically correct, and useless as a reported magnitude,
    because it says more about how tightly the middle clusters than about how
    unusual the 園 is. 行政管理費執行率 is worse: contractually fixed at exactly
    100% in most reports, so its MAD is exactly 0 and z is undefined.

    Taking the larger of the MAD-based and IQR-based scales keeps the estimator
    robust while stopping a tight middle from manufacturing huge z-scores. Both
    are standard normal-consistent rescalings (1.4826 for MAD, 1.349 for IQR).
    Returns None when neither is defined, and the caller falls back to the
    percentile, which stays meaningful whatever the spread.
    """
    c = median(peers)
    if c is None:
        return None
    scales = []
    m = mad(peers, c)
    if m is not None and m > 0:
        scales.append(MAD_TO_SD * m)
    q = quartiles(peers)
    if q is not None and q[1] > q[0]:
        scales.append((q[1] - q[0]) / 1.349)
    return max(scales) if scales else None


def robust_z(value: float, peers: list[float]) -> float | None:
    """Modified z-score against a peer group, or None when it is not defined."""
    c = median(peers)
    if c is None:
        return None
    s = robust_scale(peers)
    if s is None or s <= 0:
        return None
    return (value - c) / s


@dataclasses.dataclass
class Reason:
    """One feature's contribution to a row's score, in reportable form."""

    feature: str
    label: str
    value: float
    z: float | None
    percentile: float | None
    peer_median: float | None

    #: How many peers actually reported this feature. A share computed from 90
    #: of 132 rows is a weaker comparison than one computed from all of them, and
    #: the cohort size alone does not show that.
    n_peers_with_feature: int = 0

    @property
    def is_anomalous(self) -> bool:
        return self.z is not None and abs(self.z) >= REASON_Z

    def as_text(self) -> str:
        """Reader-facing wording. Deliberately carries no raw z.

        A modified z-score of −21 is arithmetically right and rhetorically
        useless: it says the cohort's middle is tight, which a reader will hear as
        "twenty-one times worse than normal". The raw value, the peer median and
        the percentile say the same thing in units the filed report uses, and a
        稽查員 can check every one of them against the page. The z stays in the
        machine-readable output for scoring and audit.
        """
        pos = "高" if (self.z or 0) > 0 else "低"
        med = "—" if self.peer_median is None else f"{self.peer_median:,.3f}"
        pct = "—" if self.percentile is None else f"第 {self.percentile:.0f} 百分位"
        return (f"{self.label} {self.value:,.3f}（同年中位 {med}，偏{pos}，"
                f"{pct}；同儕 {self.n_peers_with_feature} 園）")


@dataclasses.dataclass
class Scored:
    """One 園-年's anomaly result."""

    code: str
    short_name: str
    academic_year: str
    score: float | None
    n_features: int
    n_peers: int
    #: Features past REASON_Z. These may be called 異常; an empty list is the
    #: ordinary case and must be shown as such rather than padded.
    anomalies: list[Reason]
    #: The three largest contributors to the score, whatever their magnitude.
    #: Not findings -- they explain the number, and are labelled that way.
    contributions: list[Reason]
    change_score: float | None = None
    change_contributions: list[Reason] = dataclasses.field(default_factory=list)


def score_cohort(rows: list[dict], features: dict[str, str],
                 min_peers: int = MIN_PEERS) -> list[Scored]:
    """Score one cohort (one 學年度) against itself.

    ``rows`` are dicts with ``code``/``short_name``/``academic_year`` plus the
    feature columns; ``features`` maps column name to the Chinese label used when
    the reason is written out.

    The score is the mean of the capped |z| over the features this row actually
    has. Mean rather than max so that a row which is mildly unusual on six things
    outranks one that is extreme on a single ratio and ordinary elsewhere -- the
    first is the shape a real problem makes, the second is usually one odd cell.
    """
    peers: dict[str, list[float]] = {}
    for col in features:
        peers[col] = [r[col] for r in rows
                      if r.get(col) is not None and not _isnan(r.get(col))]

    out: list[Scored] = []
    for r in rows:
        zs: list[float] = []
        reasons: list[Reason] = []
        for col, label in features.items():
            v = r.get(col)
            if v is None or _isnan(v):
                continue
            pool = peers[col]
            if len(pool) < min_peers:
                continue
            z = robust_z(v, pool)
            pct = percentile_of(v, pool)
            if z is not None:
                zs.append(min(abs(z), Z_CAP))
            reasons.append(Reason(col, label, float(v), z, pct, median(pool),
                                  n_peers_with_feature=len(pool)))
        score = sum(zs) / len(zs) if zs else None
        ranked = sorted(
            [x for x in reasons if x.z is not None],
            key=lambda x: -abs(x.z or 0.0))
        out.append(Scored(
            code=r["code"], short_name=r.get("short_name", ""),
            academic_year=str(r["academic_year"]),
            score=score, n_features=len(zs), n_peers=len(rows),
            anomalies=[x for x in ranked if x.is_anomalous],
            contributions=ranked[:3]))
    return out


def change_scores(by_year: dict[str, list[dict]], features: dict[str, str],
                  min_peers: int = MIN_PEERS) -> dict[tuple, tuple]:
    """Year-on-year movement, scored against how much peers moved that year.

    A 園 whose 人事費 share jumps 8pp is only interesting if its peers did not also
    jump 8pp. So the change is standardised against the cohort's own distribution
    of changes, which absorbs city-wide shifts -- the 學雜費執行率 decline
    ``CLAUDE.md`` warns about would otherwise light up every 園 in 114.

    Returns {(code, year): (score, [Reason])} for years that have a predecessor.
    """
    years = sorted(by_year)
    out: dict[tuple, tuple] = {}
    for prev, cur in zip(years, years[1:]):
        before = {r["code"]: r for r in by_year[prev]}
        deltas: dict[str, dict[str, float]] = {}
        for r in by_year[cur]:
            p = before.get(r["code"])
            if p is None:
                continue
            d = {}
            for col in features:
                a, b = p.get(col), r.get(col)
                if a is None or b is None or _isnan(a) or _isnan(b):
                    continue
                d[col] = float(b) - float(a)
            if d:
                deltas[r["code"]] = d
        pools = {col: [d[col] for d in deltas.values() if col in d]
                 for col in features}
        for code, d in deltas.items():
            zs, reasons = [], []
            for col, label in features.items():
                if col not in d or len(pools[col]) < min_peers:
                    continue
                z = robust_z(d[col], pools[col])
                pct = percentile_of(d[col], pools[col])
                if z is not None:
                    zs.append(min(abs(z), Z_CAP))
                reasons.append(Reason(col, f"{label} 年變動", d[col], z, pct,
                                      median(pools[col]),
                                      n_peers_with_feature=len(pools[col])))
            if zs:
                ranked = sorted([x for x in reasons if x.z is not None],
                                key=lambda x: -abs(x.z or 0.0))
                out[(code, cur)] = (sum(zs) / len(zs), ranked[:3])
    return out


def _isnan(v: object) -> bool:
    try:
        return math.isnan(float(v))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
