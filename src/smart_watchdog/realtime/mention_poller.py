"""背景輪詢：把 Threads 上 @標註我們的貼文持續收進來。

**為什麼這個可以自動跑，而掃描主控台不行。** `/me/mentions` 是 Threads 官方
API，沒有計費——輪詢它的成本是零。`realtime/jobs.py` 那套「先估價 → 再授權
→ 才執行」存在的理由是 Apify 與 Google Places 每次查詢都要錢；那個理由在這裡
不成立，所以那道閘門也不該套過來。免費的東西讓人每天手動按一次，是把成本
從錢轉嫁到人的注意力上。

**但分類要收斂。** `classify.py` 每則貼文一次 Bedrock 呼叫，那是真的花錢。
所以同步無上限、分類有上限（`THREADS_CLASSIFY_MAX_PER_CYCLE`），而且超過上限
的那些會留在待分類佇列等下一輪，不會被悄悄丟掉。

三個不變式：

**一個行程只有一個輪詢。** `_STARTED` 是模組層級的旗標。兩個輪詢同時跑不會
寫壞資料（`threads_id` 的唯一索引擋得住），但會讓 Bedrock 的呼叫次數加倍，
而那是要錢的。

**失敗不會讓輪詢停掉。** 權杖過期、網路斷線、Bedrock 拒絕——每一種都只讓
這一輪沒有收穫，下一輪照跑。`last_error` 留著給 `/api/health` 說明。
一個因為 token 過期就永久靜止的輪詢，跟一個「沒有人通報」的輪詢在畫面上
長得一模一樣，而那正是這個專案最不能接受的那種混淆。

**沒有權杖就不啟動，而且說出來。** 不是靜靜地什麼都不做——`status()` 會回
`reason`，讓畫面能寫「這個管道還沒開」而不是「最近沒有人通報」。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import threading
from typing import Any

DEFAULT_INTERVAL = 300          # 5 分鐘。Threads 的 @標註不是秒級的東西。
MIN_INTERVAL = 60               # 再短沒有意義，只是替平台製造流量。
DEFAULT_CLASSIFY_CAP = 20       # 每輪最多分類幾則（這一段要錢）。

_STARTED = False
_LOCK = threading.Lock()


@dataclasses.dataclass
class PollerState:
    """輪詢的現況。每個欄位都是給 `/api/health` 與畫面照實顯示用的。"""

    enabled: bool = False
    reason: str = ""
    interval: int = DEFAULT_INTERVAL
    classify: bool = False
    classify_cap: int = DEFAULT_CLASSIFY_CAP
    cycles: int = 0
    last_run_utc: str = ""
    last_error: str = ""
    inserted_total: int = 0
    replies_total: int = 0
    classified_total: int = 0
    pending_classify: int = 0

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


STATE = PollerState()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _flag(key: str, default: bool) -> bool:
    from .. import config

    raw = (config.get(key) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "off", "no")


def _interval() -> int:
    from .. import config

    try:
        value = int((config.get("THREADS_SYNC_INTERVAL_SECONDS") or "").strip()
                    or DEFAULT_INTERVAL)
    except ValueError:
        return DEFAULT_INTERVAL
    return max(MIN_INTERVAL, value)


def _classify_cap() -> int:
    from .. import config

    try:
        value = int((config.get("THREADS_CLASSIFY_MAX_PER_CYCLE") or "").strip()
                    or DEFAULT_CLASSIFY_CAP)
    except ValueError:
        return DEFAULT_CLASSIFY_CAP
    return max(0, value)


def _institutions() -> list[dict]:
    """歸屬用的清單：真實主檔 + 示範機構，與 CLI 走同一支合併函式。"""
    import pathlib

    import pandas as pd

    from .demo_data import merged

    csv = (pathlib.Path(__file__).resolve().parents[3]
           / "data/processed/institutions_ntpc.csv")
    real: list[dict] = []
    if csv.exists():
        real = pd.read_csv(csv)[["id", "title", "town"]].to_dict("records")
    return merged(real)


def poll_once() -> dict[str, Any]:
    """跑一輪：抓 @標註 → 抓串下回覆 → 視設定分類。回傳這一輪的計數。

    不丟例外。呼叫端是一個長命的迴圈，而任何一種失敗都只該讓這一輪沒有收穫。
    """
    from ..db import session as db_session
    from ..scrape import threads
    from . import mention_store

    out: dict[str, Any] = {"inserted": 0, "replies": 0, "classified": 0, "error": ""}
    token = threads.token()
    if not token:
        out["error"] = "THREADS_ACCESS_TOKEN 未設定"
        return out

    db = None
    try:
        db_session.init_db()
        db = next(db_session.get_db())
        institutions = _institutions()

        posts = threads.mentions(token)
        counts = mention_store.record(db, posts, institutions)
        out["inserted"] = counts["inserted"]

        # 只抓**這一輪新進來的**那幾串的回覆。對已經抓過的串重抓，是白白替
        # 平台製造流量，而且回覆很少在主貼文沉下去之後才出現。
        for root_id in counts.get("inserted_ids", []):
            try:
                replies = threads.replies_of(root_id, token)
            except threads.ThreadsError:
                continue        # 讀不到回覆不該讓整輪失敗；主貼文已經入庫了
            got = mention_store.record_replies(db, root_id, replies, institutions)
            out["replies"] += got.get("inserted", 0)

        if STATE.classify and STATE.classify_cap:
            from . import classify

            backend = classify.get_backend("auto")
            if backend is not None:
                rows = classify.pending(db, limit=STATE.classify_cap)
                got = classify.classify_rows(db, rows, backend)
                out["classified"] = got.get("classified", 0)
        # 待分類的存量。超過每輪上限的那些留在這裡等下一輪，不被丟掉。
        STATE.pending_classify = len(classify_pending(db))
    except Exception as exc:  # noqa: BLE001 - 輪詢不能因為任何一種失敗而停掉
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if db is not None:
            db.close()
    return out


def classify_pending(db) -> list:
    """還在等分類的列。單獨包一層，讓 `poll_once` 不必在 except 外面 import。"""
    from . import classify

    return classify.pending(db, limit=10_000)


def _loop(stop: threading.Event) -> None:
    while not stop.is_set():
        result = poll_once()
        STATE.cycles += 1
        STATE.last_run_utc = _now()
        STATE.last_error = result["error"]
        STATE.inserted_total += result["inserted"]
        STATE.replies_total += result["replies"]
        STATE.classified_total += result["classified"]
        if result["inserted"] or result["replies"]:
            print(f"[threads] 新增 {result['inserted']} 則通報、"
                  f"{result['replies']} 則回覆"
                  + (f"、分類 {result['classified']} 則" if result["classified"] else ""))
        elif result["error"]:
            print(f"[threads] 輪詢失敗：{result['error']}")
        stop.wait(STATE.interval)


def start() -> PollerState:
    """啟動背景輪詢。重複呼叫是安全的，第二次之後什麼都不做。"""
    global _STARTED
    from ..scrape import threads

    with _LOCK:
        if _STARTED:
            return STATE

        STATE.interval = _interval()
        STATE.classify_cap = _classify_cap()

        if not _flag("THREADS_AUTO_SYNC", True):
            STATE.enabled, STATE.reason = False, "已由 THREADS_AUTO_SYNC 關閉"
            return STATE
        if not threads.token():
            STATE.enabled = False
            STATE.reason = ("THREADS_ACCESS_TOKEN 未設定——這個管道還沒開，"
                            "不是最近沒有人通報")
            return STATE

        from . import classify

        STATE.classify = (_flag("THREADS_AUTO_CLASSIFY", True)
                          and classify.get_backend("auto") is not None
                          and STATE.classify_cap > 0)
        STATE.enabled = True
        STATE.reason = ""
        stop = threading.Event()
        t = threading.Thread(target=_loop, args=(stop,), name="threads-poller",
                             daemon=True)
        t.start()
        _STARTED = True
        note = (f"、每輪最多分類 {STATE.classify_cap} 則"
                if STATE.classify else "、不自動分類（無 Bedrock 憑證或已關閉）")
        print(f"[threads] 背景輪詢已啟動：每 {STATE.interval} 秒{note}")
        return STATE


def status() -> dict[str, Any]:
    return STATE.as_dict()
