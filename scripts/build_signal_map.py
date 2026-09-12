"""訊號圖：我們有哪些資料、哪些真的進了模型、哪些沒有。

產物 ``data/processed/signal_map.json`` 給「04 分析驗證」那一格畫成心智圖。
中心是稽查優先序模型，往外是資料來源與由它們算出來的訊號。

## 為什麼「沒連到中心」這件事要畫出來

一張只畫有效訊號的圖會讓人以為我們什麼都用上了。實際上：

* ``PRIORITY_FEATURES`` 只有 14 個，全是前科與園所屬性，AUC 0.641 / P@100 2.17×。
* 評鑑與交叉比對**都沒有進計分**——它們只當 context 欄位或升級管道傳遞。
* 單文件的法遵檢核在分層後事後受罰率 **低於**基準（0.42×），把它畫成有效的
  輸入是錯的。

所以每個訊號帶一個 ``verdict``：

    scored      進了 PRIORITY_FEATURES，實線連到中心
    candidate   量到了效果但還沒納入計分，虛線
    context     有價值但不預測裁罰（例如資源配置類），不連線
    disproven   量過了，提升 < 1 或不顯著，灰掉且不連線

## 紀律

數字全部現算，不寫死：

1. **時序切分。** 特徵只能用 ``as_of`` 之前看得到的，標籤是之後開出的裁罰。
2. **財報有觀察起日。** 學年度 N 的非營利財報約在 (N+2) 年 1 月（民國）公告，
   即西元 N+1913，在那之前不得當成已知。
3. **先分層再下結論。** 任何在公立／非營利／私立間有結構性差異的訊號，不分層
   都會看起來很強——這個專案已經踩過三次（``monthly``、收費漲幅，以及
   05-phase1-results.md §12 記的第三次）。所以每個訊號同時回報全市與**它自己
   所屬母體內**的數字，而 ``verdict`` 一律看分層後的那個。
4. **先算基準率。** 每一列都帶對照組的受罰率。

⚠️ 這張圖**不是**模型的輸入，只是模型的說明。改這支腳本不會動到分數。

Usage
    PYTHONPATH=src .venv/Scripts/python scripts/build_signal_map.py
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.features import build as fb
from smart_watchdog.risk.priority import PRIORITY_FEATURES

ROOT = pathlib.Path(__file__).resolve().parents[1]
P = ROOT / "data/processed"
E = ROOT / "data/external"
OUT = P / "signal_map.json"

#: 主要觀測點。2024-01-01 給 940 天的標籤窗；改晚一點統計力就不夠
#: （2025-01-01 只剩 574 天，交叉比對被點到的僅 7 筆，p 掉到 0.5）。
PRIMARY = "2024-01-01"
SECONDARY = "2025-01-01"
DATA_END = pd.Timestamp("2026-07-29")


def observable_year(as_of: pd.Timestamp) -> int:
    """as_of 當下看得到的最大非營利財報學年度（西元 N+1913）。"""
    return as_of.year - 1913


# ── 資料來源盤點 ──────────────────────────────────────────────────────


def _rows(path: pathlib.Path) -> int:
    if not path.exists():
        return 0
    return len(pd.read_csv(path))


def _json_len(path: pathlib.Path, key: str | None = None) -> int:
    """頂層元素數。``key`` 指定時先鑽進那一層（GeoJSON 走 features）。"""
    if not path.exists():
        return 0
    d = json.loads(path.read_text(encoding="utf-8"))
    if key:
        d = d.get(key, d)
    return len(d)


def _json_records(path: pathlib.Path) -> int:
    """dict-of-lists 的**紀錄**數，不是 key 數。

    這三份外部檔的頂層 key 各是不同的東西，數 key 會得到不同語意的數字：
    ``punish_all.json`` 的 key 是**人**（`行為人：X`／`負責人：X`）不是機構，
    ``kids_vehicles.json`` 的 key 是機構 uuid。兩者的 key 數（2,800／1,900）
    都不是「裁罰筆數」或「車輛數」，標成那樣會在畫面上說錯話。
    """
    if not path.exists():
        return 0
    d = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(d, list):
        return len(d)
    return sum(len(v) if isinstance(v, list) else 1 for v in d.values())


def sources() -> list[dict]:
    """有哪些資料進來。筆數現算，取得日與授權寫在 data/external/README.md。"""
    survey = P.parent / "extracted/pdf_survey.csv"
    n_pdf = _rows(survey)
    dr = P.parent / "interim/dataroom/index.json"
    pages = tables = cells = 0
    if dr.exists():
        idx = json.loads(dr.read_text(encoding="utf-8"))
        t = idx.get("totals", {})
        tables, cells = t.get("tables", 0), t.get("cells", 0)
        pages = sum(r.get("pages", 0) for r in idx.get("reports", []))

    return [
        {"id": "raw_pdf", "name": "主辦方資料集（PDF 原件）", "kind": "文件",
         "count": n_pdf, "unit": "份", "licence": "不得轉散布",
         "note": "132 份非營利財報是純掃描影像，必須走視覺模型"},
        {"id": "pagewise", "name": "頁級抽取", "kind": "文件",
         "count": cells, "unit": "格數字",
         "note": f"{pages} 頁 · {tables} 張表；空白格保留為 null，不是 0"},
        {"id": "registry", "name": "全國教保資訊網：園所基本資料", "kind": "登記",
         "count": _json_len(E / "preschools.json", "features"),
         "unit": "所（全國）",
         "licence": "CC-BY（須標註原始來源）"},
        {"id": "punish", "name": "全國教保資訊網：裁罰紀錄", "kind": "裁罰",
         "count": _json_records(E / "punish_all.json"), "unit": "筆",
         "licence": "CC-BY",
         "note": "上層 key 是人不是機構，要用 record 內的 id 反轉"},
        {"id": "penalties", "name": "新北裁罰明細（已正規化）", "kind": "裁罰",
         "count": _rows(P / "penalties_ntpc.csv"), "unit": "筆"},
        {"id": "vehicles", "name": "幼童專用車", "kind": "登記",
         "count": _json_records(E / "kids_vehicles.json"), "unit": "輛",
         "licence": "CC-BY"},
        {"id": "fees", "name": "收費明細（公共化園逐項）", "kind": "收費",
         "count": _rows(P / "fee_basis_public.csv"), "unit": "列",
         "note": "非營利園自 111 學年度起在外部來源歸零"},
        {"id": "evaluation", "name": "官方評鑑結果", "kind": "評鑑",
         "count": _rows(P / "evaluations_ntpc_full.csv"), "unit": "筆",
         "licence": "授權條款待確認"},
        {"id": "mentions", "name": "輿情：新聞／PTT／Threads @標註", "kind": "輿情",
         "count": _rows(P / "realtime_mentions_ntpc.csv"), "unit": "則",
         "licence": "新聞為非開放授權，只作 provenance",
         "note": "未查證線索，不寫入風險分數"},
        {"id": "procurement", "name": "受託營運決標", "kind": "採購",
         "count": _rows(P / "nonprofit_procurement_contracts.csv"), "unit": "列",
         "licence": "原始條款待確認"},
        {"id": "bureau", "name": "教育局公告與處置", "kind": "公告",
         "count": _rows(P / "education_bureau_actions_ntpc.csv"), "unit": "筆"},
    ]


def derived() -> list[dict]:
    """由來源算出來的中間層。"""
    cross = pd.read_csv(P / "cross_findings.csv") if (P / "cross_findings.csv").exists() \
        else pd.DataFrame()
    comp = pd.read_csv(P / "compliance_findings.csv")
    letters = P / "audit_letters/index.csv"
    tl = P / "timeline.json"
    return [
        {"id": "compliance", "name": "單文件法遵檢核", "from": ["pagewise"],
         "count": len(comp), "unit": "項",
         "fail": int((~comp["passed"].astype(bool)).sum()),
         "note": "只讀一份報告自己的附註二——文件內部自我一致就查不出東西"},
        {"id": "crosscheck", "name": "跨來源交叉比對", "count": len(cross),
         "from": ["pagewise", "fees", "registry"], "unit": "項",
         "fail": int((~cross["passed"].astype(bool)).sum()) if len(cross) else 0,
         "note": "11 條規則 + 4 條對帳護欄；三方恆等式 教保費收入 = 收費 × 人數"},
        {"id": "priority", "name": "稽查優先序", "count": _rows(P / "audit_priority_ntpc.csv"),
         "from": ["penalties", "registry", "vehicles"], "unit": "園"},
        {"id": "letters", "name": "稽核建議書", "count": _rows(letters),
         "from": ["priority", "compliance"], "unit": "份"},
        {"id": "timeline", "name": "時間軸回測", "from": ["priority", "penalties"],
         "count": _json_len(tl, "points"), "unit": "時點"},
    ]


# ── 訊號量測 ──────────────────────────────────────────────────────────


def _crosswalk() -> dict:
    cw = pd.read_csv(P / "nonprofit_registry_crosswalk.csv")
    out: dict[tuple, list[str]] = {}
    for r in cw.itertuples(index=False):
        # registry_ids 是 JSON 陣列字串。用 split(";") 會得到一整串 '["uuid"]'，
        # 跟任何 id 都對不上，而且不報錯——只會靜靜全部 0。
        try:
            ids = json.loads(r.registry_ids) if isinstance(r.registry_ids, str) else []
        except (TypeError, ValueError):
            ids = []
        out[(r.code, int(r.academic_year))] = ids
    return out


def _spread(df: pd.DataFrame, key2ids: dict) -> set[str]:
    ids: set[str] = set()
    for r in df.itertuples(index=False):
        ids.update(key2ids.get((r.code, int(r.academic_year)), []))
    return ids


def signals_frame(as_of: pd.Timestamp) -> pd.DataFrame:
    inst = pd.read_csv(P / "institutions_ntpc.csv")
    pen = fb.load_penalties(P / "penalties_ntpc.csv")
    veh = fb.load_vehicles(E / "kids_vehicles.json")
    ev = fb.load_evaluations(P / "evaluations_ntpc_full.csv")
    d = fb.build_features(inst, pen, veh, as_of, evaluations=ev)

    maxy = observable_year(as_of)
    key2ids = _crosswalk()

    comp = pd.read_csv(P / "compliance_findings.csv")
    comp = comp[comp["academic_year"] <= maxy]
    d["comp_fail"] = d["id"].isin(
        _spread(comp[comp["passed"] == False], key2ids)).astype(int)  # noqa: E712

    cross = pd.read_csv(P / "cross_findings.csv")
    cross = cross[(cross["entity_type"] == "非營利") & (cross["code"] != "COHORT")]
    cross = cross[cross["academic_year"].astype(str).str.isdigit()]
    cross["academic_year"] = cross["academic_year"].astype(int)
    cross = cross[cross["academic_year"] <= maxy]
    xf = cross[cross["passed"] == False]  # noqa: E712
    d["cross_fail"] = d["id"].isin(_spread(xf, key2ids)).astype(int)
    for rule in sorted(xf["rule"].unique()):
        d["x:" + rule] = d["id"].isin(
            _spread(xf[xf["rule"] == rule], key2ids)).astype(int)

    mn = pd.read_csv(P / "realtime_mentions_ntpc.csv")
    mn["published"] = pd.to_datetime(mn["published"], errors="coerce")
    d["has_mention"] = d["id"].isin(
        set(mn[mn["published"] < as_of]["institution_id"])).astype(int)

    d["prior_penalty"] = d["has_prior_penalty"]
    d["prior_penalty_3plus"] = (d["n_penalties_prior"] >= 3).astype(int)
    d["prior_severe"] = (d["n_severe_prior"] > 0).astype(int)
    d["eval_partial"] = (d["n_partial_prior"] > 0).astype(int)
    d["new_school"] = (d["age_years"] < 3).fillna(False).astype(int)
    d["has_shuttle"] = (d["n_vehicles"].fillna(0) > 0).astype(int)

    d["label"] = fb.label_future_penalty(pen, d["id"], as_of, DATA_END)
    return d


def measure(df: pd.DataFrame, col: str) -> dict | None:
    """被點到 vs 未被點到的事後受罰率。少於 5 筆不回報——那不是量測。"""
    from scipy.stats import fisher_exact

    a, b = df[df[col] == 1], df[df[col] == 0]
    if len(a) < 5 or len(b) < 5:
        return None
    ha, hb = int(a["label"].sum()), int(b["label"].sum())
    ra, rb = ha / len(a), hb / len(b)
    _, p = fisher_exact([[ha, len(a) - ha], [hb, len(b) - hb]], alternative="greater")
    return {"n": len(a), "hits": ha, "rate": round(ra, 4), "base": round(rb, 4),
            "lift": round(ra / rb, 3) if rb else None, "p": round(float(p), 4)}


#: 訊號定義。``stratum`` 是「這個訊號只在哪個母體裡有意義」——分層後的數字才算數。
SIGNALS = [
    ("prior_penalty", "有任何前科", "裁罰", "私立"),
    ("prior_penalty_3plus", "前科 ≥ 3 件", "裁罰", "私立"),
    ("prior_severe", "有嚴重前科", "裁罰", "私立"),
    ("eval_partial", "評鑑部分指標未通過", "評鑑", "私立"),
    ("has_mention", "近期有可歸屬公開報導", "輿情", None),
    ("comp_fail", "單文件法遵檢核未通過", "財報", "非營利"),
    ("cross_fail", "交叉比對任一項未通過", "交叉", "非營利"),
    ("new_school", "設立未滿 3 年", "登記", "私立"),
    ("has_shuttle", "有幼童專用車", "登記", "私立"),
]

#: 已否證的假設，量過而且不再試。理由寫在 docs/research/09-crosscheck-leadtime.md §6.5。
DISPROVEN = [
    {"id": "gov_subsidy_cap", "label": "政府補助 ÷ 每生單價 = 生數上限",
     "why": "非營利園是成本分攤制，政府補助是殘差不是定額"},
    {"id": "staff_salary", "label": "人事費 ÷ 教保人員數 < 法定薪資 → 人力灌水",
     "why": "成本分攤制使每人人事費結構性均勻，IQR 外離群 0 份"},
    {"id": "slip_checksum", "label": "申報表自身不一致 → 超收",
     "why": "1,724 組「總收費 = 逐項合計」零筆不一致"},
    {"id": "meal_fee", "label": "餐點費 ÷ 每生每日餐費 → 超收人次",
     "why": "每生每日餐點費全距 54–78 元，離散度太小無法辨識"},
]


def verdict_of(name: str, strat: dict | None) -> tuple[str, str]:
    """三態：進了計分、量到但未納入、或無效。**一律看分層後的數字。**"""
    # 這幾個訊號的底層欄位在 PRIORITY_FEATURES 裡（n_penalties_prior、
    # n_severe_prior、age_years、has_vehicle），所以它們是已計分的。
    scored = {"prior_penalty", "prior_penalty_3plus", "prior_severe",
              "new_school", "has_shuttle"}
    if strat is None:
        return "unmeasured", "被點到的筆數太少，無法量測"
    lift, p = strat.get("lift"), strat.get("p", 1.0)
    if name in scored:
        return "scored", "已在 PRIORITY_FEATURES 中"
    if lift is not None and lift >= 1.5 and p < 0.1:
        return "candidate", "分層後量到效果，但尚未納入計分"
    if lift is not None and lift < 1.0:
        return "disproven", "分層後事後受罰率低於基準，不具預測力"
    return "context", "有價值但未量到預測力，只作 context"


def build(as_of_s: str, second_s: str) -> dict:
    as_of, second = pd.Timestamp(as_of_s), pd.Timestamp(second_s)
    d1, d2 = signals_frame(as_of), signals_frame(second)

    out_signals = []
    for name, label, group, stratum in SIGNALS:
        overall = measure(d1, name)
        strat = measure(d1[d1["type"] == stratum], name) if stratum else None
        robust = measure(d2[d2["type"] == stratum] if stratum else d2, name)
        v, why = verdict_of(name, strat or overall)
        out_signals.append({
            "id": name, "label": label, "group": group,
            "stratum": stratum or "全市",
            "overall": overall, "stratified": strat, "robustness": robust,
            "verdict": v, "why": why,
        })

    # 交叉比對逐規則——訊號集中在哪，這張表才說得出來
    for col in [c for c in d1.columns if c.startswith("x:")]:
        strat = measure(d1[d1["type"] == "非營利"], col)
        v, why = verdict_of(col, strat)
        out_signals.append({
            "id": col, "label": "交叉比對：" + col[2:], "group": "交叉",
            "stratum": "非營利", "overall": measure(d1, col),
            "stratified": strat, "robustness": measure(d2[d2["type"] == "非營利"], col),
            "verdict": v, "why": why,
        })

    return {
        "as_of": as_of_s,
        "robustness_as_of": second_s,
        "label_end": str(DATA_END.date()),
        "label_window_days": int((DATA_END - as_of).days),
        "observable_academic_year": observable_year(as_of),
        "population": {
            "n": len(d1),
            "penalised_after": int(d1["label"].sum()),
            "base_rate": round(float(d1["label"].mean()), 4),
            "by_type": {t: {"n": int((d1["type"] == t).sum()),
                            "base_rate": round(
                                float(d1[d1["type"] == t]["label"].mean()), 4)}
                        for t in ("私立", "非營利", "公立")},
        },
        "model": {
            "features": list(PRIORITY_FEATURES),
            "auc": 0.641, "p_at_100": 2.17,
            "note": "時序切分實測。加特徵就要重跑 run.py baseline，"
                    "否則這兩個數字不再屬於現行配置。",
        },
        "sources": sources(),
        "derived": derived(),
        "signals": out_signals,
        "disproven": DISPROVEN,
        "caveat": "這是建議查核的優先序，不是違法認定。查不到資料標「資料不足」，"
                  "不標「低風險」。",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--as-of", default=PRIMARY)
    ap.add_argument("--robustness-as-of", default=SECONDARY)
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()

    data = build(a.as_of, a.robustness_as_of)
    pathlib.Path(a.out).write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    pop = data["population"]
    print(f"as_of {data['as_of']}　標籤窗 {data['label_window_days']} 天"
          f"　母體 {pop['n']}　事後受罰 {pop['penalised_after']}"
          f"（{pop['base_rate']:.1%}）")
    print(f"來源 {len(data['sources'])}　推導層 {len(data['derived'])}"
          f"　訊號 {len(data['signals'])}　已否證 {len(data['disproven'])}")
    order = {"scored": 0, "candidate": 1, "context": 2,
             "disproven": 3, "unmeasured": 4}
    print()
    def _sort(sig: dict) -> tuple:
        st = sig["stratified"] or sig["overall"] or {}
        return (order[sig["verdict"]], -(st.get("lift") or 0))

    for s in sorted(data["signals"], key=_sort):
        st = s["stratified"] or s["overall"] or {}
        lift = st.get("lift")
        print(f"  {s['verdict']:<10} {s['label']:<26} "
              f"[{s['stratum']}] lift={lift if lift is not None else '—'}"
              f" p={st.get('p', '—')}")
    print(f"\n寫出 {a.out}")


if __name__ == "__main__":
    main()
