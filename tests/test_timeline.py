"""時間軸回測：時序紀律與「未發生 ≠ 沒事」這兩件事要被鎖住。

時間軸的整個說服力建立在「每一格只用當天看得到的資料」上。一旦有一條洩漏，
畫面上那條漂亮的命中率就只是在展示未來資訊，而且**看不出來**——這正是
CLAUDE.md 反覆警告的那種錯誤。所以這裡測的是協定本身，不是數字大小。
"""

from __future__ import annotations

import json
import pathlib
import sys

import pandas as pd
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.risk import timeline as tl

TIMELINE = pathlib.Path("data/processed/timeline.json")
needs_timeline = pytest.mark.skipif(
    not TIMELINE.exists(),
    reason="尚未產生時間軸；先執行 python run.py timeline")


def _load() -> dict:
    return json.loads(TIMELINE.read_text(encoding="utf-8"))


def test_short_ids_refuse_to_collide():
    """縮短後碰撞必須當場失敗，不能靜靜把甲園的結果畫到乙園身上。"""
    ok = pd.Series(["aaaaaaaa-1111", "bbbbbbbb-2222"])
    assert tl._short_ids(ok) == ["aaaaaaaa", "bbbbbbbb"]

    clash = pd.Series(["aaaaaaaa-1111", "aaaaaaaa-2222"])
    with pytest.raises(ValueError, match="碰撞"):
        tl._short_ids(clash)


def test_never_penalised_is_a_sentinel_not_an_imputed_mean():
    """沒有前科的園不是「距上次裁罰有平均那麼久」，而是根本沒有上一次。"""
    frame = pd.DataFrame({
        "type": ["私立", "公立"],
        "days_since_last_penalty": [30.0, None],
    })
    out = tl._prepare(frame)
    assert out["days_since_last_penalty"].iloc[1] == tl.NEVER_PENALISED_DAYS
    assert out["days_since_last_penalty"].iloc[0] == 30.0


@needs_timeline
def test_training_window_always_precedes_the_prediction_point():
    for p in _load()["points"]:
        train = pd.Timestamp(p["train_as_of"])
        as_of = pd.Timestamp(p["as_of"])
        assert train < as_of, f"{p['as_of']} 的訓練窗沒有早於預測點"
        # 訓練標籤也必須在預測點之前就觀察完畢，否則等於用未來的標籤訓練。
        assert train + pd.DateOffset(years=tl.LABEL_YEARS) <= as_of, (
            f"{p['as_of']} 的訓練標籤窗延伸到了預測點之後")


@needs_timeline
def test_censored_points_are_flagged_and_never_claim_completeness():
    data = _load()
    end = pd.Timestamp(data["protocol"]["data_end"])
    for p in data["points"]:
        if not p["label_end"]:
            continue                      # 即時格，下面另測
        label_end = pd.Timestamp(p["label_end"])
        if label_end > end:
            assert not p["label_complete"], (
                f"{p['as_of']} 的前瞻窗超出資料截止卻標成完整觀察")
            assert p["observed_fraction"] < 1.0


@needs_timeline
def test_the_live_point_reports_no_score_at_all():
    """最右端沒有前瞻窗。命中率不是 0，是不存在。"""
    live = _load()["points"][-1]
    assert live["label_end"] == ""
    assert live["auc"] is None
    assert live["precision_at"] == {}
    assert live["lift_at"] == {}
    for row in live["ranking"][:20]:
        assert row["hit"] is None, "還沒發生的事不可以記成 0"


@needs_timeline
def test_completed_points_carry_a_real_hit_label():
    for p in _load()["points"]:
        if not p["label_complete"]:
            continue
        hits = [r["hit"] for r in p["ranking"]]
        assert set(hits) <= {0, 1}, "完整觀察的格子不該有 None"
        assert sum(hits) == p["n_positive"], (
            f"{p['as_of']} 的命中總數與 n_positive 不符")


@needs_timeline
def test_precision_at_100_matches_the_ranking_it_ships():
    """畫面上的命中率必須能由它自己附的排序算回來。"""
    for p in _load()["points"]:
        if not p["precision_at"]:
            continue
        top = sorted(p["ranking"], key=lambda r: r["rank"])[:100]
        got = sum(r["hit"] for r in top) / len(top)
        assert abs(got - p["precision_at"]["100"]) < 1e-6, (
            f"{p['as_of']} P@100 對不上：宣稱 {p['precision_at']['100']}、"
            f"由排序算得 {got}")


@needs_timeline
def test_lift_is_precision_over_the_period_base_rate():
    for p in _load()["points"]:
        if not p["lift_at"] or not p["base_rate"]:
            continue
        expected = p["precision_at"]["100"] / p["base_rate"]
        assert abs(expected - p["lift_at"]["100"]) < 0.01, (
            f"{p['as_of']} 的提升倍數不是命中率除以當期基準率")


@needs_timeline
def test_every_row_carries_the_short_key_the_map_joins_on():
    for p in _load()["points"]:
        for row in p["ranking"][:50]:
            assert row["i"] == row["id"][:tl.SHORT_ID_LEN]
