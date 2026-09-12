"""產生時間軸回測資料：每一年重訓一次，看當時的排序後來對不對。

    python run.py timeline

產出 `data/processed/timeline.json`，前端的時間軸拖桿與 `/api/timeline` 都讀它。

協定與取捨寫在 `src/smart_watchdog/risk/timeline.py` 的模組說明；一句話版本是：
**每一格都重新訓練**，訓練與預測是兩份獨立的特徵快照，所以評分期的裁罰不可能
流進訓練特徵——這正是隨機切分會做錯、而這個專案從一開始就避開的事。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import warnings

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.console import use_utf8

use_utf8()

# numpy 2.x 在部分 BLAS 上對這個尺寸的 matmul 會發出偽警告，見 baseline_model.py。
warnings.filterwarnings("ignore", message=".*encountered in matmul")

from smart_watchdog.features.build import (
    load_evaluations,
    load_penalties,
    load_vehicles,
)
from smart_watchdog.risk.timeline import build_timeline

OUT = pathlib.Path("data/processed/timeline.json")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start-year", type=int, default=2021,
                    help="第一格的年份（預設 2021：更早的年份裁罰樣本過少）")
    ap.add_argument("--top-n", type=int, default=1500,
                    help="每格保留前 N 名（預設涵蓋全部機構）")
    a = ap.parse_args()

    inst = pd.read_csv("data/processed/institutions_ntpc.csv")
    pen = load_penalties("data/processed/penalties_ntpc.csv")
    veh = load_vehicles("data/external/kids_vehicles.json")
    ev = load_evaluations("data/processed/evaluations_ntpc_full.csv")

    tl = build_timeline(inst, pen, veh, evaluations=ev,
                        start_year=a.start_year, top_n=a.top_n)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(tl, ensure_ascii=False), encoding="utf-8")

    print(f"寫入 {OUT}  ({OUT.stat().st_size / 1024:.0f} KB)")
    print(f"\n資料截止 {tl['protocol']['data_end']}　"
          f"前瞻窗 {tl['protocol']['label_years']} 年\n")
    head = (f"{'預測時點':<12}{'訓練時點':<12}{'觀察':>6}{'母體':>6}"
            f"{'實際受罰':>8}{'基準率':>8}{'AUC':>7}{'P@100':>8}{'提升':>7}")
    print(head)
    print("─" * len(head))
    for p in tl["points"]:
        if not p["precision_at"]:
            print(f"{p['as_of']:<12}{p['train_as_of']:<12}"
                  f"{'即時':>6}{p['n_institutions']:>6}"
                  f"{'—':>8}{'—':>8}{'—':>7}{'—':>8}{'—':>7}")
            continue
        mark = "" if p["label_complete"] else "*"
        print(f"{p['as_of']:<12}{p['train_as_of']:<12}"
              f"{p['observed_fraction'] * 100:>5.0f}%{p['n_institutions']:>6}"
              f"{p['n_positive']:>8}{p['base_rate'] * 100:>7.1f}%"
              f"{p['auc'] or 0:>7.3f}{p['precision_at']['100'] * 100:>7.1f}%"
              f"{p['lift_at']['100']:>6.2f}x{mark}")
    s = tl["summary"]
    print(f"\n完整觀察 {s['n_complete']}/{s['n_points']} 格　"
          f"平均 AUC {s['mean_auc']}　平均提升 {s['mean_lift_at_100']}x")
    if any(not p["label_complete"] and p["precision_at"] for p in tl["points"]):
        print("* = 前瞻窗尚未走完，命中率為低估（還沒發生的裁罰不算數），"
              "不可與完整觀察的格子放在同一條趨勢線比較")


if __name__ == "__main__":
    main()
