"""Do the forensic signals actually separate future violators? (軌 B validation)

This is the question the whole 軌 B design rests on. If the answer is no, extracting
all 132 reports cannot rescue it, so it is tested on a matched 18-園 cohort first.

Design (fixed in scripts/select_forensic_cohort.py):
    features  111 學年度 statements, balance sheet dated 112/7/31
    label     penalised on or after 2023-08-01 -- strictly after every statement closes
    matching  nearest-neighbour on 核定招收人數

With 9 cases and 9 controls this is **underpowered by construction**: only a large
effect can reach significance, so a null result means "not demonstrated here",
not "no effect". Effect sizes are reported alongside p-values for that reason, and
the direction of every signal is stated in advance below so the reading cannot be
chosen after seeing the data.

Run:  PYTHONPATH=src .venv/bin/python scripts/test_forensic_signals.py
"""

from __future__ import annotations

import json
import pathlib
import sys

import pandas as pd
from scipy.stats import fisher_exact, mannwhitneyu

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.extract.forensic import compute_signals, validate_forensic

COHORT = pathlib.Path("data/processed/forensic_cohort.csv")
EXTRACT_DIR = pathlib.Path("data/extracted/nonprofit")
ACADEMIC_YEAR = "111"
OUT = pathlib.Path("data/processed/forensic_signals.csv")

# Pre-registered direction for each signal: does the *case* group sit higher or
# lower if the signal works? Declared here so the test is one-sided by intent and
# a surprise in the opposite direction is reported as such rather than reframed.
EXPECTED: dict[str, str] = {
    "repair_execution": "lower",          # deferred maintenance -> safety risk
    "maintenance_execution": "lower",
    "personnel_execution": "lower",       # understaffing / below-market pay
    "related_party_priority": "higher",   # operator's own fee paid first
    "reserve_funding_gap": "higher",      # earmarked liability not funded
    "prepaid_coverage": "lower",          # parents' prepaid fees already spent
    "enrolment_utilisation": "lower",     # financial stress
    "staff_ratio": "higher",              # more children per educator
    "equity_erosion": "lower",
    "cost_per_child": "lower",
    "unbudgeted_spend": "higher",
    "admin_fee_execution": "higher",
}


def main() -> None:
    cohort = pd.read_csv(COHORT)
    rows = []
    missing: list[str] = []
    identity_fail: list[str] = []

    for _, c in cohort.iterrows():
        path = EXTRACT_DIR / f"{c['code']}_{c['short']}_{ACADEMIC_YEAR}.json"
        if not path.exists():
            missing.append(f"{c['code']} {c['short']}")
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        passed, failed = validate_forensic(payload)
        if failed:
            identity_fail.append(f"{c['code']} {c['short']}: {failed}")
        sig = compute_signals(payload)
        row = sig.as_dict()
        row.update(
            {
                "is_case": int(c["is_case"]),
                "capacity": c["capacity"],
                "operator": c["operator"],
                "first_penalty": c["first_penalty"],
                "max_severity": c["max_severity"],
                "identity_passed": len(passed),
                "identity_failed": len(failed),
                "n_issues": len(payload.get("issues") or []),
            }
        )
        rows.append(row)

    if missing:
        print(f"⚠ 尚未回收 {len(missing)} 份：{', '.join(missing)}\n")
    if not rows:
        print("沒有可分析的抽取結果")
        return

    df = pd.DataFrame(rows)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT, index=False)
    cases, ctrls = df[df["is_case"] == 1], df[df["is_case"] == 0]
    print(f"可分析 {len(df)} 所（案例 {len(cases)} / 對照 {len(ctrls)}）")
    print(f"恆等式：通過 {df['identity_passed'].sum()} / 失敗 {df['identity_failed'].sum()}")
    if identity_fail:
        print("⚠ 恆等式未通過者（結果須保留）：")
        for f in identity_fail:
            print(f"    {f}")

    print(f"\n{'訊號':<26}{'預期':>6}{'案例中位數':>13}{'對照中位數':>13}"
          f"{'方向':>6}{'p(單尾)':>10}{'rbc':>8}{'n':>6}")
    verdicts = []
    for sig_name, expected in EXPECTED.items():
        a = cases[sig_name].dropna()
        b = ctrls[sig_name].dropna()
        if len(a) < 3 or len(b) < 3:
            print(f"{sig_name:<26}{expected:>6}{'樣本不足':>26}")
            continue
        alt = "less" if expected == "lower" else "greater"
        u, p = mannwhitneyu(a, b, alternative=alt)
        rbc = 2 * u / (len(a) * len(b)) - 1
        as_expected = (
            a.median() < b.median() if expected == "lower" else a.median() > b.median()
        )
        mark = "符合" if as_expected else "相反"
        print(
            f"{sig_name:<26}{expected:>6}{a.median():>13.4f}{b.median():>13.4f}"
            f"{mark:>6}{p:>10.4f}{rbc:>+8.3f}{len(a) + len(b):>6}"
        )
        verdicts.append((sig_name, p, rbc, as_expected))

    print("\n=== 二元紅旗（Fisher exact）===")
    flags = {
        "修繕執行率 < 50%": df["repair_execution"] < 0.5,
        "維護執行率 < 50%": df["maintenance_execution"] < 0.5,
        "行政管理費執行率 = 100%": df["admin_fee_execution"] >= 0.999,
        "準備金缺口 > 0": df["reserve_funding_gap"] > 0,
        "預收款覆蓋 < 1.5": df["prepaid_coverage"] < 1.5,
        "有無預算支出": df["unbudgeted_spend"].notna() & (df["unbudgeted_spend"] > 0),
    }
    print(f"{'紅旗':<26}{'案例':>8}{'對照':>8}{'OR':>8}{'p':>9}")
    for label, mask in flags.items():
        m = mask.fillna(False)
        tab = [
            [int((m & (df["is_case"] == 1)).sum()), int((~m & (df["is_case"] == 1)).sum())],
            [int((m & (df["is_case"] == 0)).sum()), int((~m & (df["is_case"] == 0)).sum())],
        ]
        odds, p = fisher_exact(tab)
        print(
            f"{label:<26}{f'{tab[0][0]}/{len(cases)}':>8}"
            f"{f'{tab[1][0]}/{len(ctrls)}':>8}{odds:>8.2f}{p:>9.4f}"
        )

    sig_hits = [v for v in verdicts if v[1] < 0.05]
    print("\n=== 結論 ===")
    print(f"預先登記方向共 {len(verdicts)} 個訊號，p<0.05 者 {len(sig_hits)} 個")
    for name, p, rbc, ok in sig_hits:
        print(f"  {name}: p={p:.4f} rbc={rbc:+.3f} 方向{'符合' if ok else '相反'}")
    consistent = sum(1 for v in verdicts if v[3])
    expected_by_chance = len(verdicts) / 2
    print(
        f"方向與預期一致者 {consistent}/{len(verdicts)}"
        f"（純機率下期望值 {expected_by_chance:.1f}）"
    )
    print(
        "\n⚠ 本檢定 9 vs 9，檢定力極低。無顯著不等於無效應；"
        "方向一致性比個別 p 值更值得參考。"
    )


if __name__ == "__main__":
    main()
