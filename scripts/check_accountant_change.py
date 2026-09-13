"""Track each 園's signing audit firm/accountant across 學年度 to flag a change.

docs/research/02-forensic-signals.md §7「會計師層面訊號」quotes the idea this
implements: 簽證會計師更換：連續年度換所，特別是換所後數字大幅變動 → 典型紅旗。

Why this is a screening signal, not a compliance rule. Switching accounting firms
(or the signing accountant within the same firm) is legal and unremarkable on its
own -- a firm may retire, raise fees, or merge. What makes it worth a question is
the *combination*: a change landing on the same 學年度 boundary as either a
non-unmodified opinion or a large swing in 本期餘絀. The classification itself
lives in :func:`smart_watchdog.features.compliance.classify_accountant_change` so
it can be tested against constructed cases; this script only assembles the year
pairs and reports. Flagging every change regardless of what else happened would
manufacture a red flag out of routine vendor turnover.

Data source. ``scripts/plan_from_toc.py`` now targets 會計師查核報告 for every
report, so ``data/extracted/nonprofit_pages/<report>/`` carries an
``auditor_report`` page for (as of this writing) all 132 財報, not just the 3
pilot 補充 JSON this check started with. :mod:`smart_watchdog.features.auditor`
parses each report's signature block into firm / accountant name / 核准文號
(licence number).

Firm vs accountant, and why 核准文號 is the tiebreaker for the accountant.
A firm-name change is compared exactly -- OCR does not fabricate a firm-name
match this often, so any difference is treated as real. An accountant-name
change is different: scanning this corpus, the licence number stays byte-
identical across every report while the printed name drifts (張景嵐 / 張景崗 /
張景巍 / 張燕嵐 / 張惠嵐 / 張忠崗) -- a licence is issued to one person and is not
shared, so a matching licence number with a different name is OCR noise, not a
personnel change. Where the licence number is missing or itself differs, this
falls back to :func:`smart_watchdog.features.auditor.compare_accountant_names`,
which asks an LLM whether the two strings are a plausible misread of the same
name (Bedrock unavailable -> deterministic surname + edit-distance fallback,
see that module).

This needs adjacent 學年度 for the same 園, so it only reports on 園 with two or
more extracted ``auditor_report`` pages and stays silent about the rest.

Run:  python run.py accountant-change
"""

from __future__ import annotations

import csv
import json
import pathlib
import re
import sys
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.features.auditor import (
    classify_opinion_type,
    compare_accountant_names,
    parse_auditor_signature,
)
from smart_watchdog.features.compliance import (
    classify_accountant_change,
    corroborating_signals,
)

PAGES_DIR = pathlib.Path("data/extracted/nonprofit_pages")
MAIN_DIR = pathlib.Path("data/extracted/nonprofit")
OUT = pathlib.Path("data/processed/accountant_change.csv")
FILENAME_RE = re.compile(r"^(N\d\d)_(.+?)_(\d{3})$")


def _current_surplus(code: str, short: str, year: str) -> float | None:
    path = MAIN_DIR / f"{code}_{short}_{year}.json"
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return (payload.get("balance_sheet") or {}).get("current_surplus")


def _load_auditor_reports() -> dict[str, list[dict]]:
    """{report: [page payload, ...]} for every ``auditor_report`` page found."""
    by_report: dict[str, list[dict]] = defaultdict(list)
    for report_dir in sorted(PAGES_DIR.iterdir()):
        if not report_dir.is_dir() or report_dir.name.startswith("_"):
            continue
        for page_file in sorted(report_dir.glob("p*.json")):
            payload = json.loads(page_file.read_text(encoding="utf-8"))
            if payload.get("page_kind") == "auditor_report":
                by_report[report_dir.name].append(payload)
    return by_report


def _accountant_changed(prev_name: str | None, name: str | None,
                          prev_licence: str | None, licence: str | None
                          ) -> tuple[bool, str]:
    """是否為真的換會計師（而非 OCR 誤讀），以及一句話的判斷依據。

    核准文號兩邊都有且相同 -> 一律視為同一人，姓名差異是誤讀，不呼叫 LLM
    （這是這批語料實測出來的鐵律，見 auditor.py 模組說明）。
    核准文號缺一邊或不同 -> 退回姓名比對（LLM 或其確定性退路）。
    """
    if not prev_name or not name:
        return False, "姓名缺失，無法判斷"
    if prev_name == name:
        return False, "姓名相同"
    if prev_licence and licence and prev_licence == licence:
        return False, f"核准文號相同（{licence}），姓名差異視為 OCR 誤讀"
    cmp = compare_accountant_names(prev_name, name)
    if cmp.same_person:
        return False, f"{cmp.reason}（判斷方式：{cmp.method}）"
    return True, f"{cmp.reason}（判斷方式：{cmp.method}）"


def main() -> None:
    by_report = _load_auditor_reports()
    by_school: dict[tuple[str, str], dict[int, dict]] = defaultdict(dict)
    for report, pages in by_report.items():
        m = FILENAME_RE.match(report)
        if not m:
            continue
        code, short, year = m.groups()
        sig = parse_auditor_signature(pages)
        by_school[(code, short)][int(year)] = {
            "firm": sig.firm, "accountant": sig.accountant,
            "licence_no": sig.licence_no,
            "opinion_type": classify_opinion_type(pages),
        }

    print(f"已抽取之會計師查核報告：{len(by_report)} 份園-學年度"
          f"（{len(by_school)} 所園；見 {PAGES_DIR}/）")

    rows: list[dict] = []
    accountant_only: list[dict] = []
    for (code, short), years in sorted(by_school.items()):
        ordered = sorted(years)
        for prev_y, y in zip(ordered, ordered[1:]):
            if y - prev_y != 1:
                continue  # a missing intervening year makes the comparison unreadable
            prev_audit, audit = years[prev_y], years[y]

            result = classify_accountant_change(
                prev_audit.get("firm"), audit.get("firm"),
                prev_surplus=_current_surplus(code, short, str(prev_y).zfill(3)),
                surplus=_current_surplus(code, short, str(y).zfill(3)),
                opinion=audit.get("opinion_type"),
            )
            if result is not None:
                verdict, reason = result
                rows.append({
                    "code": code, "short_name": short,
                    "prev_year": prev_y, "year": y, "changed": "事務所",
                    "prev_value": prev_audit.get("firm"), "value": audit.get("firm"),
                    "opinion_type": audit.get("opinion_type"),
                    "verdict": verdict, "reason": reason or "",
                })

            changed, why = _accountant_changed(
                prev_audit.get("accountant"), audit.get("accountant"),
                prev_audit.get("licence_no"), audit.get("licence_no"))
            accountant_only.append({
                "code": code, "short_name": short,
                "prev_year": prev_y, "year": y,
                "prev_accountant": prev_audit.get("accountant"),
                "accountant": audit.get("accountant"),
                "prev_licence_no": prev_audit.get("licence_no"),
                "licence_no": audit.get("licence_no"),
                "changed": changed, "why": why,
            })
            if changed:
                signals = corroborating_signals(
                    prev_surplus=_current_surplus(code, short, str(prev_y).zfill(3)),
                    surplus=_current_surplus(code, short, str(y).zfill(3)),
                    opinion=audit.get("opinion_type"),
                )
                verdict = "換所且有異常訊號" if signals else "換所"
                reason = "；".join([why, *signals])
                rows.append({
                    "code": code, "short_name": short,
                    "prev_year": prev_y, "year": y, "changed": "簽證會計師",
                    "prev_value": prev_audit.get("accountant"),
                    "value": audit.get("accountant"),
                    "opinion_type": audit.get("opinion_type"),
                    "verdict": verdict, "reason": reason,
                })

    paired = sum(1 for years in by_school.values() if len(years) > 1)
    print(f"可作跨年度比較的園：{paired} / {len(by_school)} 所"
          f"（需同園連續兩個學年度皆已抽取查核報告頁）")

    accountant_changes = [r for r in accountant_only if r["changed"]]
    print(f"簽證會計師比對：{len(accountant_only)} 組年度對，"
          f"{len(accountant_changes)} 組判定為真的換人（非 OCR 誤讀）")
    for r in accountant_changes:
        print(f"  {r['code']} {r['short_name']}｜{r['prev_year']}→{r['year']} 學年度"
              f"｜{r['prev_accountant']} → {r['accountant']}｜{r['why']}")

    if not rows:
        print("目前尚無同園連續年度的查核報告顯示事務所或會計師變更且伴隨異常訊號。")
        return

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {OUT}  ({len(rows)} 筆變更紀錄)\n")

    for verdict in ("換所且有異常訊號", "換所"):
        group = [r for r in rows if r["verdict"] == verdict]
        if not group:
            continue
        print(f"=== {verdict}（{len(group)} 筆）===")
        for r in group:
            print(f"  {r['code']} {r['short_name']}｜{r['prev_year']}→{r['year']} 學年度"
                  f"｜換{r['changed']}｜{r['prev_value']} → {r['value']}")
            if r["reason"]:
                print(f"    {r['reason']}")
        print()

    print("⚠️ 換所或換人本身合法且常見（退休、調價、法人更替皆可能觸發），"
          "\n   這裡列出的是**請機構說明**變更前後是否伴隨查核意見變化或數字大幅波動，"
          "\n   不是短漏編或做假帳的認定。")


if __name__ == "__main__":
    main()
