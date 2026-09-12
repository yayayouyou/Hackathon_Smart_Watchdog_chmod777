"""掃描任務：背景執行、落地、重啟對帳。

Apify 一次執行要等數十秒到數分鐘，FastAPI 的同步端點會被卡住整段時間，
所以掃描是「建立任務 → 輪詢狀態」而不是一個會等的請求。

三個不變式：

**每個管道的結局有六種，不是「有結果／沒結果」。** ``ok``／``empty``／
``partial``／``failed``／``skipped``／``blocked``。PTT 與 news 被限流時的表徵
是空結果不是錯誤，把 ``failed`` 顯示成「這些園沒人在談」是在把系統故障
呈現成稽查結論。非 ``ok`` 一律要填 ``reason``。

**掃描結果不進 payload、不進任何 CSV、不進分數。** 只寫 ``data/runtime/``。
要進卷宗必須人工逐則採用。這條是架構上的，不是約定——沒有那條程式路徑。

**重啟不釋放預留。** 進行中的任務在重啟後標為 ``interrupted``，帳上的預留
維持佔用。當機的執行照樣花了錢，把它算成零會讓重啟變成一種補額度的手段。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import pathlib
import threading
import uuid
from typing import Any, Callable

from . import ledger

ROOT = pathlib.Path(__file__).resolve().parents[3]
DIR = ROOT / "data/runtime/scan"

QUEUED, RUNNING, DONE, FAILED, CANCELLED, INTERRUPTED = (
    "queued", "running", "done", "failed", "cancelled", "interrupted")
ACTIVE = (QUEUED, RUNNING)

#: 每個管道的結局。前兩個是成功，後四個各自代表不同的「沒有結果」。
OK, EMPTY, PARTIAL, FAILED_CH, SKIPPED, BLOCKED = (
    "ok", "empty", "partial", "failed", "skipped", "blocked")


@dataclasses.dataclass
class ChannelOutcome:
    channel: str
    label: str
    outcome: str
    mentions: int = 0
    raw_items: int = 0
    reason: str = ""
    usd_actual: float | None = None
    provider_ref: str = ""

    def __post_init__(self) -> None:
        if self.outcome != OK and not self.reason:
            raise ValueError(f"{self.channel}: 非 ok 的結局必須說明原因")

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def _completeness(outcomes: list[ChannelOutcome]) -> str:
    """任一管道失敗或部分失敗，整份結果就是 partial——不確定要標資料不足。"""
    if not outcomes:
        return "unusable"
    # 順序要緊：全部失敗必須先判 unusable，否則 any(...) 先命中而回 partial，
    # 「三個管道掛了三個」會顯示成「部分管道未能取得結果」。
    if all(o.outcome in (FAILED_CH, SKIPPED, BLOCKED) for o in outcomes):
        return "unusable"
    if any(o.outcome in (FAILED_CH, PARTIAL, BLOCKED) for o in outcomes):
        return "partial"
    return "complete"


def fingerprint(req: dict) -> str:
    """同一組參數的重複提交。連按兩下不該變成兩次計費執行。"""
    keys = ("scope", "district", "top_n", "ids", "channels", "keywords",
            "max_posts")
    blob = json.dumps({k: req.get(k) for k in keys}, sort_keys=True,
                      ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


class JobStore:
    """任務的唯一持久化位置。原子寫，重啟時對帳。"""

    def __init__(self, directory: pathlib.Path = DIR) -> None:
        self.dir = directory
        self._lock = threading.Lock()

    def _path(self, job_id: str) -> pathlib.Path:
        return self.dir / f"{job_id}.json"

    def write(self, job: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(job["job_id"])
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)      # 原子；讀到的永遠是完整的 JSON

    def read(self, job_id: str) -> dict | None:
        path = self._path(job_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def list(self, limit: int = 50) -> list[dict]:
        if not self.dir.exists():
            return []
        jobs = []
        # 檔名是隨機 uuid，字典序與時間序無關——用它排序會讓 limit 切掉
        # 最近的任務，去重與重啟對帳都跟著漏。依 created_at 排。
        for f in self.dir.glob("*.json"):
            j = self.read(f.stem)
            if j:
                jobs.append({k: j.get(k) for k in (
                    "job_id", "state", "created_at", "finished_at", "scope_label",
                    "keywords", "channels", "usd_max", "usd_actual", "mentions",
                    "data_completeness", "reviewer")})
        jobs.sort(key=lambda j: j.get("created_at") or "", reverse=True)
        return jobs[:limit]

    def find_active(self, fp: str) -> dict | None:
        for j in self.list(limit=20):
            if j.get("state") in ACTIVE:
                full = self.read(j["job_id"])
                if full and full.get("fingerprint") == fp:
                    return full
        return None

    def sweep_interrupted(self) -> int:
        """啟動時對帳：進行中的任務標為中斷。**預留不釋放。**"""
        n = 0
        for j in self.list(limit=200):
            if j.get("state") not in ACTIVE:
                continue
            full = self.read(j["job_id"])
            if not full:
                continue
            full["state"] = INTERRUPTED
            full["error"] = ("伺服器在此任務進行中重新啟動。已預留的額度維持佔用——"
                             "當機的執行照樣花了錢。若有 apify_run_id，"
                             "可用它向供應商查回實際金額。")
            full["finished_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.write(full)
            n += 1
        return n


STORE = JobStore()


def new_job(plan: dict, req: dict, *, reviewer: str = "") -> dict:
    return {
        "job_id": uuid.uuid4().hex[:12],
        "state": QUEUED,
        "created_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": "",
        "reviewer": reviewer,
        "fingerprint": fingerprint(req),
        "scope": plan["scope"], "scope_label": plan["scope_label"],
        "keywords": plan["keywords"],
        "channels": [ln["channel"] for ln in plan["lines"]],
        "usd_max": plan["usd_max"], "usd_actual": None,
        "attribution_pool": plan["attribution_pool"],
        "outcomes": [], "mentions": [], "raw_items": 0,
        "data_completeness": "unusable",
        "apify_run_id": "", "reservations": [], "error": "",
        # 每一則都是候選。這個欄位不會有別的值——沒有自動判定的路徑。
        "disposition": "待人工研判",
        "note": "掃描結果為待人工研判之候選，不進入分數、不成為違規標籤。"
                "要出現在卷宗必須逐則採用。",
    }


def run_job(job: dict, worker: Callable[[dict, Callable[[dict], None]], list[ChannelOutcome]],
            store: JobStore = STORE) -> dict:
    """在背景執行緒跑一個任務。``worker`` 負責實際呼叫各管道。"""
    def publish(patch: dict) -> None:
        job.update(patch)
        store.write(job)

    publish({"state": RUNNING})
    try:
        outcomes = worker(job, publish)
    except Exception as exc:  # noqa: BLE001 - 任務失敗要看得見，不能靜靜消失
        publish({"state": FAILED, "error": f"{type(exc).__name__}: {exc}",
                 "finished_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        return job
    actual = [o.usd_actual for o in outcomes if o.usd_actual is not None]
    publish({
        "state": DONE, "outcomes": [o.as_dict() for o in outcomes],
        "usd_actual": round(sum(actual), 4) if actual else None,
        "raw_items": sum(o.raw_items for o in outcomes),
        "data_completeness": _completeness(outcomes),
        "finished_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    return job


def spawn(job: dict, worker: Callable[..., list[ChannelOutcome]],
          store: JobStore = STORE) -> dict:
    store.write(job)
    t = threading.Thread(target=run_job, args=(job, worker, store), daemon=True)
    t.start()
    return job


def settle_all(job: dict, store: JobStore = STORE) -> None:
    """把任務的每筆預留結算掉。沒有供應商金額就維持未結算（以上界佔用）。"""
    for r in job.get("reservations", []):
        if r.get("settled") or r.get("actual_usd") is None:
            continue
        ledger.settle(r["reservation_id"], float(r["actual_usd"]),
                      provider_ref=r.get("provider_ref", ""))
        r["settled"] = True
    store.write(job)


def summarise(job: dict) -> dict[str, Any]:
    """給前端的摘要。永遠帶 data_completeness，不讓空結果被讀成清白。"""
    return {k: job.get(k) for k in (
        "job_id", "state", "created_at", "finished_at", "scope", "scope_label",
        "keywords", "channels", "usd_max", "usd_actual", "outcomes", "mentions",
        "raw_items", "data_completeness", "attribution_pool", "error",
        "disposition", "note", "reviewer", "apify_run_id")}
