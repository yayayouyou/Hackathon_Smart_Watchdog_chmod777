"""對抗性審查找出並已修的缺陷，各自留一條測試防止復發。

每一條的標題就是當初的失效情境。這些不是「補強」——是實際重現過的錯誤，
其中三條會讓錢在無人察覺的情況下花掉。
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.realtime import jobs, ledger, plan, pricing


@pytest.fixture
def book(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "DIR", tmp_path)
    monkeypatch.setattr(ledger, "PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(ledger, "LOCK", tmp_path / "ledger.lock")
    assert tmp_path in ledger.PATH.parents, "測試指到了真實帳本"
    return tmp_path


def test_google_spend_counts_against_the_day_and_month_caps(book):
    """日／月上限先前只統計 apify_threads，Google 的花費完全不受管制。

    重現：免費額度用罄後，每次 20 園的評論查詢是 US$0.50，剛好等於單次上限
    因此永遠放行；而 places 的預留不進 apify 的總額，所以連跑十次共
    US$5.00 全部通過，日上限 US$1.00 與月上限 US$4.00 形同不存在。
    """
    del book
    ledger.note_call("places_reviews", 1000)
    allowed = 0
    for i in range(10):
        gate, _ = ledger.check_and_reserve(0.50, "places_reviews", f"j{i}")
        if not gate["ok"]:
            assert gate["cap"] == "day"
            break
        allowed += 1
    assert allowed == 2, "US$1.00 的日上限只該容得下兩次 US$0.50"


def test_caps_are_global_across_channels_not_per_channel(book):
    """上限是「我方一天花多少」，不是「Apify 花多少」。"""
    del book
    ledger.check_and_reserve(0.33, "apify_threads", "a")
    ledger.check_and_reserve(0.50, "places_reviews", "b")
    assert ledger.budget().day_spent_usd == pytest.approx(0.83)
    assert not ledger.check(0.30)["ok"]


def test_check_and_reserve_is_one_atomic_step(book):
    """分開 check 再 reserve，兩個同時發動的掃描會各自看到額度而一起超支。"""
    del book
    for _ in range(2):
        ledger.check_and_reserve(0.50, "apify_threads", "j")
    gate, rid = ledger.check_and_reserve(0.50, "apify_threads", "j")
    assert not gate["ok"] and rid == "", "被擋下時不得留下預留"
    assert ledger.budget().day_spent_usd == pytest.approx(1.0)


def test_settling_does_not_move_the_spend_to_settlement_day(book):
    """settle 若覆寫 ts，跑 reconcile 就會把舊花費搬進當天的日額度。"""
    del book
    rid = ledger.reserve(0.20, "apify_threads", "old",
                         now=__import__("datetime").datetime(2026, 9, 8, 10, 0))
    ledger.settle(rid, 0.15)
    entry = next(e for e in ledger._live(ledger._read())
                 if e.get("job_id") == "old")
    assert entry["ts"].startswith("2026-09-08")
    assert entry["meter"] == "apify_threads"
    assert entry["period"] == "2026-09-08"


def test_a_totally_failed_scan_is_unusable_not_partial():
    """三個管道掛了三個，不能顯示成「部分管道未能取得結果」。"""
    dead = [jobs.ChannelOutcome("a", "A", jobs.FAILED_CH, reason="x"),
            jobs.ChannelOutcome("b", "B", jobs.SKIPPED, reason="y"),
            jobs.ChannelOutcome("c", "C", jobs.BLOCKED, reason="z")]
    assert jobs._completeness(dead) == "unusable"
    mixed = [*dead[:2], jobs.ChannelOutcome("c", "C", jobs.OK, 3)]
    assert jobs._completeness(mixed) == "partial"


def test_job_listing_is_ordered_by_time_not_by_random_filename(tmp_path):
    """job_id 是隨機 uuid，字典序與時間序無關——用檔名排序會切掉最近的任務。"""
    store = jobs.JobStore(tmp_path)
    for jid, when in (("zzz111", "2026-09-01 09:00:00"),
                      ("aaa222", "2026-09-09 09:00:00"),
                      ("mmm333", "2026-09-05 09:00:00")):
        store.write({"job_id": jid, "state": "done", "created_at": when})
    assert [j["job_id"] for j in store.list()] == ["aaa222", "mmm333", "zzz111"]


def test_max_posts_is_clamped_to_what_the_actor_can_actually_do():
    """估算只能算出實際可能發生的金額。

    max_posts=9999 曾估成 US$25.08 並被單次上限擋下——使用者被告知不能做
    一件其實只花 US$0.33 的事。
    """
    assert pricing.apify_meter(9999).usd_max == pricing.apify_meter(100).usd_max
    assert pricing.apify_meter(9999).usd_max < pricing.CAP_PER_RUN_USD
    assert pricing.apify_meter(0).usd_max == 0.08, "0 筆是啟動費，對帳基準點"


def test_incomplete_selections_are_blocked_rather_than_silently_defaulted():
    """選了 Threads 卻沒關鍵字，sweep() 會回退成「幼兒園」——
    使用者付錢買了一個他沒有輸入的查詢。"""
    pts = [{"i": f"i{k}", "full": f"第{k}園", "d": "板橋區", "t": 2, "r": k}
           for k in range(5)]
    assert plan.build_plan(pts, channels=["apify_threads"],
                           keywords=[]).blockers
    assert plan.build_plan(pts, scope="district", channels=["ptt"]).blockers
    assert plan.build_plan(pts, scope="picked", ids=[],
                           channels=["ptt"]).blockers
    assert not plan.build_plan(pts, channels=["ptt"]).blockers
