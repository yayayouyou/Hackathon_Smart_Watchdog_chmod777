"""掃描主控台的端點。

流程刻意是三段，因為中間那段花的是真的錢：

    估算（不花錢，回上界與被哪道上限擋住）
      → 確認（前端把畫面上那個金額回押，伺服器持鎖重驗）
        → 執行（背景任務，輪詢查狀態，結束後以供應商自報金額結算）

回押與重驗是為了 TOCTOU：只在估算時檢查，等於讓另一個分頁在中間把額度吃掉。
使用者授權的是他看到的那個數字，不是伺服器後來重算出來的別的數字。
"""

from __future__ import annotations

import csv
import datetime as dt
import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..realtime import jobs, ledger, plan, pricing

router = APIRouter(prefix="/api/scan", tags=["scan"])

#: 由 server.py 在掛載時注入，避免與 server 相互匯入。
_ctx: dict[str, Any] = {"payload": None, "proposal": None}


def bind(payload_fn, proposal_fn) -> None:
    _ctx["payload"], _ctx["proposal"] = payload_fn, proposal_fn


def _bound() -> dict:
    """延遲綁定。startup 事件不是唯一的進入點（TestClient、CLI 都繞過它），
    所以這裡自己補上，而不是假設 bind() 一定被呼叫過。"""
    if _ctx["payload"] is None:
        from . import server

        bind(server.payload, server.get_proposal)
    return _ctx


def _points() -> list[dict]:
    return (_bound()["payload"]() or {}).get("points", [])


def _place_ids() -> dict[str, str]:
    """payload 的 i 是 UUID 前 8 碼，CSV 存完整 UUID——這裡統一成前者。"""
    path = jobs.ROOT / "data/processed/place_ids_ntpc.csv"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return {r["id"][:8]: r["place_id"] for r in csv.DictReader(fh)
                if r.get("place_id")}


class ScanRequest(BaseModel):
    scope: str = "city"
    district: str = ""
    top_n: int = 50
    ids: list[str] = []
    channels: list[str] = ["news_rss", "ptt"]
    keywords: list[str] = list(plan.DEFAULT_KEYWORDS)
    max_posts: int = 50
    reviewer: str = ""
    #: 前端把估算畫面上的金額原樣回押；不符即拒絕，不默默用新數字跑。
    confirm_ceiling_usd: Optional[float] = None


def _build(req: ScanRequest) -> plan.ScanPlan:
    pts = _points()
    proposal_ids = [p["i"] for p in (_bound()["proposal"](n=req.top_n) or {}).get(
        "proposal", [])] if req.scope == "proposal" else []
    b = ledger.budget()
    return plan.build_plan(
        pts, scope=req.scope, channels=req.channels, keywords=req.keywords,
        max_posts=req.max_posts, district=req.district, top_n=req.top_n,
        ids=req.ids, proposal_ids=proposal_ids,
        places_used_this_month=b.places_used_this_month,
        has_place_id=set(_place_ids()))


@router.get("/options")
def options() -> dict:
    """主控台開啟時要知道的一切：有哪些管道、範圍、關鍵字組、還剩多少額度。"""
    from ..realtime.sources import LIVE, default_channels

    chans = default_channels()
    b = ledger.budget()
    pts = _points()
    return {
        "channels": [{
            **c.describe(),
            "live": c.status == LIVE,
            "cost_note": {
                "apify_threads": "關鍵字掃描，一次執行覆蓋全市；成本與範圍無關",
                "places_reviews": "逐園查詢；成本與範圍成正比，每月前 1,000 次免費",
                "news_rss": "無金錢成本；成本是被限流的風險",
                "ptt": "無金錢成本；成本是被限流的風險",
            }.get(c.key, "需採購，價格未知"),
            "per_institution_supported": c.key != "apify_threads",
        } for c in chans],
        "scopes": [{"key": k, "label": lbl, "note": note,
                    "count": len(plan.resolve_scope(pts, k)[0])
                    if k in ("city", "compliance_fail", "evaluation") else None}
                   for k, lbl, note in plan.SCOPES],
        "keyword_presets": {k: {"keywords": list(v[0]), "note": v[1]}
                            for k, v in plan.KEYWORD_PRESETS.items()},
        "districts": sorted({p["d"] for p in pts}),
        "budget": b.as_dict(),
        "caps": {"run": pricing.CAP_PER_RUN_USD, "day": pricing.CAP_PER_DAY_USD,
                 "month": pricing.CAP_PER_MONTH_USD,
                 "per_institution": pricing.PER_INSTITUTION_CAP},
        "apify_per_institution_blocked":
            f"逐園 Threads 查詢不提供。{pricing.PER_INSTITUTION_CAP} 家 × "
            f"US${pricing.apify_meter(50).usd_max} = "
            f"US${round(pricing.PER_INSTITUTION_CAP * pricing.apify_meter(50).usd_max, 2)}"
            f"，超過整個月的額度 US${pricing.CAP_PER_MONTH_USD}。",
    }


@router.post("/estimate")
def estimate(req: ScanRequest) -> dict:
    """算錢，不花錢。回傳上界、被哪道上限擋住、以及預期能拿到幾則。"""
    p = _build(req)
    gate = ledger.check(p.usd_max)
    d = p.as_dict()
    # 上一輪全市掃描：100 篇 Threads 貼文，嚴格歸屬後 1 則。難看但誠實——
    # 計費按寫進 dataset 的筆數，歸屬失敗的 99 篇一樣付錢。
    if "apify_threads" in req.channels:
        lo, hi = round(req.max_posts * 0.01, 1), round(req.max_posts * 0.03, 1)
        d["expected_leads"] = f"約 {lo}–{hi} 則可歸屬（歷史歸屬率 1–3%）"
    d["gate"] = gate
    d["confirm_ceiling_usd"] = p.usd_max
    d["expires_at"] = (dt.datetime.now() + dt.timedelta(seconds=180)).strftime(
        "%Y-%m-%d %H:%M:%S")
    return d


@router.post("")
def start(req: ScanRequest) -> dict:
    """建立掃描任務。持鎖重驗後才真的發動。"""
    p = _build(req)
    if p.blockers:
        raise HTTPException(400, {"error": "BLOCKED", "blockers": p.blockers})

    if (req.confirm_ceiling_usd is not None
            and abs(req.confirm_ceiling_usd - p.usd_max) > 1e-6):
        # 估算之後條件變了。回新計畫，讓使用者重新授權他實際會付的金額。
        raise HTTPException(409, {
            "error": "PLAN_STALE", "confirmed": req.confirm_ceiling_usd,
            "now": p.usd_max, "plan": p.as_dict(),
            "message": "估算後預算或範圍已變動，請確認新的金額。"})

    gate = ledger.check(p.usd_max)
    if not gate["ok"]:
        raise HTTPException(429, {"error": "OVER_CAP", **gate})

    fp = jobs.fingerprint(req.model_dump())
    existing = jobs.STORE.find_active(fp)
    if existing:
        # 連按兩下不該變成兩次計費執行。
        return {**jobs.summarise(existing), "deduplicated": True}

    job = jobs.new_job(p.as_dict(), req.model_dump(), reviewer=req.reviewer)
    jobs.spawn(job, _worker_for(req, p))
    return jobs.summarise(job)


def _worker_for(req: ScanRequest, p: plan.ScanPlan):
    """實際跑各管道。歸屬一律對全部 1,213 園，不對掃描範圍。"""
    from ..realtime.sources import LIVE, default_channels

    pts = _points()
    by_id = {x["i"]: x for x in pts}
    # 歸屬池：全部園。縮小它不是省成本，是製造誤判。
    all_inst = [{"id": x["i"], "title": x["full"], "town": x["d"]} for x in pts]
    targets = {ln.channel: ln.target_ids for ln in p.lines}
    # 免費管道在寬範圍會翻成廣掃（成本考量），但結果必須回到使用者要的範圍：
    # 162 園的標籤配上全市結果，是在騙人。標記每則在不在範圍內，分開呈現。
    scope_ids = {x["i"] for x in plan.resolve_scope(
        pts, p.scope, district=req.district, top_n=req.top_n, ids=req.ids,
        proposal_ids=[x["i"] for x in ((_bound()["proposal"](n=req.top_n) or {})
                                       .get("proposal", []))]
        if p.scope == "proposal" else None)[0]}
    place_ids = _place_ids()

    def mark(ms: list[dict]) -> list[dict]:
        for m in ms:
            m["in_scope"] = m.get("institution_id") in scope_ids
        return ms

    def worker(job: dict, publish) -> list[jobs.ChannelOutcome]:
        chans = {c.key: c for c in default_channels()}
        out: list[jobs.ChannelOutcome] = []
        found: list[dict] = []

        for line in p.lines:
            ch = chans.get(line.channel)
            if ch is None or ch.status != LIVE:
                out.append(jobs.ChannelOutcome(
                    line.channel, line.label, jobs.SKIPPED,
                    reason=f"管道未啟用（{getattr(ch, 'status', '不存在')}）"))
                continue
            if line.blocker:
                out.append(jobs.ChannelOutcome(
                    line.channel, line.label, jobs.BLOCKED, reason=line.blocker))
                continue
            try:
                res = _run_channel(ch, line, job, publish, all_inst,
                                   targets, place_ids, req)
            except Exception as exc:  # noqa: BLE001
                out.append(jobs.ChannelOutcome(
                    line.channel, line.label, jobs.FAILED_CH,
                    reason=f"{type(exc).__name__}: {exc}"))
                continue
            out.append(res[0])
            found.extend(res[1])
            publish({"outcomes": [o.as_dict() for o in out],
                     "mentions": mark(_decorate(found, by_id))})

        publish({"mentions": mark(_decorate(found, by_id)),
                 "in_scope": sum(1 for m in found if m.get("in_scope")),
                 "out_of_scope": sum(1 for m in found if not m.get("in_scope"))})
        jobs.settle_all(job)
        return out

    return worker


def _reattribute(mentions: list, pool: list[dict]) -> list:
    """拿完整機構池重驗歸屬，過不了的丟掉。

    這不是效能優化，是鐵則：歸屬的正確性建立在「看得見全部機構」之上。
    只餵一家不是少看見，是把所有同名內容都塞給那一家。
    """
    from ..features.alerts import attribute

    kept = []
    for m in mentions:
        head = getattr(m, "headline", "") or ""
        want = getattr(m, "institution_id", None)
        att = attribute(head, pool)
        if att.attributed and att.institution_id == want:
            kept.append(m)
    return kept


def _decorate(mentions: list[dict], by_id: dict) -> list[dict]:
    for m in mentions:
        p = by_id.get(m.get("institution_id") or "")
        m["institution_title"] = p["full"] if p else ""
        m["town"] = p["d"] if p else ""
        m["status"] = "待人工研判"
    return mentions


def _run_channel(ch, line, job, publish, all_inst, targets, place_ids, req):
    """跑一個管道，回 (結局, mentions)。錢在送出請求之前就先預留。"""
    usd_max = line.meter["usd_max"]
    billable = bool(usd_max)
    rid = ""
    if billable:
        # 檢查與佔用在同一把鎖裡。分開做的話，兩個同時發動的掃描會各自
        # 看到「還有額度」然後一起衝破上限。
        gate, rid = ledger.check_and_reserve(
            usd_max, line.channel, job["job_id"], detail=line.mode)
        if not rid:
            return (jobs.ChannelOutcome(
                line.channel, line.label, jobs.BLOCKED,
                reason=f"發動時額度已不足：{gate['reason']}"), [])
        job.setdefault("reservations", []).append(
            {"reservation_id": rid, "channel": line.channel,
             "usd_max": usd_max, "actual_usd": None, "settled": False})
        publish({"reservations": job["reservations"]})

    if line.channel == "apify_threads":
        def on_started(run_id, _dataset):
            # run_id 在等待之前落地。當機時那筆錢還查得回來。
            publish({"apify_run_id": run_id})
            for r in job["reservations"]:
                if r["reservation_id"] == rid:
                    r["provider_ref"] = run_id
        before = ch.items_returned
        ms = ch.sweep(all_inst, limit=req.max_posts, keywords=req.keywords,
                      max_charge_usd=usd_max, on_started=on_started)
        raw = ch.items_returned - before
        run_id = job.get("apify_run_id", "")
        actual = ch.charged_usd

        # 三種「零」意義完全不同，塌成一種會同時說謊兩次：把系統故障
        # 講成「沒人在談」，又把沒花掉的錢當成花掉了。
        if not run_id:
            # 執行從未建立 → 沒有任何計費請求送出，額度必須放回去。
            if rid:
                ledger.release(rid, "start_run 未能建立執行")
                for r in job["reservations"]:
                    if r["reservation_id"] == rid:
                        r.update({"settled": True, "actual_usd": 0.0,
                                  "released": True})
            return (jobs.ChannelOutcome(
                line.channel, line.label, jobs.FAILED_CH, 0, 0,
                "未能建立 Apify 執行（憑證失效或連線失敗）。"
                "本次未發生費用，也未取得任何資料。", 0.0), [])

        for r in job["reservations"]:
            if r["reservation_id"] == rid:
                # 供應商還沒回報就維持上界，並記下 run_id 供 reconcile 查回。
                r["actual_usd"] = actual
                r["provider_ref"] = run_id
                r["settled"] = actual is not None
        if raw == 0:
            reason = ("供應商回傳 0 筆貼文。可能是關鍵字無結果，"
                      "也可能是執行逾時或被中止——不等於這些園沒有負面聲音。")
        elif not ms:
            reason = (f"回傳 {raw} 篇貼文，嚴格歸屬後無一則點名可辨識的機構。"
                      "這是「沒有可歸屬內容」，不是「這些園沒有負面聲音」。")
        else:
            reason = ""
        return (jobs.ChannelOutcome(
            line.channel, line.label, jobs.OK if ms else jobs.EMPTY,
            len(ms), raw, reason, actual, run_id),
            [m.as_dict() for m in ms])

    if line.channel == "places_reviews":
        got: list = []
        calls = 0
        failed: list[str] = []
        for iid in targets.get(line.channel, []):
            inst = {"id": iid, "place_id": place_ids.get(iid, "")}
            ledger.note_call("places_reviews", 1, job_id=job["job_id"],
                             detail=iid)
            calls += 1
            try:
                got.extend(ch.search(inst, limit=5))
            except Exception as exc:  # noqa: BLE001
                # 一家查失敗不該把前面已經付費取得的評論一起丟掉。
                failed.append(f"{iid}: {type(exc).__name__}")
        # 實付按實際送出的查詢次數，不是計畫的家數。
        spent = pricing.places_meter(
            calls, used_this_month=max(
                0, ledger.budget().places_used_this_month - calls)).usd_max
        for r in job.get("reservations", []):
            if r["reservation_id"] == rid:
                r["actual_usd"] = spent
        if failed:
            return (jobs.ChannelOutcome(
                line.channel, line.label, jobs.PARTIAL, len(got), calls,
                f"查詢 {calls} 家，{len(failed)} 家失敗（{'、'.join(failed[:3])}）。"
                "已取得的部分仍列出，未取得的屬資料不足。", spent),
                [m.as_dict() for m in got])
        return (jobs.ChannelOutcome(
            line.channel, line.label, jobs.OK if got else jobs.EMPTY,
            len(got), calls,
            "" if got else f"查詢 {calls} 家，皆無公開評論或該地點無評論資料。",
            spent), [m.as_dict() for m in got])

    # 免費管道
    if line.mode == "sweep":
        ms = ch.sweep(all_inst, limit=200)
    else:
        ms = []
        for iid in targets.get(line.channel, []):
            p = next((x for x in all_inst if x["id"] == iid), None)
            if p:
                ms.extend(ch.search(p, limit=20))
        # Channel.search() 只拿得到被查的那一家，於是 attribute() 的
        # 「名稱可對應 ≥2 所機構就拒絕」失效——查「板橋」時，任何提到
        # 板橋幼兒園的內容都會被歸給這一家，即使全市有好幾家同名。
        # 拿全部 1,213 園重驗一次，歸屬改變或變成拒絕的一律丟掉。
        ms = _reattribute(ms, all_inst)
    return (jobs.ChannelOutcome(
        line.channel, line.label, jobs.OK if ms else jobs.EMPTY, len(ms), 0,
        "" if ms else "此管道本次未取得可歸屬內容。該端點無公告配額，"
                      "被限流時的表徵是空結果而非錯誤——空結果不等於沒有討論。"),
        [m.as_dict() for m in ms])


@router.get("/jobs")
def list_jobs(limit: int = 30) -> dict:
    return {"jobs": jobs.STORE.list(limit)}


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = jobs.STORE.read(job_id)
    if not job:
        raise HTTPException(404, f"查無任務 {job_id}")
    return jobs.summarise(job)


class AdoptRequest(BaseModel):
    urls: list[str]
    reviewer: str = ""


@router.post("/jobs/{job_id}/adopt")
def adopt(job_id: str, req: AdoptRequest) -> dict:
    """把逐則人工挑過的線索存進待辦。

    只有這一條路徑能讓掃描結果離開 ``data/runtime/``，而且要逐則勾選。
    採用後狀態仍是「待人工研判」——採用的意思是「這條值得看」，
    不是「這條成立」。
    """
    job = jobs.STORE.read(job_id)
    if not job:
        raise HTTPException(404, f"查無任務 {job_id}")
    want = set(req.urls)
    taken = [m for m in job.get("mentions", []) if m.get("url") in want]
    path = jobs.DIR / "adopted.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with path.open("a", encoding="utf-8") as fh:
        for m in taken:
            fh.write(json.dumps({**m, "adopted_at": stamp, "job_id": job_id,
                                 "reviewer": req.reviewer,
                                 "status": "待人工研判"},
                                ensure_ascii=False) + "\n")
    return {"adopted": len(taken), "file": str(path),
            "note": "已記錄為待查線索。狀態仍為待人工研判，不進入分數、"
                    "不成為違規標籤。"}


@router.get("/budget")
def budget() -> dict:
    b = ledger.budget()
    return {**b.as_dict(),
            "caps": {"run": pricing.CAP_PER_RUN_USD,
                     "day": pricing.CAP_PER_DAY_USD,
                     "month": pricing.CAP_PER_MONTH_USD},
            "cycle_start": ledger.apify_cycle_start()}


@router.get("/ledger")
def ledger_entries(limit: int = 100) -> dict:
    return {"entries": ledger.entries(limit)}
