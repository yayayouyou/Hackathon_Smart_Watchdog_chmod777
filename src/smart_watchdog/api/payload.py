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
import math
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


# ── 行政區派工優先序 ──────────────────────────────────────────────────
#
# ⚠️ **不做型態校正的區級排名，實質上是在排「哪一區私立園比較多」。**
# 實測 corr(私立占比, 區平均風險分數) = 0.93、corr(私立占比, 原始進榜率) = 0.59；
# 型態別進榜率差 16 倍（公立 0.68% ／ 非營利 5.77% ／ 私立 10.96%）。
# 全市 7 個最小的區（坪林、烏來、平溪、石碇、雙溪、石門、貢寮）是 100% 公立，
# 它們墊底不是因為安全，是因為組成不同。
# 這是 CLAUDE.md「任何在公立／非營利／私立間有結構性差異的特徵，分層前都會
# 看起來很強」在區級的重演——繼 `monthly`（p 0.00066→0.050）與 `max_jump`
# （rbc +0.399→+0.097）之後的第三次。
#
# 所以這裡算的是**間接標準化的建議查核密度比（SIR）**：以全市的型態別進榜率
# 當標準人口，算出「這一區的型態組成，在全市平均水準下該有幾家進榜」，再跟
# 實際家數比。
#
# 兩道機制缺一不可，壓力測試證明過：
#   * 只用信賴下界擋不住小區暴衝——坪林（2 園、全公立、exp=0.0136）只要多
#     1 家進榜，點估 73.5×，取 Byar 下界仍有 12.6。所以要有曝險閘門。
#   * 只用曝險閘門擋不住「大但不確定」——八里（exp=1.13）若有 3 家進榜，
#     點估 2.65× 會排到第 2，但下界只有 0.68。所以要用下界排序。
#
# 合起來：大區不會因為家數多而自動奪冠（板橋 13 家全市最多，SIR 0.87 排第 10），
# 小區不會因為 1 家而衝第一（exp<1 直接不給名次）。

#: 曝險閘門。`exp < 1` 的意思是「在全市平均水準下，這一區連一家都不該出現」，
#: 此時看到 0 家或 1 家都無法區分是真的沒問題還是我們沒看到。
#: ⚠️ 這是**人選的門檻**，沒有經過外部驗證，而且是唯一一個會把整個區移出榜單
#: 的參數。1.0 的好處是它是可解釋的自然刻度，不是調參結果。
#: 敏感度：門檻放寬到 0.5 會多放進瑞芳（exp 0.66）與三芝（0.68），兩者 obs 都
#: 是 0，名次不變。
MIN_EXPECTED = 1.0

#: 五級。**地圖底色、地圖清單、04 分析驗證三處共用這一個定義。**
#: 各自寫一份門檻的話，同一個區在兩個房間會是兩種顏色——改版前真的發生過：
#: 地圖底色畫的是絕對家數，板橋（13 家）最深；分析驗證卻把它排第 10。
#:
#: 「明顯」＝95% 區間整段在 1 的同一側（就是 band 的高於／低於全市）。
#: 「略」＝點估計偏向一側、但區間跨過 1。
#: 把「與全市相當」拆成略高／略低，是因為 k=100 時 17 個有名次的區有 13 個落在
#: 那一帶，只畫三帶的話整張圖是同一個顏色。拆的依據是點估計，**不是**顯著性，
#: 所以用詞一律寫「略」，不寫成已確定的高低。
#:
#: 資料不足永遠排在最後，但不隱藏——全市 41% 的行政區落在這一帶，把它們收進
#: 「顯示更多」等於用介面把不確定性藏起來。
LEVELS = {4: "明顯高於全市", 3: "略高於全市", 2: "略低於全市",
          1: "明顯低於全市", 0: "資料不足"}


def _level(band: str, sir: float | None) -> int:
    if band == "資料不足":
        return 0
    if band == "高於全市":
        return 4
    if band == "低於全市":
        return 1
    return 3 if (sir or 0) >= 1 else 2


def _byar(obs: int, exp: float) -> tuple[float, float]:
    """Poisson 計數的 Byar 95% 信賴區間（封閉解，不必模擬）。

    用 Byar 而不是常態近似，是因為多數區的 obs 是個位數——鶯歌 6 家、五股 5 家。
    常態近似在 obs<10 時下界會掉到負的。
    """
    if exp <= 0:
        return (0.0, 0.0)
    lo = 0.0 if obs == 0 else (
        obs * (1 - 1 / (9 * obs) - 1 / (3 * math.sqrt(obs))) ** 3 / exp)
    o1 = obs + 1
    hi = o1 * (1 - 1 / (9 * o1) + 1 / (3 * math.sqrt(o1))) ** 3 / exp
    return (max(0.0, lo), hi)


def district_board(points: list[dict], *, k: int = 100,
                   heat: dict | None = None) -> dict[str, Any]:
    """各行政區的建議查核密度，已扣除公立／非營利／私立的組成差異。

    ``k`` 是本期派工容量（政策數字，不是統計門檻）。前端做成滑桿，因為排序對
    它敏感：Spearman 相對 k=100，k=50 是 0.972、k=150 掉到 0.721、k=200 是
    0.589。前三名在 k=50~300 都穩定，第 4–15 名會洗牌。把敏感度攤開來看，
    比藏起來誠實。

    ⚠️ **這是關於「我們這份派工名單」的陳述，不是關於一個地方的陳述。**
    29 個行政區是有居民、有園所、有名譽的真實地點，資料完全不支持「某區的
    孩子比較不安全」這種地理性斷言。所有面向使用者的措辭都要守住這條線。

    ⚠️ 前 100 名有 94% 帶既有裁罰紀錄（401 名之後只有 14.8%），所以這張榜
    實質上是「型態校正後的既有裁罰集中度」，是**回顧不是預測**。
    """
    heat = heat or {}
    big = 10 ** 9
    flagged = [p for p in points if (p.get("r") or big) <= k]

    # 標準人口＝全市，依機構類別分層。
    n_by_t: dict[int, int] = {}
    f_by_t: dict[int, int] = {}
    for p in points:
        n_by_t[p["t"]] = n_by_t.get(p["t"], 0) + 1
    for p in flagged:
        f_by_t[p["t"]] = f_by_t.get(p["t"], 0) + 1
    base = {t: (f_by_t.get(t, 0) / n) if n else 0.0 for t, n in n_by_t.items()}

    by: dict[str, list[dict]] = {}
    for p in points:
        by.setdefault(p["d"], []).append(p)

    rows = []
    for name, rs in by.items():
        obs = sum(1 for p in rs if (p.get("r") or big) <= k)
        exp = sum(base.get(p["t"], 0.0) for p in rs)
        lo, hi = _byar(obs, exp)
        if exp < MIN_EXPECTED:
            band = "資料不足"
            sir = lo = hi = None
        else:
            sir = obs / exp
            band = ("高於全市" if lo > 1 else
                    "低於全市" if hi < 1 else "與全市相當")
        rows.append({
            "d": name, "n": len(rs), "obs": obs, "exp": round(exp, 2),
            "sir": None if sir is None else round(sir, 2),
            "lo": None if lo is None else round(lo, 2),
            "hi": None if hi is None else round(hi, 2),
            "band": band,
            "level": _level(band, sir),
            "level_label": LEVELS[_level(band, sir)],
            # 本區進榜的機構，依全市名次排。地圖清單直接拿這個分組，不在前端
            # 再 filter 一次——兩份 filter 遲早會不一致。
            "ids": [p["i"] for p in sorted(rs, key=lambda p: p.get("r") or big)
                    if (p.get("r") or big) <= k],
            "pub": sum(1 for p in rs if p["t"] == 0),
            "npo": sum(1 for p in rs if p["t"] == 1),
            "prv": sum(1 for p in rs if p["t"] == 2),
            "pen": sum(1 for p in rs if (p.get("np") or 0) > 0),
            "fin": sum(1 for p in rs if p.get("fin")),
            # 輿情只當旗標，不進排序：全市只有 3 園 tier>0，樣本量撐不起區級
            # 排序，而且即時層本來就不回答「誰未來會違規」。
            "hot": sum(1 for p in rs if (heat.get(p["i"]) or {}).get("tier")),
            # exp 的分項展開，讓人當場心算驗證得了名次是怎麼來的。
            "exp_parts": [
                {"t": t,
                 "n": sum(1 for p in rs if p["t"] == t),
                 "base": round(base.get(t, 0.0), 4),
                 "exp": round(sum(base.get(t, 0.0) for p in rs if p["t"] == t), 2)}
                for t in sorted(n_by_t)],
        })

    # 等級高的先；同級內依信賴下界（大而不確定的往下壓）；資料不足最後。
    rows.sort(key=lambda r: (r["level"] == 0, -r["level"], -(r["lo"] or 0), -r["obs"]))
    rank = 0
    for r in rows:
        if r["band"] == "資料不足":
            r["rank"] = None
        else:
            rank += 1
            r["rank"] = rank

    return {
        "k": k,
        "population": len(points),
        "flagged": len(flagged),
        "base": {str(t): round(v, 4) for t, v in sorted(base.items())},
        "min_expected": MIN_EXPECTED,
        "ranked": rank,
        "insufficient": sum(1 for r in rows if r["band"] == "資料不足"),
        "levels": [{"level": lv, "label": LEVELS[lv],
                    "n": sum(1 for r in rows if r["level"] == lv)}
                   for lv in (4, 3, 2, 1, 0)],
        "rows": rows,
        # 這句要印在表格下方，不是埋在 docs。
        "caveat": ("名次代表建議先看的順序，不是違法認定。密度已扣除公立／"
                   "非營利／私立的組成差異；前 100 名有 94% 帶既有裁罰紀錄，"
                   "所以這張榜是既有紀錄的集中度，不是對未來的預測。"),
    }


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


def peer_difference(anomaly) -> dict[str, Any]:
    """Per-報告代號 peer-relative financial difference, latest year plus history.

    Kept separate from ``findings`` in the payload on purpose. A compliance
    finding is a statement about a filed document; this is a statement about how a
    園 compares to the others that filed the same year, and the two must not share
    a colour, a heading or a sentence. See ``features/anomaly.py``.

    Only the latest 學年度 is the 園's current position. Earlier years go into a
    trend, never into a maximum -- taking a 園's worst historical year as its
    present state would keep a 園 flagged for something it filed three years ago.
    """
    if anomaly is None or len(anomaly) == 0:
        return {}
    out: dict[str, Any] = {}
    for code, group in anomaly.groupby("code"):
        rows = group.sort_values("academic_year")
        history = [
            {"y": int(r.academic_year),
             "pct": None if _isna(r.percentile_in_year) else float(r.percentile_in_year),
             "rank": None if _isna(r.rank_in_year) else int(r.rank_in_year),
             "peers": int(r.n_peers)}
            for r in rows.itertuples(index=False)
        ]
        last = rows.iloc[-1]
        # An empty cell arrives from pandas as NaN, and str(NaN) is the truthy
        # string "nan". Filtering on truthiness alone therefore published three
        # fabricated 異常 entries for every 園 that had none -- the exact failure
        # the anomaly/contribution split exists to prevent.
        anomalies = [t for t in (_text(last[c])
                                 for c in ("anomaly_1", "anomaly_2", "anomaly_3")) if t]
        contributions = [t for t in (
            _text(last[c])
            for c in ("contribution_1", "contribution_2", "contribution_3")) if t]
        out[str(code)] = {
            "y": int(last["academic_year"]),
            "pct": None if _isna(last["percentile_in_year"])
            else float(last["percentile_in_year"]),
            "rank": None if _isna(last["rank_in_year"]) else int(last["rank_in_year"]),
            "peers": int(last["n_peers"]),
            "nfeat": int(last["n_features"]),
            "anomalies": anomalies,
            "contributions": contributions,
            "history": history,
        }
    return out


def _isna(v: Any) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


def _text(v: Any) -> str:
    """A CSV cell as a display string, with NaN and the literal "nan" as empty."""
    if _isna(v):
        return ""
    s = str(v).strip()
    return "" if s.lower() == "nan" else s


def dossiers(
    extract_dir: pathlib.Path,
    crosswalk,
    findings,
    timeseries,
    is_opening_year,
    anomaly=None,
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

    peer = peer_difference(anomaly)

    return {
        code: {
            "ids": sorted(ids),
            "name": series.get(code, {}).get("name", ""),
            "series": series.get(code, {}).get("years", []),
            "staff": staff.get(code, []),
            "findings": sorted(per_code.get(code, []),
                               key=lambda f: (-f["y"], f["st"] != "fail")),
            "gaps": gaps.get(code, []),
            "peer": peer.get(code),
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



#: 事件類別的嚴重度序。兒少安全在最前面不是因為模型說的，是因為幼照法把
#: 「不當對待」放在最重的一類，而公共化園 63 筆裁罰裡第33條就佔 42 筆。
CATEGORY_ORDER = ("兒少安全", "營運穩定", "財務收費")

#: 新鮮度權重。同一則報導在三天前與在三年前，對「現在要不要派人」的意義
#: 完全不同。分級而不是連續衰減，是因為要能對著畫面講清楚為什麼是這個大小。
_RECENCY = ((7, 1.0), (30, 0.6), (90, 0.3), (365, 0.1))


def _label_key(row) -> str:
    from ..realtime import news_classify

    return news_classify.key_for(str(row.channel), str(row.url),
                                 str(row.headline)[:180])


def _news_labels() -> dict[str, dict]:
    """讀分類標籤。兩份都讀，順序有意義。

    `data/external/news_labels_public.jsonl` 是進版控的那一份（只有標籤與
    provenance，沒有任何一個字的內文——`scripts/export_news_labels.py` 產生）。
    `data/runtime/news_labels.jsonl` 是本機跑分類留下的完整紀錄，整個
    `data/runtime/` 都 gitignore，因為那裡面留著送進模型的標題節錄。

    **後者蓋前者**：在這台機器上重跑過分類的人，看到的要是自己剛跑出來的結果，
    不是版控裡的舊快照。兩份都沒有就回空——那代表「尚未分類」，
    不是「沒有問題」。
    """
    import json

    root = pathlib.Path(__file__).resolve().parents[3]
    out: dict[str, dict] = {}
    for path in (root / "data/external/news_labels_public.jsonl",
                 root / "data/runtime/news_labels.jsonl"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("key"):
                out[row["key"]] = row
    return out


def _heat(items: list[dict]) -> dict:
    """近期公開報導的量與新鮮度。

    ⚠️ **這不是風險分數，也不是我們算的排序。** 它數的是「有幾家媒體在談、
    多久以前談的」——兩者都是既有的公開事實，所以它可以上地圖
    （`docs/architecture/aws-architecture.md` §6.5 禁止的是把我們算出來的
    風險分數畫到圖上）。

    只算**事件報導**與**爭議未定**：例行報導（沿革介紹、招生公告）不是事件，
    把它算進發酵程度會讓一所園因為被寫了一篇校史而變大點。尚未分類的一律
    計入但標出來——不確定時不可以當成沒事。

    即時這一層**不做回測驗證**。它回答的不是「誰未來會違規」而是「現在正在
    發生什麼」；一則「教育局已開罰 39 萬」的報導不是預測，那筆裁罰就是報導
    本身在講的事，拿它去測預測力是範疇錯誤。判準是事件的類別與量，
    不是提升倍數。
    """
    import datetime as dt

    today = dt.date.today()
    score = 0.0
    counted = 0
    unclassified = 0
    cats: dict[str, int] = {}
    latest = ""
    for m in items:
        rk = m.get("rk") or ""
        if rk == "例行報導":
            continue
        if not rk:
            unclassified += 1
        try:
            d = dt.date.fromisoformat(str(m.get("d"))[:10])
        except ValueError:
            continue
        days = (today - d).days
        w = next((v for lim, v in _RECENCY if days <= lim), 0.0)
        if w == 0.0:
            continue
        score += w
        counted += 1
        cat = m.get("cat") or ""
        if cat in CATEGORY_ORDER:
            cats[cat] = cats.get(cat, 0) + 1
        latest = max(latest, str(m.get("d") or ""))

    top = min(cats, key=lambda c: CATEGORY_ORDER.index(c)) if cats else ""
    # 三級，門檻寫死並印在圖例上：看得到的東西要說得出為什麼是這個大小。
    tier = 3 if score >= 2.0 else 2 if score >= 0.6 else 1 if score > 0 else 0
    return {"n": counted, "score": round(score, 2), "tier": tier,
            "cat": top, "latest": latest, "unclassified": unclassified,
            "total": len(items)}


def realtime(mentions, stamp: dict[str, Any]) -> dict[str, Any]:
    """Live-channel mentions, grouped by 園, with the sweep's own provenance.

    ``channels`` travels with the mentions on purpose. A published page cannot
    call the channels itself -- the Artifact CSP blocks fetch entirely -- so the
    panel shows a snapshot, and a snapshot that does not say when it was taken
    or how many channels were listening reads as "nothing is happening here".
    Two of five channels are live; the other three are waiting on a key, an app
    review, or a procurement, and the panel says so.
    """
    labels = _news_labels()
    by_institution: dict[str, list[dict]] = {}
    for r in mentions.itertuples(index=False):
        lab = labels.get(_label_key(r)) or {}
        by_institution.setdefault(str(r.institution_id)[:8], []).append({
            "ch": str(r.channel), "h": str(r.headline)[:180],
            "u": str(r.url), "d": str(r.published),
            "p": str(r.publisher), "k": str(r.kind),
            # 分類結果。缺的時候是「尚未分類」——不是「沒有問題」，
            # 也不是「語氣中性」。前端必須照這個分別顯示。
            "rk": lab.get("report_kind", ""),
            "cat": lab.get("event_category", ""),
        })
    for items in by_institution.values():
        items.sort(key=lambda m: m["d"], reverse=True)
    return {
        "swept_at": stamp.get("swept_at", ""),
        "channels_live": stamp.get("channels_live", 0),
        "channels_total": stamp.get("channels_total", 0),
        "channels": stamp.get("channels", []),
        "by_institution": by_institution,
        "heat": {k: _heat(v) for k, v in by_institution.items()},
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
