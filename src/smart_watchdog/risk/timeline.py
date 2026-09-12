"""時間軸回測：把「當時我們會怎麼排」與「後來真的出了什麼事」放在一起看。

這個模組不引進任何新的建模想法。它做的是把 `features/build.py` 已經具備、
但看不見的紀律**變成看得見的東西**：每個特徵函式都吃 `as_of` 並拒絕看它之後
的資料，所以我們可以把時鐘倒回 2021 年，問「那一天系統會叫稽查員先去哪幾家」，
再把 2021–2023 實際受罰的園疊上去。

## 協定

每個時間點 T 都是一次獨立的前進式驗證（walk-forward），與
`scripts/baseline_model.py` 的窗口設定相同，只是滑動：

    訓練   as_of = T − 2 年，標籤 = [T−2年, T) 之間是否受罰
    預測   as_of = T           ← 只用 T 當天看得到的資料
    評分   標籤 = [T, T+2年) 之間是否受罰

訓練與預測是**兩份各自獨立建出來的特徵快照**，所以評分期的裁罰不可能流進
訓練特徵。這不是「同一個模型在不同年份的表現」，而是**每一年都重新訓練一次**
——因為 2021 年的稽查員不可能用 2024 年的資料訓練模型。

## 三種時間點，講的話不一樣

* **完整觀察**（T+2年 ≤ 資料截止）：命中率可以算，數字算數。
* **部分觀察**（前瞻窗只走了一半）：標籤右設限，命中率會**低估**，
  因為還沒發生的裁罰不算數。這種點一定標 `label_complete=False`，
  不可以跟完整觀察的點放在同一條趨勢線上比較。
* **即時**（今天）：沒有前瞻窗，只有排序，沒有命中率。這是系統實際運作的
  狀態——它永遠站在這一格，時間軸只是讓人看見它過去每一格都站得住。

## 為什麼命中率是主角而不是 AUC

教育局一年只能查固定家數。「排前 100 名裡有幾家後來真的受罰」是可以拿去排
人力的數字；AUC 是整體排序品質，看不出前段有沒有用。兩個都報，但**提升倍數**
（前 100 名命中率 ÷ 全體基準率）才是簡報上那一句話。
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd

from ..features.build import build_features, label_future_penalty
from .priority import (
    NEVER_PENALISED_DAYS,
    PRIORITY_FEATURES,
    fit_priority_model,
    usable_features,
)

#: 前瞻窗長度。與 baseline_model.py 相同，改動會讓已報告的數字失效。
LABEL_YEARS = 2

#: 訓練窗相對預測點的位移。訓練標籤必須在預測點之前就已經觀察完畢。
TRAIN_LAG_YEARS = 2

#: 一年可稽查家數的量級。前 k 名命中率是拿來排人力的數字。
TOP_K = (50, 100, 200)

#: 前端 payload 的點位用 `id[:8]` 當 key（見 api/payload.institution_points），
#: 時間軸要能跟地圖 join 就必須用同一把 key。完整 id 一併保留，
#: 因為短 id 只是顯示層的約定，不該變成資料層唯一的識別。
SHORT_ID_LEN = 8


@dataclasses.dataclass
class Point:
    """時間軸上的一格。"""

    as_of: str
    train_as_of: str
    label_start: str
    label_end: str
    label_complete: bool
    observed_fraction: float
    n_institutions: int
    n_positive: int
    base_rate: float
    auc: float | None
    precision_at: dict
    lift_at: dict
    ranking: list
    #: 這一格實際餵進模型的特徵。各年可用性不同（例如車籍快照日之前全空），
    #: 明確記下來，報告的數字才知道屬於哪一個特徵集。
    features_used: list = dataclasses.field(default_factory=list)

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def _short_ids(ids) -> list:
    """把機構 id 縮成 payload 用的短 key，並確認縮完仍然唯一。

    1,213 個 UUID 取前 8 碼目前無碰撞，但那是資料的性質不是保證。若某次資料
    更新產生碰撞，靜靜接受會讓時間軸把甲園的事後結果畫到乙園身上——那是最難
    發現、也最不能接受的一種錯。所以在這裡當場失敗。
    """
    short = [str(x)[:SHORT_ID_LEN] for x in ids]
    if len(set(short)) != len({str(x) for x in ids}):
        raise ValueError(
            f"機構 id 取前 {SHORT_ID_LEN} 碼後發生碰撞，"
            "時間軸無法安全地與地圖 payload join")
    return short


def _prepare(frame: pd.DataFrame) -> pd.DataFrame:
    """補上模型需要的衍生欄位。與 priority.prepare_frame 同一套規則。

    `days_since_last_penalty` 用大哨兵值而不是中位數插補：沒有前科的園並不是
    「距上次裁罰有平均那麼久」，而是根本沒有上一次。插補會替它憑空造出一段歷史。
    """
    d = frame.copy()
    d["is_private"] = (d["type"] == "私立").astype(int)
    d["days_since_last_penalty"] = d["days_since_last_penalty"].fillna(
        NEVER_PENALISED_DAYS)
    for col in ("days_since_evaluation",):
        if col in d.columns:
            d[col] = d[col].fillna(NEVER_PENALISED_DAYS)
    if "eval_partial" in d.columns:
        d["eval_partial"] = d["eval_partial"].fillna(0)
    return d


def _precision_at_k(y: np.ndarray, scores: np.ndarray, k: int) -> float:
    k = min(k, len(y))
    order = np.argsort(-scores)[:k]
    return float(y[order].mean()) if k else float("nan")


def evaluate_point(
    inst: pd.DataFrame,
    penalties: pd.DataFrame,
    vehicles: pd.DataFrame,
    as_of: pd.Timestamp,
    *,
    evaluations: pd.DataFrame | None = None,
    data_end: pd.Timestamp,
    top_n: int = 1500,
) -> Point:
    """算出一個時間點：當時的排序，以及後來實際發生了什麼。"""
    train_as_of = as_of - pd.DateOffset(years=TRAIN_LAG_YEARS)
    label_end = as_of + pd.DateOffset(years=LABEL_YEARS)

    train = _prepare(build_features(inst, penalties, vehicles, train_as_of,
                                    evaluations=evaluations))
    train_y = label_future_penalty(penalties, train["id"], train_as_of, as_of)

    test = _prepare(build_features(inst, penalties, vehicles, as_of,
                                   evaluations=evaluations))
    test_y = label_future_penalty(penalties, test["id"], as_of, label_end)

    # 特徵可用性是「在這兩份快照上」判斷的，不是寫死的清單。一個在早期年份
    # 全空的特徵（例如車輛快照日之前的車籍）無法插補，SimpleImputer 會靜靜
    # 把它丟掉並讓訓練與預測的欄位不一致——這裡明確取交集。
    feats = [f for f in usable_features(train) if f in usable_features(test)]
    feats = [f for f in PRIORITY_FEATURES if f in feats]

    model = fit_priority_model(train, train_y, feats)
    scores = model.predict_proba(test[feats].astype(float))[:, 1]

    y = np.asarray(test_y, dtype=float)
    order = np.argsort(-scores)
    rank = np.empty(len(scores), dtype=int)
    rank[order] = np.arange(1, len(scores) + 1)

    # 右設限：前瞻窗還沒走完的話，命中率是低估而不是真值。
    observed = min(1.0, max(0.0,
                            (data_end - as_of).days / (label_end - as_of).days))
    complete = observed >= 0.999

    auc = None
    if 0 < y.sum() < len(y):
        from sklearn.metrics import roc_auc_score
        auc = float(roc_auc_score(y, scores))

    base = float(y.mean())
    prec = {str(k): _precision_at_k(y, scores, k) for k in TOP_K}
    lift = {k: (v / base if base > 0 else float("nan")) for k, v in prec.items()}

    short = _short_ids(test["id"])
    keep = order[:top_n]
    ranking = [
        {
            "id": str(test["id"].iloc[i]),
            "i": short[i],                   # 與 payload 點位同一把 key
            "rank": int(rank[i]),
            "score": round(float(scores[i]), 4),
            # hit = 這家在 [T, T+2年) 真的受罰了。時間軸把它點亮。
            "hit": int(y[i]),
            "prior": int(test["n_penalties_prior"].iloc[i] or 0),
        }
        for i in keep
    ]

    return Point(
        as_of=str(as_of.date()),
        train_as_of=str(train_as_of.date()),
        label_start=str(as_of.date()),
        label_end=str(label_end.date()),
        label_complete=complete,
        observed_fraction=round(observed, 3),
        n_institutions=len(test),
        n_positive=int(y.sum()),
        base_rate=round(base, 4),
        auc=round(auc, 4) if auc is not None else None,
        precision_at={k: round(v, 4) for k, v in prec.items()},
        lift_at={k: round(v, 3) for k, v in lift.items()},
        ranking=ranking,
        features_used=feats,
    )


def live_point(
    inst: pd.DataFrame,
    penalties: pd.DataFrame,
    vehicles: pd.DataFrame,
    as_of: pd.Timestamp,
    *,
    evaluations: pd.DataFrame | None = None,
    train_as_of: pd.Timestamp,
    top_n: int = 1500,
) -> Point:
    """時間軸最右端：今天的排序，沒有命中率可言。

    訓練窗用最近一段**已經觀察完畢**的期間；預測用今天的特徵。沒有前瞻標籤，
    所以 `auc`／`precision_at` 一律為空——這一格不是成績，是待辦清單。
    """
    train = _prepare(build_features(inst, penalties, vehicles, train_as_of,
                                    evaluations=evaluations))
    train_y = label_future_penalty(
        penalties, train["id"], train_as_of,
        train_as_of + pd.DateOffset(years=LABEL_YEARS))

    test = _prepare(build_features(inst, penalties, vehicles, as_of,
                                   evaluations=evaluations))
    feats = [f for f in usable_features(train) if f in usable_features(test)]
    feats = [f for f in PRIORITY_FEATURES if f in feats]

    model = fit_priority_model(train, train_y, feats)
    scores = model.predict_proba(test[feats].astype(float))[:, 1]
    order = np.argsort(-scores)
    rank = np.empty(len(scores), dtype=int)
    rank[order] = np.arange(1, len(scores) + 1)

    short = _short_ids(test["id"])
    ranking = [
        {
            "id": str(test["id"].iloc[i]),
            "i": short[i],
            "rank": int(rank[i]),
            "score": round(float(scores[i]), 4),
            "hit": None,                     # 還沒發生，不是 0
            "prior": int(test["n_penalties_prior"].iloc[i] or 0),
        }
        for i in order[:top_n]
    ]
    return Point(
        as_of=str(as_of.date()),
        train_as_of=str(train_as_of.date()),
        label_start=str(as_of.date()),
        label_end="",
        label_complete=False,
        observed_fraction=0.0,
        n_institutions=len(test),
        n_positive=0,
        base_rate=0.0,
        auc=None,
        precision_at={},
        lift_at={},
        ranking=ranking,
        features_used=feats,
    )


def build_timeline(
    inst: pd.DataFrame,
    penalties: pd.DataFrame,
    vehicles: pd.DataFrame,
    *,
    evaluations: pd.DataFrame | None = None,
    start_year: int = 2021,
    now: pd.Timestamp | None = None,
    top_n: int = 1500,
) -> dict:
    """整條時間軸。每年一格，最後一格是今天。"""
    data_end = pd.Timestamp(penalties["date"].max())
    now = now or data_end
    points: list[Point] = []

    last_complete: pd.Timestamp | None = None
    year = start_year
    while True:
        as_of = pd.Timestamp(year=year, month=1, day=1)
        if as_of >= now:
            break
        p = evaluate_point(inst, penalties, vehicles, as_of,
                           evaluations=evaluations, data_end=data_end,
                           top_n=top_n)
        points.append(p)
        if p.label_complete:
            last_complete = as_of
        year += 1

    # 即時格：訓練窗取最後一個標籤已觀察完畢的時點，避免拿右設限的標籤訓練。
    train_as_of = last_complete or pd.Timestamp(year=start_year, month=1, day=1)
    points.append(live_point(inst, penalties, vehicles, now,
                             evaluations=evaluations, train_as_of=train_as_of,
                             top_n=top_n))

    complete = [p for p in points if p.label_complete]
    return {
        "schema_version": 1,
        "protocol": {
            "label_years": LABEL_YEARS,
            "train_lag_years": TRAIN_LAG_YEARS,
            "top_k": list(TOP_K),
            "data_end": str(data_end.date()),
            "note": "每格各自重新訓練；訓練與預測是兩份獨立的特徵快照，"
                    "評分期的裁罰不可能流進訓練特徵。",
        },
        "points": [p.as_dict() for p in points],
        "summary": {
            "n_points": len(points),
            "n_complete": len(complete),
            "mean_lift_at_100": (
                round(float(np.mean([p.lift_at["100"] for p in complete])), 3)
                if complete else None),
            "mean_auc": (
                round(float(np.mean([p.auc for p in complete if p.auc])), 4)
                if complete else None),
        },
    }
