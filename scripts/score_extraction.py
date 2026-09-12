"""Score model-extracted statements against the hand-transcribed ground truth.

Two things are measured, and they are not the same:

* **Arithmetic self-consistency** -- does the extraction satisfy the statement's own
  identities? This is available for *every* page in production, with no ground
  truth, so it is what will gate the real pipeline.
* **Cell-level agreement with ground truth** -- available only for the five PoC
  pages, and the only way to know whether self-consistency actually implies
  correctness. An extraction can be internally consistent and still wrong (e.g. a
  whole column shifted), so the second check validates the first.

Error classes are reported separately because they carry very different risk:

    false_zero    ground truth is blank, model wrote 0  -- the dangerous one. It
                  turns "no budget was appropriated" into "budgeted zero", i.e. it
                  fabricates a financial assertion about a real institution.
    missing       ground truth has a figure, model wrote null -- safe. A human
                  fills the gap; nothing false is asserted.
    wrong_value   both present and different -- a misread digit.
    spurious      ground truth blank, model wrote a non-zero figure.

Run:  PYTHONPATH=src .venv/bin/python scripts/score_extraction.py
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.extract.schema import validate_statement

GT_PATH = pathlib.Path("data/ground_truth/nonprofit_statements.json")
EXTRACT_DIR = pathlib.Path("data/interim/poc_extract")
TOL = 1.0


def norm(label: str) -> str:
    """Collapse whitespace so 合　　計 matches 合計 when pairing rows."""
    return "".join(str(label).split())


def score_one(key: str, gt: dict, extracted: dict) -> dict:
    gt_items: dict[str, list] = {norm(k): v for k, v in gt["items"].items()}
    ex_items: dict[str, list] = {
        norm(it.get("label", "")): (it.get("values") or [])
        for it in extracted.get("items") or []
    }

    counts = {
        "correct": 0, "wrong_value": 0, "false_zero": 0,
        "missing": 0, "spurious": 0,
    }
    details: list[str] = []
    n_cols = len(gt["columns"])

    for label, gt_values in gt_items.items():
        ex_values = ex_items.get(label)
        if ex_values is None:
            counts["missing"] += sum(1 for v in gt_values[:n_cols] if v is not None)
            if any(v is not None for v in gt_values[:n_cols]):
                details.append(f"未抽到項目：{label}")
            continue
        for i in range(n_cols):
            g = gt_values[i] if i < len(gt_values) else None
            e = ex_values[i] if i < len(ex_values) else None
            if g is None and e is None:
                continue
            if g is None:
                if e == 0:
                    counts["false_zero"] += 1
                    details.append(f"⚠ false_zero：{label}[{i}] 空白被填 0")
                else:
                    counts["spurious"] += 1
                    details.append(f"spurious：{label}[{i}] 空白被填 {e:,}")
            elif e is None:
                counts["missing"] += 1
                details.append(f"missing：{label}[{i}] 應為 {g:,} 但未填")
            elif abs(float(e) - float(g)) <= TOL:
                counts["correct"] += 1
            else:
                counts["wrong_value"] += 1
                details.append(f"⚠ wrong_value：{label}[{i}] 應為 {g:,} 抽到 {float(e):,.0f}")

    extra = set(ex_items) - set(gt_items)
    payload = dict(extracted)
    payload.setdefault("period_labels", extracted.get("period_labels") or gt["columns"])
    v = validate_statement(payload)

    total_cells = sum(counts.values())
    return {
        "key": key,
        "counts": counts,
        "total_cells": total_cells,
        "cell_accuracy": counts["correct"] / total_cells if total_cells else None,
        "identity_passed": len(v.passed),
        "identity_failed": len(v.failed),
        "identity_skipped": len(v.skipped),
        "extra_labels": sorted(extra),
        "reported_issues": extracted.get("issues") or [],
        "details": details,
    }


def main() -> None:
    gt_all = json.loads(GT_PATH.read_text(encoding="utf-8"))
    statements = {k: v for k, v in gt_all.items() if not k.startswith("_")}

    results = []
    for key, gt in statements.items():
        path = EXTRACT_DIR / f"{key}.json"
        if not path.exists():
            print(f"— {key}: 尚無抽取結果，略過")
            continue
        try:
            extracted = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"✗ {key}: JSON 無法解析 — {exc}")
            continue
        results.append(score_one(key, gt, extracted))

    if not results:
        print("沒有可評分的抽取結果")
        return

    print(f"\n{'報表':<26}{'格數':>6}{'正確':>6}{'錯值':>6}{'假零':>6}{'缺漏':>6}"
          f"{'多餘':>6}{'準確率':>9}{'恆等式':>12}")
    agg = dict.fromkeys(("correct", "wrong_value", "false_zero", "missing", "spurious"), 0)
    ident_ok = ident_bad = 0
    for r in results:
        c = r["counts"]
        for k in agg:
            agg[k] += c[k]
        ident_ok += r["identity_passed"]
        ident_bad += r["identity_failed"]
        acc = "—" if r["cell_accuracy"] is None else f"{r['cell_accuracy'] * 100:.1f}%"
        ident = f"{r['identity_passed']}/{r['identity_passed'] + r['identity_failed']}"
        print(
            f"{r['key']:<26}{r['total_cells']:>6}{c['correct']:>6}{c['wrong_value']:>6}"
            f"{c['false_zero']:>6}{c['missing']:>6}{c['spurious']:>6}{acc:>9}{ident:>12}"
        )

    total = sum(agg.values())
    print(f"\n=== 合計 {total} 格 ===")
    for k, v in agg.items():
        print(f"  {k:<12}{v:>6}  ({v / total * 100:.2f}%)")
    print(f"\n儲存格準確率：{agg['correct']}/{total} = {agg['correct'] / total * 100:.2f}%")
    print(f"恆等式：{ident_ok}/{ident_ok + ident_bad} 通過")

    dangerous = agg["false_zero"] + agg["wrong_value"] + agg["spurious"]
    print(
        f"\n⚠ 會產生錯誤財務陳述的格數（假零＋錯值＋多餘）："
        f"{dangerous} ({dangerous / total * 100:.2f}%)"
    )
    miss_pct = agg["missing"] / total * 100
    print(f"  安全的缺漏（可由人工補）：{agg['missing']} ({miss_pct:.2f}%)")

    for r in results:
        if r["details"] or r["extra_labels"] or r["reported_issues"]:
            print(f"\n--- {r['key']} ---")
            for d in r["details"][:12]:
                print(f"  {d}")
            if len(r["details"]) > 12:
                print(f"  …另有 {len(r['details']) - 12} 項")
            if r["extra_labels"]:
                print(f"  ground truth 未收錄的項目：{r['extra_labels']}")
            for issue in r["reported_issues"]:
                print(f"  模型自報問題：{issue}")


if __name__ == "__main__":
    main()
