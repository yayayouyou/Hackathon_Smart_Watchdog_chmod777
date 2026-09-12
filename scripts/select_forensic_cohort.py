"""Pick a matched case-control cohort for validating the forensic signals.

Design constraints, in priority order:

1. **The financials must precede the penalty.** The whole claim of this project is
   forward-looking warning, so reading a 113 學年度 statement to "predict" a 2019
   penalty proves nothing. Statements are therefore fixed at 111 學年度 (period
   111/8/1-112/7/31, balance sheet dated 112/7/31) and the label is "penalised on
   or after 2023-08-01" -- strictly after every statement in the cohort closes.

2. **One single 學年度 for everyone.** The 公校 analysis found a systemwide
   enrolment decline of 5.5pp over two years (Wilcoxon p=0.026), so mixing years
   would let a cohort-wide trend masquerade as a case-control difference.

3. **Match controls on 核定招收人數.** Size drives absolute amounts and several
   ratios (a 60-child 園 and a 484-child 園 are not comparable on 每人成本), and it
   is the only strong covariate available without reading the statements first.

All eight penalised 非營利園 that we hold reports for happen to have a first
penalty on or after 2023-08, so no case is dropped by the cutoff.

Output: data/processed/forensic_cohort.csv
Run:  PYTHONPATH=src .venv/bin/python scripts/select_forensic_cohort.py
"""

from __future__ import annotations

import csv
import pathlib
import re
import sys

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

REPORT_DIR = pathlib.Path("data/raw/資料集/非營利園財報")
ACADEMIC_YEAR = "111"
LABEL_CUTOFF = pd.Timestamp("2023-08-01")
OUT = pathlib.Path("data/processed/forensic_cohort.csv")

FILENAME_RE = re.compile(r"^(N\d\d)(.+?)_(\d{3})學年度")


def available_reports(year: str) -> dict[str, pathlib.Path]:
    """short name -> report path, for one 學年度."""
    out: dict[str, pathlib.Path] = {}
    for path in sorted((REPORT_DIR / f"{year}學年度").glob("*.pdf")):
        m = FILENAME_RE.match(path.name)
        if m:
            out[m.group(2)] = path
    return out


def main() -> None:
    inst = pd.read_csv("data/processed/institutions_ntpc.csv")
    pen = pd.read_csv("data/processed/penalties_ntpc.csv")
    pen["date"] = pd.to_datetime(pen["date"], format="%Y/%m/%d")

    reports = available_reports(ACADEMIC_YEAR)
    print(f"{ACADEMIC_YEAR} 學年度可用財報：{len(reports)} 份")

    nonprofit = inst[inst["type"] == "非營利"]
    late = pen[pen["date"] >= LABEL_CUTOFF]

    rows = []
    for short, path in reports.items():
        matches = nonprofit[nonprofit["title"].str.contains(short, regex=False)]
        if matches.empty:
            print(f"  ⚠ {short}: 對不上主檔，排除")
            continue
        # A 園 can hold several registry entries across contract periods; any of
        # them carrying a penalty counts, and capacity is taken as the maximum.
        ids = set(matches["id"])
        hits = late[late["id"].isin(ids)]
        rows.append(
            {
                "short": short,
                "title": matches.iloc[0]["title"],
                "code": FILENAME_RE.match(path.name).group(1),
                "path": str(path),
                "capacity": int(matches["count_approved"].max()),
                "operator": matches.iloc[0]["operator"] or "",
                "n_penalties_after_cutoff": len(hits),
                "first_penalty": (
                    hits["date"].min().strftime("%Y-%m") if len(hits) else ""
                ),
                "articles": _articles(hits),
                "max_severity": _max_severity(hits),
            }
        )

    df = pd.DataFrame(rows)
    df["is_case"] = (df["n_penalties_after_cutoff"] > 0).astype(int)
    cases = df[df["is_case"] == 1].sort_values("capacity")
    pool = df[df["is_case"] == 0].copy()

    print(
        f"\n案例組（{ACADEMIC_YEAR} 學年度有財報 且 "
        f"{LABEL_CUTOFF.date()} 後受罰）：{len(cases)} 所"
    )
    print(f"對照候選池：{len(pool)} 所")

    chosen = _match_on_capacity(cases, pool)

    cohort = pd.concat([cases, chosen]).sort_values(
        ["is_case", "capacity"], ascending=[False, True]
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    cohort.to_csv(OUT, index=False, quoting=csv.QUOTE_MINIMAL)

    header = f"{'組別':<6}{'園':<12}{'核定':>6}{'首次裁罰':>10}"
    print(f"\n{header}{'條次':>10}{'嚴重度':>7}  受託法人")
    for _, r in cohort.iterrows():
        grp = "案例" if r["is_case"] else "對照"
        print(
            f"{grp:<6}{r['short']:<12}{r['capacity']:>6}{r['first_penalty'] or '—':>10}"
            f"{r['articles'] or '—':>10}{r['max_severity'] or 0:>7}  {r['operator'][:24]}"
        )

    print(f"\n案例組核定人數中位數 {cases['capacity'].median():.0f}"
          f" / 對照組 {chosen['capacity'].median():.0f}")
    print(f"wrote {OUT} ({len(cohort)} 所，共 {len(cohort)} 份 {ACADEMIC_YEAR} 學年度財報)")

    shared = set(cases["operator"]) & set(chosen["operator"]) - {""}
    if shared:
        print(f"\n注意：案例與對照組有共同受託法人 {sorted(shared)}，"
              "法人層級效應無法由本設計分離")


def _articles(hits: pd.DataFrame) -> str:
    if hits.empty:
        return ""
    arts = hits["article"].dropna().astype(int).astype(str).unique()
    return ",".join(sorted(arts))


def _max_severity(hits: pd.DataFrame) -> int:
    from smart_watchdog.features.build import severity_of

    if hits.empty:
        return 0
    return int(max(severity_of(a) for a in hits["article"]))


def _match_on_capacity(cases: pd.DataFrame, pool: pd.DataFrame) -> pd.DataFrame:
    """Nearest-neighbour match on capacity, without replacement."""
    chosen_idx: list[int] = []
    remaining = pool.copy()
    for _, case in cases.iterrows():
        if remaining.empty:
            break
        gap = (remaining["capacity"] - case["capacity"]).abs()
        pick = gap.idxmin()
        chosen_idx.append(pick)
        remaining = remaining.drop(index=pick)
    return pool.loc[chosen_idx]


if __name__ == "__main__":
    main()
