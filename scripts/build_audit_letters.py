"""Draft an audit recommendation letter for every 園 on the review list.

    PYTHONPATH=src .venv/bin/python scripts/build_audit_letters.py
    PYTHONPATH=src .venv/bin/python scripts/build_audit_letters.py --backend bedrock

Output: data/processed/audit_letters/<id>_<園名>.txt plus an index CSV recording
which backend produced each letter and whether it passed verification.

The default backend assembles the letter deterministically. ``--backend bedrock``
is the competition deliverable; both are handed identical facts and both outputs
go through the same verifier, so the choice changes the prose and nothing about
what a letter is permitted to claim. A Bedrock draft that fails verification is
not written -- the script falls back to the template and records the reason,
because an unusable letter is better than a wrong one addressed to a real 園.
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import sys

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.console import use_utf8
from smart_watchdog.report.backends import TemplateBackend, get_backend
from smart_watchdog.report.facts import build_facts

# 新增的 `--limit` 防線是中文錯誤訊息，而錯誤訊息最常在輸出被導向管線時才出現
# ——那正是 Windows 退回 cp950 的時機。不能靠呼叫端記得設 PYTHONIOENCODING。
use_utf8()

DEFAULT_OUT_DIR = pathlib.Path("data/processed/audit_letters")
INDEX_NAME = "index.csv"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backend", default="template", choices=["template", "bedrock"])
    ap.add_argument("--limit", type=int, default=0, help="只產生前 N 份")
    # `--limit` 搭配預設輸出目錄曾經是破壞性的：index.csv 以 "w" 整個重寫，
    # 所以 `--limit 1` 會把 141 列的索引砍成 1 列。而 `bedrock-check --full`
    # 的煙霧測試正是用 `--limit 1` 跑的——決賽當天每天早上要跑的那條指令，
    # 每跑一次就毀掉一次索引。冒煙測試應該寫到別的地方。
    ap.add_argument("--out", default=str(DEFAULT_OUT_DIR),
                    help="輸出目錄（預設 data/processed/audit_letters）")
    # 預設 None：讓 BedrockBackend 去問 smart_watchdog.bedrock，
    # 模型 ID 只能有一個來源，不能在 CLI 再寫死一份。
    ap.add_argument("--model", default=None,
                    help="覆寫模型 ID（預設用 bedrock.REASONING_MODEL）")
    ap.add_argument("--region", default=None,
                    help="覆寫 AWS region（預設用 bedrock.region()）")
    a = ap.parse_args()
    out_dir = pathlib.Path(a.out)
    index_path = out_dir / INDEX_NAME
    if a.limit and out_dir.resolve() == DEFAULT_OUT_DIR.resolve():
        sys.exit(
            f"--limit {a.limit} 會把 {index_path} 重寫成只有 {a.limit} 列，"
            "蓋掉完整名單。\n"
            "要試跑請指定別的目錄，例如：\n"
            "  --limit 1 --out data/interim/letters_smoke"
        )

    priority = pd.read_csv("data/processed/audit_priority_ntpc.csv")
    findings = pd.read_csv("data/processed/compliance_findings.csv")
    crosswalk = pd.read_csv("data/processed/nonprofit_registry_crosswalk.csv")
    timeseries = pd.read_csv("data/processed/reserve_timeseries.csv")
    known = set(priority["title"])

    dossier = None
    dist = pathlib.Path("dist/data/payload.json")
    if dist.exists():
        import json
        dossier = json.loads(dist.read_text(encoding="utf-8")).get("dossier")

    flagged = priority[priority["flagged"] == 1].sort_values("priority_rank_overall")
    if a.limit:
        flagged = flagged.head(a.limit)

    backend = get_backend(a.backend, **({"model": a.model, "region": a.region}
                                        if a.backend == "bedrock" else {}))
    fallback = TemplateBackend()
    out_dir.mkdir(parents=True, exist_ok=True)

    rows, failed = [], 0
    for row in flagged.itertuples(index=False):
        facts = build_facts(row, findings, crosswalk, timeseries,
                            dossier=dossier, total=len(priority))
        result = backend.write(facts, known_titles=known)
        if not result["verified"] and a.backend != "template":
            print(f"⚠️ {facts.title[:24]} 未通過驗證，改用模板："
                  f"{'；'.join(result['problems'][:2])}")
            failed += 1
            result = fallback.write(facts, known_titles=known)
        safe = "".join(c for c in facts.title if c.isalnum())[:28]
        path = out_dir / f"{facts.institution_id[:8]}_{safe}.txt"
        path.write_text(result["text"], encoding="utf-8")
        rows.append({
            "id": facts.institution_id, "title": facts.title,
            "type": facts.establishment_type, "town": facts.town,
            "priority_rank": facts.priority_rank,
            "findings": len(facts.findings),
            "review_reason": "；".join(facts.review_reasons),
            "backend": result["backend"], "verified": result["verified"],
            "problems": "；".join(result["problems"]),
            "file": path.name,
        })

    with index_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    ok = sum(1 for r in rows if r["verified"])
    print(f"\n產生 {len(rows)} 份 → {out_dir}")
    print(f"  通過驗證 {ok}／{len(rows)}"
          + (f"　Bedrock 未通過改用模板 {failed} 份" if failed else ""))
    print(f"  含財務發現 {sum(1 for r in rows if r['findings'])} 份")
    print(f"  索引 → {index_path}")


if __name__ == "__main__":
    main()
