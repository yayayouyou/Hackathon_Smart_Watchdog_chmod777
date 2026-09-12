"""花費帳本：唯一寫錢的地方，append-only，跨行程以檔案鎖保護。

三個性質決定了它的形狀：

**預留寫在呼叫之前，結算寫在供應商回報之後。** 先送出請求再記帳，等於在
「錢已經花掉但我們不知道」與「程式當掉」之間留了一道縫。當機時該筆預留仍在
帳上，且以**上界**計入已花費（``status="unknown"``），不會靜靜消失——重啟後
剩餘額度不得比重啟前多，這是有測試鎖住的不變式。

**兩個計費週期不一樣。** Apify 是訂閱週期（本帳號 2026-09-08 → 10-07），
Google 的免費額度是日曆月。用同一個「本月」會在月初與週期交界處算錯。

**權威來源不同。** Apify 自己回報 ``usageTotalUsd``，所以我方帳本可被校正；
Google 沒有即時用量端點，1,000 次免費額度只有本機計數知道——同一把金鑰若被
別的程式用過，本機計數會低估。UI 一律標明「本機計數，非 Google 帳單」，
不假裝精確。
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import json
import os
import pathlib
import uuid

from .. import filelock
from . import pricing

ROOT = pathlib.Path(__file__).resolve().parents[3]
DIR = ROOT / "data/runtime/scan"
PATH = DIR / "ledger.jsonl"
LOCK = DIR / "ledger.lock"

#: Apify 訂閱週期起日（GET /v2/users/me 的 plan cycle）。日曆月會在交界處算錯。
APIFY_CYCLE_DAY = 8


@dataclasses.dataclass(frozen=True)
class Budget:
    """一個管道目前還剩多少可花，以及那個數字是誰說的。"""

    run_remaining_usd: float
    day_spent_usd: float
    month_spent_usd: float
    day_remaining_usd: float
    month_remaining_usd: float
    places_used_this_month: int
    unsettled: int
    source: str

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def _now() -> dt.datetime:
    return dt.datetime.now()


@contextlib.contextmanager
def _locked():
    """跨行程互斥。掃描是人工發動的低頻動作，鎖的成本無關緊要。"""
    DIR.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as fh:
        filelock.acquire(fh)
        try:
            yield
        finally:
            filelock.release(fh)


def _read() -> list[dict]:
    if not PATH.exists():
        return []
    out: list[dict] = []
    for line in PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            # 半行（寫入中斷）不能讓整本帳讀不出來；略過並繼續。
            continue
    return out


def _append(entry: dict) -> None:
    """附加一筆。續寫之前先確認前一行是完整的。

    寫入中斷會留下沒有換行的半行；直接 append 會把新的一筆接在同一行後面，
    整行解析失敗，**兩筆一起消失**。方向正好是危險的那邊：帳面變少、
    額度變多、下一次掃描獲准超花。所以檔案結尾不是換行時先補一個。
    """
    DIR.mkdir(parents=True, exist_ok=True)
    torn = False
    if PATH.exists() and PATH.stat().st_size:
        with PATH.open("rb") as fh:
            fh.seek(-1, os.SEEK_END)
            torn = fh.read(1) != b"\n"
    with PATH.open("a", encoding="utf-8") as fh:
        if torn:
            fh.write("\n")
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())      # 錢的紀錄要落到磁碟才算數


def apify_cycle_start(now: dt.datetime | None = None) -> str:
    """本訂閱週期的起日（YYYY-MM-DD）。"""
    now = now or _now()
    start = now.replace(day=APIFY_CYCLE_DAY, hour=0, minute=0, second=0,
                        microsecond=0)
    if now.day < APIFY_CYCLE_DAY:
        start = (start.replace(day=1) - dt.timedelta(days=1)).replace(
            day=APIFY_CYCLE_DAY, hour=0, minute=0, second=0, microsecond=0)
    return start.strftime("%Y-%m-%d")


def _period_of(meter: str, now: dt.datetime | None = None) -> str:
    now = now or _now()
    if meter == "apify_threads":
        return apify_cycle_start(now)
    return now.strftime("%Y-%m")          # Google 免費額度按日曆月


def _effective_usd(e: dict) -> float:
    """一筆帳實際佔用多少額度。

    已結算用實付；未結算用預留上界。**不是零**——當掉的執行照樣花了錢，
    把它算成零會讓重啟變成一種補額度的手段。
    """
    if e.get("kind") == "settle":
        return float(e.get("actual_usd") or 0.0)
    if e.get("kind") == "release":
        return 0.0
    return float(e.get("usd_max") or 0.0)


def _live(entries: list[dict]) -> list[dict]:
    """把 reserve 與其後的 settle/release 收斂成每個 reservation 一筆。"""
    by_id: dict[str, dict] = {}
    for e in entries:
        rid = e.get("reservation_id")
        if not rid:
            continue
        if e.get("kind") == "reserve":
            by_id[rid] = dict(e)
        elif rid in by_id:
            base = by_id[rid]
            # 只讓後續 entry 覆寫金額與狀態。ts/meter/period/job_id 必須留在
            # 預留當下的值——否則跑 reconcile 會把上個月的花費搬到今天，
            # 吃掉當天的日額度。
            by_id[rid] = {**base, **e,
                          "ts": base.get("ts"), "meter": base.get("meter"),
                          "period": base.get("period"),
                          "job_id": base.get("job_id")}
    return list(by_id.values())


#: 會產生金錢支出的 meter。上限是「我方一天／一個月總共花多少」，
#: 不是「Apify 花多少」——只統計其中一個等於另一個完全不受管制。
MONEY_METERS = frozenset({"apify_threads", "places_reviews", "places_rating"})


def _budget_from(entries: list[dict], now: dt.datetime) -> Budget:
    """由帳本內容算出預算狀況。純函式，方便在鎖內重算。

    **日與月的加總跨越所有計費 meter。** 先前只統計 ``apify_threads``，
    Google Places 的預留因此永遠不進總額——$1.00 日上限與 $4.00 月上限
    對它形同不存在，逐園查評論可以無限次數重複執行。

    月的判定用**每筆自己的**週期：Apify 是訂閱週期、Google 是日曆月，
    兩者交界日不同，共用一個 period 字串會在月初與週期邊界算錯。
    """
    live = _live(entries)
    today = now.strftime("%Y-%m-%d")

    def counted(e: dict) -> bool:
        return e.get("meter") in MONEY_METERS

    day = sum(_effective_usd(e) for e in live
              if counted(e) and str(e.get("ts", ""))[:10] == today)
    month = sum(_effective_usd(e) for e in live
                if counted(e)
                and e.get("period") == _period_of(e.get("meter", ""), now))
    unsettled = sum(1 for e in live
                    if counted(e) and e.get("kind") == "reserve")

    cal = now.strftime("%Y-%m")
    places = sum(int(e.get("calls") or 0) for e in entries
                 if e.get("kind") == "note" and e.get("meter", "").startswith("places")
                 and str(e.get("ts", ""))[:7] == cal)

    return Budget(
        run_remaining_usd=pricing.CAP_PER_RUN_USD,
        day_spent_usd=round(day, 4), month_spent_usd=round(month, 4),
        day_remaining_usd=round(max(0.0, pricing.CAP_PER_DAY_USD - day), 4),
        month_remaining_usd=round(max(0.0, pricing.CAP_PER_MONTH_USD - month), 4),
        places_used_this_month=places, unsettled=unsettled,
        source="本機帳本，跨管道總額（Apify 另可由 /v2/users/me 校正；"
               "Google 無用量端點）",
    )


def budget(meter: str = "", now: dt.datetime | None = None) -> Budget:
    """目前的預算狀況。未結算的預留以上界計入。

    ``meter`` 已無作用，保留是為了不破壞既有呼叫端——上限現在是全域的。
    """
    del meter
    return _budget_from(_read(), now or _now())


def _gate(usd_max: float, b: Budget) -> dict:
    """三道上限的純判定。放在鎖內重跑時不能再去讀檔。"""
    if usd_max > pricing.CAP_PER_RUN_USD:
        return {"ok": False, "cap": "run", "limit": pricing.CAP_PER_RUN_USD,
                "budget": b.as_dict(),
                "reason": f"單次掃描上限 US${pricing.CAP_PER_RUN_USD}，"
                          f"這次估算 US${round(usd_max, 4)}"}
    if usd_max > b.day_remaining_usd:
        return {"ok": False, "cap": "day", "limit": pricing.CAP_PER_DAY_USD,
                "budget": b.as_dict(),
                "reason": f"今日剩餘 US${b.day_remaining_usd}，"
                          f"這次估算 US${round(usd_max, 4)}"}
    if usd_max > b.month_remaining_usd:
        return {"ok": False, "cap": "month", "limit": pricing.CAP_PER_MONTH_USD,
                "budget": b.as_dict(),
                "reason": f"本週期剩餘 US${b.month_remaining_usd}，"
                          f"這次估算 US${round(usd_max, 4)}"}
    return {"ok": True, "cap": None, "budget": b.as_dict(), "reason": ""}


def check(usd_max: float, meter: str = "", now: dt.datetime | None = None) -> dict:
    """這筆花費放行嗎？三道上限，回傳被哪一道擋下。估算用；不佔額度。"""
    del meter
    return _gate(usd_max, budget(now=now))


def check_and_reserve(usd_max: float, meter: str, job_id: str,
                      detail: str = "",
                      now: dt.datetime | None = None) -> tuple[dict, str]:
    """在同一把鎖裡檢查並預留。回傳 (閘門結果, reservation_id)。

    分開呼叫 ``check()`` 再 ``reserve()`` 之間有空隙：兩個分頁同時發動，
    兩邊都看到「還有額度」，於是一起衝破上限。檢查與佔用必須是一個動作。
    """
    now = now or _now()
    with _locked():
        gate = _gate(usd_max, _budget_from(_read(), now))
        if not gate["ok"]:
            return gate, ""
        rid = uuid.uuid4().hex[:12]
        _append({"ts": now.strftime("%Y-%m-%d %H:%M:%S"), "kind": "reserve",
                 "reservation_id": rid, "meter": meter, "job_id": job_id,
                 "period": _period_of(meter, now), "usd_max": round(usd_max, 4),
                 "detail": detail, "status": "unknown"})
    return gate, rid


def reserve(usd_max: float, meter: str, job_id: str,
            detail: str = "", now: dt.datetime | None = None) -> str:
    """在送出請求**之前**佔住額度。回傳 reservation_id。"""
    now = now or _now()
    rid = uuid.uuid4().hex[:12]
    with _locked():
        _append({"ts": now.strftime("%Y-%m-%d %H:%M:%S"), "kind": "reserve",
                 "reservation_id": rid, "meter": meter, "job_id": job_id,
                 "period": _period_of(meter, now), "usd_max": round(usd_max, 4),
                 "detail": detail, "status": "unknown"})
    return rid


def settle(reservation_id: str, actual_usd: float, *, provider_ref: str = "",
           now: dt.datetime | None = None) -> dict:
    """以供應商自報金額校正。

    **只有實付「超過」預估才算價目表漂移。** 預留的是上界，回傳筆數少於要求
    時實付本來就會低——把那個當成漂移會讓警示在每次正常執行都亮，
    真的算錯時就沒人看了。實付超過上界才表示我方公式低估，那是危險方向。
    """
    now = now or _now()
    live = {e.get("reservation_id"): e for e in _live(_read())}
    predicted = float((live.get(reservation_id) or {}).get("usd_max") or 0.0)
    drift = round(actual_usd - predicted, 4)
    with _locked():
        _append({"ts": now.strftime("%Y-%m-%d %H:%M:%S"), "kind": "settle",
                 "reservation_id": reservation_id,
                 "meter": (live.get(reservation_id) or {}).get("meter", ""),
                 "period": (live.get(reservation_id) or {}).get("period", ""),
                 "actual_usd": round(actual_usd, 4), "predicted_usd": predicted,
                 "drift_usd": drift, "provider_ref": provider_ref,
                 "status": "settled"})
    return {"predicted_usd": predicted, "actual_usd": round(actual_usd, 4),
            "drift_usd": drift, "rate_card_drift": drift > 0.01,
            "under_ran": drift < -0.01}


def release(reservation_id: str, reason: str,
            now: dt.datetime | None = None) -> None:
    """只有在**確定沒有送出任何計費請求**時才可呼叫。"""
    now = now or _now()
    with _locked():
        _append({"ts": now.strftime("%Y-%m-%d %H:%M:%S"), "kind": "release",
                 "reservation_id": reservation_id, "reason": reason,
                 "status": "released"})


def note_call(meter: str, calls: int = 1, *, job_id: str = "",
              detail: str = "", now: dt.datetime | None = None) -> None:
    """記一次免費額度用量。

    ``/api/reviews`` 開卷宗時也走這裡——免費額度是全域的，計費器就必須是全域的，
    否則「本月 138/1,000」在上線第一天就是錯的，而 1,000 次會在某個沒人按過
    「掃描」的下午被開卷宗耗盡。
    """
    now = now or _now()
    with _locked():
        _append({"ts": now.strftime("%Y-%m-%d %H:%M:%S"), "kind": "note",
                 "meter": meter, "calls": int(calls), "job_id": job_id,
                 "detail": detail})


def entries(limit: int = 200) -> list[dict]:
    return _read()[-limit:][::-1]
