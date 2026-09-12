"""花費帳本：這是唯一寫錢的地方，所以它錯的方式都是「以為還有額度」。

每一條測試都把 `ledger.DIR/PATH/LOCK` 指到 tmp_path。真正的
`data/runtime/scan/ledger.jsonl` 裡是實際發生過的花費紀錄，測試碰它等於
竄改帳。`book` fixture 最後那行 assert 就是在擋這件事，且它以 pytestmark 套用到整檔。

這一檔鎖住的性質，按重要性排：

**重啟不得補回額度。** 預留寫在呼叫之前，當機時那筆錢已經花了但沒人知道金額。
把它算成零，重啟就成了一種補額度的手段——跑到一半當掉、重啟、再跑，帳面永遠
是乾淨的，錢一直在流。

**上界不是預估。** 回傳筆數少於要求時實付本來就會低，那是正常的 under_ran；
只有實付「超過」預估才是價目表漂移。反過來設會讓警示每次都亮，真的算錯時沒人看。

**兩個計費週期不一樣。** Apify 是訂閱週期（每月 8 日），Google 免費額度是日曆月。
用同一個「本月」會在月初與交界處算錯。
"""

from __future__ import annotations

import datetime as dt
import importlib
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

from smart_watchdog.realtime import ledger, pricing

APIFY = "apify_threads"


def _point_at(monkeypatch, directory: pathlib.Path) -> None:
    monkeypatch.setattr(ledger, "DIR", directory)
    monkeypatch.setattr(ledger, "PATH", directory / "ledger.jsonl")
    monkeypatch.setattr(ledger, "LOCK", directory / "ledger.lock")


@pytest.fixture
def book(tmp_path, monkeypatch):
    """一本空帳，位在 tmp_path。絕不可以是真的那一本。"""
    _point_at(monkeypatch, tmp_path / "scan")
    assert tmp_path in ledger.PATH.parents, "測試指到了真實帳本"
    return tmp_path / "scan"


pytestmark = pytest.mark.usefixtures("book")


def _at(text: str) -> dt.datetime:
    return dt.datetime.strptime(text, "%Y-%m-%d %H:%M")


def test_a_reservation_occupies_its_ceiling_immediately():
    """預留當下就以上界佔用額度，不是等結算才算。

    先送出請求再記帳，等於在「錢已經花掉但我們不知道」與「程式當掉」之間留一道縫。
    """
    ledger.reserve(0.33, APIFY, "job1")
    b = ledger.budget(APIFY)
    assert b.month_spent_usd == 0.33
    assert b.day_spent_usd == 0.33
    assert b.month_remaining_usd == pytest.approx(pricing.CAP_PER_MONTH_USD - 0.33)
    assert b.unsettled == 1


def test_a_restart_never_hands_budget_back(book, monkeypatch):
    """重啟後剩餘額度不得比重啟前多。這條是這個模組存在的理由。

    reload 是刻意的：它丟掉所有 in-memory 狀態，只留下檔案，正是重啟會發生的事。
    未結算的預留仍以上界佔用（status="unknown"），不會靜靜消失。
    """
    for k in range(3):
        ledger.reserve(0.12, APIFY, f"job{k}")
    before = ledger.budget(APIFY)

    importlib.reload(ledger)          # ← 伺服器重啟
    _point_at(monkeypatch, book)

    after = ledger.budget(APIFY)
    assert after.month_remaining_usd <= before.month_remaining_usd
    assert after.day_remaining_usd <= before.day_remaining_usd
    assert after.month_spent_usd == before.month_spent_usd == pytest.approx(0.36)
    assert after.unsettled == 3


def test_settling_replaces_the_ceiling_with_what_was_actually_charged():
    """實付由供應商自報，帳本要被它校正——否則上界會永久佔著沒花掉的額度。"""
    rid = ledger.reserve(0.33, APIFY, "job1")
    assert ledger.budget(APIFY).month_spent_usd == 0.33
    ledger.settle(rid, 0.1075, provider_ref="run_abc")
    b = ledger.budget(APIFY)
    assert b.month_spent_usd == 0.1075
    assert b.unsettled == 0


def test_returning_fewer_items_than_asked_is_not_rate_card_drift():
    """上界是上界。實付較低是正常的 under_ran，不是價目表算錯。

    把它當成漂移，警示會在每次正常執行都亮；真的算錯的那次就沒人看了。
    """
    rid = ledger.reserve(0.33, APIFY, "job1")
    out = ledger.settle(rid, 0.105)
    assert out["under_ran"] is True
    assert out["rate_card_drift"] is False
    assert out["predicted_usd"] == 0.33 and out["actual_usd"] == 0.105


def test_paying_more_than_predicted_is_the_dangerous_direction():
    """實付超過上界代表我方公式低估——那才是要跳出來的漂移。"""
    rid = ledger.reserve(0.33, APIFY, "job1")
    out = ledger.settle(rid, 0.51)
    assert out["rate_card_drift"] is True
    assert out["under_ran"] is False
    assert out["drift_usd"] == pytest.approx(0.18)


def test_settling_at_exactly_the_estimate_is_neither():
    """剛好等於預估不是漂移也不是短收，兩個旗標都必須是 False。"""
    rid = ledger.reserve(0.33, APIFY, "job1")
    out = ledger.settle(rid, 0.33)
    assert out["rate_card_drift"] is False and out["under_ran"] is False


def test_release_zeroes_the_reservation():
    """只有確定沒送出任何計費請求時才 release，那時額度必須完全還回來。"""
    rid = ledger.reserve(0.4, APIFY, "job1")
    ledger.release(rid, "管道未啟用，未送出請求")
    b = ledger.budget(APIFY)
    assert b.month_spent_usd == 0.0
    assert b.month_remaining_usd == pricing.CAP_PER_MONTH_USD


def test_the_run_cap_stops_a_single_expensive_scan():
    """單次上限擋的是手滑把 max_posts 打成 1000 那種錯。"""
    gate = ledger.check(pricing.CAP_PER_RUN_USD + 0.01, APIFY)
    assert gate["ok"] is False and gate["cap"] == "run"
    assert gate["limit"] == pricing.CAP_PER_RUN_USD
    assert gate["reason"], "被擋下來必須說得出原因"
    assert ledger.check(pricing.CAP_PER_RUN_USD, APIFY)["ok"] is True


def test_the_day_cap_stops_the_fifth_scan_of_one_afternoon():
    """每一次都在單次上限之內，加起來仍可能一天燒掉一個月的額度。"""
    now = _at("2026-09-20 15:00")
    for k in range(2):
        ledger.reserve(0.45, APIFY, f"job{k}", now=now)
    gate = ledger.check(0.4, APIFY, now=now)
    assert gate["ok"] is False and gate["cap"] == "day"
    assert gate["limit"] == pricing.CAP_PER_DAY_USD


def test_the_month_cap_stops_a_scan_on_a_fresh_day():
    """今天什麼都還沒花，但這個訂閱週期已經花完了——日上限攔不到這種。"""
    for day in range(10, 19):
        ledger.reserve(0.45, APIFY, f"job{day}", now=_at(f"2026-09-{day} 09:00"))
    now = _at("2026-09-20 09:00")
    b = ledger.budget(APIFY, now)
    assert b.day_spent_usd == 0.0, "今天沒花錢，所以擋下來的必須是月上限"
    gate = ledger.check(0.4, APIFY, now=now)
    assert gate["ok"] is False and gate["cap"] == "month"
    assert gate["limit"] == pricing.CAP_PER_MONTH_USD


def test_a_passing_check_names_no_cap():
    gate = ledger.check(0.2, APIFY)
    assert gate["ok"] is True and gate["cap"] is None and gate["reason"] == ""


def test_apify_cycle_is_a_subscription_cycle_not_a_calendar_month():
    """訂閱週期每月 8 日換期。用日曆月會在 1–7 日把上一期的花費算成新的一期。"""
    assert ledger.apify_cycle_start(_at("2026-09-03 10:00")) == "2026-08-08"
    assert ledger.apify_cycle_start(_at("2026-09-08 00:30")) == "2026-09-08"
    assert ledger.apify_cycle_start(_at("2026-10-07 23:59")) == "2026-09-08"
    assert ledger.apify_cycle_start(_at("2026-10-08 00:00")) == "2026-10-08"
    # 跨年也要退到去年 12 月，不是今年 12 月。
    assert ledger.apify_cycle_start(_at("2026-01-03 10:00")) == "2025-12-08"


def test_spending_before_the_cycle_boundary_does_not_follow_us_into_the_new_cycle():
    """9/3 的花費屬於 8/8 那一期；到了 9/20 它不該還佔著額度。"""
    ledger.reserve(0.4, APIFY, "job1", now=_at("2026-09-03 10:00"))
    assert ledger.budget(APIFY, _at("2026-09-05 10:00")).month_spent_usd == 0.4
    assert ledger.budget(APIFY, _at("2026-09-20 10:00")).month_spent_usd == 0.0


def test_places_free_tier_counts_on_calendar_months_not_the_apify_cycle():
    """Google 的 1,000 次免費額度按日曆月重設，與 Apify 的訂閱週期無關。"""
    ledger.note_call("places_reviews", 5, now=_at("2026-09-03 10:00"))
    ledger.note_call("places_reviews", 7, now=_at("2026-08-30 10:00"))
    # 9/3 在 Apify 的上一期，但在日曆月的 9 月——免費額度算它。
    assert ledger.budget(APIFY, _at("2026-09-20 10:00")).places_used_this_month == 5
    assert ledger.budget(APIFY, _at("2026-08-31 10:00")).places_used_this_month == 7


def test_free_tier_calls_do_not_consume_the_dollar_budget():
    """免費額度用的是次數不是錢；把它混進金額會讓兩個上限互相誤擋。"""
    ledger.note_call("places_reviews", 3, job_id="job1")
    b = ledger.budget(APIFY)
    assert b.places_used_this_month == 3
    assert b.month_spent_usd == 0.0 and b.unsettled == 0


def _tear_a_line() -> None:
    """模擬寫入中斷：留下一段沒有換行的半行 JSON，然後繼續正常寫入。

    半行沒有換行，所以之後的 append 會接在同一行後面——這是實際會出現的檔案狀態。
    """
    ledger.reserve(0.2, APIFY, "before")
    with ledger.PATH.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-09-09 10:00:00", "kind": "res')   # 斷電
    ledger.reserve(0.1, APIFY, "swallowed")
    ledger.reserve(0.05, APIFY, "after")


def test_a_half_written_line_does_not_destroy_the_book():
    """整本帳讀不出來 = 額度歸零 = 可以重跑到飽。壞行要跳過，其餘照算。"""
    _tear_a_line()
    b = ledger.budget(APIFY)
    assert b.month_spent_usd > 0.0, "一個壞行讓整本帳歸零"
    jobs_read = [e.get("job_id") for e in ledger.entries()]
    assert "before" in jobs_read and "after" in jobs_read


def test_the_entry_written_after_a_torn_line_is_not_swallowed():
    """半行之後的下一筆預留不能消失——少算花費正是危險的方向。

    重現：reserve → 寫入一段無換行的半行 → reserve。第二筆被接在半行後面，
    整行 json.loads 失敗，於是那 US$0.1 從帳上消失，額度被還了回來。
    """
    _tear_a_line()
    assert ledger.budget(APIFY).month_spent_usd == pytest.approx(0.35)
    assert "swallowed" in [e.get("job_id") for e in ledger.entries()]


def test_the_budget_says_where_its_numbers_come_from():
    """Google 沒有即時用量端點，本機計數會低估。UI 不得假裝那是帳單。"""
    b = ledger.budget(APIFY)
    assert "本機帳本" in b.source
    assert "Google 無用量端點" in b.source


def test_entries_are_returned_newest_first():
    """帳本頁面是拿來查「剛剛那次跑了多少錢」的，最新的必須在最上面。"""
    ledger.reserve(0.1, APIFY, "first")
    ledger.reserve(0.2, APIFY, "second")
    assert [e["job_id"] for e in ledger.entries()] == ["second", "first"]


def test_recording_the_provider_amount_is_not_the_same_as_settling():
    """`settled` 的意思是「帳本已結算」，不是「供應商已回報金額」。

    這兩件事被當成同一件事的那一版，`api/scan.py` 在拿到供應商金額時就把
    `settled` 設成 True，於是 `jobs.settle_all()` 的
    `if r.get("settled"): continue` 把它跳過，`ledger.settle()` 永遠不被呼叫。
    帳本只留 reserve、沒有 settle，那筆預留就以**上界**永久佔住日／月額度——
    實測預留 US$0.33、實付 US$0.08，0.25 的額度平白燒掉且不會回來，
    而 job 與畫面都顯示「已結算」。錢的帳不能靠兩個欄位互相假設。
    """
    from smart_watchdog.realtime import jobs

    rid = ledger.reserve(0.33, APIFY, "j1", "sweep")
    assert ledger.budget().day_spent_usd == pytest.approx(0.33)

    # 供應商回報了金額，但還沒結算——settled 必須仍是 False。
    job = {"job_id": "j1", "reservations": [
        {"reservation_id": rid, "channel": "apify_threads", "usd_max": 0.33,
         "actual_usd": 0.08, "settled": False, "provider_ref": "run-abc"}]}

    jobs.settle_all(job, store=jobs.JobStore())
    assert job["reservations"][0]["settled"] is True, "結算後才輪到它變 True"
    assert ledger.budget().day_spent_usd == pytest.approx(0.08), (
        "帳本要換成實付；停在上界代表 ledger.settle() 沒被呼叫")
    assert ledger.budget().unsettled == 0


def test_settle_all_skips_what_is_already_settled_so_nothing_is_charged_twice():
    """`settle_all` 跳過已結算的預留——結算兩次會把同一筆錢記兩遍。

    這個性質正是上一支測試要防的那個 bug 的成因：因為 settle_all 信任這個
    旗標，呼叫端就**絕對不可以**自己提前設它。提前設的後果是靜默的——
    沒有例外、沒有警告，只有一筆永遠停在上界、回不來的額度。

    這一支同時界定了修法的方向：要修的是呼叫端不要亂設旗標，
    不是讓 settle_all 忽略旗標（那會引入重複計費）。
    """
    from smart_watchdog.realtime import jobs

    rid = ledger.reserve(0.33, APIFY, "j2", "sweep")
    job = {"job_id": "j2", "reservations": [
        {"reservation_id": rid, "channel": "apify_threads",
         "usd_max": 0.33, "actual_usd": 0.08, "settled": False}]}

    jobs.settle_all(job, store=jobs.JobStore())
    assert ledger.budget().day_spent_usd == pytest.approx(0.08)
    # 再跑一次：旗標已經是 True，不該再寫一筆。
    jobs.settle_all(job, store=jobs.JobStore())
    assert ledger.budget().day_spent_usd == pytest.approx(0.08), "重複結算"
