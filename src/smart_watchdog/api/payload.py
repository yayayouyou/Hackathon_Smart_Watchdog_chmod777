"""The contract between the analysis pipeline and any front end.

Everything the console renders comes from :func:`build_payload`, and nothing in
the front end reads a CSV or knows a column name. That separation is the point:

* **Today** ``scripts/build_frontend.py`` calls this, writes the payload into a
  self-contained HTML file, and publishes it. The published page must be
  self-contained because an Artifact's CSP blocks fetch/XHR entirely.
* **At the finals** the same functions can sit behind an HTTP handler
  (``GET /api/points`` …) with the front end fetching instead of inlining. The
  shapes below do not change, so the front end does not change.

This mirrors ``extract/backends.py``: swapping where the data comes from must
never alter what the data means.

Every field is named for what a person sees, not for the column it came from,
and each payload carries ``schema_version`` so a front end can refuse a payload
it does not understand rather than silently mis-render one.
"""

from __future__ import annotations

import json
import pathlib
import re
import statistics as st
from typing import Any

SCHEMA_VERSION = 1

TYPE_INDEX = {"公立": 0, "非營利": 1, "私立": 2}

# 附註三 items whose label marks them as personnel spending routed through
# 其他支出 as a pass-through subsidy rather than through 人事費. Without adding
# these back, a 園 looks like it cut salaries in a year when it merely booked the
# 調薪補助 elsewhere -- 海工 112 drops 28% on the 人事費 line alone and rises once
# the 1,875,818 調薪及延長照顧差額補助 in 附註三 is included.
PERSONNEL_SUBSIDY = re.compile(r"調薪|助理員|獎金|團保|鐘點|代課|人事|薪資|加班|勞健保|退休")


def _line(lines: list[dict], label: str) -> dict:
    for ln in lines:
        if label in "".join(str(ln.get("label", "")).split()):
            return ln
    return {}


def _int(v: object) -> int | None:
    """NaN-safe int; pandas leaves missing numerics as float("nan")."""
    return int(v) if v == v and v is not None else None


def _str(v: object) -> str:
    return str(v) if v == v and v is not None else ""


def _short_name(title: str) -> str:
    """A map label short enough to read, keeping the identifying part."""
    t = str(title)
    for prefix in ("新北市私立", "新北市立", "新北市"):
        if t.startswith(prefix):
            t = t[len(prefix):]
            break
    for sep in ("(委託", "（委託"):
        if sep in t:
            t = t.split(sep)[0]
    return t.replace("幼兒園", "") or str(title)


def institution_points(priority, coords: dict[str, tuple[float, float]]) -> list[dict]:
    """One entry per registered 園, carrying its score and why it is surfaced.

    ``fin`` and ``why`` travel together on purpose: a front end that shows the
    ranking without showing which evidence was available would let a 園 we could
    not check read as a 園 we checked and cleared.
    """
    out = []
    for r in priority.itertuples(index=False):
        if r.id not in coords:
            continue
        lon, lat = coords[r.id]
        out.append({
            "i": r.id[:8], "n": _short_name(r.title), "full": str(r.title),
            "t": TYPE_INDEX.get(r.type, 2), "d": r.town,
            "x": lon, "y": lat,
            "s": round(float(r.priority_score), 4), "r": int(r.priority_rank_overall),
            "why": str(r.review_reason or ""),
            "fin": int(r.financial_data_available),
            "cf": int(r.compliance_failed_total), "ch": int(r.compliance_failed_high),
            "ct": str(r.compliance_top_finding or "")[:240],
            "e90": int(r.events_90d), "e365": int(r.events_365d),
            "np": int(r.n_penalties_prior),
            "cap": _int(r.count_approved), "fee": _int(r.monthly),
            "ev": _str(r.latest_event_type), "evd": _str(r.latest_event_date),
            "er": _str(r.eval_result), "erd": _str(r.eval_date),
            "ep": int(r.eval_recent_partial),
        })
    return out


def _hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    p = sorted(set(points))
    if len(p) < 3:
        return list(p)

    def half(seq):
        out: list[tuple[float, float]] = []
        for q in seq:
            while len(out) >= 2:
                (x1, y1), (x2, y2) = out[-2], out[-1]
                if (x2 - x1) * (q[1] - y1) - (y2 - y1) * (q[0] - x1) <= 0:
                    out.pop()
                else:
                    break
            out.append(q)
        return out

    return half(p)[:-1] + half(list(reversed(p)))[:-1]


def district_summary(points: list[dict]) -> list[dict]:
    """Per-行政區 counts used for the choropleth and the district filter.

    ``fin_pct`` is deliberately available as a shading option: the districts with
    the least financial coverage are not the safest, and letting an inspector see
    coverage as a map layer is the honest counterpart to ranking on it.
    """
    by: dict[str, list[dict]] = {}
    for p in points:
        by.setdefault(p["d"], []).append(p)
    out = []
    for name, rows in sorted(by.items(), key=lambda kv: -len(kv[1])):
        xs = [r["x"] for r in rows]
        ys = [r["y"] for r in rows]
        fin = sum(r["fin"] for r in rows)
        out.append({
            "d": name, "n": len(rows),
            "cx": round(sum(xs) / len(xs), 5), "cy": round(sum(ys) / len(ys), 5),
            "pub": sum(1 for r in rows if r["t"] == 0),
            "np_": sum(1 for r in rows if r["t"] == 1),
            "prv": sum(1 for r in rows if r["t"] == 2),
            "fin": fin, "fin_pct": round(100 * fin / len(rows), 1),
            "pen": sum(1 for r in rows if r["np"] > 0),
            "hull": [[round(a, 5), round(b, 5)]
                     for a, b in _hull([(r["x"], r["y"]) for r in rows])],
        })
    return out


def personnel_spend(report: dict) -> tuple[float | None, float]:
    """(人事費 as filed, personnel-related subsidy sitting in 其他支出).

    Returned separately rather than summed so a caller can show both. The second
    figure is only visible for reports whose 附註三 itemises 其他支出; where it
    does not, the true personnel spend is unknown rather than equal to the first
    figure, and any year-over-year comparison has to say so.
    """
    lines = (report.get("income_statement") or {}).get("lines") or []
    filed = _line(lines, "人事費").get("actual")
    note3 = report.get("note_3") or {}
    subsidy = sum(
        it.get("amount") or 0
        for it in (note3.get("other_expense_items") or [])
        if PERSONNEL_SUBSIDY.search(str(it.get("label", "")))
    )
    return filed, float(subsidy)


def dossiers(
    extract_dir: pathlib.Path,
    crosswalk,
    findings,
    timeseries,
    is_opening_year,
) -> dict[str, Any]:
    """Per-報告代號 forensic detail for the 園 that file financial statements."""
    code_ids: dict[str, set[str]] = {}
    for r in crosswalk.itertuples(index=False):
        for rid in json.loads(r.registry_ids or "[]"):
            code_ids.setdefault(str(r.code), set()).add(rid[:8])

    series: dict[str, dict] = {}
    staff: dict[str, list[dict]] = {}
    for path in sorted(extract_dir.glob("*.json")):
        code, name, year = path.stem.split("_")
        d = json.loads(path.read_text(encoding="utf-8"))
        bs = d.get("balance_sheet") or {}
        n1 = d.get("note_1") or {}
        filed, subsidy = personnel_spend(d)
        series.setdefault(code, {"name": name, "years": []})["years"].append({
            "y": int(year),
            "ra": bs.get("reserve_asset"), "rl": bs.get("reserve_liability"),
            "sa": bs.get("severance_asset"), "sl": bs.get("severance_liability"),
        })
        staff.setdefault(code, []).append({
            "y": int(year), "st": n1.get("total_staff"), "ed": n1.get("educators"),
            "cap": n1.get("approved_capacity"), "en": n1.get("actual_enrolment"),
            "cost": filed, "sub": subsidy,
            "op": 1 if is_opening_year(n1.get("contract_period"), year) else 0,
        })
    for v in series.values():
        v["years"].sort(key=lambda x: x["y"])
    for v in staff.values():
        v.sort(key=lambda x: x["y"])

    per_code: dict[str, list[dict]] = {}
    for r in findings.itertuples(index=False):
        state = {"True": "pass", "False": "fail"}.get(str(r.passed), "hold")
        per_code.setdefault(str(r.code), []).append({
            "y": int(r.academic_year), "rule": r.rule, "st": state,
            "sev": r.severity, "detail": str(r.detail)[:300],
            "text": str(r.rule_text)[:220],
        })

    gaps: dict[str, list[dict]] = {}
    for r in timeseries.itertuples(index=False):
        gaps.setdefault(str(r.code), []).append({
            "res": r.reserve, "y0": int(r.prev_year), "y1": int(r.year),
            "v": r.verdict, "note": str(r.note)[:200],
        })

    return {
        code: {
            "ids": sorted(ids),
            "name": series.get(code, {}).get("name", ""),
            "series": series.get(code, {}).get("years", []),
            "staff": staff.get(code, []),
            "findings": sorted(per_code.get(code, []),
                               key=lambda f: (-f["y"], f["st"] != "fail")),
            "gaps": gaps.get(code, []),
        }
        for code, ids in code_ids.items()
    }


def benchmarks(dossier: dict[str, Any]) -> dict[str, Any]:
    """Corpus-wide staffing reference points, with their own caveat attached.

    ``ph_outliers`` is 0 and that is the finding: 非營利園 operates on approved
    cost-sharing budgets, so per-head personnel cost is structurally uniform and
    cannot separate 園 from one another. The front end shows these numbers as
    context for reading one report, never as a risk score.
    """
    rows = [r for v in dossier.values() for r in v["staff"]
            if not r["op"] and r["st"] and r["cost"]]
    per_head = [r["cost"] / r["st"] for r in rows]
    ratios = [r["en"] / r["ed"] for v in dossier.values() for r in v["staff"]
              if r["ed"] and r["en"]]
    q1, q3 = st.quantiles(per_head, n=4)[0], st.quantiles(per_head, n=4)[2]
    lower = q1 - 1.5 * (q3 - q1)
    subsidised = [r for v in dossier.values() for r in v["staff"] if r["sub"]]
    return {
        "ph_med": round(st.median(per_head)),
        "ph_q1": round(q1), "ph_q3": round(q3),
        "ph_outliers": sum(1 for v in per_head if v < lower),
        "n_norm": len(rows),
        "ratio_med": round(st.median(ratios), 1),
        "n_subsidised": len(subsidised),
        "n_reports": sum(len(v["staff"]) for v in dossier.values()),
    }


def realtime(mentions, stamp: dict[str, Any]) -> dict[str, Any]:
    """Live-channel mentions, grouped by 園, with the sweep's own provenance.

    ``channels`` travels with the mentions on purpose. A published page cannot
    call the channels itself -- the Artifact CSP blocks fetch entirely -- so the
    panel shows a snapshot, and a snapshot that does not say when it was taken
    or how many channels were listening reads as "nothing is happening here".
    Two of five channels are live; the other three are waiting on a key, an app
    review, or a procurement, and the panel says so.
    """
    by_institution: dict[str, list[dict]] = {}
    for r in mentions.itertuples(index=False):
        by_institution.setdefault(str(r.institution_id)[:8], []).append({
            "ch": str(r.channel), "h": str(r.headline)[:180],
            "u": str(r.url), "d": str(r.published),
            "p": str(r.publisher), "k": str(r.kind),
        })
    for items in by_institution.values():
        items.sort(key=lambda m: m["d"], reverse=True)
    return {
        "swept_at": stamp.get("swept_at", ""),
        "channels_live": stamp.get("channels_live", 0),
        "channels_total": stamp.get("channels_total", 0),
        "channels": stamp.get("channels", []),
        "by_institution": by_institution,
        "disposition": "待人工研判",
    }


def build_payload(*, points, districts, dossier, bench, boundary,
                  realtime_panel=None) -> dict[str, Any]:
    """The whole contract, versioned."""
    return {
        "schema_version": SCHEMA_VERSION,
        "points": points,
        "districts": districts,
        "dossier": dossier,
        "bench": bench,
        "boundary": boundary,
        "realtime": realtime_panel or {},
    }
