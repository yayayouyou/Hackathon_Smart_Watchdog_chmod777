"""Rank 非營利園-年 by how unlike their same-year peers they look, and say why.

This is 軌 B's breadth layer. 軌 B's depth layer -- the compliance checks -- can
only speak about the 60 園 whose reports state a policy it can check them against,
and it answers seven fixed questions. This answers a different one: *given what
the other 非營利園 filed for the same 學年度, which of these look unusual enough
to be worth an inspector's hour?*

**It is not a classifier and has no label.** The panel carries 10 positive
compliance outcomes across 132 園-年, below the threshold ``CLAUDE.md`` sets for
fitting anything supervised. So nothing here is fitted: the score is a robust
distance from the peer median, every contribution is reportable in the units the
report printed, and the compliance findings are used only *after* the ranking
exists, as an outside check on it.

Every feature is a ratio, a share or a per-child figure. A raw amount would rank
大園 above 小園 and call it risk -- ``reserve_funding_gap`` and
``unbudgeted_spend`` arrive from the panel as amounts and are divided here before
use, which is the whole reason they are not passed through untouched.

Usage
    PYTHONPATH=src .venv/Scripts/python scripts/build_nonprofit_anomaly.py
"""

from __future__ import annotations

import argparse
import collections
import csv
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pandas as pd

from smart_watchdog.console import use_utf8
from smart_watchdog.features.anomaly import (
    REASON_Z,
    Z_CAP,
    change_scores,
    score_cohort,
)

PANEL = pathlib.Path("data/processed/nonprofit_panel.csv")
FACTS = pathlib.Path("data/processed/nonprofit_pagewise_facts.csv")
FINDINGS = pathlib.Path("data/processed/compliance_findings.csv")
OUT = pathlib.Path("data/processed/nonprofit_anomaly.csv")
REASONS_OUT = pathlib.Path("data/processed/nonprofit_anomaly_reasons.csv")

#: Feature column -> the label printed in a reason. Deliberately excludes
#: related_party_priority: ``docs/FINDINGS.md`` §5 records it as the forensic
#: signal whose measured effect was *completely reversed* (rbc −0.432), which
#: means the project does not currently understand what it measures. A feature
#: nobody can interpret has no business generating an audit reason.
FEATURES: dict[str, str] = {
    "personnel_share": "人事費占支出比",
    "operating_share": "業務費占支出比",
    "material_share": "材料費占支出比",
    "admin_share": "行政管理費占收入比",
    "agency_share": "代收代付等占收入比",
    "personnel_execution": "人事費執行率",
    "maintenance_execution": "維護費執行率",
    "repair_execution": "修繕購置費執行率",
    "cost_per_child": "每生成本",
    "staff_ratio": "師生比（每師幼兒數）",
    "enrolment_utilisation": "招收利用率",
    "liquidity": "流動比率",
    "liability_share": "負債占資產比",
    "surplus_margin": "本期餘絀率",
    "prepaid_coverage": "預收款覆蓋倍數",
    "reserve_gap_share": "準備金缺口占資產比",
    "unbudgeted_share": "未編列預算支出占比",
}

AGENCY_SECTIONS = ("agency_passthrough", "agency_subsidy", "project_subsidy")


def safe_div(a, b):
    """a / b, or None when the quotient would not mean anything.

    Returns None rather than 0 for a missing numerator: the rule the extraction
    follows for blank cells holds here too, because a 園 with no 附註三 has an
    unknown agency share, not a zero one.
    """
    if a is None or b is None or pd.isna(a) or pd.isna(b) or b == 0:
        return None
    return float(a) / float(b)


def agency_totals(facts_path: pathlib.Path) -> dict[tuple, float]:
    """Total agency receipts per 園-年, from 附註三's gross detail tables.

    Only the current period's 收 column counts. These tables print the prior year
    beside it, and the period label carries the 學年度 it belongs to, so the match
    is on the label rather than on column position -- which varies by report.
    """
    if not facts_path.exists():
        return {}
    totals: dict[tuple, float] = collections.defaultdict(float)
    seen: set = set()
    with facts_path.open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            if r["section"] not in AGENCY_SECTIONS:
                continue
            label = "".join((r["item_label"] or "").split())
            if not label.startswith("合"):
                continue          # 合計 rows only, so items are not double-counted
            period = "".join((r["period_label"] or "").split())
            year = str(r["academic_year"])
            if not period.startswith(year) or not period.endswith(("收", "收入")):
                continue
            try:
                value = float(r["value"])
            except (TypeError, ValueError):
                continue
            key = (r["code"], year, r["pdf_page"], r["table_index"], period)
            if key in seen:
                continue
            seen.add(key)
            totals[(r["code"], year)] += value
    return dict(totals)


def build_rows() -> list[dict]:
    panel = pd.read_csv(PANEL)
    agency = agency_totals(FACTS)

    rows: list[dict] = []
    for rec in panel.to_dict("records"):
        code, year = rec["code"], str(rec["academic_year"])
        expense = rec.get("支出合計_決算")
        income = rec.get("收入合計_決算")
        row = {
            "code": code, "short_name": rec.get("short_name", ""),
            "academic_year": year,
            # ── shares: scale cancels ────────────────────────────────
            "personnel_share": safe_div(rec.get("人事費_決算"), expense),
            "operating_share": safe_div(rec.get("業務費_決算"), expense),
            "material_share": safe_div(rec.get("材料費_決算"), expense),
            "admin_share": safe_div(rec.get("行政管理費_決算"), income),
            "agency_share": safe_div(agency.get((code, year)), income),
            # ── execution rates: already ratios in the panel ─────────
            "personnel_execution": rec.get("personnel_execution"),
            "maintenance_execution": rec.get("maintenance_execution"),
            "repair_execution": rec.get("repair_execution"),
            # ── per-child / structural ───────────────────────────────
            "cost_per_child": rec.get("cost_per_child"),
            "staff_ratio": rec.get("staff_ratio"),
            "enrolment_utilisation": rec.get("enrolment_utilisation"),
            "liquidity": safe_div(rec.get("current_assets_total"),
                                  rec.get("current_liabilities_total")),
            "liability_share": safe_div(rec.get("total_liabilities"),
                                        rec.get("total_assets")),
            "surplus_margin": safe_div(rec.get("current_surplus"), income),
            "prepaid_coverage": rec.get("prepaid_coverage"),
            # ── amounts from the panel, divided before use ───────────
            "reserve_gap_share": safe_div(rec.get("reserve_funding_gap"),
                                          rec.get("total_assets")),
            "unbudgeted_share": safe_div(rec.get("unbudgeted_spend"), expense),
        }
        for k, v in row.items():
            if k in FEATURES and v is not None and pd.isna(v):
                row[k] = None
        rows.append(row)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="非營利園同儕財務異常排序")
    ap.add_argument("--top", type=int, default=12, help="列出前 N 名")
    a = ap.parse_args()
    use_utf8()

    rows = build_rows()
    by_year: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        by_year[r["academic_year"]].append(r)

    scored: list = []
    for year in sorted(by_year):
        scored.extend(score_cohort(by_year[year], FEATURES))
    changes = change_scores(dict(by_year), FEATURES)
    for s in scored:
        hit = changes.get((s.code, s.academic_year))
        if hit:
            s.change_score, s.change_contributions = hit

    # Rank within 學年度: a score is only comparable to its own cohort's scores.
    per_year: dict[str, list] = collections.defaultdict(list)
    for s in scored:
        per_year[s.academic_year].append(s)
    rank: dict[tuple, int] = {}
    pct_in_year: dict[tuple, float] = {}
    for year, group in per_year.items():
        ordered = sorted([g for g in group if g.score is not None],
                         key=lambda g: -g.score)
        n = len(ordered)
        for i, g in enumerate(ordered, start=1):
            rank[(g.code, year)] = i
            # Percentile within the cohort. Raw scores are not comparable across
            # 學年度 -- cohort size and spread both move -- but a position within
            # one's own year is, which is what the dossier and the validation use.
            pct_in_year[(g.code, year)] = round(100.0 * (n - i) / max(n - 1, 1), 1)

    out_rows = []
    reason_rows = []
    for s in sorted(scored, key=lambda s: (-(s.score or -1), s.code)):
        out_rows.append({
            "code": s.code, "short_name": s.short_name,
            "academic_year": s.academic_year,
            "anomaly_score": None if s.score is None else round(s.score, 4),
            "rank_in_year": rank.get((s.code, s.academic_year)),
            "percentile_in_year": pct_in_year.get((s.code, s.academic_year)),
            "n_peers": s.n_peers, "n_features": s.n_features,
            "change_score": None if s.change_score is None else round(s.change_score, 4),
            # 達到 REASON_Z 的項目才是「異常」；其餘只是分數的主要構成。
            "n_anomalies": len(s.anomalies),
            "anomaly_1": s.anomalies[0].as_text() if len(s.anomalies) > 0 else "",
            "anomaly_2": s.anomalies[1].as_text() if len(s.anomalies) > 1 else "",
            "anomaly_3": s.anomalies[2].as_text() if len(s.anomalies) > 2 else "",
            "contribution_1": s.contributions[0].as_text() if s.contributions else "",
            "contribution_2": (s.contributions[1].as_text()
                               if len(s.contributions) > 1 else ""),
            "contribution_3": (s.contributions[2].as_text()
                               if len(s.contributions) > 2 else ""),
        })
        for kind, group in (("level", s.contributions),
                            ("change", s.change_contributions)):
            reason_rows.extend(
                {"code": s.code, "short_name": s.short_name,
                 "academic_year": s.academic_year, "kind": kind,
                 "feature": r.feature, "label": r.label, "value": r.value,
                 "z_raw": None if r.z is None else round(r.z, 3),
                 "z_capped": (None if r.z is None
                              else round(min(abs(r.z), Z_CAP), 3)),
                 "is_anomalous": r.is_anomalous,
                 "percentile": None if r.percentile is None else round(r.percentile, 1),
                 "peer_median": r.peer_median,
                 "n_peers_with_feature": r.n_peers_with_feature}
                for r in group
            )

    for path, data in ((OUT, out_rows), (REASONS_OUT, reason_rows)):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(data[0]))
            w.writeheader()
            w.writerows(data)

    print(f"{len(out_rows)} 個園-學年度，{len(FEATURES)} 個比率特徵")
    for year in sorted(by_year):
        n = len(by_year[year])
        got = sum(1 for s in per_year[year] if s.score is not None)
        print(f"  {year} 學年度  同儕 {n:>3} 園　可評分 {got:>3}")
    print()
    print(f"特徵覆蓋（非空 / {len(rows)}）：")
    for col, label in FEATURES.items():
        n = sum(1 for r in rows if r.get(col) is not None)
        flag = "  ⚠ 覆蓋偏低" if n < len(rows) * 0.7 else ""
        print(f"  {label:<22}{n:>4}{flag}")
    print()
    print(f"=== 異常分數前 {a.top} 名（跨年度合併排序，僅供瀏覽）===")
    for r in out_rows[:a.top]:
        print(f"  {r['anomaly_score']:.2f}  {r['code']} {r['short_name'][:6]:<8}"
              f"{r['academic_year']}　同年第 {r['rank_in_year']}/{r['n_peers']}　"
              f"特徵 {r['n_features']}")
        if r["n_anomalies"]:
            for key in ("anomaly_1", "anomaly_2", "anomaly_3"):
                if r[key]:
                    print(f"       ⚠ {r[key]}")
        else:
            print(f"       （無單項達異常門檻 |z|≥{REASON_Z}；以下為分數主要構成）")
            for key in ("contribution_1", "contribution_2", "contribution_3"):
                if r[key]:
                    print(f"       · {r[key]}")
    print()
    print(f"寫入 {OUT}")
    print(f"寫入 {REASONS_OUT}")
    print()
    print("⚠️ 這是同儕相對的異常排序，不是違規機率，也不併入軌 A 總分。"
          "分數高只代表「與同年度同類型的園相比不尋常」，"
          "不尋常的原因可能完全合法。")


if __name__ == "__main__":
    main()
