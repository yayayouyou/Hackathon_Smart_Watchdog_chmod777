"""Check every extracted report against the accounting policy it states about itself.

Output: data/processed/compliance_findings.csv -- one row per (園, 學年度, rule).

The rules come from each report's own 附註二「重大會計政策之彙總說明」, so a finding
quotes the institution's filed policy rather than a threshold we invented. See
src/smart_watchdog/features/compliance.py for the quoted rule text.

A failed check is a **question to ask**, not a finding of wrongdoing.

Run:  PYTHONPATH=src .venv/bin/python scripts/run_compliance_checks.py
"""

from __future__ import annotations

import csv
import json
import pathlib
import re
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.features.compliance import check_report

EXTRACT_DIR = pathlib.Path("data/extracted/nonprofit")
OUT = pathlib.Path("data/processed/compliance_findings.csv")
TIMESERIES = pathlib.Path("data/processed/reserve_timeseries.csv")
FILENAME_RE = re.compile(r"^(N\d\d)_(.+?)_(\d{3})$")

# The two reserve checks are point-in-time: they read one balance sheet and ask
# whether the liability was matched by cash at that instant. classify_reserve_gap
# (scripts/check_reserve_timeseries.py) reads the *next* year and can see whether
# the shortfall was topped up. N08 鷺江 111's 資遣費 gap of 24,453 is exactly this
# case -- it reads as a stand-alone high-severity failure until 112's statement
# shows it funded again. Without this cross-reference, a reader of this table
# alone has no way to know the two tables disagree about how worried to be.
RESERVE_RULES = {"資遣費準備金 資產=負債", "業務發展準備金 資產=負債"}
RULE_TO_RESERVE = {
    "資遣費準備金 資產=負債": "資遣費準備",
    "業務發展準備金 資產=負債": "業務發展準備",
}


def _load_resolutions() -> dict[tuple[str, str, str], str]:
    """Map (code, reserve, year-the-gap-was-open) -> the CSV note for its cure.

    Keyed on `prev_year` because that is the year whose statement showed the
    shortfall; `year` is where it got funded.
    """
    if not TIMESERIES.exists():
        return {}
    resolved: dict[tuple[str, str, str], str] = {}
    with TIMESERIES.open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            if r["verdict"] != "缺口縮小" or abs(float(r["gap"])) > 1_000:
                continue  # only a gap that actually closed counts as cured
            resolved[(r["code"], r["reserve"], r["prev_year"])] = (
                f"{r['year']} 學年度已補足（見 reserve_timeseries.csv）"
            )
    return resolved


def main() -> None:
    files = sorted(EXTRACT_DIR.glob("*.json"))
    if not files:
        sys.exit(f"{EXTRACT_DIR} 裡沒有抽取結果")

    resolved = _load_resolutions()
    rows: list[dict] = []
    for path in files:
        m = FILENAME_RE.match(path.stem)
        if not m:
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.setdefault("code", m.group(1))
        payload.setdefault("short_name", m.group(2))
        payload["academic_year"] = m.group(3)
        for c in check_report(payload):
            d = c.as_dict()
            if d["rule"] in RESERVE_RULES and d["passed"] is False:
                cure = resolved.get(
                    (d["code"], RULE_TO_RESERVE[d["rule"]], d["academic_year"])
                )
                if cure:
                    d["detail"] += f"　⚠️ 但 {cure}，非持續性缺口"
            rows.append(d)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    reports = len({(r["code"], r["academic_year"]) for r in rows})
    print(f"wrote {OUT}  ({len(rows)} 項檢核，涵蓋 {reports} 份報告)\n")

    by_rule: dict[str, Counter] = {}
    for r in rows:
        if r["passed"] is True:
            state = "通過"
        elif r["passed"] is False:
            state = "未通過"
        else:
            state = "待判讀"
        by_rule.setdefault(r["rule"], Counter())[state] += 1

    print(f"{'規則':<28}{'通過':>6}{'未通過':>8}{'待判讀':>8}")
    for rule, counts in by_rule.items():
        print(f"{rule:<28}{counts['通過']:>6}{counts['未通過']:>8}{counts['待判讀']:>8}")

    fails = [r for r in rows if r["passed"] is False]
    holds = [r for r in rows if r["passed"] is None]

    if fails:
        print(f"\n=== 未通過 {len(fails)} 項（依嚴重度）===")
        order = {"high": 0, "medium": 1, "low": 2}
        for r in sorted(fails, key=lambda r: order[r["severity"]]):
            head = f"{r['code']} {r['short_name']} {r['academic_year']} 學年度"
            print(f"\n[{r['severity']}] {head}")
            print(f"  規則：{r['rule']}")
            print(f"  實況：{r['detail']}")

    if holds:
        print(f"\n=== 待判讀 {len(holds)} 項 ===")
        for r in sorted(holds, key=lambda r: (r["rule"], r["code"])):
            if "缺" in r["detail"] or "未列示" in r["detail"]:
                continue  # 純資料不足，不列入需人工判讀
            who = f"{r['code']} {r['short_name']} {r['academic_year']}"
            print(f"  {who}｜{r['rule']}｜{r['detail'][:100]}")


if __name__ == "__main__":
    main()
